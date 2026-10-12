#!/usr/bin/env python3
"""Preview the Minecraft job progression curve and reward track.

This is a design tool, not the shipping implementation. It exists so the curve
and the reward track can be tuned and *seen* before any of it is wired into the
plugin, and its output is the spec both sides agree on.

The reward structure below is deliberately shaped to become the Appwrite
``mc_progression`` payload later: one ``curve`` row and one ``rewards`` row, so
a balance change can ship by editing a document instead of rebuilding the jar.

Run with no arguments for the default curve, or override:

    python3 scripts/preview_progression.py --base 120 --growth 1.5
"""

from __future__ import annotations

import argparse
import json

# ── curve ────────────────────────────────────────────────────────────────
# Per-level cost is flat inside a band and grows geometrically between bands.
# Level 10 is deliberately cheap so a first session produces a real number to
# chase; every 10 levels after that the step is ~1.5x, which is what makes 100
# a genuine peak rather than a formality.
DEFAULT_BASE = 120          # XP for one level inside the first band
DEFAULT_BAND = 10           # levels per band
DEFAULT_GROWTH = 1.5        # cost multiplier added at each band boundary
MAX_REWARD_LEVEL = 100      # rewards track ends here
PRESTIGE_EVERY = 25         # reward cadence past the cap


def level_cost(level: int, base: int, band: int, growth: float) -> int:
    """XP to advance *into* ``level`` (level 1 is free)."""
    if level <= 1:
        return 0
    band_index = (level - 1) // band
    return int(round(base * (growth ** band_index)))


def cumulative_xp(level: int, base: int, band: int, growth: float) -> int:
    return sum(level_cost(lvl, base, band, growth) for lvl in range(1, level + 1))


# ── rewards ──────────────────────────────────────────────────────────────
# Every level pays something so the bar always moves. Only every 10th pays a
# milestone worth remembering, so there is a reason to plan a session around
# hitting a multiple of ten.
LEVEL_R_BASE, LEVEL_R_PER_LEVEL = 50, 5
MILESTONE_R_MULTIPLE = 500

JOBS = (
    "lumberjack", "miner", "builder", "hunter",
    "farmer", "blacksmith", "crafter", "businessman",
)

# Perks unlock at 10/25/50/75. Crafter compounds hardest because its XP weight
# makes it the grindliest job by rate, so its rewards buy XP back rather than
# adding another output multiplier on top of an already weak one.
PERKS = {
    "lumberjack": ["+5% log yield", "+10% log yield", "+15% log yield", "+20% log yield"],
    "miner":     ["+5% ore drop", "+10% ore drop", "+15% ore drop", "+20% ore drop"],
    "builder":   ["place freely in your own claim", "+5% sell value",
                  "+10% sell value", "+15% sell value"],
    "hunter":    ["+5% mob drops", "+10% mob drops", "+15% mob drops", "+20% mob drops"],
    "farmer":    ["auto-replant", "+10% crop yield", "+20% crop yield", "+30% crop yield"],
    "blacksmith": ["10% faster smelting", "20% faster smelting",
                   "30% faster smelting", "40% faster smelting"],
    # Crafter: XP, not output. 0.4 XP/craft vs Miner's 6.0 XP/ore is a 15x gap.
    "crafter":   ["+25% craft XP", "+50% craft XP", "+75% craft XP", "+100% craft XP"],
    "businessman": ["+10% daily sell cap", "+20% daily sell cap",
                    "+30% daily sell cap", "+40% daily sell cap"],
}
PERK_LEVELS = (10, 25, 50, 75)

UNIVERSAL_PERKS = {
    10: "+0.5% shop discount",
    20: "+0.5% shop discount",
    30: "+0.5% shop discount",
    40: "+0.5% shop discount",
    50: "claim cost -25%",
    60: "+0.5% shop discount",
    70: "+0.5% shop discount",
    80: "+0.5% shop discount",
    90: "+0.5% shop discount",
    100: "title: Peak",
}


def level_reward(level: int) -> int:
    return LEVEL_R_BASE + LEVEL_R_PER_LEVEL * level


