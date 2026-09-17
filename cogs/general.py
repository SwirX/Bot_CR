import discord
from discord.ext import commands


class General(commands.Cog):
    """Basic utility commands that ship with the bot."""

    def __init__(self, bot):
        self.bot = bot

    # Command: hello
    @commands.hybrid_command(name="hello", description="Say hello!")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def hello(self, ctx):
        await ctx.send(f"Hello, {ctx.author.mention}!")

    # Command: ping
    @commands.hybrid_command(name="ping", description="Check the bot's heartbeat.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ping(self, ctx):
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! 🏓 Latency: {latency} ms")

    # Command: help (custom, replaces the default)
    @commands.hybrid_command(name="help", description="List every command, grouped by cog.")
    async def help_command(self, ctx):
        embed = discord.Embed(
            title="📚 Bot_CR Commands",
            description=f"Prefix `{self.bot.command_prefix}` or use the slash `/` versions.",
            color=discord.Color.blue(),
        )
        for cog in self.bot.cogs.values():
            commands_in_cog = [
                command for command in cog.walk_commands()
                if not command.hidden and command.parent is None
            ]
            if not commands_in_cog:
                continue
            lines = []
            for command in commands_in_cog:
                usage = command.name
                for param in command.clean_params.values():
                    usage += f" <{param.name}>" if param.required else f" [{param.name}]"
                doc = command.description or command.short_doc or ""
                lines.append(f"`{usage}` — {doc}")
            embed.add_field(name=cog.qualified_name, value="\n".join(lines), inline=False)
        embed.set_footer(text=f"{len(self.bot.commands)} commands available")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(General(bot))