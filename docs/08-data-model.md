# 08 — Data model

## Principle

**Configuration lives in the database, not the jar.** Curves, rewards, tiers and
costs must all be changeable without a rebuild. A balance change ships as a
document edit, not a deploy.

## `mc_progression` table

```
key         string, required, unique index
payload     string, required        (JSON)
updated_at  datetime
```

Appwrite has no JSON attribute type, so `payload` is a string. This is the same
pattern the hub already uses for `links`.

### Rows

| key | contents |
|---|---|
| `curve` | the level formula and constants |
| `weights` | per-job XP weights and per-job base rate |
| `tiers:lumberjack` | tier table: sources, XP, stop level |
| `tiers:miner` | ” |
| `tiers:builder` | ” |
| `tiers:hunter` | mob tier table |
| `tiers:farmer` | ” |
| `tiers:blacksmith` | ” |
| `tiers:crafter` | recipe tier table |
| `tiers:businessman` | activity tier table |
| `rewards:milestones` | unlock + cosmetic tables |
| `rewards:perks` | per-job perk table |
| `rewards:prestige` | prestige cadence and formula |
| `unlockables` | command eligibility, purchase cost, per-use formulas |
| `quests:daily` | daily pool and rewards |
| `quests:weekly` | weekly pool and rewards |
| `goals:server` | server-wide goal definitions |
| `market` | spread, mint constants, rotation pool |

### Example payload — `curve`

```json
{
  "mode": "tutorial_then_banded_geometric",
  "tutorial_base": 150,
  "base": 800,
  "band_size": 10,
  "growth": 1.42,
  "income_growth": 1.1,
  "base_hourly_xp": 1500,
  "max_reward_level": 100,
  "prestige_every": 50,
  "cosmetic_every": 5
}
```

This is exactly `preview_progression.build()["curve"]`.

## Loading rules — the server must not be breakable by a document edit

1. **Load at boot**, before jobs or rewards are initialised.
2. **Refresh on a timer** (5 minutes) so a balance change takes effect without a
   restart.
3. **Validate before adopting.** Reject and keep the previous value if:
   - JSON is malformed
   - required keys are missing or non-numeric
   - `growth <= 1` (a non-increasing curve makes levels cheaper forever)
   - `tutorial_base <= 0` or `base <= 0`
   - `band_size < 1`
   - any tier has `value < 0` or `stopLevel < 1`
4. **Keep last-known-good in memory.** A failed refresh logs and continues with
   the cached value. It never partially applies.
5. **Fall back to `config.yml`**, which ships with the current constants, if the
   table or row is missing entirely on first boot.

A malformed `mc_progression` document must never be able to take the server down
or make levels unearnable. These rules exist for exactly that.

## Versioning

Each payload carries `_rev`. The server logs the active revision at boot and on
every adopted change, so "which curve was live when this player levelled" is
answerable after the fact. This matters because balance will be tuned live and
players will notice the moment it changes.

## Existing tables

| table | purpose | notes |
|---|---|---|
| `mc_profiles` | profile mirror | **must gain** `mc_xp_credited`, `mc_xp_credited_day`, `mc_xp_credited_today` |
| `mc_shop_items` | market mirror | 12 rows live |
| `mc_orders` | Discord order transport | `discord_user` relationship is unused and should be dropped |
| `discord_data` | hub XP | keyed by Discord id |

`minecraft_accounts.discord_user` is referenced by nothing that writes it and is
NULL on every row. It is a dead column — but MCLink's `LinkService` and
`OtpService` both reference the `minecraft_accounts` table and `LinkService`
references `discord_user`, so dropping it must wait until MCLink's source
confirms it does not write that column. Dropping a column in Appwrite is
effectively irreversible.

## Server-side SQLite — authoritative

| table | contents |
|---|---|
| `player_stats` | UUID, username, discord_user, gameplay/playtime counters, sync dirty flag |
| `job_progress` | **(uuid, job) -> xp** — the only job state that exists |
| `ledger` | every balance change, keyed for idempotency |
| `processed_orders` | settled orders, prevents double-fulfilment |
| `claims`, `claim_trust`, `squads`, `squad_members` | territory |
| `player_homes` | `/home` locations |
| `player_orders` | escrow order board |
| `price_book` | drifted market prices |

### Changes required

- **Drop `lv_*` persistence.** `ProfileSyncService` currently writes a
  materialised level column per job. Levels must be derived, or a curve change
  leaves stale columns that silently disagree with the formula.
- **Add `unlocks`** — `(uuid, command, purchased_at)`.
- **Add `quest_progress`** — `(uuid, quest_id, date, state)`.
- **Add `streaks`** — `(uuid, current, longest, last_day)`.
- **Add `treasury`** — single-row balance plus ledger.
- **Extend `ledger`** with `(source, level)` on reward grants so a replay is a
  no-op. The profile sync, AFK award and quit-persist paths can all observe the
  same level-up within one tick.

## Bot-side

The bot reads `mc_progression` to display levels and rewards, and writes only to
`discord_data`. It must never:

- author a balance change
- compute a level from a formula of its own
- cache a balance

`/leaderboard` joins `mc_profiles.gameplay_xp` with `discord_data.xp` on Discord
id and sorts on the total. It shows no level, because the two systems use
different curves and a single level against a summed total would mean nothing in
either.