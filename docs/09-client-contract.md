# 09 — Modded client contract

The client is a **continuation**: same map, same database, same players, same
progression. Nothing resets. This document is what a client may rely on.

## What the client must not do

1. **Never be authoritative.** The server owns every balance, level, unlock and
   territory decision. The client renders what the server says.
2. **Never reimplement the curve.** Levels are computed server-side from
   `mc_progression`. A client that computes its own will drift the moment the
   curve is retuned — which is the entire reason the curve lives in the
   database.
3. **Never hold state that only the client knows.** No local unlock flags, no
   local quest state, no local balances.

## What the client may rely on

### Stable

| surface | notes |
|---|---|
| SQLite schema | server-authoritative; the client reads a mirror or an API |
| `mc_progression` payloads | the single definition of curve, rewards, tiers |
| SQLite as the system of record | balances, levels, claims, homes |
| the existing map | the client ships as a continuation of the same world |

### Guaranteed to exist by the time the client lands

- `mc_progression` with `curve`, `tiers:*`, `rewards:*`, `unlockables`,
  `quests:daily`, `quests:weekly`, `goals:server`
- quest state exposed for rendering
- unlock state per player
- a stable revision (`_rev`) on every payload

### Explicitly not guaranteed

- any specific GUI layout
- particle or cosmetics implementation
- the exact shape of tier tables beyond the documented fields
- anything derived client-side today that later moves server-side

## Ordering: why the client comes last

The client renders the progression. If the curve and rewards are not final, the
UI is written twice. More importantly:

**Mods add XP sources.** A mod that grants XP has to plug into the tier system
rather than invent its own scale — otherwise one mod with a generous reward
invalidates the whole curve and there is no way to retune eight mod authors at
once. That interface has to exist before mods are written against it.

The tier system in [03-jobs.md](03-jobs.md) **is** that interface. It is the
most important thing to get right before modding begins, and it is the reason
the job retier work sits before the client in the build plan.

## What a client needs from the tier system

For a mod to integrate cleanly it must be able to declare:

```json
{
  "mod": "examplemod",
  "job": "miner",
  "tier": 4,
  "value": 12,
  "stopLevel": 70
}
```

and have the server treat it exactly like a vanilla source. That requires the
tier lookup to be **data-driven and extensible at runtime**, not a switch
statement over vanilla materials — which is another reason it lives in
`mc_progression` rather than in code.

If a mod declares a source the server cannot categorise, the server must
**refuse the award and log it loudly**. Silently granting unknown XP at an
unknown rate is how a curve gets invalidated without anyone noticing.

## Client-side rendering guidance

The single highest-leverage thing the client can do for retention is make
progress **visible**:

- exact XP to next level, not a percentage
- what the next milestone unlocks, named
- active quests with their progress
- which rewards are still unclaimed in the track

A grind that is clearly communicated is engaging. The same grind hidden in a
text command is attrition. See [07-retention.md](07-retention.md).
