import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dateutil import parser

import config

LOG = logging.getLogger("bot.birthdays")

# Server timezone (e.g., Morocco).
tz = ZoneInfo("Africa/Casablanca")

BIRTHDAYS_FILE = Path(config.BASE_DIR) / "birthdays.json"


def load_birthdays():
    try:
        with open(BIRTHDAYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class BirthdayTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.birthdays = load_birthdays()
        self.check_birthdays.start()  # Start daily check

    def save_birthdays(self):
        with open(BIRTHDAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.birthdays, f, indent=4)

    # 🎂 Birthday Modal
    class BirthdayModal(discord.ui.Modal, title="Enter Your Birthday"):
        def __init__(self, cog):
            super().__init__()
            self.cog = cog

        date = discord.ui.TextInput(label="Enter your birthdate", placeholder="e.g., 2004-12-25")

        async def on_submit(self, interaction: discord.Interaction):
            user_id = str(interaction.user.id)
            birthdate_input = self.date.value.strip()

            try:
                # Auto-detect and normalize date format
                parsed_date = parser.parse(birthdate_input, dayfirst=False)
                formatted_birthday = parsed_date.strftime("%Y-%m-%d")

                self.cog.birthdays[user_id] = {
                    "username": interaction.user.name,
                    "birthday": formatted_birthday,
                }
                self.cog.save_birthdays()

                await interaction.response.send_message(
                    f"🎉 Your birthday has been saved: {formatted_birthday}", ephemeral=True
                )
                # Check immediately if today is his birthday
                await self.cog.check_and_announce_birthday(user_id)

            except (ValueError, OverflowError):
                await interaction.response.send_message(
                    "⚠️ Invalid date! Try again (e.g., 2004-12-25).", ephemeral=True
                )

    # 🎉 Button to Open Modal
    class BirthdayButton(discord.ui.View):
        def __init__(self, cog):
            super().__init__(timeout=None)
            self.cog = cog
            self.birthday_button = discord.ui.Button(
                label="Set Your Birthday 🎂",
                style=discord.ButtonStyle.primary,
                custom_id="birthday_button",
            )
            self.birthday_button.callback = self.birthday_button_callback
            self.add_item(self.birthday_button)

        async def birthday_button_callback(self, interaction: discord.Interaction):
            await interaction.response.send_modal(self.cog.BirthdayModal(self.cog))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Send a DM with a button to open the birthday modal when a user joins."""
        try:
            view = self.BirthdayButton(self)
            await member.send("🎉 Welcome! Click the button below to set your birthday:", view=view)
        except discord.Forbidden:
            LOG.warning("Cannot DM %s (DMs are closed)", member)
            channel = discord.utils.get(member.guild.text_channels, name=config.CHANNEL_BOTLOG)
            if channel:
                await channel.send(f"❌ Cannot send message to {member} (DMs are closed)")

    async def check_and_announce_birthday(self, user_id):
        """Check if the registered birthday is today and announce it immediately."""
        today = datetime.now(tz).strftime("%m-%d")

        if user_id in self.birthdays:
            user_birthday = self.birthdays[user_id]["birthday"]
            if datetime.strptime(user_birthday, "%Y-%m-%d").strftime("%m-%d") == today:
                LOG.info("Announcing immediate birthday for %s", self.birthdays[user_id]["username"])
                for guild in self.bot.guilds:
                    channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_ANNOUNCEMENTS)
                    if channel:
                        await channel.send(
                            f"🎉 Today is the Birthday of {self.birthdays[user_id]['username']}! 🎂🎈"
                        )
                        return

    @tasks.loop(hours=24)
    async def check_birthdays(self):
        await self.bot.wait_until_ready()
        today = datetime.now(tz).strftime("%m-%d")

        channel = None
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_ANNOUNCEMENTS)
            if channel:
                break

        if channel is None:
            LOG.warning("No announcement channel %r found", config.CHANNEL_ANNOUNCEMENTS)
            return

        for user_id, info in self.birthdays.items():
            user_birthday = info["birthday"]
            try:
                matches = datetime.strptime(user_birthday, "%Y-%m-%d").strftime("%m-%d") == today
            except ValueError:
                LOG.warning("Invalid stored birthday for %s: %r", user_id, user_birthday)
                continue
            if matches:
                LOG.info("Sending birthday message for %s", info["username"])
                await channel.send(f"🎉 Happy Birthday to {info['username']}! 🎂🎈")

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_birthdays.is_running():
            self.check_birthdays.start()


async def setup(bot):
    await bot.add_cog(BirthdayTracker(bot))