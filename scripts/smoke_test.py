#!/usr/bin/env python3
"""Hermetic smoke test: compile-free verification that every cog loads.

This does NOT touch the network or Appwrite — it only builds a bot, loads every
extension in cogs/, and checks the command surface. Runtime env vars are read
from the environment (CI injects dummy values; locally the real .env is used).

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/smoke_test.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

EXPECTED_COMMANDS = {
    "8ball", "accountage", "addrole", "advice", "admin", "assign", "ban", "bot", "cancel", "cell",
    "challenge",
    "choose", "claim", "clap", "close", "coinflip", "competition",
    "competitions", "complete", "compliment", "create", "crypto",
    "dashboard", "define", "del", "dice", "edit", "end", "endroom", "event", "events",
    "fact", "fixname", "github", "hierarchy", "hello", "help", "history",
    "hug", "joke", "kick", "language", "last", "leaderboard", "levelrewards", "link", "linkmember", "list", "lock", "loop", "lyrics",
    "mc", "mclink", "mcotp", "mcrestart", "mcsession", "mcstart", "mcstop", "meeting", "meme", "minecraft", "modlog", "mute", "notifications", "nowplaying",
    "overdue", "pause", "ping", "play", "poll", "profile", "queue",
    "quiz", "quote", "rank", "remind", "removerole", "resume", "results", "reverse", "roast",
    "robot", "roles", "rps", "set", "setbirthday", "setlead", "setname", "setprofile",
    "settings", "ship", "skip",
    "slap", "slowmode", "spacex", "start", "stats", "stop", "task", "tasks", "timeout", "today",
    "unban", "unlink", "unlinkmember", "unlock", "unmute",
    "untimeout", "view", "voicetime", "volume", "vote", "warn", "weather", "website", "whois",
    "namesweep",
    # Identity / record sync — prefix-only (see PREFIX_ONLY_COMMANDS).
    "syncdb", "joindatesync", "askbirthday", "askname",
}

# Deliberately NOT hybrid: these are admin data-maintenance commands that must
# never appear as slash commands, so they can't be triggered by clicking in the
# Discord UI. Using a prefix command keeps them a deliberate typed action.
PREFIX_ONLY_COMMANDS = {"syncdb", "joindatesync", "askbirthday", "askname"}

# Commands that must NOT exist any more. Guards against a silent regression
# reintroducing a self-service Minecraft-link path (see cogs/minecraft.py:
# `/linkmc` allowed claiming an arbitrary username and receiving the owner's
# login OTP — a full account takeover).
FORBIDDEN_COMMANDS = {"linkmc"}


async def _check_startup_wiring() -> str:
    """Import BOT and verify main()'s signal/handler setup is actually valid.

    Deliberately does NOT connect to Discord. It replaces ``bot.start`` with a
    stub, runs the pre-``start`` half of ``main``, and asserts that both the
    signal handlers and ``on_ready`` (including the self-check) are callable.
    This is the check that would have caught both startup TypeErrors.
    """
    import signal
    import types

    sys.modules.setdefault("KeepAlive", types.SimpleNamespace(
        keep_alive=lambda: None,
        mark_ready=lambda: None,
        mark_not_ready=lambda: None,
    ))
    import BOT  # noqa: E402

    fired: list[str] = []

    async def fake_shutdown(name: str = "") -> None:
        fired.append(name)

    BOT.shutdown = fake_shutdown
    # Don't actually connect to the gateway.
    async def fake_start(_token):
        return None
    BOT.bot.start = fake_start

    # Run main() up to (but not including) bot.start. BOT does
    # `from data.store import store`, so patch the singleton it actually holds.
    orig_init = BOT.store.init
    async def fake_init():
        return None
    BOT.store.init = fake_init
    try:
        await BOT.main()
    finally:
        BOT.store.init = orig_init

    # Now prove a signal actually reaches shutdown().
    for sig in (signal.SIGTERM, signal.SIGINT):
        os.kill(os.getpid(), sig)
        await asyncio.sleep(0.2)
    if not {"SIGTERM", "SIGINT"} <= set(fired):
        raise AssertionError(
            f"signal handlers did not fire shutdown(): {fired}")

    # And that on_ready runs end to end, including self_check().
    BOT.sync_commands = lambda: asyncio.sleep(0)
    ready = BOT.on_ready()
    if asyncio.iscoroutine(ready):
        await ready

    return f"{len(fired)} signal handlers ok, on_ready + self_check ok"


def main() -> int:
    # config requires these; fail loudly (not cryptically) if they're missing.
    for var in ("BOT_TOKEN", "APPWRITE_API_KEY"):
        if not os.getenv(var):
            print(f"✗ Missing required env var {var} (set a dummy value for this test)")
            return 1

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all(), help_command=None)

    async def run() -> None:
        # Anchored to the repo root: a CWD-relative "cogs" globs to nothing when
        # run from elsewhere, which silently verified nothing.
        cogs_dir = ROOT / "cogs"
        loaded = 0
        for path in sorted(cogs_dir.glob("*.py")):
            if path.name.startswith("_") or path.name == "__init__.py":
                continue
            extension = f"cogs.{path.stem}"
            await bot.load_extension(extension)
            loaded += 1

        names = {c.name for c in bot.walk_commands()}
        missing = EXPECTED_COMMANDS - names
        forbidden = FORBIDDEN_COMMANDS & names
        non_hybrid = [
            c.name for c in bot.walk_commands()
            if c.name not in PREFIX_ONLY_COMMANDS
            and not isinstance(c, (commands.HybridCommand, commands.HybridGroup))
        ]
        if missing:
            print(f"✗ Missing expected commands: {sorted(missing)}")
            raise SystemExit(1)
        if forbidden:
            print(f"✗ Forbidden commands are back: {sorted(forbidden)}")
            raise SystemExit(1)
        if non_hybrid:
            print(f"✗ Non-hybrid commands: {non_hybrid}")
            raise SystemExit(1)
        if bot.help_command is not None:
            print("✗ Custom help was not installed (help_command must be None)")
            raise SystemExit(1)
        if "help" not in names:
            print("✗ No custom help command registered")
            raise SystemExit(1)

        print(f"✓ Loaded {loaded} cogs and {len(names)} commands (all hybrid, incl. custom help)")

        # Exercise the *startup wiring* in BOT.py, not just cog loading. Two
        # shipping bugs lived here and neither the cog-load path nor the unit
        # suite caught them, because both only crash once main() runs:
        #   * add_signal_handler(sig, lambda=...) -> TypeError (missing callback)
        #   * add_listener(lambda: ..., "on_ready") -> "Listeners must be
        #     coroutines"
        # We never connect to Discord here, so we drive the handler-registration
        # block directly and assert it registers and fires.
        startup = await _check_startup_wiring()
        print(f"✓ startup wiring: {startup}")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - test runner
        print(f"✗ Smoke test failed: {exc}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())