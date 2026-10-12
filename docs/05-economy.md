# 05 — Economy

## Currency

A single in-game currency, **R**. One currency with strong sinks and several
bounded faucets is easier to reason about than a multi-currency design, and it
makes every reward a real decision.

## Current state

### Working today

| mechanism | detail |
|---|---|
| wallet | SQLite-backed, per player, ledger-audited |
| `/sell` | resolves the held stack's `Material` to a shop listing, pays `sellPrice x quantity` |
| `/buy` | buys from the shop, debits the wallet |
| `/convert` | converts unsellable items to R at 70% of sell value |
| price drift | ±20% daily, seeded, so the market is not static |
| caps | 40,000 R/day sell, 12,000 R/day convert |

### Rare ore to money: yes, it works

Ores are priced in the shop and `/sell` pays out:

| item | buy price | sell (0.35 ratio) |
|---|---:|---:|
| coal | priced | — |
| iron ingot | 140 | **49 R** |
| gold ingot | 90 | **31 R** |
| diamond | 900 | **315 R** |

Capped at 40,000 R/day, so roughly 127 diamonds saturate the day. A player
alone with a pickaxe has a working income.

### What is missing: a floor

`/sell` pays only for the **12 items in the shop**. Everything else — most mobs,
most crops, most crafted goods, all mob drops — has **zero** sell value.

A player whose build does not produce those 12 items has no solo income path at
all. That is the single largest gap in the economy.

---

## The market maker

The fix. The server always takes the other side of a trade.

```
serverBuys(item)  = round(listing.sellPrice * (1 - SPREAD))
serverSells(item) = listing.buyPrice
```

`SPREAD` defaults to **0.10**. This guarantees:

- **A floor.** Nothing in the shop is ever unsellable, regardless of who is
  online. The player who mines alone is never stuck.
- **A sink.** The spread is the server's margin, which funds events and
  rotating stock.
- **No free money.** A player cannot profit by buying and re-selling to the
  server, because the server's sell price is below its buy price.

### Treasury funding

The market maker spends treasury R it does not have unless something mints it.
A daily mint, **scaled by distinct active players that day**:

```
dailyMint = MINT_BASE + MINT_PER_ACTIVE_PLAYER * distinctActivePlayers
           MINT_BASE            = 2,000 R
           MINT_PER_ACTIVE_PLAYER = 250 R
```

Scaling matters: a server with one player online mints 2,250 R, not enough to
fund a market maker that should never run a deficit. Twenty active players mint
7,000 R, and the mint grows with the player base instead of inflating on quiet
days when there is nobody to inflate for.

The treasury balance is capped. Overflow is **not** discarded — it is spent on
rotating stock and server-goal rewards, so a quiet period banks a surplus that
is then spent on the next busy event.

---

## Rotating limited stock

A small number of items cycle daily at a premium.

- 3 items per day, drawn from a rotation pool, deterministic by date seed so
  every player sees the same stock
- premium of **1.6x** the base price, decaying to base over the day
- cost the server nothing until bought, so it is free to run on a quiet server
- creates a reason to log in daily without gating anything

## Server-wide goals

Community progress that unlocks something for everyone.

```
serverGoal = { metric, target, progress, reward, endsAt }
```

Example: *the server mines 10,000 ore → every participant receives 2,000 R and
the next claim is free.*

The key property is that **solo play contributes**. A player on an otherwise
empty server still advances the goal, which is what stops a quiet server from
feeling dead. Metrics are read from the same ledger the economy already writes,
so there is no new instrumentation.

| metric | example target | cadence |
|---|---|---|
| ores mined server-wide | 10,000 | rolling 7 days |
| total job XP earned | 500,000 | rolling 7 days |
| distinct players active | 25 | rolling 7 days |
| items sold through the shop | 2,000 | rolling 7 days |

Rolling windows rather than permanent totals, so the goal stays reachable and
does not become a permanent wall once passed.

---

## Day-to-day player income

A player needs three ways to earn, in descending priority:

| route | who it suits | notes |
|---|---|---|
| **sell ore and goods** | everyone | 40,000 R/day cap; the baseline |
| **complete daily quests** | everyone | 3 per day, resets daily, the reason to log in |
| **fulfil orders for other players** | Businessman | uncapped by the sell cap, but requires other players |

Layer 1 is the floor the market maker guarantees. Layer 2 is the daily reason.
Layer 3 is what makes other players valuable to each other.

## Faucet and sink ledger

Every source and sink, with its cap, so the economy can be audited as a whole.

| source | cap | notes |
|---|---|---|
| `/sell` | 40,000 R/day | per player |
| `/convert` | 12,000 R/day | per player, at 70% rate |
| level rewards | 57,750 R total to L100 | one-time each, idempotent |
| daily quests | ~600 R/day | 3 per day |
| weekly challenge | ~2,500 R/week | 1 per week |
| server goals | varies | shared |
| prestige | unbounded above L100 | every 50 levels |

| sink | notes |
|---|---|
| `/buy` from shop | primary R sink |
| claim creation | 2,500 R after the first free claim |
| command unlocks | 5,000–12,000 R, one-time |
| command per-use | continuous |
| `/convert` input | consumes items, creates no R |

The economy is only healthy if total faucets and total sinks stay in rough
balance. This table is the thing to check after any change.

## Anti-inflation rules

1. **Every faucet is capped.** No uncapped R source exists or may be added.
2. **The mint scales with activity**, never with time.
3. **The server cannot be arbitraged.** `serverSells > serverBuys` always.
4. **Rewards are one-time.** The wallet ledger keys every grant on
   `(player, source, level)` so a replayed grant is a no-op — this matters
   because the profile sync, AFK award and quit-persist paths can all observe
   the same level-up in one tick.
5. **Appwrite never balances.** It mirrors for display. Every balance change is
   a SQLite transaction on the server.