import discord
from discord.ext import commands


class HelpView(discord.ui.View):
    """Interactive help: category dropdown + overview + close button."""

    def __init__(self, bot, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.categories = {}
        for cog in bot.cogs.values():
            cmds = [c for c in cog.walk_commands() if not c.hidden and c.parent is None]
            if cmds:
                self.categories[cog.qualified_name] = (cog, cmds)

        options = [
            discord.SelectOption(
                label="All commands",
                value="__all__",
                description="Browse every command with usage and help text",
            )
        ]
        for name in sorted(self.categories):
            cog, cmds = self.categories[name]
            first_line = (cog.__doc__ or "").strip().splitlines()
            blurb = first_line[0][:96] if first_line else f"{len(cmds)} commands"
            options.append(
                discord.SelectOption(label=name, value=name, description=blurb)
            )

        self.select = discord.ui.Select(
            placeholder="Choose a category…",
            options=options[:25],  # Discord caps a select at 25 options
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    def build_overview_embed(self) -> discord.Embed:
        prefix = self.bot.command_prefix
        embed = discord.Embed(
            title="📚 Bot_CR Commands",
            description=(
                f"Prefix `{prefix}command` or the slash `/command` — they do the "
                "same thing. Use the dropdown to see a category's full usage."
            ),
            color=discord.Color.blue(),
        )
        for name in sorted(self.categories):
            _cog, cmds = self.categories[name]
            embed.add_field(
                name=name,
                value=", ".join(f"`{c.name}`" for c in cmds) or "—",
                inline=False,
            )
        embed.set_footer(text=f"{len(self.bot.commands)} commands available")
        return embed

    def build_category_embed(self, name: str):
        cog, cmds = self.categories[name]
        embed = discord.Embed(
            title=f"📚 {name} commands",
            description=(cog.__doc__ or "").strip(),
            color=discord.Color.blurple(),
        )
        lines = []
        for command in sorted(cmds, key=lambda c: c.name):
            usage = command.name
            for param in command.clean_params.values():
                usage += f" <{param.name}>" if param.required else f" [{param.name}]"
            doc = command.description or command.short_doc or ""
            lines.append(f"`{usage}` — {doc}")
        for chunk in (lines[i:i + 12] for i in range(0, len(lines), 12)):
            embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
        return embed

    async def on_select(self, interaction: discord.Interaction):
        value = self.select.values[0] if self.select.values else "__all__"
        if value == "__all__":
            embed = self.build_overview_embed()
        else:
            embed = self.build_category_embed(value)
        self.select.placeholder = "Jump to another category…"
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="🏠", style=discord.ButtonStyle.secondary, label="All")
    async def home(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            await interaction.response.edit_message(
                embed=self.build_overview_embed(), view=self
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, label="Close")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(
                content="Command list closed. 👋 (use `/help` to reopen)",
                embed=None,
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


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
    @commands.hybrid_command(name="help", description="Browse commands with an interactive menu.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def help_command(self, ctx):
        view = HelpView(self.bot)
        embed = view.build_overview_embed()
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(General(bot))