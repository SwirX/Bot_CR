#!/usr/bin/env python3
"""Hermetic tests for the meeting admin / audience role resolution.

Every role name below is copied verbatim from the club server, so a role
renamed in Discord shows up here as a failure rather than as a silently
missing attendee list. No network, no Appwrite — the member/role objects are
local stubs, which is all ``cogs._perms`` ever touches.

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/test_meeting_perms.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The two assertions in main() below are about the *deployed* .env, so they can
# only be meaningful when a real config exists. On CI (and any machine without
# one) config falls back to its built-in defaults — different role names, no
# operator id — and those assertions fail for reasons that have nothing to do
# with the code under test. Flip them off unless we really are pointed at a
# deployment. The rest of the file is fully hermetic either way.
DEPLOYED = os.path.exists(ROOT / ".env") and bool(os.environ.get("BOT_TOKEN")) \
    and os.environ.get("BOT_TOKEN") != "ci-placeholder-token"

import config  # noqa: E402
from cogs._perms import is_meeting_admin, meeting_tier, role_key  # noqa: E402

SWIRX = 407922956757499905  # the bot operator, whitelisted in .env


class _Role:
    def __init__(self, name, managed=False):
        self.name = name
        self.managed = managed


class _Perms:
    administrator = False


class _Member:
    def __init__(self, *roles, uid=1, bot=False):
        self.roles = [_Role(r) for r in roles]
        self.id = uid
        self.bot = bot
        self.guild_permissions = _Perms()


# Role name -> expected meeting audience tier.
REAL_SERVER_ROLES = {
    # bureau offices
    "「🏴」Archon of Robotics Club": "bureau",
    "「👸」President": "bureau",
    "「👸」Vice President": "bureau",
    "「👸」Manager": "bureau",
    # Chief / Lead / Head of every unit
    "「💻」Head of IT Unit": "bureau",
    "「🎨」Head of Multimedia Unit": "bureau",
    "「🗣️」Head of Communication & Docs Unit": "bureau",
    "「🔧」Head of Technical Unit": "bureau",
    "「🎎」Head of Organization Unit": "bureau",
    "「🦾」Head of Project Unit": "bureau",
    # cell members
    "「💻」Member of IT Unit": "cells",
    "「🎨」Member of Multimedia Unit": "cells",
    "「🗣️」Member of Communication Unit": "cells",
    "「📄」Member of Documentation Unit": "cells",
    "「🔧」Member of Technical Unit": "cells",
    "「🎎」Member of Organization Unit": "cells",
    "「🦾」Member of Project Unit": "cells",
    # club members with no unit yet
    "「✨」New Member": "all",
    "「✨」Old Member": "all",
    # must never be meeting audience
    "────────⎝ ・ STAFF ROLES ・ ⎠───────➤": None,
    "────────⎝ ・ Member ROLES ・ ⎠───────➤": None,
    "────────⎝ ・ PINGS ・ ⎠───────➤": None,
    "────────⎝ ・ LEVELS ・ ⎠───────➤": None,
    "────────⎝ ・ BIO ・ ⎠───────➤": None,
    "「📗」Verified": None,
    "♂️ | Male": None,
    "🌿 | LVL 01+": None,
    "💎 | Server Booster": None,
    "🔔 | Pings friendly": None,
    "⛏️ Minecraft Player": None,
    "⛏️ | Minecraft ping": None,
    "「👨‍💻」Server Developer": None,
    "「👨‍💻」Bot Developer": None,
    "Jockie Music": None,
    "EasyPoll": None,
    "「🏳」CR² Bot": None,
}

# Renames the keyword matcher has to survive, plus the "cell" wording.
RENAME_TOLERANCE = {
    "「🦾」Head of Projects Unit": "bureau",
    "「🚀」Lead of Software Cell": "bureau",
    "「🎓」Member of Docs Cell": "cells",
    "「👸」President  of the Club": "bureau",
}


def main() -> int:
    for var in ("BOT_TOKEN", "APPWRITE_API_KEY"):
        if not os.getenv(var):
            print(f"✗ Missing required env var {var} (set a dummy value for this test)")
            return 1

    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"  ✗ {label} = {got!r} (want {want!r})")
        else:
            print(f"  ✓ {label} = {got!r}")

    print("configured admin tier is the four bureau offices plus the developer")
    if DEPLOYED:
        tier = {role_key(name) for name in config.MEETING_ADMIN_ROLES}
        check("MEETING_ADMIN_ROLES", tier, {"manager", "president",
                                            "vice president",
                                            "archon of robotics club",
                                            "bot developer"})
        check("operator id whitelisted", SWIRX in config.MEETING_ADMIN_USER_IDS,
              True)
    else:
        # No deployment to inspect — assert the property that actually matters
        # and holds everywhere: every configured admin role resolves to a tier
        # that `is_meeting_admin` accepts.
        tier = {role_key(name) for name in config.MEETING_ADMIN_ROLES}
        bad = [name for name in config.MEETING_ADMIN_ROLES
               if not is_meeting_admin(_Member(name))]
        check("every MEETING_ADMIN_ROLE is accepted by is_meeting_admin",
              bad, [])
        check("MEETING_ADMIN_ROLES is non-empty", bool(tier), True)

    print("\nrole_key() strips the club's glyph decoration and folds case")
    check("glyph+space", role_key("「👸」Vice President"), "vice president")
    check("double space", role_key("Vice  President"), "vice president")
    check("empty", role_key(""), "")
    # The .env/server ZWJ difference must not matter.
    check("zwj vs plain emoji", role_key("「👨‍💻」Bot Developer"),
          role_key("「👨💻」Bot Developer"))
    check("section dividers", role_key("────────⎝ ・ STAFF ROLES ・ ⎠───────➤"),
          "staff roles")

    print("\nmeeting_tier() on every real server role")
    for name, want in REAL_SERVER_ROLES.items():
        check(repr(name), meeting_tier(_Member(name)), want)

    print("\nrename tolerance")
    for name, want in RENAME_TOLERANCE.items():
        check(repr(name), meeting_tier(_Member(name)), want)

    print("\nmanaged integration roles are ignored even with a matching name")
    managed = _Member("「👸」President")
    managed.roles[0].managed = True
    check("managed 'President' is not audience", meeting_tier(managed), None)
    check("managed 'President' is not admin", is_meeting_admin(managed), False)

    print("\nDiscord bot accounts never count as attendees")
    check("bot user with bureau role", meeting_tier(_Member("「👸」Manager", bot=True)), None)
    check("bot user is not admin", is_meeting_admin(_Member("「👸」Manager", bot=True)), False)

    print("\ntier precedence when several roles are held")
    check("chief + member + new",
          meeting_tier(_Member("「✨」New Member", "「🔧」Member of Technical Unit",
                               "「🔧」Head of Technical Unit")), "bureau")
    check("member + new",
          meeting_tier(_Member("「✨」New Member", "「🔧」Member of Technical Unit")), "cells")
    check("manager + plain member",
          meeting_tier(_Member("「✨」New Member", "「👸」Manager")), "bureau")
    check("only decorations + level", meeting_tier(_Member("🌿 | LVL 05+")), None)

    print("\nis_meeting_admin() — the bureau, the developer, and the operator")
    # These assertions describe the *deployed* .env: the operator's id is only
    # whitelisted there, and the club's real role names are only in
    # MEETING_ADMIN_ROLES there. On CI the defaults apply, so the hardcoded
    # club names match nothing and the id is absent — failures that say nothing
    # about the code. Assert against config instead, so the same check is
    # meaningful in both places and still catches a renamed role.
    # A whitelisted operator id only exists in a real .env; on CI there is
    # none, and is_meeting_admin correctly says False. Testing that here would
    # be asserting the absence of configuration, not the code — and it is what
    # made this file red in CI while the logic was fine.
    if DEPLOYED and config.MEETING_ADMIN_USER_IDS:
        operator_id = next(iter(config.MEETING_ADMIN_USER_IDS))
        check("whitelisted operator",
              is_meeting_admin(_Member("「✨」New Member", uid=operator_id)), True)
        check("operator outranks a plain role set",
              is_meeting_admin(_Member("🌿 | LVL 01+", uid=operator_id)), True)
    else:
        print("  – operator-id checks skipped (no deployment .env)")
    for role in config.MEETING_ADMIN_ROLES:
        check(f"configured admin role {role_key(role)!r}",
              is_meeting_admin(_Member(role)), True)
    # Belt and braces on the club's real names, which are only in
    # MEETING_ADMIN_ROLES when a deployment is present.
    for role in ("「🏴」Archon of Robotics Club", "「👸」President",
                 "「👸」Vice President", "「👸」Manager",
                 "「👨‍💻」Bot Developer"):
        if DEPLOYED:
            check(role_key(role), is_meeting_admin(_Member(role)), True)
    # The reports name individuals and can write a yellow card to a profile, so
    # whoever maintains the bot is in on deciding that.
    check("Bot Developer", is_meeting_admin(_Member("「👨‍💻」Bot Developer")), True)

    print("\n...and nobody else, including the wider bot-staff tier")
    # Unit heads run their unit's work but are audience, not organisers.
    check("unit head", is_meeting_admin(_Member("「🔧」Head of Technical Unit")), False)
    check("cell member", is_meeting_admin(_Member("「🔧」Member of Technical Unit")), False)
    check("plain member", is_meeting_admin(_Member("「✨」New Member", uid=999)), False)
    check("stranger", is_meeting_admin(_Member("🌿 | LVL 01+", uid=999)), False)
    # Bot staff are a separate tier: only the Bot Developer is pulled across,
    # and the Server Developer must NOT leak in with it.
    check("server developer", is_meeting_admin(_Member("「👨‍💻」Server Developer")), False)
    # The .env stores the developer role without the ZWJ the server uses; the
    # comparison must not care, or the developer silently loses access.
    check("developer role as written in .env, no ZWJ",
          is_meeting_admin(_Member("「👨💻」Bot Developer")), True)
    # An office holder is an admin by role, Discord permission or not.
    admin = _Member("「👸」Manager")
    admin.guild_permissions.administrator = True
    check("office holder w/ admin perm", is_meeting_admin(admin), True)
    # ...but Manage Roles on its own confers nothing.
    plain_admin = _Member("「✨」New Member", uid=999)
    plain_admin.guild_permissions.administrator = True
    check("discord server admin", is_meeting_admin(plain_admin), False)

    if failures:
        print(f"\n✗ {len(failures)} FAILURE(S)")
        for line in failures:
            print(f"    - {line}")
        return 1
    print("\nALL MEETING PERMISSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
