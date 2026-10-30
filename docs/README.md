# RoboticsCMC — progression & economy specification

**Status: locked design. Nothing in this document is implemented yet.**

These documents are the contract between the game server, the Discord bot and
the future modded client. They are written to be implementable without further
design questions, and to be regenerable where numbers are concerned.

## The single source of truth for numbers

`scripts/preview_progression.py` owns every progression constant. This
documentation describes them; it does not define them independently. If the two
ever disagree, the script is correct and this documentation is stale.

```bash
python3 scripts/preview_progression.py                    # human-readable
python3 scripts/preview_progression.py --json             # DB payload shape
python3 scripts/preview_progression.py --base 900         # try a change
python3 scripts/preview_progression.py --per-level         # all 100 levels
```

Its `build()` return value is, by design, the payload proposed for the
Appwrite `mc_progression` table. Agreeing with this documentation is agreeing
with that stored document.

## Documents

| # | Document | Covers |
|---|----------|--------|
| 01 | [Curve](01-curve.md) | the level formula, bands, income scaling, prestige |
| 02 | [Level rewards](02-level-rewards.md) | every level 1-100, all reward cadences |
| 03 | [Jobs](03-jobs.md) | all 8 jobs: XP sources, tiers, stop-levels, perks |
| 04 | [Unlockables](04-unlockables.md) | slash commands, the two-stage purchase model |
| 05 | [Economy](05-economy.md) | currency, pricing, caps, solo income, market maker |
| 06 | [Quests & goals](06-quests-and-goals.md) | daily/weekly board, server goals, achievements |
| 07 | [Retention](07-retention.md) | why players leave, and the ordered fixes |
| 08 | [Data model](08-data-model.md) | Appwrite tables, payloads, config fallback |
| 09 | [Client contract](09-client-contract.md) | what a modded client may rely on |
| 10 | [Build plan](10-build-plan.md) | implementation order and acceptance criteria |

## Locked constants

| Constant | Value | Meaning |
|----------|------:|---------|
| `tutorial_base` | 150 | XP per level, levels 1–10 |
| `base` | 800 | XP per level, levels 11–20 |
| `growth` | 1.42 | cost multiplier at each 10-level boundary after that |
| `band_size` | 10 | levels per band |
| `income_growth` | 1.1 | XP/hour multiplier per band |
| `base_hourly_xp` | 1,500 | XP/hour at level 1 (Miner baseline) |
| `max_reward_level` | 100 | rewards track ends here; levels do not |
| `prestige_every` | 50 | reward cadence past the cap |
| `cosmetic_every` | 5 | cosmetic unlock cadence |

Derived totals: **429,440 XP** to level 100, **150.2 active hours**, **57,750 R**
earned across the track.

## Design principles these documents follow

1. **The server is authoritative.** SQLite decides. Appwrite is a display
   mirror and an order transport. The bot may never be the source of truth for
   anything that affects gameplay.
2. **Configuration lives in the database, not the jar.** Curves, rewards and
   tiers must be changeable without a rebuild. Every document below assumes
   this and specifies the fallback behaviour when the database is unreachable.
3. **Every faucet has a cap and a floor.** No income source may run unbounded,
   and no player may be unable to earn.
4. **Progress must be visible.** A grind nobody can see is not content.
5. **Nothing resets.** No seasons, no wipes, no leaderboard resets. Progression
   accumulates over years, which is why prestige is expensive and why rewards
   are unlocks rather than percentage nudges.