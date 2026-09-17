import asyncio
from pathlib import Path

import discord
from discord.ext import commands

from config import BOT_TOKEN  # Import the token from .env
from KeepAlive import keep_alive

keep_alive()

# Create a bot instance
intents = discord.Intents.default()
intents.message_content = True  # Privileged intent
intents.presences = True  # Track online status
intents.members = True  # Required for the on_member_join event
intents.voice_states = True  # Track voice state changes

bot = commands.Bot(command_prefix='!', intents=intents)

# Event: When the bot is ready
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')


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
        print(f"Loaded cog: {extension}")


async def main():
    await load_cogs()
    await bot.start(BOT_TOKEN)

# Run the bot
if __name__ == "__main__":
    asyncio.run(main())