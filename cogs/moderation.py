import discord
from discord.ext import commands


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="del", description="Delete the last N messages (1–100).")
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def delete_messages(self, ctx, number: int):
        """Delete the last N messages in this channel plus the command itself."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command can only be used in text channels.")
            return

        # The bulk-delete API only accepts 2..100 messages per call.
        number = max(1, min(number, 100))
        try:
            deleted = await ctx.channel.purge(limit=number, before=ctx.message)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass  # Command message already gone or cannot be deleted
            await ctx.send(f"Deleted {len(deleted)} message(s) in #{ctx.channel.name}.", delete_after=3)
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages here.", delete_after=5)
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to delete messages: {exc}", delete_after=5)


async def setup(bot):
    await bot.add_cog(Moderation(bot))