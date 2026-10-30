# 04 — Unlockables (slash commands)

## The three-stage model

Every premium slash command uses the same shape, deliberately:

1. **Eligibility** — a level milestone makes the command *appear* for the
   player. This is a goal.
2. **Purchase** — the player pays R once, permanently, to unlock it. This is a
   decision.
3. **Use** — every individual invocation costs R. This is a sink.

If it were only stage 2, the command appears randomly and players never learn
it exists. If it were only stage 1, unlocking is free and there is nothing to
choose. If it were only stage 3, there is no sense of owning anything.

Stage 3 is what stops the unlock from being a one-off purchase that generates
no ongoing activity.

## The command set

| command | eligible at | unlock cost | per-use cost | scaled by |
|---|---:|---:|---|---|
| `/chest` | L20 | 5,000 R | 50 R per slot opened | **Builder** |
| `/furnace` | L30 | 8,000 R | 20 R per smelt | **Farmer** |
| `/craft` | L40 | 12,000 R | 15 R x recipe tier | **Crafter** |
| `/shop` concurrent slots | L25 | 10,000 R | — (slot count scales with Businessman level) | **Businessman** |

## Cost formulas

The tables below are generated from these formulas and asserted in
`UnlockServiceTest`. An earlier revision carried hand-written tables that had
never been computed and disagreed with the formulas in several cells; the
formula is normative and the tables are checked against it.

### `/chest` — Builder

Storage is an extension of territory: it is where you put what you mined and
where you claim you are. Builder owns it because Builder is the job about
placing and holding ground, and this gives Builder a purpose beyond placing
blocks.

```
slotCost = max(10, round(50 * 0.97 ^ builderLevel))
```

| Builder level | 0 | 10 | 25 | 50 | 75 |
|---|---:|---:|---:|---:|---:|
| cost per slot | 50 R | 37 R | 23 R | 11 R | 10 R |

The floor of 10 R is reached around Builder 53, so the last twenty levels of
the curve buy a flat 10 R and nothing more. That is deliberate: a player with a
maxed job should not be able to make the command free.

Opening a chest costs `slots x slotCost` for the slots it exposes. The chest
persists; the cost is per *access session*, so a player pays once to open their
storage rather than once per slot per glance.

### `/furnace` — Farmer

Cooking is the step after harvesting. Farmer already owns harvest XP; the furnace
is the next link in that chain.

```
smeltCost = max(5, round(20 * 0.97 ^ farmerLevel))
```

| Farmer level | 0 | 10 | 25 | 50 | 75 |
|---|---:|---:|---:|---:|---:|
| cost per smelt | 20 R | 15 R | 9 R | 5 R | 5 R |

The 5 R floor lands around Farmer 36, earlier than the Builder floor because the
per-unit base is smaller.

A batch of 64 costs `64 x smeltCost`. At Farmer 0 that is 1,280 R, which is
deliberately expensive enough that players use it for value-added processing
rather than as a free shortcut around the sell cap.

### `/craft` — Crafter

```
craftCost = max(3, round(15 * recipeTier * 0.97 ^ crafterLevel))
```

`recipeTier` is the tier index from [03-jobs.md](03-jobs.md). Tier 0 (planks,
sticks) is **free** — those cost 0 R, consistent with them paying 0 XP. Charging
for the items everyone crafts constantly would be punitive rather than a choice.

| Crafter level | tier 1 | tier 2 | tier 3 | tier 4 |
|---|---:|---:|---:|---:|
| 0 | 15 R | 30 R | 45 R | 60 R |
| 10 | 11 R | 22 R | 33 R | 44 R |
| 25 | 7 R | 14 R | 21 R | 28 R |
| 50 | 3 R | 7 R | 10 R | 13 R |

The 3 R floor is reached around Crafter 45 at tier 1, so the deepest craft tiers
stay meaningfully expensive even for a maxed Crafter.

### `/shop` concurrent sell slots — Businessman

```
sellSlots = 1 + businessmanLevel     (capped at 8)
```

Purchasable once for 10,000 R at L25. After purchase the slot count is purely
Businessman level. This is Businessman's first real function — see
[03-jobs.md](03-jobs.md), where it is currently a SkillType with no XP source
and no behaviour at all.

---

## Purchase and refund semantics

- **Unlock is permanent.** There is no refund, deliberately: a refundable
  purchase is not a decision.
- **Purchase requires the wallet to cover it at the moment of purchase.**
  The debit and the flag set are one SQLite transaction.
- **Per-use costs are debited before the effect is applied.** If the furnace
  fails to place a result, the cost is not charged.
- **Daily cap interaction:** per-use costs are **not** counted against the
  40,000 R daily sell cap or the 12,000 R conversion cap. Those caps exist to
  limit faucets; command costs are sinks and must not have their own faucet
  blocked by them.

## What a player sees before unlocking

The command must not be hidden — a command a player cannot see is not a goal.

```
/chest is not yet unlocked for you.
  requires: Builder level 20   (you are level 8)
  cost:     5,000 R           (you have 1,240 R)
```

Showing the level requirement, the player's current level and the shortfall in
R makes the unlock a visible target. This is the single most important piece of
feedback for this system.

## Failure and edge cases

| case | behaviour |
|---|---|
| insufficient R for purchase | rejected, shows shortfall, no state change |
| insufficient R for a use | rejected, cost not charged |
| job level too low for the use | uses the cost floor; the command is still usable |
| player offline | no costs accrue; unlocks and storage persist |
| server restart mid-command | the debit is committed with the effect, never separately |
| concurrent use from two sessions | wallet debits are serialised per player |

## Interaction with claims

`/chest` storage is **per-claim**, not per-player-global. A player with three
claims has three chests, each costing its own access session. This makes
claiming and storage reinforce each other rather than being parallel progression
tracks that never intersect.