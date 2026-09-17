# 🤖 **Bot_CR** 🤖

**Bot_CR** is the Robotics Club's Discord bot: onboarding, engagement, stats,
moderation and levelling, all backed by an **Appwrite** data store. Every
command works as both `!prefix` and `/slash`.

---

## ✨ Features

- 🪪 **Real-name onboarding** — new members get a temp role, then a DM button
  opens a modal to enter their **real full name**. The bot applies a
  title-cased *cursive* nickname (`𝓨𝓪𝓼𝓼𝓮𝓻 𝓔𝓵 𝓙𝓸𝓾𝓷𝓭𝓲`), grants the verified +
  member roles, and asks for their birthday. No more reaction-based
  verification (anyone could have verified or kicked others) — now the member
  proves it with their real identity.
- 🎂 **Birthday tracker** — birthdays are stored per member in Appwrite and
  announced in the announcements channel on the day (plus an immediate
  announcement if you set a birthday that *is* today).
- 📈 **XP & levels** — chatting earns XP (per-user cooldown), levels grow with
  `100·L²` cumulative XP, level-ups are shouted in chat, and `/rank` +
  `/leaderboard` read straight from the store.
- 🔥 **Daily challenges** — staff set a challenge for the day
  (`/challenge set`), members claim it once (`/challenge claim`), history is
  kept, and each day's challenge is auto-posted to announcements.
- 📊 **Live dashboard** — member/message/voice counters persisted in Appwrite,
  refreshed every `DASHBOARD_REFRESH_SECONDS` (default 60 s). No more
  full-channel history scans.
- 🛡️ **Moderation** — `/kick`, `/ban`, `/unban`, `/timeout`, `/untimeout`,
  `/mute`, `/unmute`, `/warn`, `/modlog`, and `/del`. Every action is recorded
  in the Appwrite modlog with the moderator, target, reason and timestamp.
- 🔑 **Bot-staff override** — the Archon, bot-developer and bot-admin roles
  (config `ROLE_ARCHON` / `ROLE_BOT_DEVELOPER` / `ROLE_BOT_ADMIN`) can run
  every staff command without needing the guild permissions, exactly like the
  server owner did before.
- 🎖️ **Role management** — `/addrole <role> <@members...>` and
  `/removerole <role> <@members...>` assign/remove roles on the spot;
  `/setlead <role> <@members...>` **replaces** the holders of a leadership
  role (Lead / Vice President / President from `LEADER_ROLES`) with exactly
  the members you tag — previous holders are stripped first.
- 🌐 **Public-API commands** — `/weather`, `/define`, `/meme`, `/crypto`,
  `/spacex`, `/github`, `/advice` and `/lyrics`, all keyless, selected from
  openpublicapis.com.
- 🎵 **Music** — `/play <song>` streams YouTube audio (yt-dlp + ffmpeg) into
  your voice channel: `/pause`, `/resume`, `/skip` (majority vote — requester
  and staff skip instantly), `/stop`, `/loop`, `/volume`, `/queue`,
  `/nowplaying`, plus an interactive panel with pause / vote-skip / loop /
  stop / lyrics buttons.
- 🤖 **Private meeting rooms** — `/meeting create @a @b [name]` spins up a
  private VC (auto-deleted when empty), `/meeting end` cleans it up.
- 🔐 **Club permission scopes** — an authorization layer instead of scattered
  role checks: every club command is gated on a scope (`tasks.create`,
  `members.manage`, `competitions.manage`, …) resolved from the member's
  **linked club account** (Discord → club account → club role → cell) with
  Discord-side staff roles and server admins bootstrapping full access.
- 👤 **Member system** — `/link <club-id> [real name]` ties your Discord to
  your club account (role/cell are set by leadership via `/setprofile`, so you
  can never self-promote), `/profile`, `/whois @member` (staff),
  `/roles` (what your access means), `/hierarchy`, and a permission-aware
  `/dashboard` with quick-action buttons.