def milestone_reward(level: int) -> int:
    return MILESTONE_R_MULTIPLE * (level // 10)


def prestige_reward(level: int) -> int:
    """Every PRESTIGE_EVERY levels past the cap, scaled so prestige stays worth it."""
    step = (level - MAX_REWARD_LEVEL) // PRESTIGE_EVERY
    return 2_000 * step * step


def build(base: int, band: int, growth: float) -> dict:
    """The shape both the preview and the future DB payload are built from."""
    rewards: dict[str, list[dict]] = {}
    for job in JOBS:
        entries = []
        for level in range(1, MAX_REWARD_LEVEL + 1):
            reward = {"level": level, "r": level_reward(level)}
            if level % 10 == 0:
                reward["milestone_r"] = milestone_reward(level)
                reward["perk"] = UNIVERSAL_PERKS.get(level, "")
                if level in PERK_LEVELS:
                    reward["job_perk"] = PERKS[job][PERK_LEVELS.index(level)]
            entries.append(reward)
        rewards[job] = entries

    prestige = []
    level = MAX_REWARD_LEVEL + PRESTIGE_EVERY
    while level <= 300:
        prestige.append({"level": level, "r": prestige_reward(level)})
        level += PRESTIGE_EVERY

    return {
        "curve": {
            "mode": "banded_geometric", "base": base, "band_size": band,
            "growth": growth, "max_reward_level": MAX_REWARD_LEVEL,
            "prestige_every": PRESTIGE_EVERY,
        },
        "rewards": rewards,
        "prestige": prestige,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=int, default=DEFAULT_BASE)
    parser.add_argument("--band", type=int, default=DEFAULT_BAND)
    parser.add_argument("--growth", type=float, default=DEFAULT_GROWTH)
    parser.add_argument("--per-level", action="store_true",
                        help="print all 100 levels instead of band boundaries")
    parser.add_argument("--json", action="store_true", help="emit the DB payload")
    args = parser.parse_args()

    spec = build(args.base, args.band, args.growth)

    if args.json:
        print(json.dumps(spec, indent=2))
        return

    print("\n  LEVEL CURVE  (per-level cost is flat within a band, x{growth} each band)"
          .format(growth=args.growth))
    print(f"  {'lvl':>4} {'cost into lvl':>14} {'cumulative':>12} {'R':>7} {'+milestone':>11}")
    previous = 0
    for level in range(1, MAX_REWARD_LEVEL + 1):
        cumulative = cumulative_xp(level, args.base, args.band, args.growth)
        milestone = f"{milestone_reward(level):,}" if level % 10 == 0 else ""
        is_boundary = level % 10 == 0
        if args.per_level or is_boundary:
            cost = cumulative - previous
            print(f"  {level:>4} {cost:>14,} {cumulative:>12,} "
                  f"{level_reward(level):>7,} {milestone:>11}")
        previous = cumulative

    print(f"\n  TOTAL to level {MAX_REWARD_LEVEL}: "
          f"{cumulative_xp(MAX_REWARD_LEVEL, args.base, args.band, args.growth):,} XP")
    total_r = sum(level_reward(l) + (milestone_reward(l) if l % 10 == 0 else 0)
                  for l in range(1, MAX_REWARD_LEVEL + 1))
    print(f"  TOTAL R earned across 1-{MAX_REWARD_LEVEL}: {total_r:,} R")

    print(f"\n  PRESTIGE TRACK  (past {MAX_REWARD_LEVEL}, reward every {PRESTIGE_EVERY} levels)")
    for entry in spec["prestige"][:6]:
        print(f"    level {entry['level']:>4}: {entry['r']:,} R")
    print("    ...")

    print("\n  MILESTONE PERKS  (every 10 = universal, job perk at 10/25/50/75)")
    for job in JOBS:
        cells = "  ".join(f"{lvl}:{p}" for lvl, p in zip(PERK_LEVELS, PERKS[job]))
        print(f"    {job:<12} {cells}")

    print("\n  UNIVERSAL MILESTONES")
    for level, perk in UNIVERSAL_PERKS.items():
        print(f"    {level:>4}: {perk}")
    print()


if __name__ == "__main__":
    main()