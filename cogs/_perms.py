"""Staff-permission helpers for Bot_CR (not a cog).

Full bot-staff bypass: members holding the Archon / bot-developer / bot-admin
roles (plus server admins) can run every staff command, regardless of Discord
guild permissions. Regular members must still hold the required permission.

Meeting administration is the one deliberate exception: ``/meeting start|end|
lock|unlock`` runs on :func:`require_meeting_admin`, a *narrower* allowlist
(the bureau offices) rather than bot staff, because starting a meeting rewrites
a real channel's permissions and rewrites the club's attendance record.
"""

import re

import discord
from discord.ext import commands

import config

# The club prefixes every role with a bracketed glyph ("「👸」President"), and
# ``.env`` files cannot be trusted to carry the *same* emoji codepoints Discord
# stores: "👨‍💻" (U+1F468 + ZWJ + U+1F4BB) and "👨💻" (no ZWJ) render alike but
# compare unequal, which would silently strip an admin's role. Both the glyph
# and the bracketing are therefore removed before any comparison.
_EMOJI = re.compile(
    "["                                  # pictographs, symbols, dingbats
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U00002190-\U000021ff"               # arrows, the club's "➤" dividers
    "\U0000fe0e\U0000fe0f"                # emoji presentation selectors
    "\U0000200d"                          # zero-width joiner
    "]+"
)
_DECORATION = re.compile(
    "["                                 # brackets and separators
    "「」『』\\[\\]()|·・\\-_~*"
    "\U00002500-\U0000257f"               # box drawing (the club's role dividers)
    "\U00002300-\U000023ff"               # ⎝ ⎠ misc technical, used in dividers
    r"]+|\s+"
)

# Roles that are never meeting audience, matched on **word boundaries**. A plain
# substring test would exclude "Archon of Robotics Club" itself, because
# "robotics" contains "bot".
_EXCLUDED = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(k) for k in config.MEETING_EXCLUDED_ROLE_KEYWORDS)
)


def role_key(name: str) -> str:
    """Normalise a Discord role name to its comparable form.

    Strips the leading glyph and its brackets, collapses whitespace and
    lowercases, so ``"「👸」Vice President"``, ``"Vice  President"`` and
    ``"vice president"`` all compare equal. Applied to *both* sides of every
    comparison, so a ZWJ / variation-selector difference between the emoji in
    ``.env`` and the one Discord stores cannot cost an admin their role.
    """
    return _DECORATION.sub(" ", _EMOJI.sub("", str(name or ""))).strip().lower()


def is_bot_admin(member: discord.Member) -> bool:
    """True for server admins, whitelisted operator IDs and staff-role holders."""
    if member.id in config.BOT_ADMIN_USER_IDS:
        return True
    if member.guild_permissions.administrator:
        return True
    roles = {role.name for role in member.roles}
    staff = {config.ROLE_ARCHON, config.ROLE_BOT_DEVELOPER, config.ROLE_BOT_ADMIN}
    return bool(roles & staff)


# ── Meeting audience roles ─────────────────────────────────────────────
def _role_keys(member: discord.Member) -> set[str]:
    """Every role the member holds, normalised for keyword matching.

    Integration roles (``role.managed`` — the CR² Bot, Jockie, EasyPoll) are
    dropped here rather than by name, so a bot joining the server later can
    never be counted as an attendee by accident.
    """
    return {
        key
        for key in (
            role_key(r.name) for r in member.roles
            if not getattr(r, "managed", False)
        )
        if key
    }


def _is_excluded(key: str) -> bool:
    return bool(_EXCLUDED.search(key))


def _is_bureau_office(key: str) -> bool:
    return any(word in key for word in config.BUREAU_OFFICE_KEYWORDS)


def _is_unit_head(key: str) -> bool:
    """A Chief / Lead / Head *of a unit* — never a bare "Lead" (e.g. "Minecraft")."""
    if not any(word in key for word in config.UNIT_HEAD_KEYWORDS):
        return False
    return any(word in key for word in config.UNIT_KEYWORDS)


def _is_unit_member(key: str) -> bool:
    return any(key.startswith(word) or f" {word}" in key
               for word in config.UNIT_MEMBER_KEYWORDS)


def _is_catchall_member(key: str) -> bool:
    """A New/Old Member tier — a club member with no unit assigned yet."""
    return any(tier in key for tier in config.CATCHALL_MEMBER_KEYWORDS)


# Audience rank per tier. Higher wins when a member holds several roles, which
# is the normal case: a Cell Chief also holds "Member of <unit>" and everyone
# also holds "New Member".
_TIER_RANK = {"all": 0, "cells": 1, "bureau": 2}


def meeting_tier(member: discord.Member) -> str | None:
    """Which ``/meeting start`` audience a member belongs to.

    ``"bureau"`` (office or unit head), ``"cells"`` (unit member), ``"all"``
    (club member with no unit yet) or ``None`` (not part of the club's meeting
    audiences). Deliberately requires a role: ``guild_permissions`` is *not*
    consulted, so a Discord permission such as Manage Roles on some unrelated
    channel can never pull a non-member into a bureau meeting.
    """
    if getattr(member, "bot", False):
        return None
    best = None
    for key in _role_keys(member):
        if _is_excluded(key):
            continue
        if _is_bureau_office(key) or _is_unit_head(key):
            tier = "bureau"
        elif _is_unit_member(key):
            tier = "cells"
        elif _is_catchall_member(key):
            tier = "all"
        else:
            continue
        if best is None or _TIER_RANK[tier] > _TIER_RANK[best]:
            best = tier
    return best


def is_meeting_admin(member: discord.Member) -> bool:
    """True for the bureau offices or a whitelisted operator id.

    Deliberately *only* those two sources — Archon, President, Vice President,
    Manager (``MEETING_ADMIN_ROLES``) and the ids in
    ``MEETING_ADMIN_USER_IDS``. Bot staff are intentionally **not** folded in:
    :func:`is_bot_admin` is a separate, much wider tier (server admins, the
    developer roles), and running a meeting is not a developer action. If the
    club ever wants a developer able to recover a channel mid-meeting, add
    that id to ``MEETING_ADMIN_USER_IDS`` rather than widening this gate —
    that keeps the escape hatch explicit and auditable in ``.env``.
    """
    if member.id in config.MEETING_ADMIN_USER_IDS:
        return True
    if getattr(member, "bot", False):
        return False
    admin_keys = {role_key(name) for name in config.MEETING_ADMIN_ROLES}
    return bool(_role_keys(member) & admin_keys)


def require_meeting_admin():
    """Gate a command on :func:`is_meeting_admin`."""

    async def predicate(ctx):
        if is_meeting_admin(ctx.author):
            return True
        raise commands.MissingPermissions(["meetings.manage"])

    return commands.check(predicate)


def mod_perms(**perms):
    """Permission gate that bot-staff bypass.

    Combines ``has_permissions`` and ``bot_has_permissions`` into one check:
    the author (or the bot) must hold the requested guild permissions — unless
    the author is bot staff, in which case the command runs regardless.
    """

    async def predicate(ctx):
        if is_bot_admin(ctx.author):
            return True
        required = discord.Permissions(**perms)
        if not ctx.bot.user.guild_permissions.is_superset(required):
            raise commands.BotMissingPermissions(list(perms))
        if not ctx.author.guild_permissions.is_superset(required):
            raise commands.MissingPermissions(list(perms))
        return True

    return commands.check(predicate)
