# 03 — Jobs

Eight jobs. Each is independent: XP, levels, perks and unlockability are tracked
separately, and a player may hold any combination.

## Design rules that apply to every job

1. **XP is the only stored quantity.** Levels are derived, never persisted. The
   server must not write an `lv_*` column — the current build does, and that is
   being removed. A stored level is a curve version frozen into a column, and it
   silently goes stale the moment the curve is retuned.
2. **Every XP tier stops paying at a level.** This is the core anti-farming
   rule. Without it, the optimal play is to repeat the cheapest action in the
   game forever.
3. **Rates are configurable per job.** `base` is per-job so each takes a
   comparable number of *actions* per level. Difficulty comes from how tedious
   the action is, not from an accidental rate mismatch.
4. **Weights scale XP earned**, separate from the curve, which scales XP spent.
   Keeping them independent is what stops one from silently compounding the other.

## Current state, and what is wrong with it

### Hunter — flat rate across almost everything

```java
rarityXp = hunterBaseXp * rarityMultiplier(entityType)
```

| multiplier | mobs |
|---:|---|
| 20 | ender dragon, warden |
| 15 | wither |
| 5 | blaze, ghast, enderman |
| **1** | **everything else** |

A cow and a skeleton pay identically, at every level, forever. There is no reason
to progress up the difficulty ladder because farming the easiest mob never stops
paying.

### Crafter — every recipe pays the same

```
craft XP = 2.0 base x 2.0 xp-multiplier x 0.1 weight = 0.4 XP per craft
```

Crafting planks pays exactly as much as crafting an Elytra, so the optimal
strategy is spam the cheapest recipe in the game. At ~100 crafts/hour that is
**~40 XP/hour**, putting level 100 around 10,700 hours away.

### Businessman — does not exist

`SkillType.BUSINESSMAN` is declared, has an `xp_businessman` column, appears in
leaderboards and profiles — and has **no XP source and no function**. It is the
only job with no reason for a player to pick.

---

## The tier system

Every XP source belongs to a tier with a value and a stop-level. When the
player's level in that job reaches the stop-level, the tier pays **zero**.

```
awardXp(player, job, tier) =
    0                                          if playerJobLevel >= tier.stopLevel
    tier.value * xpMultiplier * jobXpWeight    otherwise
```

This is the single mechanism that makes every job below self-balancing.

---

## Job specifications

### Lumberjack

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | common logs (_LOG, _WOOD, _STEM, _HYPHAE) | 4 | L20 |
| 1 | bamboo, stripped variants | 3 | L30 |
| 2 | dark oak, mangrove, cherry, pale oak | 6 | never |

- **Perks:** +5/10/15/20% log yield at 10/25/50/75.
- **Behaviour:** felling a tree counts once per block, not per tree, so a
  jungle tree is not worth a single oak.
- **Baseline:** ~1,200 XP/hr, ~300 logs/hr.

### Miner

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | stone, cobble, deepslate, gravel | 1 | L20 |
| 1 | coal, copper, lapis | 4 | L35 |
| 2 | iron, gold | 8 | L50 |
| 3 | redstone, emerald | 14 | L70 |
| 4 | diamond, nether gold, ancient debris, gilded blackstone | 26 | never |
| 5 | quest-only ore targets | quest | quest |

- **Perks:** +5/10/15/20% ore drop at 10/25/50/75.
- **Behaviour:** drops are multiplied server-side at the break event, before the
  drop rolls to the world — this is the mechanic the perk reads.
- **Baseline:** ~1,500 XP/hr, ~250 ores/hr. This is the reference job for
  `base_hourly_xp`.

### Builder

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | stone bricks, bricks, cobble, planks, glass | 1 | L25 |
| 1 | smooth stone, quartz, terracotta, concrete | 2 | never |
| 2 | prismarine, cut copper, polished stone | 4 | L60 |
| 3 | any block placed inside a **claim the player owns** | 2 | never |

- **Perks:** place freely in your own claim (10), then +5/10/15% sell value.
- **Behaviour:** tier 3 only pays inside owned claims, which ties Builder to the
  claims system and gives claiming a progression reason beyond protection.
- **Baseline:** ~800 XP/hr, ~400 blocks/hr. The lowest natural rate, so Builder's
  `base` is set higher to equalise effort.

### Hunter

