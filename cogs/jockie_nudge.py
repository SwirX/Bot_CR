"""Migration nudge for JockieMusic (`m!`) users.

Members who still type JockieMusic's prefix get a friendly pitch for this
bot's `/play` and `/queue auto`, once per user and rarely per channel, so
the hint converts users without ever feeling like spam.
"""

import logging
import time

import discord
from discord.ext import commands

LOG = logging.getLogger("music.jockie_nudge")

_JOCKIE_PREFIXES = ("m!", "M!")
_USER_COOLDOWN_SECONDS = 600
_CHANNEL_COOLDOWN_SECONDS = 120
_PITCH = (
    "That `m!` prefix belongs to JockieMusic, but you don't need it here. "
    "I'm a full music player: `/play` a song, `/queue auto` builds a radio "
    "queue, and there's skip, loop, volume, lyrics and more. "
    "Same music, fewer bots."
)


class JockieNudge(commands.Cog):
    """Reply to JockieMusic commands with a friendly pitch for /play."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._user_last_nudge: dict[int, float] = {}
        self._channel_last_nudge: dict[int, float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        content = message.content.strip()
        if not content.startswith(_JOCKIE_PREFIXES):
            return
        if not content[2:].lstrip()[:1].isalpha():
            return
        now = time.monotonic()
        # Use `None` as "never nudged", NOT 0.0. time.monotonic() is measured
        # from an arbitrary origin (Linux: boot time), so on a host that has
        # been up less than the cooldown window `now - 0.0 < 600` is TRUE and
        # the very first nudge is silently suppressed — for every user, on
        # every fresh boot and every CI runner. A missing key must mean "no
        # cooldown", not "nudged at epoch zero".
        last_user = self._user_last_nudge.get(message.author.id)
        if last_user is not None and now - last_user < _USER_COOLDOWN_SECONDS:
            return
        last_channel = self._channel_last_nudge.get(message.channel.id)
        if (last_channel is not None
                and now - last_channel < _CHANNEL_COOLDOWN_SECONDS):
            return
        self._user_last_nudge[message.author.id] = now
        self._channel_last_nudge[message.channel.id] = now
        if len(self._user_last_nudge) > 500:
            self._prune(now)
        try:
            await message.reply(_PITCH)
        except discord.HTTPException as exc:
            LOG.warning("Jockie nudge failed for %s: %s", message.author.id, exc)

    def _prune(self, now: float) -> None:
        """Drop stale stamps so the cooldown dicts stay bounded."""
        cutoff = now - 2 * _USER_COOLDOWN_SECONDS
        self._user_last_nudge = {
            user_id: stamp for user_id, stamp in self._user_last_nudge.items()
            if stamp >= cutoff}
        self._channel_last_nudge = {
            channel_id: stamp for channel_id, stamp in self._channel_last_nudge.items()
            if stamp >= cutoff}


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JockieNudge(bot))