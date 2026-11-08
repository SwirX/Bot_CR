#!/usr/bin/env python3
"""Hermetic tests for the voice-move allowlist (can_move_members).

The gate must accept exactly the bureau offices (Archon, President,
Vice President, Manager), the IT chief/lead, and the whitelisted bot
operator — nobody else, not bots, not lookalike roles ("Digit Lead" must
fail the word-boundary "IT Lead" test).

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/test_move_perms.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The whitelisted-operator case only holds against the real deployment, where
# .env carries BOT_ADMIN_USER_IDS=... . On CI the whitelist is empty and that
# one case is skipped rather than failed.
DEPLOYED = os.path.exists(ROOT / ".env") and bool(os.environ.get("BOT_TOKEN")) \
    and os.environ.get("BOT_TOKEN") != "ci-placeholder-token"

from cogs._perms import can_move_members  # noqa: E402

SWIRX = 407922956757499905  # the bot operator, whitelisted in .env


class _Role:
    def __init__(self, name):
        self.name = name


class _Perms:
    administrator = False


class _Member:
    def __init__(self, *roles, uid=1, bot=False):
        self.roles = [_Role(r) for r in roles]
        self.id = uid
        self.bot = bot
        self.guild_permissions = _Perms()


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"  ✓ {label}")


def main():
    # Bureau offices (real server role names, emoji prefixes and all).
    check("archon = allowed",
          can_move_members(_Member("「🏴」Archon of Robotics Club")), True)
    check("president = allowed",
          can_move_members(_Member("「👸」President")), True)
    check("vice president = allowed",
          can_move_members(_Member("「👸」Vice President")), True)
    check("manager = allowed",
          can_move_members(_Member("「👸」Manager")), True)
    # IT office, both spellings.
    check("Head of IT Unit = allowed",
          can_move_members(_Member("「💻」Head of IT Unit")), True)
    check("plain IT Lead = allowed",
          can_move_members(_Member("「💻」IT Lead")), True)

    # Everyone the gate must reject.
    check("plain member = rejected",
          can_move_members(_Member("「✨」New Member")), False)
    check("a non-IT Lead (Minecraft) = rejected",
          can_move_members(_Member("「⛏️」Lead of Minecraft")), False)
    check("'Digit Lead' must not match 'IT Lead' = rejected",
          can_move_members(_Member("📡 Digit Lead")), False)
    check("member of IT unit (not its head) = rejected",
          can_move_members(_Member("🔧 Member of IT Unit")), False)
    check("a bot is never allowed",
          can_move_members(_Member("「🤖」Bot", bot=True)), False)

    # The bot operator passes whatever their roles, against the deployment.
    if DEPLOYED:
        check("whitelisted operator = allowed",
              can_move_members(_Member("「✨」New Member", uid=SWIRX)), True)

    print("ALL MOVE PERMISSION CHECKS PASSED")


if __name__ == "__main__":
    main()
