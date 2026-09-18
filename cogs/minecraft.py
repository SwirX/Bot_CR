"""Minecraft bridge — Robotics CMC status + self-service whitelist.

Talks to the club's Pterodactyl panel through its Client API (start/stop
state, console command, file read/write of ``whitelist.json``) and does a
standard Minecraft server-list ping on the game port for the version and
player counts — the same protocol the multiplayer menu uses. **No Minecraft
plugins needed**: whitelisting is vanilla (``whitelist add`` via the console
when running, direct ``whitelist.json`` edit when stopped), and the ping
works on any vanilla/paper server with nothing enabled server-side.
"""

import asyncio
import json
import logging
import re
import struct

import aiohttp
import discord
from discord.ext import commands

import config
from data.store import store

LOG = logging.getLogger("bot.minecraft")

# Vanilla username rules: 1–16 chars of ASCII letters/digits/underscore.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_PING_TIMEOUT = 10.0


# ── Pterodactyl Client API ─────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.MC_PTERO_CLIENT_KEY}",
    }


def _server_url(*parts: str) -> str:
    base = f"{config.MC_PTERO_URL}/api/client/servers/{config.MC_SERVER_ID}"
    return "/".join([base, *parts])


async def server_state(session: aiohttp.ClientSession) -> str:
    """current_state: running / starting / stopping / offline."""
    async with session.get(_server_url("resources")) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return (data.get("attributes") or {}).get("current_state", "unknown")


async def server_name(session: aiohttp.ClientSession) -> str:
    async with session.get(_server_url()) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return (data.get("attributes") or {}).get("name", "")


async def read_whitelist(session: aiohttp.ClientSession) -> list:
    """Current whitelist.json entries ([] if missing/empty)."""
    async with session.get(_server_url("files", "contents"),
                           params={"file": "/whitelist.json"}) as resp:
        if resp.status == 404:
            return []
        resp.raise_for_status()
        text = await resp.text()
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


async def write_whitelist(session: aiohttp.ClientSession, entries: list) -> None:
    """Overwrite whitelist.json (used while the server is stopped)."""
    async with session.post(
        _server_url("files", "write"),
        params={"file": "/whitelist.json"},
        data=json.dumps(entries),
        headers={"Content-Type": "text/plain"},
    ) as resp:
        resp.raise_for_status()


async def send_command(session: aiohttp.ClientSession, command: str) -> None:
    """Pipe a console command to the running server (e.g. ``whitelist add``)."""
    async with session.post(_server_url("command"), json={"command": command}) as resp:
        resp.raise_for_status()


async def mojang_profile(username: str) -> dict | None:
    """Resolve a Minecraft username to {id (dashed uuid), name}; None if unknown."""
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 204:
                    return None
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:  # noqa: BLE001 - network hiccups mean "unknown", not failure
        return None


# ── Minecraft server-list ping (protocol >= 1.7) ───────────────────────
def _pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF  # -1 ("any protocol") must encode as an unsigned varint
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader, timeout: float) -> int:
    result = 0
    shift = 0
    while True:
        b = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result
        shift += 7


