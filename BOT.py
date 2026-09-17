import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

import config
from data import store
from KeepAlive import keep_alive

LOG = logging.getLogger("bot")

keep_alive()

# Create a bot instance
intents = discord.Intents.default()
intents.message_content = True  # Privileged intent
intents.presences = True  # Track online status
intents.members = True  # Required for the on_member_join event
intents.voice_states = True  # Track voice state changes

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents)


def configure_logging() -> None:
    """Set up human-readable logging for the bot and its dependencies."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The Appwrite SDK logs through urllib3 — keep it quiet unless it matters.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# Event: When the bot is ready
@bot.event
async def on_ready():
    LOG.info("Logged in as %s (latency %.1f ms)", bot.user, bot.latency * 1000)


async def load_cogs():
    """Auto-discover and load every cog in the cogs/ package.

    Drop a new file in cogs/ and it is picked up automatically; no wiring
    needed in this launcher.
    """
    cogs_dir = Path("cogs")
    for path in sorted(cogs_dir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        extension = f"cogs.{path.stem}"
        await bot.load_extension(extension)
        LOG.info("Loaded cog: %s", extension)


async def main():
    configure_logging()
    try:
        await store.init()
    except Exception as exc:  # keep the bot alive even if Appwrite is down
        LOG.critical("Appwrite store unavailable: %s — continuing without persistence", exc)
    await load_cogs()
    await bot.start(config.BOT_TOKEN)

# Run the bot
if __name__ == "__main__":
    asyncio.run(main())