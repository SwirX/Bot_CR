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
# Two-band model. The first band is a deliberately cheap tutorial ramp so a
# first session produces a real number to chase; every band after it is an
# order of magnitude dearer, which is what stops level 70 from costing the same
# as the entire 0-20 journey.
DEFAULT_TUTORIAL = 150      # XP per level inside the tutorial band (1-10)
DEFAULT_BASE = 800          # XP per level in the second band (11-20)
DEFAULT_GROWTH = 1.42       # cost multiplier at each band boundary after that
DEFAULT_BAND = 10           # levels per band
MAX_REWARD_LEVEL = 100      # rewards track ends here
PRESTIGE_EVERY = 50         # reward cadence past the cap
COSMETIC_EVERY = 5          # cosmetic unlock cadence


# The command table from docs/04-unlockables.md. The cost formulas stay in code
# because they decay per level; what belongs here is the shape a client needs to
# render "you are 3,000 R short of this".
UNLOCKABLES = [
    {"key": "chest", "job": "builder", "required_level": 20, "unlock_r": 5_000,
     "use": "per claimed slot opened", "per_claim": True},
    {"key": "furnace", "job": "farmer", "required_level": 30, "unlock_r": 8_000,
     "use": "per smelt", "per_claim": False},
    {"key": "craft", "job": "crafter", "required_level": 40, "unlock_r": 12_000,
     "use": "per recipe tier", "per_claim": False},
    {"key": "sell_slots", "job": "businessman", "required_level": 25, "unlock_r": 10_000,
     "use": "slot count scales with job level", "per_claim": False},
]

