"""Minecraft bridge — Robotics CMC status + self-service whitelist.

Talks to the club's Pterodactyl panel through its Client API (start/stop
state, console command, file read/write of ``whitelist.json``) and does a
standard Minecraft server-list ping on the game port for the version and
player counts — the same protocol the multiplayer menu uses. **No Minecraft
plugins needed**: whitelisting is vanilla (whitelist.json is written directly
and live-reloaded via ``whitelist reload`` when the server is running), and
the ping works on any vanilla/paper server with nothing enabled server-side.

The server runs cracked/offline mode (``online-mode=false``), so matching is
by the UUID the client presents. ``/linkmc`` therefore asks the member to
declare their account type: **paid** accounts get their real UUID *and* the
offline UUID (so the official and free launchers both work), while **free**
accounts get only the offline UUID of the exact-case name they type
(``MD5("OfflinePlayer:<name>")`` — Mojang's capitalisation would be a
different account, e.g. ``hatim`` vs ``Hatim``).
"""

import asyncio
import hashlib
import json
import logging
import re
import struct
import uuid as uuidlib
from typing import Literal

import aiohttp
import discord
from discord.ext import commands

import config
from data.store import store
from i18n.core import resolve_member_lang, t

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

    async def _require_configured(self, ctx, lang: str) -> bool:
        if self._configured():
            return True
        await ctx.send(t("mc.unconfigured", lang))
        return False

    async def _lang(self, ctx) -> str:
        locale = str(ctx.interaction.locale) if ctx.interaction else None
        return await resolve_member_lang(ctx.author.id, locale)

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
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        lang = await self._lang(ctx)
        if not await self._require_configured(ctx, lang):
            return
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
            await ctx.send(t("mc.panel_unreachable", lang))
            return

        state_emoji = {
            "running": t("mc.status.running", lang),
            "starting": t("mc.status.starting", lang),
            "stopping": t("mc.status.stopping", lang),
        }.get(state, t("mc.status.offline", lang))
        embed = discord.Embed(title=t("mc.title", lang, name=name), color=0x55AA55)
        embed.add_field(name=t("mc.field.ip", lang),
                        value=f"`{config.MC_ADDRESS}:{config.MC_PORT}`")
        embed.add_field(name=t("mc.field.version", lang), value=version or "—")
        embed.add_field(name=t("mc.field.status", lang), value=state_emoji)
        if players is not None:
            embed.add_field(name=t("mc.field.players", lang),
                            value=f"{players.get('online', 0)}/{players.get('max', 0)}")
        else:
            embed.add_field(name=t("mc.field.players", lang), value="—")
        embed.add_field(
            name=t("mc.linking.title", lang),
            value=t("mc.linking.body", lang),
        )
        embed.set_footer(text=t("mc.footer", lang))
        await ctx.send(embed=embed)

    # ── whitelist ─────────────────────────────────────────────
    @commands.hybrid_command(name="linkmc",
                             description="Whitelist a Minecraft username (free or paid account).")
    @commands.guild_only()
    @commands.cooldown(3, 60, commands.BucketType.user)
    async def linkmc(self, ctx: commands.Context, username: str,
                     account: Literal["free", "paid"]):
        """Add <username> to Robotics CMC's whitelist (self-service).

        account: \"paid\" if it's a bought Minecraft account (adds the real
        UUID so the official launcher works too), \"free\" for offline/cracked
        accounts (adds the offline UUID of the exact name as typed).
        """
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        lang = await self._lang(ctx)
        if not await self._require_configured(ctx, lang):
            return
        username = username.strip()
        if not _USERNAME_RE.match(username):
            await ctx.send(t("linkmc.not_username", lang))
            return
        if account == "paid":
            # Paid account — whitelist the REAL uuid (official launcher) AND
            # the offline uuid of the same exact name (offline launchers), so
            # the member is covered whichever launcher they use.
            profile = await mojang_profile(username)
            if profile:
                canonical = profile.get("name", username)
                try:
                    real_uuid = str(uuidlib.UUID(profile["id"]))
                except (KeyError, TypeError, ValueError):
                    real_uuid = _offline_uuid(canonical)  # never write junk
                entries_to_add = [
                    {"uuid": real_uuid, "name": canonical},
                    {"uuid": _offline_uuid(canonical), "name": canonical},
                ]
                note = t("linkmc.note_paid", lang)
            else:
                # Claimed paid but Mojang doesn't know the name — don't write
                # a fake real UUID; add the offline entry and tell the member.
                canonical = username
                entries_to_add = [
                    {"uuid": _offline_uuid(canonical), "name": canonical},
                ]
                note = t("linkmc.note_paid_unknown", lang)
        else:
            # Free (cracked) account: whitelist the offline uuid of the EXACT
            # name as typed. Mojang's capitalisation of the same letters is a
            # different account ("hatim" !== "Hatim"), so it must never be
            # reused here.
            canonical = username
            entries_to_add = [
                {"uuid": _offline_uuid(canonical), "name": canonical},
            ]
            note = t("linkmc.note_free", lang)

        try:
            async with self._new_session() as session:
                state = await server_state(session)
                existing = await read_whitelist(session)
                known = {str(e.get("uuid")) for e in existing}
                fresh = [e for e in entries_to_add if e["uuid"] not in known]
                if not fresh:
                    await ctx.send(t("linkmc.already", lang, name=canonical))
                    return
                existing.extend(fresh)
                # Write the file directly and reload if running — uniform for
                # both states, and the only way to get the real-UUID entry in
                # (console \"whitelist add\" only derives the offline UUID on
                # an offline-mode server).
                await write_whitelist(session, existing)
                if state == "running":
                    await send_command(session, "whitelist reload")
                    msg = t("linkmc.success_running", lang, name=canonical)
                else:
                    msg = t("linkmc.success_stopped", lang, name=canonical)
        except Exception as exc:
            LOG.warning("linkmc failed for %r: %s", canonical, exc)
            await ctx.send(t("linkmc.failed", lang))
            return
        try:
            await store.merge_member(ctx.author.id, {"mc_username": canonical})
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("linkmc: could not save link for %s: %s", ctx.author.id, exc)
        await ctx.send(msg + note + t("linkmc.linked_audit", lang))

    @linkmc.error
    async def linkmc_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.MissingRequiredArgument,
                              commands.BadArgument)):
            lang = await self._lang(ctx)
            await ctx.send(
                t("linkmc.usage_title", lang) + "\n" + t("linkmc.usage_body", lang)
            )
            return
        raise error  # cooldown and friends keep the default handling


async def setup(bot: commands.Bot):
    await bot.add_cog(Minecraft(bot))