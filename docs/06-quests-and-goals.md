# 06 — Quests and mid-game goals

Between level 20 and level 70 a player needs reasons to log in that are not
"a number went up". These are them.

## Daily quest board

Three quests per player per day, drawn deterministically from a pool weighted by
the player's job levels. Resets at 00:00 UTC.

```
dailyBoard(player, date) = pick(pool, 3, seed = hash(playerId, date))
```

Deterministic on `(playerId, date)` so a player cannot reroll by re-logging, and
reproducible so a bug is debuggable.

**Why this is the single most important retention feature:** it is the only
mechanic that gives a reason to appear *today* that is independent of level. A
player who has not levelled in a month still has three things to do.

### Quest categories

| category | example | pool source |
|---|---|---|
| craft | craft 8 tier-2 items | Crafter tiers |
| hunt | kill 15 tier-2 mobs | Hunter tiers |
| harvest | harvest 40 tier-1 crops | Farmer tiers |
| mine | mine 60 tier-3 ore | Miner tiers |
| chop | fell 25 logs | Lumberjack tiers |
| smelt | smelt 32 ingots | Blacksmith tiers |
| build | place 200 blocks in your own claim | Builder tiers |
| trade | sell 12 items through the shop | Businessman |
| spend | spend 1,500 R | economy |

Weighting by job level means a Miner sees more mining quests, which keeps a
specialist's board relevant without hiding the other categories.

### Rewards

| kind | value |
|---|---|
| daily R | 150–300 R per quest |
| job XP | scaled to the quest, matching the tier it references |
| streak bonus | +10% per consecutive day, capped at +100% |

The streak is the strongest retention lever available and must be **visible and
attainable** — a streak shown in the hub is worth more than the reward it pays.

## Weekly challenge

One harder quest per player per week, resetting Monday 00:00 UTC. Larger scope
and materially better rewards.

| kind | value |
|---|---|
| weekly R | 1,500–2,500 R |
| weekly XP | ~15% of a level's cost |
| completion | rare cosmetic or permanent unlock |

Weekly is where Businessman shines: "complete 20 player-to-player trades" is
impossible solo, which gives the job a reason to exist beyond sell slots.

## Server-wide goals

Rolling 7-day community objectives. See [05-economy.md](05-economy.md) for the
mechanism.

The defining property: **solo play advances them.** A player on an empty server
still moves the needle, which is what stops a quiet server feeling dead.

```
goal = { id, metric, target, progress, reward, windowStart, windowEnd }
```

| metric | example target | reward |
|---|---:|---|
| ores mined | 10,000 | 2,000 R to all participants + free claim |
| job XP earned | 500,000 | next week, +5% sell cap for all |
| distinct active players | 25 | server-wide shop discount |
| items sold | 2,000 | everyone gets a bonus chest |

Rolling windows, not permanent counters. Once passed, a new goal replaces it —
a permanent counter becomes a wall the moment it is cleared once.

## Job mastery

Reaching **level 50 in any single job** unlocks that job's endgame perk.

Specialising must lead somewhere, otherwise the optimal play is to spread thin
across all eight, which makes every job meaningless.

| job | mastery perk |
|---|---|
| lumberjack | fell 3 trees per swing below level 30 |
| miner | +10% chance of double ore on tier 3+ |
| builder | claim radius +3 and build cost waived in your own claim |
| hunter | one guaranteed rare drop on any tier-3 kill |
| farmer | crops regrow fully in one growth tick |
| blacksmith | smelting never consumes fuel below stack size 1 |
| crafter | one free craft per hour, any recipe |
| businessman | sell orders settle instantly with no expiry |

## Collection achievements

One-time, tracked on the server, not tied to level at all.

| achievement | condition | reward |
|---|---|---|
| first blood | sell your first item | 100 R |
| prospector | mine 100 tier-4 ore | 5,000 R |
| provisioner | craft one of every tier 3 recipe | cosmetic |
| exterminator | kill 50 of each tier 2 mob | 3,000 R |
| hoarder | hold 64 of every shop item | title |
| architect | own 5 claims simultaneously | +1 claim slot |
| regular | 30-day streak | permanent +1% discount |
| veteran | 100-day streak | permanent +2% discount |

Streak achievements deliberately reward **returning** rather than **being good**,
because time is the resource the game cannot give anyone and returning is the
behaviour that actually keeps a server alive.

## What is deliberately absent

- **No daily login reward.** It rewards opening the game, not playing it, and
  it punishes anyone who misses a day in a way that makes them not return.
- **No login streak that resets to zero.** Streaks increase; they never reset.
- **No leaderboard reset.** Ever.