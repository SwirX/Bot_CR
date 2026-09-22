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

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - test runner
        print(f"✗ Smoke test failed: {exc}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())