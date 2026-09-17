# 🤖 **Bot_CR** 🤖

**Bot_CR** is a dynamic and versatile Discord bot for the Robotics Club server! 🚀
Packed with features to keep the community engaged, organized and entertained.

---

## ✨ **What does Bot_CR do?**

- ✅ **Verification system** – new members get a temporary role and must confirm the rules (✅ / ❌ reactions).
- 👋 **Welcome & Goodbye messages** – personalized greetings and farewells.
- 🎂 **Birthday tracker** – members set their birthday via DM button + modal; anniversaries are announced daily.
- 📊 **Live server dashboard** – member/message/voice stats, refreshed every 30 seconds.
- 🔊 **Voice / message / member stats** – see total voice time, message count and online members.
- 📜 **Rules board** – keeps the server rules pinned and up to date automatically.
- 🗑️ **Message purge** – admins can bulk-delete messages.

> 🆙 *Leveling / XP and daily challenges are planned but not part of the active bot yet.*

---

## ⚡ **Commands** (all use the `!` prefix)

| Command | Who | What it does |
|---|---|---|
| `!hello` | everyone | Says hi 👋 |
| `!ping` | everyone | Bot latency check |
| `!total_voice_time` | everyone | Total voice time since the bot started 🔊 |
| `!current_voice_time` | everyone | Time spent in your current voice channel |
| `!total_messages` | everyone | Messages sent since the bot started 💬 |
| `!online_members` | everyone | How many members are online 🟢 |
| `!del <number>` | **admin** | Deletes the last N messages in the channel 🗑️ |
| `!help` | everyone | Shows all commands |

---

## 🧱 **Project structure**

```
BOT.py                    thin launcher — starts the bot and loads cogs
cogs/                     every feature lives here as its own module
  general.py              basic commands (hello, ping)
  verification.py         join verification flow
  welcome.py / goodbye.py member join / leave messages
  birthday_tracker.py     birthday modal + daily announcements
  dashboard.py            live stats dashboard
  voice_time_state.py     voice channel time tracking
  total_messages_state.py message counter
  members_state.py        online member count
  moderation.py           admin commands (message purge)
  rules.py                rules board
config.py                 loads settings from .env
KeepAlive.py              Flask keep-alive server (free-host ping)
```

**Adding a feature = dropping a new file in `cogs/`.** The launcher auto-discovers it;
no wiring needed.

---

## 🛠️ **How to set it up?**

1. Use **Python 3.12** — the pinned `discord.py 2.4.0` does not work on Python 3.13+.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create your environment file:
   ```bash
   cp .env.example .env
   ```
   Then open `.env` and paste your bot token (from the [Discord Developer Portal](https://discord.com/developers/applications) → your application → Bot → Token).
4. Start the bot:
   ```bash
   python BOT.py
   ```

> ⚠️ The bot expects specific channel/role names (rules, welcome, verification, dashboard,
> announcements channels and the verification roles). Renaming them breaks the related
> feature — names are currently hardcoded inside the cogs.

---

## 🌟 **Contributors** 🌟

A huge thanks to the amazing folks who helped bring **Bot_CR** to life! 👏✨

- **[Hamza](https://github.com/Yasahiru)** - The genius behind it all! 🤓💡
- **[Kawtar](https://github.com/ELGADDIxKawtar)** - For her outstanding contributions! 🧑‍💻🌟
- **[Wieam](https://github.com/wieam-ar)** - For her dedication and hard work! 🧑‍💻💪
- **[Yaser](https://github.com/0yaser0)** - For his innovative ideas and passion! 🧑‍💻🔥

---

## 📝 **License** 📝

This project is open-source and licensed under the MIT License.
Feel free to fork it, contribute, and make it your own! 🔄📜