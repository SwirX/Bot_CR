"""The club's XP -> level curve, defined once and shared by the store and cogs.

Single source of truth so the *persisted* ``discord_data.level`` value (written
by the store on every XP flush) can never drift from what the cogs display:
``cogs/engagement.py`` and ``cogs/members.py`` import from here instead of
defining their own copies.
"""

import math


def level_from_xp(xp: int) -> int:
    """Level for cumulative XP. Level L needs 100·L² cumulative XP."""
    return int(math.isqrt(max(0, int(xp or 0)) // 100))


def xp_for_level(level: int) -> int:
    """Cumulative XP needed to reach ``level``."""
    level = max(0, int(level or 0))
    return 100 * level * level


def xp_progress(xp: int) -> tuple[int, int, int]:
    """Return (level, xp into level, xp needed for the next level)."""
    xp = max(0, int(xp or 0))
    level = level_from_xp(xp)
    return level, xp - xp_for_level(level), xp_for_level(level + 1) - xp_for_level(level)