def _parse_varint(buf: bytes, offset: int):
    result = 0
    shift = 0
    while True:
        b = buf[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7


async def status_ping(host: str, port: int,
                      timeout: float = _PING_TIMEOUT) -> dict:
    """Server-list ping -> {version, players{online,max}, description, ...}."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout)
    try:
        host_b = host.encode()
        payload = (b"\x00" + _pack_varint(-1) + _pack_varint(len(host_b))
                   + host_b + struct.pack(">H", port) + _pack_varint(1))
        writer.write(_pack_varint(len(payload)) + payload)
        writer.write(_pack_varint(1) + b"\x00")  # status request
        await writer.drain()
        packet_len = await _read_varint(reader, timeout)
        body = await asyncio.wait_for(reader.readexactly(packet_len), timeout)
        _pid, off = _parse_varint(body, 0)          # packet id (0x00)
        json_len, off = _parse_varint(body, off)    # JSON string length
        text = body[off:off + json_len].decode("utf-8", "replace")
        return json.loads(text)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - best-effort socket close
            pass


# ── Cog ────────────────────────────────────────────────────────────────
class Minecraft(commands.Cog):
    """Robotics CMC server status and self-service whitelist."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _configured(self) -> bool:
        return bool(config.MC_PTERO_CLIENT_KEY and config.MC_SERVER_ID
                    and config.MC_ADDRESS)

    async def _require_configured(self, ctx) -> bool:
        if self._configured():
            return True
        await ctx.send("⚠️ Minecraft commands aren't configured on this bot yet.")
        return False

    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=_headers(), timeout=_TIMEOUT)

    # ── status ────────────────────────────────────────────────
    @commands.hybrid_command(name="mc",
                             description="Robotics CMC status: IP, version, players.")
    @commands.guild_only()
    async def mc(self, ctx: commands.Context):
        await self._status(ctx)

    @commands.hybrid_command(name="minecraft",
                             description="Robotics CMC status: IP, version, players.")
    @commands.guild_only()
    async def minecraft(self, ctx: commands.Context):
        await self._status(ctx)

    async def _status(self, ctx: commands.Context):
        if not await self._require_configured(ctx):
            return
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        version = None
        players = None
        state = "offline"
        name = config.MC_SERVER_NAME
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                stored = await server_name(session)
                if stored:
                    name = stored
                if state in ("running", "starting"):
                    try:
                        ping_info = await status_ping(config.MC_ADDRESS,
                                                      config.MC_PORT)
                        version = (ping_info.get("version") or {}).get("name")
                        players = ping_info.get("players") or {}
                    except Exception as exc:  # ping is best-effort
                        LOG.warning("Minecraft status ping failed: %s", exc)
        except Exception as exc:
            LOG.warning("Minecraft status: Pterodactyl API failed: %s", exc)
            await ctx.send("⚠️ Couldn't reach the Pterodactyl panel for server state.")
            return

        state_emoji = {"running": "🟢 Running", "starting": "🟡 Starting",
                       "stopping": "🟠 Stopping"}.get(state, "🔴 Offline")
        embed = discord.Embed(title=f"⛏️ {name}", color=0x55AA55)
        embed.add_field(name="IP", value=f"`{config.MC_ADDRESS}:{config.MC_PORT}`")
        embed.add_field(name="Version", value=version or "—")
        embed.add_field(name="Status", value=state_emoji)
        if players is not None:
            embed.add_field(name="Players",
                            value=f"{players.get('online', 0)}/{players.get('max', 0)}")
        else:
            embed.add_field(name="Players", value="—")
        embed.set_footer(text="Want in? Use /linkmc <minecraft username>")
        await ctx.send(embed=embed)

    # ── whitelist ─────────────────────────────────────────────
    @commands.hybrid_command(name="linkmc",
                             description="Whitelist your Minecraft username on the club server.")
    @commands.guild_only()
    @commands.cooldown(3, 60, commands.BucketType.user)
    async def linkmc(self, ctx: commands.Context, username: str):
        """Add <username> to Robotics CMC's whitelist (self-service)."""
        if not await self._require_configured(ctx):
            return
        username = username.strip()
        if not _USERNAME_RE.match(username):
            await ctx.send("⚠️ That doesn't look like a Minecraft username "
                           "(1–16 letters, digits or underscores).")
            return
        profile = await mojang_profile(username)
        canonical = (profile or {}).get("name", username)
        entry = {"name": canonical}
        if profile:
            entry["uuid"] = profile.get("id", "").replace("-", "")

        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                entries = await read_whitelist(session)
                lower = canonical.lower()
                if any(str(e.get("name", "")).lower() == lower for e in entries):
                    await ctx.send(f"ℹ️ **{canonical}** is already on the whitelist.")
                    return
                if state == "running":
                    # Console command — vanilla updates memory + whitelist.json.
                    await send_command(session, f"whitelist add {canonical}")
                    msg = (f"✅ **{canonical}** was whitelisted — the server "
                           "applies it immediately. 🎮")
                else:
                    # Stopped/starting: edit the file so it applies at launch.
                    entries.append(entry)
                    await write_whitelist(session, entries)
                    msg = (f"✅ **{canonical}** was added to whitelist.json — "
                           "it applies when the server starts. 🎮")
        except Exception as exc:
            LOG.warning("linkmc failed for %r: %s", canonical, exc)
            await ctx.send("⚠️ Something went wrong talking to the panel — "
                           "try again or ask staff to add you.")
            return
        try:
            await store.merge_member(ctx.author.id, {"mc_username": canonical})
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("linkmc: could not save link for %s: %s", ctx.author.id, exc)
        await ctx.send(msg + "\nIt's linked to your Discord, so staff can "
                       "audit who asked for what.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Minecraft(bot))