# One entry per quest category. Targets and rewards are generated from these
# bands rather than authored, which is what makes the board indefinite.
QUEST_CATEGORIES = [
    {"key": "mine", "job": "miner", "small_target": 40, "large_target": 130,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "chop", "job": "lumberjack", "small_target": 40, "large_target": 130,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "build", "job": "builder", "small_target": 40, "large_target": 130,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "hunt", "job": "hunter", "small_target": 25, "large_target": 90,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "harvest", "job": "farmer", "small_target": 40, "large_target": 130,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "smelt", "job": "blacksmith", "small_target": 40, "large_target": 130,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "craft", "job": "crafter", "small_target": 30, "large_target": 110,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
    {"key": "trade", "job": "businessman", "small_target": 8, "large_target": 30,
     "reward_floor_r": 150, "reward_ceiling_r": 300},
]

# Every metric is a read of the ledger the economy already writes. A separate
# counter would need updating from every path that grants R, and the first path
# anyone forgot would undercount the goal forever.
GOAL_METRICS = [
    {"key": "distinct_players", "ledger": "count(distinct uuid)", "label": "players active"},
    {"key": "items_sold", "ledger": "count(reason like 'sell:%')", "label": "items sold"},
    {"key": "sell_income", "ledger": "sum(amount where reason like 'sell:%')",
     "label": "R earned from sales"},
    {"key": "quest_r", "ledger": "sum(amount where reason like 'quest:%')",
     "label": "R earned from daily quests"},
    {"key": "level_rewards", "ledger": "sum(amount where reason like 'level-reward:%')",
     "label": "R earned from level rewards"},
]



def level_cost(level: int, base: int, band: int, growth: float,
               tutorial: int = DEFAULT_TUTORIAL) -> int:
    """XP to advance *into* ``level`` (level 1 is free).

    Band 0 is the tutorial ramp at a flat ``tutorial`` cost. Band 1 costs
    ``base`` — roughly five times as much — and every band after that is
    ``growth`` times the one before it. Splitting band 0 out is what makes the
    early game readable: without it the geometric ramp is so shallow early that
    one level near 70 costs almost the same as the whole first twenty levels.
    """
    if level <= 1:
        return 0
    band_index = (level - 1) // band
    if band_index == 0:
        return int(round(tutorial))
    return int(round(base * (growth ** (band_index - 1))))


def cumulative_xp(level: int, base: int, band: int, growth: float,
                  tutorial: int = DEFAULT_TUTORIAL) -> int:
    return sum(level_cost(lvl, base, band, growth, tutorial)
               for lvl in range(1, level + 1))


# ── rewards ──────────────────────────────────────────────────────────────
# Three tiers, because one is not enough to hold attention over years:
#   every level  — R, scaled by band, so the bar always moves
#   every 5th     — a cosmetic, so progress is visible to other players
#   every 10th    — a milestone: real perk plus an unlock you can feel in game
# Vanilla goes boring precisely because nothing accumulates; these are the
# things that keep accumulating in a game with no seasons to reset them.
LEVEL_R_BASE, LEVEL_R_PER_LEVEL = 50, 5
MILESTONE_EVERY = 10           # milestone R + unlock cadence
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

# Milestones are permanent unlocks, not percentage nudges. These compound into
# real convenience over a long game, which is the point.
MILESTONES = {
    10:  "home slot 1  ·  +0.5% shop discount",
    20:  "home slot 2  ·  +0.5% shop discount",
    30:  "home slot 3  ·  claim radius +1  ·  +0.5% discount",
    40:  "home slot 4  ·  +0.5% shop discount",
    50:  "home slot 5  ·  claim radius +1  ·  AFK daily cap +1h  ·  claim cost -25%",
    60:  "home slot 6  ·  +0.5% shop discount",
    70:  "home slot 7  ·  claim radius +1  ·  +0.5% discount",
    80:  "home slot 8  ·  +0.5% shop discount",
    90:  "home slot 9  ·  +0.5% shop discount",
    100: "home slot 10  ·  title: Peak  ·  permanent +1% shop discount",
}

# Cosmetics are the every-5th cadence. Purely social, so they cost the economy
# nothing while still being the thing other players notice.
COSMETICS = {
    5:   "chat tag: verdant",
    15:  "particle trail: ember",
    25:  "join message colour: aqua",
    35:  "particle trail: frost",
    45:  "chat tag: gilded",
    55:  "join message colour: rose",
    65:  "particle trail: storm",
    75:  "chat tag: ascendant",
    85:  "particle trail: void",
    95:  "particle trail: aurora",
}


def level_reward(level: int) -> int:
    """Every level pays, so the progress bar is never dead."""
    return LEVEL_R_BASE + LEVEL_R_PER_LEVEL * level


def milestone_reward(level: int) -> int:
    """Every 10th level, on top, sized by which multiple of ten it is.

    The guard matters: without it every level from 10 up is a "multiple of ten"
    in Python's floor division, so the ladder would pay milestone R on 81 levels
    it should not and the budget would overrun by 202,500 R.
    """
    if MILESTONE_EVERY <= 0 or level % MILESTONE_EVERY != 0:
        return 0
    return MILESTONE_R_MULTIPLE * (level // MILESTONE_EVERY)


# ── earning rate ─────────────────────────────────────────────────────────
# XP income is NOT flat. Cost per level grows ~1.42x per band, so a flat income
# would make every band take 42% longer than the last and the wall into the
# 90s would be uncrossable. Income therefore grows with level, chosen so time
# per level rises gently (~15% per band) instead of exploding. This is also the
# core retention lever: levelling makes you earn faster, so there is always a
# reason to come back tomorrow.
INCOME_GROWTH = 1.1
DEFAULT_HOURLY_XP = 1500  # Miner baseline: ~250 ores/hr at 6.0 XP each


def hourly_income(level: int, base_rate: int) -> int:
    """XP/hour at ``level``, scaling by band so time-per-level grows gently."""
    return int(round(base_rate * (INCOME_GROWTH ** ((level - 1) // DEFAULT_BAND))))


def time_to(base: int, band: int, growth: float, tutorial: int,
            base_rate: int) -> dict[int, float]:
    """Cumulative active hours to reach each level."""
    out, elapsed = {}, 0.0
    for level in range(1, MAX_REWARD_LEVEL + 1):
        if level > 1:
            cost = level_cost(level, base, band, growth, tutorial)
            elapsed += cost / hourly_income(level, base_rate)
        out[level] = elapsed
    return out


def prestige_reward(level: int) -> int:
    """Quadratic in the prestige step.

    Minecraft has no season to reset for, so prestige cannot pay out the way a
    battle pass does — there is nothing to compete against except yourself,
    years apart. Every 50 levels past 100, growing quadratically so it stays
    worth chasing rather than being outrun within a season.
    """
    step = (level - MAX_REWARD_LEVEL) // PRESTIGE_EVERY
    return 5_000 * step * step


def build(base: int, band: int, growth: float,
          tutorial: int = DEFAULT_TUTORIAL) -> dict:
    """The shape both the preview and the future DB payload are built from."""
    rewards: dict[str, list[dict]] = {}
    for job in JOBS:
        entries = []
        for level in range(1, MAX_REWARD_LEVEL + 1):
            reward = {"level": level, "r": level_reward(level)}
            if level % COSMETIC_EVERY == 0:
                reward["cosmetic"] = COSMETICS.get(level, "")
            if level % 10 == 0:
                reward["milestone_r"] = milestone_reward(level)
                reward["unlock"] = MILESTONES.get(level, "")
            # Perks sit at 10/25/50/75, so this must not be nested inside the
            # milestone branch: 25 and 75 are not multiples of ten and would
            # otherwise never be emitted at all.
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
            "mode": "tutorial_then_banded_geometric",
            "tutorial_base": tutorial, "base": base, "band_size": band,
            "growth": growth, "income_growth": INCOME_GROWTH,
            "base_hourly_xp": DEFAULT_HOURLY_XP,
            "max_reward_level": MAX_REWARD_LEVEL,
            "prestige_every": PRESTIGE_EVERY, "cosmetic_every": COSMETIC_EVERY,
        },
        "rewards": rewards,
        "prestige": prestige,
    }


def rows(base: int = DEFAULT_BASE, band: int = DEFAULT_BAND,
         growth: float = DEFAULT_GROWTH,
         tutorial: int = DEFAULT_TUTORIAL) -> dict[str, dict]:
    """The ``mc_progression`` documents this script seeds.

    This is the single definition of the balance data: the Discord bot reads it,
    the server loads it, and the documentation describes it. Keys match the row
    names in ``docs/08-data-model.md``.
    """
    spec = build(base, band, growth, tutorial)
    milestones, perks = {}, {}
    for job, entries in spec["rewards"].items():
        for entry in entries:
            # Every level carrying either cadence, not just milestones. The
            # cosmetic cadence fires on odd multiples of 5 (5, 15, 25 ...) which
            # never have a milestone, so keying this off milestone_r alone
            # silently dropped all nine of them.
            level = entry["level"]
            is_milestone = "milestone_r" in entry
            is_cosmetic = level in COSMETICS
            if is_milestone or is_cosmetic:
                milestones[str(level)] = {
                    "r": entry.get("milestone_r", 0),
                    "unlock": entry.get("unlock", ""),
                    "cosmetic": COSMETICS.get(level, entry.get("cosmetic", "")),
                }
            if "job_perk" in entry:
                perks.setdefault(job, []).append(
                    {"level": entry["level"], "perk": entry["job_perk"]})
    return {
        "curve": dict(spec["curve"], _rev=1),
        # byLevel/byJob wrap the keyed collections so _rev is always a sibling of
        # the data and can never be mistaken for a level or a job name.
        # The R ladder is a formula rather than 100 stored rows, so a balance
        # change is a one-line edit instead of a regenerate-and-reseed.
        "rewards:formula": {
            "level_r_base": LEVEL_R_BASE,
            "level_r_per_level": LEVEL_R_PER_LEVEL,
            "milestone_r_multiple": MILESTONE_R_MULTIPLE,
            "milestone_every": 10,
            "perk_levels": list(PERK_LEVELS),
            # The ladder follows total job XP: summing all eight jobs keeps the
            # 1-100 payout at the documented 57,750 R instead of paying it eight
            # times over.
            "track": "total_job_xp",
            "max_level": MAX_REWARD_LEVEL,
            "_rev": 1,
        },
        "rewards:milestones": {"byLevel": milestones, "_rev": 1},
        "rewards:perks": {"byJob": perks, "_rev": 1},
        "rewards:prestige": dict(
            entries=spec["prestige"],
            formula="5000 * step^2",
            every=PRESTIGE_EVERY,
            after=MAX_REWARD_LEVEL,
            _rev=1,
        ),
        # The client contract promises these exist before a client is written
        # against them. Until they did, the promise was aspirational and a mod
        # author had nothing to render a goal or integrate a tier against.
        "unlockables": {
            "entries": UNLOCKABLES,
            "cost_decay": 0.97,
            # Tier 0 crafting is free because it pays no XP either.
            "craft_free_tier": 0,
            "_rev": 1,
        },
        # Quest rules, not quest instances. The board is derived per player and
        # per UTC date from these numbers, so the pool never needs authoring and
        # a client can run the same arithmetic the server will.
        "quests:daily": {
            "board_size": 3,
            "categories": QUEST_CATEGORIES,
            "streak_bonus_percent_per_day": 10,
            "streak_bonus_cap_percent": 100,
            "seed": "hash(player_uuid, utc_date)",
            "_rev": 1,
        },
        "goals:server": {
            "window_days": 7,
            "metrics": GOAL_METRICS,
            "mint_base_r": 2_000,
            "mint_per_active_player_r": 250,
            "market_spread": 0.10,
            "_rev": 1,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tutorial", type=int, default=DEFAULT_TUTORIAL)
    parser.add_argument("--base", type=int, default=DEFAULT_BASE)
    parser.add_argument("--band", type=int, default=DEFAULT_BAND)
    parser.add_argument("--growth", type=float, default=DEFAULT_GROWTH)
    parser.add_argument("--per-level", action="store_true",
                        help="print all 100 levels instead of band boundaries")
    parser.add_argument("--hourly", type=int, default=DEFAULT_HOURLY_XP,
                        help="base XP/hour at level 1")
    parser.add_argument("--json", action="store_true", help="emit the DB payload")
    parser.add_argument("--emit-rows", action="store_true",
                        help="emit the mc_progression rows for seeding Appwrite")
    args = parser.parse_args()

    spec = build(args.base, args.band, args.growth, args.tutorial)

    if args.emit_rows:
        print(json.dumps(rows(args.base, args.band, args.growth, args.tutorial), indent=2))
        return

    if args.json:
        print(json.dumps(spec, indent=2))
        return

    print(f"\n  LEVEL CURVE  (per-level cost is flat within a band, x{args.growth} each band)"
          )
    print(f"  {'lvl':>4} {'cost into lvl':>14} {'cumulative':>12} {'R':>7} {'+milestone':>11}")
    previous = 0
    for level in range(1, MAX_REWARD_LEVEL + 1):
        cumulative = cumulative_xp(level, args.base, args.band, args.growth, args.tutorial)
        milestone = f"{milestone_reward(level):,}" if level % 10 == 0 else ""
        is_boundary = level % 10 == 0
        if args.per_level or is_boundary:
            cost = cumulative - previous
            print(f"  {level:>4} {cost:>14,} {cumulative:>12,} "
                  f"{level_reward(level):>7,} {milestone:>11}")
        previous = cumulative

    peak = cumulative_xp(MAX_REWARD_LEVEL, args.base, args.band, args.growth, args.tutorial)
    print(f"\n  TOTAL to level {MAX_REWARD_LEVEL}: {peak:,} XP")
    total_r = sum(level_reward(lvl) + (milestone_reward(lvl) if lvl % 10 == 0 else 0)
                  for lvl in range(1, MAX_REWARD_LEVEL + 1))
    print(f"  TOTAL R earned across 1-{MAX_REWARD_LEVEL}: {total_r:,} R")

    print(f"\n  PRESTIGE TRACK  (past {MAX_REWARD_LEVEL}, reward every {PRESTIGE_EVERY} levels)")
    for entry in spec["prestige"][:6]:
        print(f"    level {entry['level']:>4}: {entry['r']:,} R")
    print("    ...")

    print("\n  MILESTONE PERKS  (every 10 = universal, job perk at 10/25/50/75)")
    for job in JOBS:
        cells = "  ".join(f"{lvl}:{p}" for lvl, p in zip(PERK_LEVELS, PERKS[job], strict=True))
        print(f"    {job:<12} {cells}")

    hours = time_to(args.base, args.band, args.growth, args.tutorial, args.hourly)
    print(f"\n  ACTIVE TIME  (income grows {INCOME_GROWTH}x per band, "
          f"from {args.hourly:,} XP/hr at level 1)")
    print(f"  {'lvl':>4} {'XP needed':>12} {'XP/hr':>9} {'this level':>12} {'cumulative':>12}")
    previous_xp = previous_time = 0.0
    for level in range(1, MAX_REWARD_LEVEL + 1):
        if level % 10 == 0:
            cumulative = cumulative_xp(level, args.base, args.band, args.growth, args.tutorial)
            rate = hourly_income(level, args.hourly)
            this_level = hours[level] - previous_time
            print(f"  {level:>4} {cumulative - previous_xp:>12,} {rate:>9,} "
                  f"{this_level:>11.1f}h {hours[level]:>11.1f}h")
        previous_time = hours[level]

    print("\n  MILESTONE UNLOCKS  (every 10 — permanent, not a percentage nudge)")
    for level, unlock in MILESTONES.items():
        print(f"    {level:>4}: {unlock}")

    print(f"\n  COSMETICS  (every {COSMETIC_EVERY} — social only, free to the economy)")
    for level, cosmetic in COSMETICS.items():
        print(f"    {level:>4}: {cosmetic}")
    print()


if __name__ == "__main__":
    main()
