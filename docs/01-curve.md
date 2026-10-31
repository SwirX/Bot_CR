# 01 — Level curve

## The formula

Job levels come from cumulative XP. The curve is **banded-geometric with a
separated tutorial band**.

```
band(level) = floor((level - 1) / 10)

cost(level) = 0                                             if level <= 1
            = tutorial_base                                if band(level) == 0
            = base * growth ^ (band(level) - 1)             otherwise

cumulativeXp(level) = sum(cost(1) .. cost(level))

levelFromXp(xp) = max { L : cumulativeXp(L) <= xp }
```

With the locked constants:

```
tutorial_base = 150        levels 1-10   cost 150 each
base          = 800        levels 11-20  cost 800 each
growth        = 1.42       every 10 levels beyond, cost x 1.42
band_size     = 10
```

### Why the tutorial band is split out

A single geometric ramp from a flat base is **too shallow early**, which inverts
the difficulty a player actually feels. Measured as "cost of one level at 70
divided by the entire 0→20 run":

| configuration | ratio | verdict |
|---|---:|---|
| single band, `base=150, growth=1.7` | 89% | level 70 costs as much as the whole first 20 levels |
| split tutorial band | **49%** | mid-game feels like work |

Raising `growth` alone does not fix this — it only pushes the same flattening
further out. Splitting band 0 does: levels 1–10 stay a cheap ramp so a first
session yields a number worth chasing, and band 1 starts at ~5x that.

## Band table

| band | levels | cost per level | band total | cumulative at end |
|-----:|-------:|---------------:|-----------:|-------------------:|
| 0 | 1–10 | 150 | 1,350 | 1,350 |
| 1 | 11–20 | 800 | 8,000 | 9,350 |
| 2 | 21–30 | 1,136 | 11,360 | 20,710 |
| 3 | 31–40 | 1,613 | 16,130 | 36,840 |
| 4 | 41–50 | 2,291 | 22,910 | 59,750 |
| 5 | 51–60 | 3,253 | 32,530 | 92,280 |
| 6 | 61–70 | 4,619 | 46,190 | 138,470 |
| 7 | 71–80 | 6,559 | 65,590 | 204,060 |
| 8 | 81–90 | 9,313 | 93,130 | 297,190 |
| 9 | 91–100 | 13,225 | 132,250 | **429,440** |

## Earning rate

**Income is not flat.** This is the most important part of the curve design.

Cost grows `1.42x` per band. If income were flat, time-per-level would grow
`1.42x` per band too, and level 100 would cost **8.8 hours of pure mining** in a
single level — level 90 becomes a wall nobody crosses.

Income therefore scales with level:

```
income(level) = base_hourly_xp * income_growth ^ band(level)
```

`base_hourly_xp = 1500` (Miner baseline: ~250 ores/hour at 6.0 XP each),
`income_growth = 1.1`.

Time per level then rises ~10% per band instead of 42% — a ramp that stays
crossable.

### Trade-off table

| income_growth | hours to L100 | hours for level 100 alone | verdict |
|---|---:|---:|---|
| 1.0 (flat) | 286 | 8.8 | uncrossable wall |
| **1.1** | **150** | **3.7** | **chosen** |
| 1.2 | 87 | 1.7 | too soft |
| 1.235 | 73 | 1.3 | far too soft |

### Resulting pacing

| level | XP for band | income XP/hr | time this level | cumulative |
|------:|------------:|-------------:|----------------:|-----------:|
| 10 | 1,350 | 1,500 | 0.1h | 0.9h |
| 20 | 8,000 | 1,650 | 0.5h | 5.7h |
| 30 | 11,360 | 1,815 | 0.6h | 12.0h |
| 40 | 16,130 | 1,997 | 0.8h | 20.1h |
| 50 | 22,910 | 2,196 | 1.0h | 30.5h |
| 60 | 32,530 | 2,416 | 1.3h | 44.0h |
| 70 | 46,190 | 2,657 | 1.7h | 61.4h |
| 80 | 65,590 | 2,923 | 2.2h | 83.8h |
| 90 | 93,130 | 3,215 | 2.9h | 112.8h |
| 100 | 132,250 | 3,537 | 3.7h | **150.2h** |

150 active hours to level 100. At one hour a day that is roughly five months —
a genuine achievement without an unreachable stretch at the end.

### Per-job baseline rates

`base_hourly_xp` is the Miner reference. Other jobs have different natural
rates, which is why `base` is **per-job configurable** rather than global — see
[03-jobs.md](03-jobs.md). Normalising them means every job takes a comparable
number of *actions* per level, so difficulty comes from how tedious the action
is, not from an accidental rate difference.

| job | current rate | note |
|---|---|---|
| lumberjack | ~1,200 XP/hr | ~300 logs/hr at 4.0 XP each |
| miner | ~1,500 XP/hr | ~250 ores/hr at 6.0 XP each (reference) |
| builder | ~800 XP/hr | ~400 blocks/hr at 2.0 XP each |
| hunter | highly variable | mob-tier dependent; see 03 |
| farmer | ~1,600 XP/hr | ~200 harvests/hr at 8.0 XP each |
| blacksmith | ~750 XP/hr | ~150 smelts/hr at 5.0 XP each |
| crafter | **~40 XP/hr today** | 0.4 XP/craft — unplayable, fixed in 03 |
| businessman | **none** | no XP source exists today |

## The level cap, and prestige

**Level 100 ends the *rewards* track, not levelling.** Players continue to gain
levels indefinitely.

Past 100 a reward lands every **50** levels, on a quadratic that keeps it
worth chasing:

```
prestigeStep(level) = floor((level - 100) / 50)
prestigeReward(level) = 5000 * prestigeStep(level)^2
```

| level | 150 | 200 | 250 | 300 | 350 | 400 |
|---|-----|-----|-----|-----|-----|-----|
| R | 5,000 | 20,000 | 45,000 | 80,000 | 125,000 | 180,000 |

Quadratic because **Minecraft has no season to reset for**. A battle-pass payout
works when there is a leaderboard and a deadline; here there is nothing to
compete against except yourself, years apart. Every 50 levels rather than 25
because a player reaching 100 has already proven the game can hold them — the
question after that is whether it can hold them for *years*.

Per-level R continues past 100 at the same rate, so prestige is a bonus track
rather than a second curve.

## Relationship to the Discord level

**Deliberately independent.** Discord uses a quadratic `100 x L^2` and gates
real Discord roles via `LEVEL_ROLE_REWARDS`. Minecraft uses the banded curve
above. They measure different things and are never summed into a single level.

The one place they meet is the `/leaderboard` ranking, which sorts on **total XP
(Minecraft + Discord)** and deliberately shows no level at all, because a single
level against a summed total would mean nothing in either system.