"""Minecraft level curve, read live from the same document the server reads.

``scripts/preview_progression.py`` is the single definition of the curve. It is
a script rather than a library, so it is loaded by path here rather than copied:
a second copy of the formula in this bot is exactly the drift the design forbids
between the docs, the server and the bot.

Levels are never read from the ``lv_*`` columns. The server stopped publishing
them because a published level is a snapshot of whichever curve was live at the
moment of the sync, and the curve is retunable while the server runs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SPEC = Path(__file__).resolve().parent.parent / "scripts" / "preview_progression.py"


def _load():
    spec = importlib.util.spec_from_file_location("preview_progression", _SPEC)
    if spec is None or spec.loader is None:  # pragma: no cover - build error
        raise ImportError(f"cannot load the canonical curve from {_SPEC}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_canonical = _load()

DEFAULT_CURVE: dict[str, Any] = {
    "mode": "tutorial_then_banded_geometric",
    "tutorial_base": _canonical.DEFAULT_TUTORIAL,
    "base": _canonical.DEFAULT_BASE,
    "band_size": _canonical.DEFAULT_BAND,
    "growth": _canonical.DEFAULT_GROWTH,
    "income_growth": _canonical.INCOME_GROWTH,
    "base_hourly_xp": _canonical.DEFAULT_HOURLY_XP,
    "max_reward_level": _canonical.MAX_REWARD_LEVEL,
    "prestige_every": _canonical.PRESTIGE_EVERY,
    "cosmetic_every": _canonical.COSMETIC_EVERY,
    "_rev": 0,
}


def row_id_for(key: str) -> str:
    """Appwrite row ids cannot contain a colon; the design keys do."""
    return key.replace(":", "-")


def cumulative_xp(level: int, curve: dict[str, Any] | None = None) -> int:
    spec = curve or DEFAULT_CURVE
    return _canonical.cumulative_xp(
        int(level),
        int(spec["base"]),
        int(spec["band_size"]),
        float(spec["growth"]),
        int(spec["tutorial_base"]),
    )


def level_for_xp(total_xp: int, curve: dict[str, Any] | None = None) -> int:
    """Largest level whose cumulative XP is reached.

    Levels continue past the reward cap, and level 1 is free, so a player with no
    experience is level 1 rather than level 0.
    """
    if total_xp is None or total_xp <= 0:
        return 1
    level, ceiling = 0, cumulative_xp(1, curve)
    while ceiling <= total_xp:
        level += 1
        ceiling = cumulative_xp(level + 1, curve)
    return max(1, level)


def progress_bar(total_xp: int, level: int, curve: dict[str, Any] | None = None,
                 width: int = 10) -> str:
    floor = cumulative_xp(level, curve)
    span = cumulative_xp(level + 1, curve) - floor
    if span <= 0:
        return "█" * width
    ratio = min(1.0, max(0.0, (int(total_xp) - floor) / span))
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)
