"""Minecraft bridge — Robotics CMC status + self-service whitelist.

Talks to the club's Pterodactyl panel through its Client API (start/stop
state, console command, file read/write of ``whitelist.json``) and does a
standard Minecraft server-list ping on the game port for the version and
player counts — the same protocol the multiplayer menu uses. **No Minecraft
plugins needed**: whitelisting is vanilla (whitelist.json is written directly
and live-reloaded via ``whitelist reload`` when the server is running), and
the ping works on any vanilla/paper server with nothing enabled server-side.

The server runs cracked/offline mode (``online-mode=false``), so matching is
by the UUID the client presents — a real Mojang account joins with its real
UUID on a premium launcher but with ``MD5("OfflinePlayer:<name>")`` on a
cracked launcher, so real accounts get **both** UUIDs whitelisted while
cracked accounts only need their offline UUID (exact-case name).
"""

import asyncio
import hashlib
import json
import logging
import re
import struct
import uuid as uuidlib

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


# ── Offline-mode UUID derivation ───────────────────────────────────────
def _offline_uuid(name: str) -> str:
    """The UUID a cracked/offline-mode server derives from a username.

    Same algorithm as vanilla: UUID v3 (MD5) of ``OfflinePlayer:<name>``.
    This is what must sit in whitelist.json for a cracked account to match,
    and it is what a cracked launcher sends on login.
    """
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30  # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuidlib.UUID(bytes=bytes(digest)))


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
        entries_to_add = []
        if profile and profile.get("name") == username:
            # Typed with the exact capitalisation of a real Mojang account, so
            # the member almost certainly owns it — cover BOTH launcher modes:
            # the real uuid (premium join) and the offline uuid (cracked join).
            # (If the typed case differs from the real account, e.g. "hatim"
            # vs "Hatim", that account is NOT the member's — don't whitelist
            # a stranger's uuid; fall through to the cracked path below.)
            canonical = username
            try:
                real_uuid = str(uuidlib.UUID(profile["id"]))
            except (KeyError, TypeError, ValueError):
                real_uuid = _offline_uuid(canonical)  # fall back, never write junk
            entries_to_add = [
                {"uuid": real_uuid, "name": canonical},
                {"uuid": _offline_uuid(canonical), "name": canonical},
            ]
            note = (" (real Mojang account — covered on **both** the official "
                    "and cracked launcher)")
        else:
            # Cracked account: whitelist the offline uuid of the EXACT name as
            # typed. Mojang's capitalisation of the same letters is a different
            # account ("hatim" !== "Hatim"), so it must never be reused here.
            canonical = username
            entries_to_add = [
                {"uuid": _offline_uuid(canonical), "name": canonical},
            ]
            note = (" (cracked account — name is case-sensitive, keep it "
                    "exactly as your launcher uses it)")

        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                existing = await read_whitelist(session)
                known = {str(e.get("uuid")) for e in existing}
                fresh = [e for e in entries_to_add if e["uuid"] not in known]
                if not fresh:
                    await ctx.send(f"ℹ️ **{canonical}** is already fully on the "
                                   "whitelist.")
                    return
                existing.extend(fresh)
                # Write the file directly and reload if running — uniform for
                # both states, and the only way to get the real-UUID entry in
                # (console \"whitelist add\" only derives the offline UUID on
                # an offline-mode server).
                await write_whitelist(session, existing)
                if state == "running":
                    await send_command(session, "whitelist reload")
                    msg = (f"✅ **{canonical}** was whitelisted "
                           f"({len(fresh)} UUID entr{'y' if len(fresh) == 1 else 'ies'}) "
                           "— applied live. 🎮")
                else:
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
        await ctx.send(msg + note + "\nIt's linked to your Discord, so staff can "
                       "audit who asked for what.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Minecraft(bot))