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
- 📈 **XP & levels** — chatting earns XP (per-user cooldown) with **bonus XP for
  rich content** (🖼️ images, 🎙️ voice notes, 🔗 links and 📝 long posts stack on
  the base roll), and **voice-channel time earns XP continuously** (daily cap).
  Levels grow with `100·L²` cumulative XP, level-ups are shouted in chat, and
  **level-up role rewards** (`LEVEL_ROLE_REWARDS`) auto-grant a role at
  milestone levels — `/rank` + `/leaderboard` read straight from the store, and
  staff catch up existing members with `/levelrewards backfill`.
- ⏰ **Reminders** — `/remind me duration: 30m text: …` DMs you when it's due
  (spans like `30s`, `2h`, `1d`, even `1h30m`), Appwrite-persisted so they
  survive restarts; `/remind list` and `/remind cancel <id>` manage them.
- 👑 **Account-age flex** — `/accountage [member]` shows a tiered flex card for
  any Discord account's age (fresh 🍼 → certified ancient 🦴), and
  `/accountage leaderboard` ranks the server's oldest accounts.
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
- 🎵 **Music** — `/play <song>` streams into your voice channel from
  Deezer → Audius in order (Deezer decrypts to a local file), plus `/pause`,
  `/resume`, `/skip`, `/remove <n>`, `/keep`, `/stop`, `/loop`, `/volume`,
  `/nowplaying` and `/radio <station>` (any live world radio station by name),
  and an interactive panel with pause / vote-skip / loop / stop / lyrics
  buttons. **Skip & remove voting** — the requester of a song, the session
  host and staff act instantly; anyone else opens a 20-second yes/no vote
  that passes as soon as the yes side holds a majority of the listeners
  present, and resolves at its deadline (ties and silence pass unless a
  strict majority voted no), so a lone troll can never deadlock the room;
  `/keep` votes against the motion — and the requester's keep is an outright
  veto. 📻 **Auto-radio** — `/queue auto` keeps the queue full around the
  *last played* song: whenever it runs low it refills from that track's Deezer
  radio (YTMusic as fallback), every refill is announced, and nothing heard
  this session is ever re-served. While anything plays *or is paused*, an
  extra `/play` only queues — playback is never interrupted. The controls
  panel is always the newest chat message (deleted and re-posted on every
  state change), and the bot auto-disconnects after a minute of idle with a
  friendly goodbye.
- 🔁 **JockieMusic migration nudge** — members who still type `m!` get a
  friendly pitch for `/play` and `/queue auto` (once per user, rarely per
  channel), so the old music bot converts without ever feeling like spam.
