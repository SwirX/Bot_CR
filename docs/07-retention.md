# 07 — Retention

## The honest assessment

**Retention today is bad, and the curve is not why.**

The curve is roughly right. What is missing is any reason to come back:

- levelling changes one number on one screen
- nothing about the player scales with level except the cost of levelling
- there is no daily content
- there is no reason for another player to exist
- the economy gives a player nothing to do alone beyond mining twelve specific
  items

A player who finishes a session has nothing specific to do tomorrow. They will
not return, and no amount of curve tuning fixes that.

## Ranked fixes

By expected impact, not by ease of implementation.

### 1. Income scales with level

The most important change in the whole design. Cost per level grows 1.42x per
band; if income were flat the last level would cost 8.8 hours of pure mining and
level 90 would be a wall nobody crosses. Income growing **1.1x per band** reaches
100 in 150 active hours with the last level at 3.7 hours.

Beyond the pacing, this is what gives levelling a *payoff*: you get faster, not
just bigger. See [01-curve.md](01-curve.md).

### 2. Daily quest board

The only mechanic that creates a reason to appear today independent of level.
A player who has not levelled in a month still has three things to do. This is
what converts "logs in sometimes" into "logs in daily". See
[06-quests-and-goals.md](06-quests-and-goals.md).

### 3. Visible progress

**A grind nobody can see is not content.** Right now progression is one number
in a text command. The modded client is the actual fix here: a real GUI showing
the exact XP bar, the next milestone and what it unlocks, and active quests.

This is why the client matters more than any server-side number. Difficulty that
is clearly communicated is engaging; difficulty that is hidden is attrition.

### 4. Server-wide goals

A reason that is not about you, and the thing that makes a quiet server still
feel alive because solo play contributes.

### 5. Economy floor

The market maker guarantees a player alone is never stuck. Removing the
"nothing to do" state matters as much as adding content — a player who cannot
earn has no reason to log in at all.

### 6. Command unlockables

New capability as a visible goal, and permanent R sinks so the currency has
somewhere to go. See [04-unlockables.md](04-unlockables.md).

## Why the curve specifically is not the lever

It is tempting to make levelling slower to make players stay longer. That is
backwards. A longer grind on the same content does not increase retention; it
just delays the churn until the grind outlasts the novelty.

The failure mode to avoid is a **flat early game followed by a brutal late
game**. Players quit during the flat part out of boredom, and the brutal part
then cements it. The split tutorial band in [01-curve.md](01-curve.md) exists
precisely to fix this shape.

## Projected effect

Not a promise — a hypothesis to be measured:

| stage | expected |
|---|---|
| now | session ends at the player's own discretion; return is chance |
| + income scaling, daily board, economy floor | players return daily because there is a reason |
| + server goals, streaks | players return because others are progressing |
| + modded client | progression is visible, so the grind reads as progress |

## What to measure

| metric | why it matters |
|---|---|
| D1 and D7 retention | the only honest retention signal |
| median session length | distinguishes "logs in daily" from "logs in and leaves" |
| % of daily logins with a quest completed | measures whether the daily board works |
| distinct players active per day | feeds the mint scaling |
| level distribution at days 30/60/90 | shows whether the curve wall is where it should be |
| quest completion rate by level | a low rate at a level means a wall, not engagement |

**Level distribution over time is the diagnostic that matters most.** If a
spike builds at any level and empties above it, that level is a wall, and the
fix is the curve or the reward at that level — not a notification.
