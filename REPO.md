# Bot_CR repository notes (verified 2026-10-30)

Bot_CR is a discord.py bot with feature cogs under `cogs/` and Appwrite
persistence through `data/store.py`. `BOT.py` discovers and loads the cogs;
`config.py` reads environment settings.

- Runtime dependencies: pinned in `requirements.txt`; deployment is in `render.yaml`.
- Offline meeting checks: `.venv-local/bin/python scripts/test_meeting_{perms,lib,cog}.py`
  and `.venv-local/bin/python -m unittest scripts.test_meeting_lifecycle` (verified).
- Lint for changed production files: `.venv-local/bin/ruff check` with explicit
  file paths (verified). Repository-wide lint currently has 168 existing findings.
- `scripts/smoke_test.py` currently fails during unrelated `cogs.minecraft` load
  with `CommandAlreadyRegistered: Command 'mc' already registered`.
- `scripts/test_meeting_store.py` reaches the live Appwrite hub; do not run it
  as an offline test. Meeting rows and join sessions are in Appwrite tables;
  channel permission snapshots and pending schedules live in `bot_settings`.

Risk: Discord channel overwrites cap a private member audience. The default
everyone audience uses `@everyone` permissions to avoid that cap; custom role
audiences are limited to 90 members. Recheck gateway behavior in a test guild
before deployment because offline fakes cannot prove Discord permission writes.
