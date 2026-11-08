import logging

import discord
from discord.ext import commands

import config

LOG = logging.getLogger("bot.rules")

RULES_TEXT = (
    "** ١. إحترام جميع الموجودين في السيرفر **.......................................\n"
    "** ٢. عدم العنصريه القبلية او الدينيه **..............................................\n"
    "** ٣. عدم التدخل في المواضيع السياسيه او الدينيه **............................\n"
    "** ٤. ممنوع طلب الرتب مهما كانت قرابتك من الإدارة **.........................\n"
    "**٥. اذا واجهتك مشكله تواصل مع اي اداري**.....................................\n"
    "** ٦. يمنع الشتم حتى لو عن طريق المزح او حتى مع صديقك** ................\n"
    "**٧. يمنع تقمص دور الاداري في السيرفر**.........................................\n"
    "**٨. في حال وجود إنتهاك للقوانين من احد المشرفين يرجى ابلاغ الإدارة**..\n"
    "**٩. قراءة القوانين الدسكورد كل فترة لأنها قد تتغير في أي وقت**.............\n"
    "**١٠. اذا كنت تريد اي مساعد ابلغ الإدارة**............................................\n\n"
)


class Rules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.posted = False

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        # Reconnect guard: only post the rules once per process.
        if not self.posted:
            self.posted = True
            await self.send_rules_image()

    async def send_rules_image(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return
        channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_RULES)
        if not channel:
            LOG.warning("Rules channel %r not found", config.CHANNEL_RULES)
            return
        embed = self._build_embed()

        # Newest-first list of everything the bot already posted there.
        bot_posts = []
        async for message in channel.history(limit=100):
            if message.author == self.bot.user:
                bot_posts.append(message)

        keep = next((m for m in self._matching(bot_posts, embed)), None)
        if keep is not None:
            # The channel already carries the current rules text; editing or
            # reposting would only churn it (and duplicate across restarts).
            # Leftover duplicates from older boots are swept so the channel
            # ends up with exactly one up-to-date rules post.
            LOG.info("Rules in #%s already up to date", channel.name)
            await self._sweep(channel, [m for m in bot_posts if m is not keep])
            return

        if bot_posts:
            # Rules text changed since the last boot: update the newest bot
            # post in place so it keeps its position, and clear any stray
            # duplicates from older versions of this cog.
            keep = bot_posts[0]
            try:
                await keep.edit(embed=embed)
                LOG.info("Updated existing rules message in #%s", channel.name)
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOG.warning("Could not edit the rules message: %s", exc)
                keep = None
            await self._sweep(channel, bot_posts[1:] if keep else bot_posts)
            if keep is not None:
                return

        await channel.send(embed=embed)

    @staticmethod
    def _build_embed() -> discord.Embed:
        return discord.Embed(
            title=(
                "**                    :black_heart: Robotics Club :black_heart: "
                "حياكم الله في السيرفر                  **\n"
                " **يفضل قراءة القوانين كاملة                                         **\n\n "
            ),
            description=RULES_TEXT,
            color=discord.Color.blue(),
        ).add_field(
            name="Other suggestions:",
            value="Introduce yourself \nAsk questions \nRa7na gha f discord",
        ).set_footer(text="Have Fun asahbi✨🖤")

    @staticmethod
    def _matching(bot_posts: list, embed: discord.Embed):
        """Every bot post still carrying the *current* rules (newest first)."""
        return (m for m in bot_posts if Rules._is_same_rules(m, embed))

    @staticmethod
    def _is_same_rules(message: discord.Message, embed: discord.Embed) -> bool:
        """True when the posted embed is still identical to the current one.

        Serialised comparison, so any edit to `_build_embed` (footer, rules
        text, fields) makes the old post count as outdated automatically.
        """
        return bool(message.embeds) and message.embeds[0].to_dict() == embed.to_dict()

    async def _sweep(self, channel: discord.TextChannel, stale: list):
        """Best-effort delete leftover bot rules posts (needs Manage Messages)."""
        for message in stale:
            try:
                await message.delete()
            except discord.HTTPException as exc:
                LOG.warning("Could not delete a stale rules post: %s", exc)


async def setup(bot):
    await bot.add_cog(Rules(bot))