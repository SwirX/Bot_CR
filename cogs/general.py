import discord
from discord.ext import commands

class General(commands.Cog):
    """Basic utility commands that ship with the bot."""

    def __init__(self, bot):
        self.bot = bot

    # Command: !hello
    @commands.command()
    async def hello(self, ctx):
        await ctx.send(f"Hello, {ctx.author.mention}!")

    # Command: !ping
    @commands.command()
    async def ping(self, ctx):
        await ctx.send("Pong!")

async def setup(bot):
    await bot.add_cog(General(bot))