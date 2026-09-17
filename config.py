"""Bot_CR configuration.

Every knob lives in the environment (loaded from a `.env` file next to this
module). Channel/role names that were previously hard-coded all over the cogs
now come from here, so the bot can be reconfigured without touching code.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default=None, required: bool = False, cast=str):
    """Read an env var, cast it, and enforce required-ness."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# ── Discord ────────────────────────────────────────────────────────────
BOT_TOKEN = _env("BOT_TOKEN", required=True)
PREFIX = _env("PREFIX", "!")

# Enables instant slash-command sync to one server (great for testing).
# Leave empty to sync commands globally (can take up to an hour to propagate).
GUILD_ID = _env("GUILD_ID", default=None, cast=int)

# ── Appwrite data store ────────────────────────────────────────────────
APPWRITE_ENDPOINT = _env("APPWRITE_ENDPOINT", "https://appwrite.alibks.dev/v1")
APPWRITE_PROJECT_ID = _env("APPWRITE_PROJECT_ID", "robotics-ops", required=True)
APPWRITE_API_KEY = _env("APPWRITE_API_KEY", required=True)
APPWRITE_DATABASE_ID = _env("APPWRITE_DATABASE_ID", "robotics_ops")

# ── Channel names (matches #channel names on the server) ───────────────
CHANNEL_RULES = _env("CHANNEL_RULES", "•📚•-rules-of-the-server")
CHANNEL_WELCOME = _env("CHANNEL_WELCOME", "•👋•-joins")
CHANNEL_GOODBYE = _env("CHANNEL_GOODBYE", "•👋•-leaves")
CHANNEL_VERIFICATION = _env("CHANNEL_VERIFICATION", "•📑•-verification")
CHANNEL_ANNOUNCEMENTS = _env("CHANNEL_ANNOUNCEMENTS", "⦿announcements⦿")
CHANNEL_DASHBOARD = _env("CHANNEL_DASHBOARD", "⦿dashboard⦿")
CHANNEL_BOTLOG = _env("CHANNEL_BOTLOG", "🤖bot-development")
CHANNEL_MAIN = _env("CHANNEL_MAIN", "「💬」main-chat")

# ── Role names ─────────────────────────────────────────────────────────
ROLE_TEMP = _env("ROLE_TEMP", "⛔ | None")
ROLE_VERIFIED = _env("ROLE_VERIFIED", "「📗」Verified")
ROLE_MEMBER = _env("ROLE_MEMBER", "🌿 | LVL 01+")

# ── Behaviour ──────────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = _env("DASHBOARD_REFRESH_SECONDS", 60, cast=int)
XP_COOLDOWN_SECONDS = _env("XP_COOLDOWN_SECONDS", 60, cast=int)
XP_MIN = _env("XP_MIN", 4, cast=int)
XP_MAX = _env("XP_MAX", 12, cast=int)

# ── Club website ───────────────────────────────────────────────────────
WEBSITE_URL = _env("WEBSITE_URL", "https://robotics.ma")