- 🗣 **Club meetings with attendance** — `/meeting start` creates a channel for
  the meeting, or name one to scope it instead; either way the bot restricts it
  to the audience you pick from a dropdown (**Bureau**, **Cell members**, **All
  members**, or **Custom** for a hand-picked list), records every join and leave
  as it happens, and hands back a report splitting the audience into who stayed,
  who left early and who never came — plus a CSV of the full timeline and a
  paged absentee list with a yellow-card button per absence. Reports are
  answered privately, and per-member detail is limited to the bureau and the bot
  developer. `/meeting lock` denies re-entry to leavers until an admin calls
  `/meeting unlock @member` by name. See
  [Meetings](#-meetings-meeting-start--end) below.
- 🤖 **Private meeting rooms** — `/meeting create [name]` spins up a private VC
  (auto-deleted when empty) and offers a dropdown to pick who gets in;
  `/meeting endroom` cleans it up.
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
- 🧩 **Cells** — `/cell add <@members...> [cell]` places members into a cell:
  Cell Chiefs can pull people into their **own** cell, while leadership and bot
  staff may name any cell. A plain Core Member is promoted to Cell Member
  (higher ranks are never demoted) and moving a member out of another cell is
  reported back.
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
  single/multiple choice) posts a panel with **one button per option**: tap to
  vote and the count goes up **live on the message**. Transparent polls show
  the per-option breakdown and who voted for what ("who's coming to the
  competition?"); anonymous polls hash the voter and show only the aggregate
  total on the panel, so anonymity hides both *who* voted and *how*, until
  `/poll close` reveals the final counts. Single-choice polls confirm when a
  tap **changes** your existing vote, and an optional locked-results mode hides
  everything until close. Every vote carries a timestamp and polls record
  `created_at` / `closed_at`, all persisted in Appwrite for the dashboard.
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

All commands are hybrid (prefix **and** slash). `/help` / `!help` opens the
interactive menu — **one button per section** (General, Fun, Entertainment,
Cell Management, Events & Meetings, Competitions, Polls, Members & Stats,
Moderation & Rules, Server Ops), with General front and centre.

| Command | Who | What it does |
|---|---|---|
| `help` | everyone | Interactive help with section buttons, grouped by topic |
| `hello`, `ping` | everyone | Hello / latency check |
| `rank [member]` | everyone | Level, XP and progress bar |
| `leaderboard` | everyone | Top 10 by XP |
| `levelrewards backfill` | staff | Grant level reward roles to members past the thresholds |
| `accountage [member]` | everyone | Flex card: how old is a Discord account |
| `accountage leaderboard` | everyone | The club's oldest accounts |
| `remind me duration: <span> text: <text>` | everyone | DM reminder (survives restarts) |
| `remind list`, `remind cancel <id>` | everyone | Manage your reminders |
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
| `bot status`, `bot update`, `bot restart` | bot-admin | Bot health, pull nightly + relaunch, restart |
| `weather <city>`, `define <word>`, `meme` | everyone | Open-Meteo / dictionary / meme |
| `crypto [coin]`, `spacex`, `github <user>`, `advice` | everyone | Live data APIs |
| `lyrics [song]` | everyone | LRCLIB lyrics — omit the song to look up the current track |
| `play <song>` | everyone | Stream music (joins your voice channel) |
| `queue [auto]` | everyone | Show the queue — `auto` keeps it full with 📻 radio from the last played song (nothing heard is re-served) |
| `pause`, `resume` | everyone | Pause / resume music |
| `skip` | everyone | Vote to skip (requester / host / staff skip instantly) |
| `remove <n>` | everyone | Remove queue song #n — instant if you added it, else a vote |
| `keep` | everyone | Vote against an open skip/remove vote (the requester's keep vetoes) |
| `radio <station>` | everyone | Stream any live internet radio station by name |
| `stop`, `loop`, `volume <1-100>`, `nowplaying` | everyone | Music control |
| `meeting create [name]`, `meeting endroom` | everyone | Private VC room, with a dropdown to pick who gets in (aliases: `closeroom`) |
| `meeting start [<#channel>] [audience] [minutes]` | meeting admin | Start a tracked meeting. `audience` is a dropdown: Bureau / Cell members / All members / Custom. No channel? The bot makes one and deletes it at the end |
| `meeting end` | meeting admin | End the meeting; restore the channel, or delete it if the bot made it |
| `meeting lock` | meeting admin | Deny re-entry to everyone who joined and left |
| `meeting unlock <@member>` | meeting admin | Let one locked-out member back in |
| `meeting list` | everyone | Meeting history. Meeting admins also get the report dropdown |
| `meeting last` | everyone | Stats for the last meeting. Per-member detail for meeting admins only |
| `link <club-id> [real-name]`, `unlink` | everyone | Link/unlink your club account |
| `profile [member]` | everyone | Identity hub: Overview · Minecraft link · Robotics (soon) tabs |
| `roles` | everyone | What your club role can do (scopes) |
| `hierarchy` | everyone | Club org chart |
| `dashboard` | everyone | Permission-aware club overview |
| `whois <member>` | staff | Internal record (club ID, warnings, prefs) |
| `setprofile <member> role/cell/club-id` | leadership | Set club role / cell / club ID |
| `cell add <@members...> [cell]` | chiefs+ | Add members to a cell (chiefs → their own; staff pick any) |
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
| `poll create <question> <options> [mode] [selection] [hide_results]` | everyone | Create a poll (`\|`-separated options; transparent or anonymous, single or multiple) with one vote button per option |
| `poll vote <id> <option>` | everyone | Vote by command (buttons on the poll are the quick way; same again = undo) |
| `poll results <id>` | everyone | Live results — voters named for transparent, counts only for anonymous |
| `poll close <id>` | staff / creator | Stop voting and stamp `closed_at` |
| `robot` | everyone | Playful telemetry readout |
| `quiz` | everyone | 5-question robotics quiz |
| `slowmode <seconds>`, `lock`, `unlock` | staff | Channel controls |

> 💡 **Poll examples**
> - Attendance (transparent — everyone sees the names):
>   `/poll create "Who's coming to RoboCup?" "Yes|No|Maybe"`
> - Secret Chief ballot (anonymous — counts only, and hidden until close):
>   `/poll create "Next Chief of IT?" "SwirX|Yaser|Taybi" anonymous single hide_results`
> - **Vote in one tap**: every poll has a button per option — click it and the
>   counts update in place. `/poll vote P-1 2` does the same thing by command
>   (pick the same thing again to undo). Results are always live:
>   `/poll results P-1`.

---

## 🗄️ Data layer

All persistent state lives in the club's Appwrite project — no JSON files, no
in-memory counters that vanish on restart:

| Collection | Contents |
|---|---|
| `bot_members` | one doc per member (doc id = Discord user id): real name, cursive nickname, birthday, joined date, verified flag, XP, messages, voice seconds, warnings, **linked club account** (`club_id`, `club_role`, `cell`), **identity links** (`links.minecraft` username/type/UUIDs/date, `links.robotics` reserved), `lang` (menu language), notification prefs |
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

### 🚫 YouTube playback was removed (SSRF)

`/play` used to hand whatever URL a member typed straight to `yt-dlp`, behind
no validation beyond an `^https?://` regex and with no socket timeout. Any
member could therefore make the host fetch **arbitrary internal URLs** —
`http://169.254.169.254/latest/meta-data/`, `http://10.0.0.x:6379/`, a
localhost service — and use the bot as an internal port/host prober (blind:
no response body ever reached Discord, but the requests were real). A
black-holed target also pinned a thread from the default executor forever.

The provider was removed rather than patched. **Deezer** (with an `ARL` token)
and **Audius** cover search and playback, and neither accepts an arbitrary
URL — a Deezer query is a search string, not a fetch target.

Because of this, `YT_COOKIES_FILE` and `youtube.com_cookies.txt` are no longer
read. If that file still exists on the host it is a live logged-in YouTube
session sitting on disk for nothing — delete it:

```bash
rm -f /home/ubuntu/Bot_CR/youtube.com_cookies.txt
```

### 🎧 Deezer source for `/play` (mainstream catalog, any network)

When YouTube is bot-blocked, Deezer fills the top-40 gap: full-length tracks
stream from any network (no datacenter-IP wall), and a **free account is
enough** (128 kbps MP3). The bot grabs the track through Deezer's grey-web
API, downloads the encrypted stream, and decrypts its BF-CBC "stripe" locally
before playing.

> 🎧 **Premium upgrade:** serve the ARL from a **Deezer Premium** session and
> the bot automatically negotiates **320 kbps MP3** (or **FLAC** for
> lossless-enabled accounts) at login — no code change. 128 kbps MP3 is the
> *quality ceiling* on free accounts and shows audible "sparkle" on loud,
> high-frequency-dense tracks; 320 kbps largely eliminates it. Confirm the
> active format in the bot's log: `Deezer session ready: format=MP3_320 …`.

1. Log into https://www.deezer.com in a browser used for nothing else.
2. Open DevTools → **Storage → Cookies** (or Application → Cookies) and copy
   the value of the `arl` cookie for `deezer.com`.
3. Put it in `.env`:
   `DEEZER_ARL=<the arl cookie value>`
4. Restart the bot. `/play <any mainstream song>` now has a working source.

> 🔒 The ARL is the account's session key — keep it in `.env` only, never in
> version control. If the account is ever flagged, the fix is a fresh
> throwaway account + a new ARL.

---

## ⛏️ Minecraft server bridge (`/mc`, `/minecraft`)

The club's Minecraft server (Robotics CMC) is managed through **Pterodactyl's
Client API** — no Minecraft plugins required:

- `/mc` or `/minecraft` — opens the **interactive Minecraft hub**: the join
  address `minecraft.alibks.dev:25566`, the server version (from a standard
  server-list ping), run state, player count and your own Discord ↔ Minecraft
  link status, with **Dank-Memer-style button drill-downs**:
  - 🔄 **Refresh** — re-check the live status;
  - 🔗 **Link your account** → explains the one supported path: run `/mclink`
    (the bot DMs a pairing code) and prove it in-game with `/mcverify <code>`.
    There is no username modal here on purpose — see the mc-link section for
    why an unverified self-service link was a full account takeover;
  - ❌ **Unlink** → confirmation → removes the whitelist entries and the
    Discord ↔ Minecraft link;
  - 🎛 **Server control** (visible **only** to the bot operator / Archon):
    ▶️ Start · ⏹ **Stop** · 🔄 **Restart** — same power signals as
    `/mcstart` `/mcstop` `/mcrestart`.
- `/mcstart` — boot the server.
- `/mcstop` — graceful shutdown.
- `/mcrestart` — graceful restart (offline server → hints `/mcstart` instead).

The operator's IDs come from `MC_CONTROL_USER_IDS` (defaults to
`BOT_ADMIN_USER_IDS`). The Archon role is matched by `ROLE_ARCHON` (fallback:
any role whose name contains "archon", so emoji-prefixed names survive).
They send Pterodactyl power signals through the **Client API** (`/power`), so
the panel key needs power scope on top of file/console.

### 🏷 Player tagging & join rally

Every member who links a Minecraft account is auto-assigned the
**`MC_PLAYER_ROLE`** role (created by the bot on first use, backfilled for
existing links on boot) and loses it again on unlink — so **one `@role`
mention pings the whole linked player base**:

- **`/mcsession [message]`** — MC operators (`MC_CONTROL_USER_IDS`) or anyone
  with the `announcements.create` scope (VP+ / bot staff) posts an
  **English rally card** in the announcements channel with a live join address.
  The ack message itself is localized per member.
- **Auto join-rally** — the bot streams the server console over a
  **Pterodactyl websocket** (no plugins) and watches for `joined the game`.
  Joins are burst-coalesced, then capped at 5 names (+N) and posted as one
  role ping to the announcements channel, **at most once per
  `MC_RALLY_COOLDOWN`** (default 2700 s). The live names also enrich the
  “Players” line on the `/mc` hub while the watcher is connected.

Additional env vars:

```
MC_PLAYER_ROLE=⛏️ Minecraft Player      # role auto-assigned on link
MC_RALLY_COOLDOWN=2700                  # min seconds between auto pings
```

### 🔐 mc-link (Discord ↔ Minecraft single sign-on, `/mclink`)

The cracked (offline-mode) server can't trust what clients claim, so a Paper
plugin gates logins through **AuthMe** and this bot — the **only** component
that mints links and credentials — anchors every name to a Discord identity.
Minecraft is an **untrusted client boundary**: a username, UUID or "op" flag a
client presents is data, never proof.

The design is **OTP-only** (mitigation plan §5). The old shared AES secret,
`/mcpass`, IP-trust auto-login and the `mc_link_codes` / `mc_auth` /
`mc_challenges` tables are **gone**. The bot's Appwrite key is the only shared
credential, and the plugin only ever *consumes* backend state.

- **`/mclink <username>`** (hybrid, guild-only, 2/60 s cooldown) — validates the
  name, refuses it if this user already has a link *or* that name is already
  taken by someone else, mints a single-use code and **DMs it** (never posted in
  a channel; falls back to an ephemeral, invoker-only message if DMs are closed).
  In-game `/mcverify <code>` proves the person actually controls the account and
  only *then* does the link become active.
- **There is deliberately no `/linkmc`.** A self-service command that took a
  username and whitelisted it without in-game proof could be pointed at someone
  else's unlinked name; because it wrote an *active* link, the OTP watcher would
  then deliver that account's login code to the claimant — a full account
  takeover. `/mclink` + `/mcverify` is the only linking path, and
  `scripts/smoke_test.py` fails the build if a `linkmc` command reappears.
- **Login OTP (`/otp <code>` in-game)** — when the plugin arms a challenge on
  join, the watcher mints a **bcrypt-12 hash** of a fresh code, DMs the plaintext
  to the linked Discord user, and expires the row if that DM fails. The plugin
  verifies with `checkpw`; the plaintext is never stored. Hashing runs off the
  event loop and the cycle has a wall-clock budget.
- **Expired codes re-arm.** A minted-but-unused row that passes its TTL is
  treated as stale and re-armed on the next join, so a forgotten code can never
  wedge a player out of logging in.
- **`/mcotp`** (hybrid, 2/60 s) re-arms a row the bot itself expired after a
  failed DM. It refuses to overwrite a live code and never touches
  `failed_attempts` — the plugin's brute-force lockout.
- **Watchers (~`MC_LINK_POLL_SECONDS`)** — pending-OTP minting, pair expiry, and
  an activation sync that grants `MC_PLAYER_ROLE`, the thanks DM, the
  `links.minecraft` record shown on `/mc` / `/profile`, and an audit entry. A
  marker in `bot_settings` resumes after restarts (at-least-once delivery).

```
MC_LINK_CODE_TTL=300                   # OTP + pair-code lifetime (s)
MC_PAIR_KEY_TTL=300                    # unclaimed pairing-code lifetime (s)
MC_LINK_POLL_SECONDS=5                 # watcher poll interval (s)
```

`scripts/test_mclink.py` covers the code generation and hashing contract
(CSPRNG `secrets.choice` over an unambiguous alphabet, bcrypt-12 at rest).

### 🔄 `/bot status` update checker

`/bot status` now compares the running build to **`origin/nightly`** (the same
ref `/bot update` pulls) — fetch is cached 120 s — and shows:

- ✅ **Up to date** when `HEAD` equals (or is ahead of) the remote;
- ⬆️ **N commits behind** with a 3-commit preview and an **`⬆️ Update to
  nightly (N commits)` button** (bot-admin interaction only);
- a **confirm-first** step that warns it runs `git pull --ff-only origin nightly`
  and restarts the bot — identical to `/bot update`, restart-ack included;
- ⚠️ a **diverged** note when fast-forwarding isn't possible (manual redeploy).

---

## 🗣 Meetings (`/meeting start` → `end`)

A tracked meeting is an **event with a report**. The admin opens one in a voice
channel, the bot scopes that channel to the invited audience for the duration,
every join and leave is written to the hub as it happens, and ending the meeting
produces an attendance report.

### 🚪 Who can run it

Meeting commands and the per-member reports are gated on a tier of their own,
separate from general bot staff: **Archon, President, Vice President, Manager,
Bot Developer**, plus anyone in `MEETING_ADMIN_USER_IDS`. The bureau offices are
matched on role *name* keywords (`BUREAU_OFFICE_KEYWORDS`), so a rename to
`「👸」President` or `President of Robotics` still resolves. The **Server
Developer** is deliberately **not** included — use `MEETING_ADMIN_USER_IDS` to
add anyone by id.

Two reasons the Bot Developer is in despite not being a bureau office. The
reports name individual members and can write a yellow card to their profile,
which is a judgement about people that whoever maintains the bot should be in
the room for. And emoji are stripped before role names are compared, so a
`.env` that spells the developer glyph without the server's zero-width joiner
still resolves instead of silently failing.

`MEETING_ADMIN_USER_IDS` is the escape hatch for a named person: list them by id
and they keep the override without holding — or ever having held — the Archon
role.

### ▶️ Starting a meeting

```
/meeting start minutes:60 audience:bureau                      # bot makes a channel
/meeting start channel:#⦿Bureau⦿ minutes:60 audience:bureau    # use an existing one
```

The meeting begins immediately; `minutes` is how long it is *planned* to run
(the report shows planned vs actual).

`channel` is **optional**. Point it at an existing voice channel and the bot
scopes that one for the meeting, restoring its permissions at the end. Leave it
blank and the bot **creates a dedicated channel**, named after the audience
(`📣 Bureau Meeting`), in the same category as wherever the command was run —
falling back to the guild root. A channel the bot made has no previous state to
return to, so `/meeting end` **deletes it** rather than restoring permissions
onto it. Every failure path after creation removes the channel again, so a
rejected audience or a failed write cannot leave an orphan behind.

`audience` is a **dropdown**:

| Audience | Who can join |
| --- | --- |
| `Bureau` | Archon, President, VP, Manager **and** the Chief/Lead/Head of every unit |
| `Cell members` | everything in `Bureau`, plus every member of any cell |
| `All members` | every club member |
| `Custom` | only the people you pick from the dropdown that appears |

`Custom` ignores roles entirely — the people you pick are the whole audience, so
a meeting cannot quietly widen itself to whoever happens to hold a qualifying
role. The bot adds whoever ran the command, since no role would have put them in
a custom audience and they need to be able to hear the meeting they opened.

Extra members can be tagged on top of a role-based audience with `also:@a @b`
(capped by `MEETING_MAX_EXTRA_MEMBERS`). `also` is **text-command only**:
Discord has no command option that accepts several users at once, which is why
the dropdown exists and why `Custom` is the way to choose people from the
Discord UI. Resolution reads role names, not ids, so a promotion between
meetings is picked up automatically — and the **expected list is snapshotted at
start**, so the absentee report describes who the meeting was for on the day
rather than re-resolving the club org chart later. Managed integration roles and
bot accounts are excluded.

### 🔒 Lockouts

`/meeting lock` denies **re-entry** to everyone who entered and left. Someone who
left and came back is left alone — denying them would disconnect the person
running the meeting from their own channel — and someone who never joined is not
denied either, which would invent an absence they did not make and then report
them absent on a meeting they were locked out of.

Locking is a per-member `connect` deny on the channel, not a new Discord role:
it targets exactly the people who left and evaporates at `/meeting end` when the
overwrite is deleted, so there is no role to un-assign afterwards.
`view_channel` is left alone on purpose, so a locked-out member can still see the
channel and read why they cannot join instead of finding it silently missing.
`/meeting unlock <@member>` clears one person's deny and flags them as
**readmitted** in the report.

### 📊 The report

Both commands are answered **ephemeral**, including their error paths. A report
that names who attended and who didn't is not something to post where the whole
server can scroll back through it later.

Everyone who runs them gets the headline — when, where, and how many of the
invited audience attended. Only the meeting tier gets the rest:

- **the stats page** — three lists that partition the invited audience, so their
  counts add up to it: 🟢 **stayed to the end** (or *in the channel now*, while
  the meeting is running), 🚪 **left early**, and 🚫 **absent**. Each shows ten
  names and points at the CSV for the rest. Also total presence, visit counts (a
  leave and return is two visits, not one averaged row), and 🔑 for anyone
  readmitted after a lockout;
- **📄 Attendance CSV** — one row per join with exact timestamps, plus a
  per-member totals block, as an attachment;
- **🚫 Absentees** — a **separate page** listing who was invited and never
  joined, one 🟨 button each. Pressing one writes a yellow card through the
  same path as `/warn`, so it lands on the member's profile as a real record
  and is mirrored to the modlog with the meeting as the reason.

The `/meeting list` **dropdown is withheld from non-admins** — it is the door to
the per-member report, so attaching it would make the redaction theatre. The
meeting *history* is still shown, since "has the club been meeting" is not a
secret, and a bot that refuses an ordinary question just trains people to ask
someone who will answer.

The absentee list is its own page on purpose: a club-wide meeting can have
forty absentees, which is eight rows of buttons, and Discord caps a view at 25
components. Inline on the stats embed it would push the attendance table off
the embed limit and give the whole server a shared penalty menu.

An unclosed attendance row is measured to the meeting's end rather than to
"now", so a report read a week later does not credit a week of presence.

### 🔁 Private rooms (older feature)

`/meeting create <@members...> [name]` still spins up a throwaway voice channel
only the creator and the tagged members can see, and it deletes itself the
moment it empties. Its `end` subcommand is `/meeting endroom` (alias
`closeroom`) — `end` was claimed by meeting tracking, and the two features
share the group because a private room is a *place* while a tracked meeting is
an *event with a report*.

### 🗄 Storage

Two Appwrite tables, `meetings` and `meeting_sessions`. `meeting_sessions` is
**append-only, one row per join**, so a leave-and-return is reconstructable with
no in-memory state and the bot can restart mid-meeting without losing the
timeline. CSVs are generated on demand rather than stored. The channel's
permission snapshot is kept in a `bot_settings` sidecar (`meeting_side.<id>`),
which is the only place `/meeting end` reads to restore what it changed.

That same sidecar records whether the bot created the channel, under the
reserved key `__bot_created__`. It has to be an explicit flag rather than an
inferred "empty snapshot means delete": a missing sidecar also looks empty, and
reading that as permission to delete would have `/meeting end` remove a channel
it merely failed to record. `restore_plan()` skips the reserved key so replaying
the snapshot can never mistake it for a permission overwrite. The delete /
restore / skip decision lives in `end_plan()` so all three outcomes are testable
without a live guild.

---

## 🌐 Languages (`/settings`, `/language`)

The bot speaks **English, French and Arabic**. Which language you see depends
on *who triggered the command*:

- `/settings` opens an interactive menu (Dank Memer style): the message's
  content is the current state, and **buttons drill down until the choice** —
  `⚙️ Settings → 🌐 Language → 🇬🇧 English / 🇫🇷 Français / 🇸🇦 العربية`.
- `/language <english|french|arabic>` is the quick, non-interactive version.
  Both work in a server **or in a DM** (private message to the bot).
- Resolution per member: their stored `lang` → their **Discord locale** (if it
  matches a supported language) → English.
- **Server-broadcast messages** (welcome channel, dashboard tracker,
  announcements) stay in **English** — the server's default language — and are
  unaffected by member choices.

Strings live in `i18n/{en,fr,ar}.json` (one table per language, flat keys with
`{placeholder}` formatting). `i18n/core.py` holds the `t(key, lang)` lookup and
the resolver; new cogs import `resolve_member_lang(ctx)` + `t()`.

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
  engagement.py           XP & levels, daily challenges, level-reward roles
  reminders.py            Appwrite-persisted /remind me DM scheduler
  fun.py                  lighthearted commands (+ robot status, quiz, account-age flex)
  moderation.py           kick/ban/timeout/warn + modlog + lock/slowmode/unlock
  welcome.py / goodbye.py join / leave messages
  rules.py                rules board
  general.py              hello / ping / custom help
  music.py                full music player — playback engine, skip/remove voting, panel
  music_sources/          providers: YouTube, Deezer (+ ARL gateway), Audius, world radio
  minecraft.py            MC server control/power + linked-player session pings
  mclink.py               MC↔Discord account linking (CipherBox handshake)
  meetings.py             tracked meetings (attendance, lockouts, reports) + private VC rooms
  _meetings.py            meeting helper lib — audience resolution, permission snapshot, attendance rollup
  _perms.py               permission tiers incl. the meeting-admin tier
  apis.py                 weather / dictionary / meme / crypto / spacex / github / advice / lyrics
  settings.py             guild settings + language switch (AR/EN/FR)
  bot_admin.py            /bot status · update · restart (ff-only pull + relaunch)
  jockie_nudge.py         friendly /play pitch when members type JockieMusic's `m!`
  i18n/                   per-language string tables (core.py resolver)
  _perms.py / _scopes.py  staff bypass + club permission-scope resolver
  _ui.py                  shared embed / button / select builders
  _mc_crypto.py           CipherBox crypto for MC credentials
  members.py              link/unlink, profiles, hierarchy, notifications, dashboard
  cells.py                /cell add — place members into a cell
  tasks.py                task CRUD + lists (priority/due/cell)
  competitions.py         competitions + registration
  events.py               events + RSVP attendance
  polls.py                polls + votes (transparent / anonymous, Appwrite-backed)
  _dates.py               shared date parsing/formatting helpers
scripts/
  bootstrap_appwrite.py   idempotent schema provisioning
  smoke_test.py           hermetic cog-load check (used by CI)
  test_music_player.py    skip/remove voting + motion logic (hermetic)
  test_music_sources.py   YouTube/Deezer/Audius/radio source resolution
  test_deezer_radio.py    Deezer radio feed & refill behavior
  test_jockie_nudge.py    nudge cooldowns & pitch wording
  test_mclink.py          CipherBox interop vectors + credential mint
  test_level_roles.py     level-up reward role grants (hermetic)
  test_reminders.py       reminder span parsing + scheduler delivery
  test_xp_bonuses.py      content-driven XP bonus tiers
  test_account_age.py     account-age summary + oldest-accounts ranking
  test_memberlinks.py / test_names.py / test_voice_xp.py / test_updates.py / test_mc_hub.py
                          unit checks per feature
KeepAlive.py              Flask keep-alive on $PORT
render.yaml               Render worker service definition
```

**Adding a feature = dropping a new file in `cogs/`.** The launcher discovers
it automatically.

---

## ✅ Quality gates

`.github/workflows/ci.yml` runs on every push/PR against Python 3.12 and 3.13:
byte-compiles every module, runs `scripts/smoke_test.py` (loads all 26 cogs,
asserts every command is hybrid and the custom `help` is installed), the
hermetic `scripts/test_mclink.py` (CipherBox interop vectors + credential
mint) and the music suite — `test_music_player.py` (skip/remove voting),
`test_music_sources.py` (source resolution), `test_deezer_radio.py` (radio
feed/refill) and `test_jockie_nudge.py` — plus the per-feature unit checks
(`test_level_roles.py`, `test_reminders.py`, `test_xp_bonuses.py`,
`test_account_age.py`, `test_memberlinks.py`, `test_names.py`,
`test_voice_xp.py`, `test_updates.py`, `test_mc_hub.py`) and the meeting
suites — `test_meeting_perms.py` (role → audience tier), `test_meeting_lib.py`
(audience resolution, permission snapshot, rollup, CSV, absentee list) and
`test_meeting_cog.py` (voice-state attendance, lockout rules, report view
layout). `test_meeting_store.py` round-trips the real Appwrite tables and
needs live credentials.

```bash
python scripts/smoke_test.py   # run the same check locally
python scripts/test_mclink.py  # mc-link crypto/credential unit checks
python scripts/test_music_player.py   # skip/remove voting logic
```

---

## 🌟 Contributors 🌟

A huge thanks to the amazing folks who helped bring **Bot_CR** to life! 👏✨

- **[Ali](https://github.com/SwirX)** - The one who revived the bot with a modern twist! 🧑💻🚀
- **[Hamza](https://github.com/Yasahiru)** - The genius behind it all! 🤓💡
- **[Kawtar](https://github.com/ELGADDIxKawtar)** - For her outstanding contributions! 🧑‍💻🌟
- **[Wieam](https://github.com/wieam-ar)** - For her dedication and hard work! 🧑‍💻💪
- **[Yaser](https://github.com/0yaser0)** - For his innovative ideas and passion! 🧑‍💻🔥

---

## 📝 License 📝

This project is open-source and licensed under the MIT License.
Feel free to fork it, contribute, and make it your own! 🔄📜