- 📋 **Tasks** — `/task create/assign/claim/complete/edit/cancel` +
  `/tasks` / `/tasks overdue` with priority colours and due dates; creation
  and assignment are permission-gated, and everything lives in Appwrite so the
  app sees the same tasks.
- 🏆 **Competitions** — `/competitions` lists upcoming events,
  `/competition <name>` shows details with capacity-enforced **Register /
  Unregister** buttons that write back to Appwrite.
- 📅 **Events & attendance** — `/event create` (chiefs+), `/events`, and
  `/event <title>` with ✅/❌/❔ RSVP buttons whose answers land in Appwrite —
  one source of truth for attendance.
- 🗳️ **Polls & votes** — `/poll create` (transparent or anonymous,
  single/multiple choice), `/poll vote`, `/poll results` (anytime) and
  `/poll close`. Transparent polls show who voted for what ("who's coming to
  the competition?"); anonymous polls hash the voter so counts are all anyone
  can see and double-votes can still be blocked — ideal for secret Chief
  ballots, with an optional locked-results mode. Every vote carries a
  timestamp and polls record `created_at` / `closed_at`, all persisted in
  Appwrite for the dashboard.
- 🔔 **Notifications** — `/notifications` toggles task/event/competition/
  announcement preferences (stored per member).
- 🔬 **Robotics fun** — `/robot` telemetry readout and a multiple-choice
  `/quiz` (engineering + robotics questions).
- 🛡️ **Channel controls** — `/slowmode`, `/lock`, `/unlock`.
- 👋 **Welcome & goodbye**, 📜 **rules board**, and 🎲 **fun commands**
  (8ball, coinflip, dice, slap, hug, joke, fact, compliment, rps, ship,
  choose, reverse, clap, roast, quote, website).

---

## ⚡ Commands

All commands are hybrid (prefix **and** slash). `/help` / `!help` lists them.

| Command | Who | What it does |
|---|---|---|
| `help` | everyone | Custom help, grouped by cog |
| `hello`, `ping` | everyone | Hello / latency check |
| `rank [member]` | everyone | Level, XP and progress bar |
| `leaderboard` | everyone | Top 10 by XP |
| `challenge today` | everyone | Today's challenge + claims |
| `challenge claim` | everyone | Claim today's challenge (once) |
| `challenge history` | everyone | The last 7 challenges |
| `challenge set <title> [desc]` | staff | Set today's challenge |
| `total_messages` | everyone | Total server messages (persisted) |
| `total_voice_time` | everyone | Total voice time (persisted) |
| `current_voice_time` | everyone | Time in your voice channel this session |
| `online_members` | everyone | Online/idle/dnd member count |
| `8ball`, `coinflip`, `dice`, `joke`, `fact`, `compliment` | everyone | Fun 🎲 |
| `slap <member>`, `hug <member>` | everyone | Social fun |
| `del <n>` | staff | Bulk-delete up to 100 messages |
| `warn <member> [reason]` | staff | Record a warning + modlog entry |
| `timeout <member> <min> [reason]`, `untimeout` | staff | Timeouts |
| `mute <member> [min]`, `unmute` | staff | Mute via Discord timeout |
| `kick <member> [reason]` | staff | Kick |
| `ban <member> [reason]`, `unban <id>` | staff | Ban / unban |
| `fixname <member>` | staff | Re-apply cursive nickname |
| `modlog [limit]` | staff | Recent moderation actions |
| `addrole <role> <@members...>` | staff | Give a role |
| `removerole <role> <@members...>` | staff | Take a role away |
| `setlead <role> <@members...>` | staff | Replace leadership holders (Lead/VP/President) |
| `weather <city>`, `define <word>`, `meme` | everyone | Open-Meteo / dictionary / meme |
| `crypto [coin]`, `spacex`, `github <user>`, `advice` | everyone | Live data APIs |
| `lyrics <song>` | everyone | LRCLIB lyrics |
| `play <song>` | everyone | Stream music (joins your voice channel) |
| `pause`, `resume` | everyone | Pause / resume music |
| `skip` | everyone | Vote to skip (requester/staff: instant) |
| `stop`, `loop`, `volume <1-100>`, `queue`, `nowplaying` | everyone | Music control |
| `meeting create <@members...> [name]`, `meeting end` | everyone | Private VC room |
| `link <club-id> [real-name]`, `unlink` | everyone | Link/unlink your club account |
| `profile [member]` | everyone | Club profile (respects visibility) |
| `roles` | everyone | What your club role can do (scopes) |
| `hierarchy` | everyone | Club org chart |
| `dashboard` | everyone | Permission-aware club overview |
| `whois <member>` | staff | Internal record (club ID, warnings, prefs) |
| `setprofile <member> role/cell/club-id` | leadership | Set club role / cell / club ID |
| `notifications` | everyone | Toggle notification categories |
| `tasks` / `tasks mine` | everyone | Your open tasks, colour-coded |
| `tasks overdue` | everyone | Overdue tasks (staff: whole club) |
| `task view <id>` | everyone | Full task detail + complete/claim buttons |
| `task create <title> [priority] [due] [cell]` | chiefs+ | Create a task |
| `task assign <id> @member` | chiefs+ | Assign a task |
| `task claim <id>` | cell members | Claim an unassigned task |
| `task complete <id>` | assignee | Mark your task done |
| `task edit <id> [title] [priority] [due] [status] [cell]` | chiefs+ | Edit a task |
| `task cancel <id>` | chiefs+ | Cancel a task |
| `competitions` | everyone | Upcoming competitions |
| `competition <name>` | everyone | Details + Register/Unregister buttons |
| `competition create <name> [date] [location] [capacity]` | leadership | Add a competition |
| `events` | everyone | Upcoming events |
| `event <title>` | everyone | Details + ✅/❌/❔ RSVP buttons |
| `event create <title> [date] [time] [location]` | chiefs+ | Schedule an event |
| `poll` / `poll list` | everyone | Overview of every poll |
| `poll create <question> <options> [mode] [selection] [hide_results]` | everyone | Create a poll (`\|`-separated options; transparent or anonymous, single or multiple) |
| `poll vote <id> <option>` | everyone | Vote (number or exact text; same again = undo) |
| `poll results <id>` | everyone | Live results — voters named for transparent, counts only for anonymous |
| `poll close <id>` | staff / creator | Stop voting and stamp `closed_at` |
| `robot` | everyone | Playful telemetry readout |
| `quiz` | everyone | 5-question robotics quiz |
| `slowmode <seconds>`, `lock`, `unlock` | staff | Channel controls |

---

## 🗄️ Data layer

All persistent state lives in the club's Appwrite project — no JSON files, no
in-memory counters that vanish on restart:

| Collection | Contents |
|---|---|
| `bot_members` | one doc per member (doc id = Discord user id): real name, cursive nickname, birthday, joined date, verified flag, XP, messages, voice seconds, warnings, **linked club account** (`club_id`, `club_role`, `cell`), notification prefs |
| `bot_counters` | global totals (messages, voice seconds) flushed incrementally |
| `bot_challenges` | one doc per date: title, description, who claimed it |
| `bot_modlog` | every moderation action with moderator/target/reason/timestamp |
| `bot_settings` | generic key → value storage |
| `bot_tasks` | tasks: id (`T-1`), title, description, assignee, cell, status, priority, due date, created by/at |
| `bot_competitions` | competitions: name, date, location, capacity, registered (user-id array) |
| `bot_events` | events: title, date/time/location, description, attendees + declined arrays |
| `bot_polls` | polls: id (`P-1`), question, options (array), mode (transparent/anonymous), selection (single/multiple), hide_results, closed/closed_at, created_by/created_at, votes (JSON per vote: voter id/hash, option index, timestamp) |

The bot connects with a server-side API key (no user auth), and the schema
(collections, attributes, indexes) is provisioned **idempotently on boot** or
via the standalone script:

```bash
python scripts/bootstrap_appwrite.py
```

High-frequency events (messages, voice time, XP) are accumulated in memory and
flushed in small batches to keep writes in the single-digits-per-minute range.

---

## 🛠️ Setup

1. **Python 3.10+** (Python 3.13/3.14 pull `audioop-lts` automatically via
   requirements).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create your environment file and fill it in:
   ```bash
   cp .env.example .env
   ```
   Required: `BOT_TOKEN`, `APPWRITE_ENDPOINT`, `APPWRITE_PROJECT_ID`,
   `APPWRITE_API_KEY`, `APPWRITE_DATABASE_ID` (channel/role names and behaviour
   knobs all default in `.env.example`).
4. Provision the Appwrite schema (idempotent, safe to re-run):
   ```bash
   python scripts/bootstrap_appwrite.py
   ```
5. Start the bot:
   ```bash
   python BOT.py
   ```

> 💡 Set `GUILD_ID` in `.env` during development so slash commands sync
> instantly to one server. Leave it empty to sync globally (up to an hour).
> Channel/role names are all configurable via env — no code changes needed if
> your server renames things.

---

## 🚀 Deploy (Render)

`render.yaml` describes a free-worker service. `BOT_TOKEN` and
`APPWRITE_API_KEY` are marked `sync: false` — set them in the Render dashboard
(never in the repo). `KeepAlive.py` runs a tiny Flask server on `$PORT` so a
ping service can keep the free instance awake.

---

## 🧱 Project structure

```
BOT.py                    launcher — logging, store init, cog discovery, tree sync
config.py                 every knob is an env var
data/                     Appwrite connectivity + async store
  appwrite_client.py      schema (collections/attributes/indexes) + bootstrap
  store.py                async typed wrappers (members, counters, challenges, modlog, settings, tasks, competitions, events, polls)
cogs/                     one file per feature; auto-discovered
  onboarding.py           join → name modal → cursive nickname → roles → birthday
  birthday_tracker.py     daily + immediate birthday announcements (store-backed)
  stats.py                messages / voice / presence tracking + periodic flush
  dashboard.py            persisted-counter dashboard embed
  engagement.py           XP & levels, daily challenges
  fun.py                  lighthearted commands (+ robot status, robotics quiz)
  moderation.py           kick/ban/timeout/warn + modlog + lock/slowmode/unlock
  welcome.py / goodbye.py join / leave messages
  rules.py                rules board
  general.py              hello / ping / custom help
  _perms.py / _scopes.py  staff bypass + club permission-scope resolver
  members.py              link/unlink, profiles, hierarchy, notifications, dashboard
  tasks.py                task CRUD + lists (priority/due/cell)
  competitions.py         competitions + registration
  events.py               events + RSVP attendance
  polls.py                polls + votes (transparent / anonymous, Appwrite-backed)
  _dates.py               shared date parsing/formatting helpers
scripts/
  bootstrap_appwrite.py   idempotent schema provisioning
  smoke_test.py           hermetic cog-load check (used by CI)
KeepAlive.py              Flask keep-alive on $PORT
render.yaml               Render worker service definition
```

**Adding a feature = dropping a new file in `cogs/`.** The launcher discovers
it automatically.

---

## ✅ Quality gates

`.github/workflows/ci.yml` runs on every push/PR against Python 3.12 and 3.13:
byte-compiles every module and runs `scripts/smoke_test.py` (loads all cogs,
asserts every command is hybrid and the custom `help` is installed).

```bash
python scripts/smoke_test.py   # run the same check locally
```

---

## 🌟 Contributors 🌟

A huge thanks to the amazing folks who helped bring **Bot_CR** to life! 👏✨

- **[Hamza](https://github.com/Yasahiru)** - The genius behind it all! 🤓💡
- **[Kawtar](https://github.com/ELGADDIxKawtar)** - For her outstanding contributions! 🧑‍💻🌟
- **[Wieam](https://github.com/wieam-ar)** - For her dedication and hard work! 🧑‍💻💪
- **[Yaser](https://github.com/0yaser0)** - For his innovative ideas and passion! 🧑‍💻🔥

---

## 📝 License 📝

This project is open-source and licensed under the MIT License.
Feel free to fork it, contribute, and make it your own! 🔄📜