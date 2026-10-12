# 10 — Build plan

Ordered by dependency, not by convenience. Each step has an acceptance test
that can be checked before moving on.

## Step 1 — `mc_progression` table and loader — **DONE**

Shipped and verified in production. The table, the `curve` row, and the loader
with validation and last-known-good caching described in
[08-data-model.md](08-data-model.md) are live.

**Accept — all three confirmed against the running server:**
- editing the `curve` payload changes level costs within 5 minutes with no
  restart (verified: growth 1.42 to 1.50 moved the level-100 cost from 429,440
  to 600,440 XP on the timer, then back)
- a malformed payload logs a warning naming the rule and the server continues
  on the cached value (verified: `growth: 1.0` was rejected with its reason and
  revision 1 kept)
- the curve row is the only one consumed so far; the other seeded rows are inert
  until their step

Also shipped with this step:

- `ProgressionParityTest` pins all 300 curve values against
  `preview_progression.py`, so the Java and Python definitions cannot drift
- `ProgressionStoreParsingTest` pins the TablesDB response shape against a body
  captured from the live instance
- `preview_progression.py --emit-rows` emits the seeded rows, so the bot
  repository remains the single definition of the numbers

Three bugs were found and fixed while building this, all of them in code that
had never run. See the defect list below.

## Step 2 — Derive levels, stop persisting them — **DONE**

`lv_*` is gone from `ProfileSyncService` and `ProfilePayloadBuilder`, and the
static `SkillCurve` is deleted. `SkillProfile` now derives every level through
the live curve, and the Discord bot derives from the same document.

**Accept — both confirmed:**
- changing `growth` changes displayed levels for existing players with no data
  migration, verified live in both the server log and the bot
- no `lv_*` column is written on sync, pinned by `ProfilePayloadLevelsTest`

`SkillProfile` takes a `Supplier<ProgressionCurve>` rather than an instance
because the curve is hot-reloaded: a cached profile has to observe a retuned
curve without being rebuilt.

The bot reads `xp_*` and applies the same formula, importing the canonical
script rather than copying the maths. Two things there were worse than the
`lv_*` read they replaced:

- `_progress` drew every job bar against a hand-copied `120 * level^1.45`, the
  curve the server had already deleted. Bars were rendering against a formula
  that no longer existed anywhere.
- the profile summed Minecraft and Discord XP and square-rooted the total. The
  two curves are unrelated, so that number was neither level. They are now
  reported separately and never combined.

**Left in place deliberately:** the `lv_*` columns still exist in Appwrite and
now hold stale values written under the old curve. Nothing reads them. Dropping
an Appwrite column is irreversible, so it waits for an explicit decision.

## Step 3 — Level rewards

Implement the three cadences with idempotent crediting.

**Accept:** reaching a level pays exactly once even when the AFK path, the
profile sync and the quit-persist all fire in the same tick; replaying a grant
is a no-op; the wallet ledger shows one row per `(player, source, level)`.

## Step 4 — Job tier retier  ← the interface mods will target

Convert Hunter and Crafter to tiered XP with stop-levels. Add Businessman XP
sources and its sell-slot function.

**Accept:** crafting planks grants **zero** XP at every level; killing a cow
grants XP at level 14 and none at level 16; Businessman XP is granted on order
settlement only, and an expired order pays nothing; `/mc top` Power still
computes as the sum of derived levels.

**This step is the interface the modded client will build against.** Doing it
before any modding starts is what makes future XP sources pluggable instead of
curve-breaking.

## Step 5 — Quest framework and daily board

**Accept:** three deterministic quests per player per day, stable across
re-logins, resetting at 00:00 UTC; the streak increases and never resets;
completing a quest pays exactly once.

## Step 6 — Unlockables

`/chest`, `/furnace`, `/craft`, and Businessman sell slots.

**Accept:** an ineligible player sees the requirement, their current level and
the R shortfall; a failed command charges nothing; per-use costs do not count
against the daily sell or convert caps.

## Step 7 — Market maker, mint and server goals

**Accept:** with zero players online, every shop item is still sellable; the
spread is positive in both directions; the treasury cannot be arbitraged; the
mint scales with distinct daily actives.

## Step 8 — Client

Only after 1–7 are live. See [09-client-contract.md](09-client-contract.md).

## Bugs to fix before or during this work

These are live defects found while writing this spec, not future work:

| issue | where | severity |
|---|---|---|
| `resolveIdentity` had zero callers | `PlaytimeService` | critical — no profile ever synced |
| profile sync read only an in-memory map | `ProfileSyncScheduler` | critical — offline players skipped forever |
| `IdentityResolver` built `/rowsqueries[]=` | `IdentityResolver` | critical — missing `?` |
| relationship sent as an array | `ProfilePayloadBuilder` | critical — Appwrite 1.9 rejects, HTTP 400 |
| `/mc profile` gated on a dead link chain | `mc_economy` | high — refused synced players |
| `/mc buy` refused on low mirrored balance | `mc_economy` | high — blocked every purchase from a fresh player |
| `PlayerProgressDao.UPSERT` writes `last_seen_at` as epoch ms against an epoch-seconds column | `PlayerProgressDao` | medium — type confusion |
| `OrderProcessor` calls Bukkit from an async task | `OrderProcessor` | high — not thread-safe |
| backfill never persisted `gameplay_xp` | `BackfillCommand` | medium — profiles carried zero play XP |

Found while building Step 1, all of them code that had never run:

| issue | where | severity |
|---|---|---|
| `job_perk` nested inside `if level % 10`, so perks at 25 and 75 were never emitted | `preview_progression.py` | high — two of four perks silently missing from every job |
| `_rev` written *inside* job-keyed and level-keyed maps, where it read as a job named `_rev` | `preview_progression.py` | medium — polluted every keyed payload |
| TablesDB list rows return attributes flattened, not nested under `data` | `ProgressionStore` | critical — payload never found, server silently ran on the fallback curve forever |
| Appwrite list query embedded as raw JSON, so `URI.create` threw | `ProgressionStore` | critical — stack trace on boot, no curve loaded |

The first four were found only because `mc_profiles` was empty and nothing had
ever exercised the path. Assume more exist in the same family.

## The general lesson to carry forward

Every one of those bugs shared a shape: **code that had never run**. A path
exercised for the first time in production should be assumed broken until proven
otherwise. Tests that cover the sync, the identity resolution and the order
path are worth more than any of the features above.

## Documentation that must stay current

| document | updates when |
|---|---|
| this set | any progression or economy change |
| `scripts/preview_progression.py` | any constant change — **it is the source of truth** |
| `COMMANDS.md` | any command or permission change |
| `PLAN.md` | any architecture change |
