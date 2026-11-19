"""Prefix commands for bulk server voice mute."""

from discord.ext import commands

from cogs import _voice_mute


class VoiceMute(commands.Cog):
    @commands.group(name="servermute", invoke_without_command=True)
    @commands.guild_only()
    async def servermute(self, ctx):
        await ctx.send("Use `!servermute all` or `!servermute undo`.")

    @servermute.command(name="all")
    @commands.guild_only()
    async def all(self, ctx, channel: commands.VoiceChannelConverter = None,
                  exclude: commands.MemberConverter = None):
        await _voice_mute.mute_all(ctx, channel, exclude)

    @servermute.command(name="undo")
    @commands.guild_only()
    async def undo(self, ctx, channel: commands.VoiceChannelConverter = None):
        await _voice_mute.undo_mute(ctx, channel)


async def setup(bot):
    await bot.add_cog(VoiceMute())