| tier | mobs | XP | stops at |
|---|---|---:|---|
| 0 fodder | cow, pig, sheep, chicken, rabbit, squid, bat | 2 | L15 |
| 1 common hostile | zombie, spider, creeper, skeleton, slime | 8 | L25 |
| 2 uncommon | husk, drowned, witch, cave spider, pillager, ravager | 20 | L40 |
| 3 rare | blaze, ghast, enderman, phantom, guardian, elder guardian, evoker, vindicator | 60 | L60 |
| 4 elite | wither, ender dragon, warden | 400 | never |
| 5 quest | boss/rare targets | quest | quest |

- **Perks:** +5/10/15/20% mob drops at 10/25/50/75.
- **Behaviour:** only the **killer** receives XP, and only on a player-caused
  death. Farmed or despawned mobs pay nothing, so a mob farm cannot substitute
  for hunting.

### Farmer

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | wheat, carrots, potatoes, beetroot | 4 | L25 |
| 1 | melon, pumpkin, sugarcane, bamboo, cactus | 6 | L45 |
| 2 | any crop harvested | 10 | never |

- **Perks:** auto-replant (10), then +10/20/30% crop yield at 25/50/75.
- **Behaviour:** XP is on *harvest*, not on plant, so a player cannot farm XP
  by planting and breaking.
- **Baseline:** ~1,600 XP/hr.

### Blacksmith

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | smelt stone, sand, cobble | 1 | L20 |
| 1 | smelt iron ore, gold ore | 3 | L40 |
| 2 | smelt any ingot from raw ore | 5 | L65 |
| 3 | smelt any block (stone bricks, glass, terracotta) | 4 | never |
| 4 | anvil repairs and combines | 15 | never |

- **Perks:** 10/20/30/40% faster smelting at 10/25/50/75.
- **Behaviour:** faster smelting is a **fuel and queue-time** effect, not a
  speed multiplier on the furnace tick — the plugin must not alter vanilla
  smelt progress, only advance it.
- **Baseline:** ~750 XP/hr.

### Crafter

| tier | recipes | XP | stops at |
|---|---|---:|---|
| 0 **trivial** | planks, sticks, torches, ladders, slabs, stairs, panes, buttons, pressure plates | **0** | **never — always zero** |
| 1 basic | stone tools/armour, wooden furniture, chests, crafting table, barrels | 4 | L25 |
| 2 intermediate | iron tools/armour, stone bricks, blast furnace, hoppers, anvils | 12 | L40 |
| 3 advanced | gold/diamond tools/armour, enchanting table, shulker box, beacon | 35 | L60 |
| 4 master | enchanted books, elytra, modded rare recipes | 90 | never |
| 5 quest | named challenge crafts | quest | quest |

- **Perks:** +25/50/75/100% craft XP at 10/25/50/75.
- **Behaviour:** tier 0 is **worthless at every level, permanently**. This is
  the single most important rule in this document — planks and sticks are
  crafted constantly by everyone for everything, and any XP they carry is XP
  that can be generated sitting in a 2x2 grid.
- **Baseline after retier:** a player crafting mid-tier gear sees roughly
  300–600 XP/hr. Crafter's `base` is set accordingly, and the `0.1` weight is
  compensated by the XP perks rather than by inflating the weight.

### Businessman

| tier | sources | XP | stops at |
|---|---|---:|---|
| 0 | completing a shop **sell** through another player | 10 | never |
| 1 | completing a player **buy** order via `/mc buy` | 15 | never |
| 2 | fulfilling a player order on the order board | 25 | never |

- **Perks:** +10/20/30/40% daily sell cap at 10/25/50/75.
- **Function:** each level grants one extra concurrent slot in the `/shop` sell
  window, starting at 1. A player with Businessman 3 lists three item types at
  once instead of one.
- **Behaviour:** XP is granted on **settlement**, not on listing, so an order
  that expires unfulfilled pays nothing.
- **Why it exists:** Businessman rewards trading *with other players*, which is
  the one activity the solo economy cannot substitute for. That makes it the
  natural home for the market maker's competition.

---

## Job mastery

Reaching **level 50 in any single job** unlocks that job's endgame perk, so
specialising has a destination rather than being a slower route to the same
place. Endgame perks are specified with the job they belong to and are stored
per job, not globally.

## Power

`power = sum(levelFromXp(jobXp) for every job)`

It is a **sum of levels, not XP**. Two consequences that the display must make
obvious:

- It is breadth, not progression. A player who spreads across eight jobs scores
  far higher than one who masters a single job.
- It does **not** compound with XP weights. Weighting reduces XP *earned*; Power
  adds up whatever levels resulted. The difficulty weighting has no effect on it.

`/mc top` keeps Power as a board because it is a distinct and interesting
stat, but it must never be presented as a strength measure.