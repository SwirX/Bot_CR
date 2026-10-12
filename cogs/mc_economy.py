"""Minecraft gameplay economy — RoboticsCMC profiles, leaderboards and shop.

This cog is **read-mostly on purpose**. Every command either displays mirrored
state or writes a *request*. It never moves money: ``/mcbuy`` creates a
``PENDING`` row in ``mc_orders`` and stops. The game server settles it against
its own SQLite only after re-checking the live price and the live balance, then
writes the outcome back. That inversion is what makes a bot-driven shop safe:
Appwrite has no atomic increment, and the bot's view of a balance is always
seconds old.

Profiles are mirrored every 30 minutes, so numbers here can lag gameplay by half
an hour. That is irrelevant for display and irrelevant for correctness.

**Slash-command budget.** Discord allows 100 global slash commands. This bot
already syncs 99 of them, and ``/mc`` and ``/minecraft`` are taken by
``cogs.minecraft``. Everything here therefore lives under one ``/mceco`` group:
a group costs a single top-level slot no matter how many children it holds, so
all four commands fit in the one that remains. Adding a fifth top-level command
anywhere in this bot will stop it booting with
``CommandLimitReached``. If you need more, retire an existing command first.
"""

import logging
import uuid as uuidlib

import discord
from discord import app_commands
from discord.ext import commands

from data.store import store

LOG = logging.getLogger("bot.mc_economy")

JOBS = [
    ("lumberjack", "Lumberjack"),
    ("miner", "Miner"),
    ("builder", "Builder"),
    ("hunter", "Hunter"),
    ("farmer", "Farmer"),
    ("blacksmith", "Blacksmith"),
    ("crafter", "Crafter"),
    ("businessman", "Businessman"),
]

BOARDS = {
    "playtime": ("playtime_seconds", "Playtime"),
    "active": ("active_seconds", "Active time"),
    "balance": ("money", "Balance"),
    "xp": ("gameplay_xp", "Gameplay XP"),
    "power": ("power", "Power"),
}

PAGE_SIZE = 10
MAX_QUANTITY = 64


def _seconds(value) -> str:
    value = max(0, int(value or 0))
    if value >= 86400:
        return f"{value // 86400}d {(value % 86400) // 3600}h"
    if value >= 3600:
        return f"{value // 3600}h {(value % 3600) // 60}m"
    if value >= 60:
        return f"{value // 60}m {value % 60}s"
    return f"{value}s"


def _progress(xp: int, level: int, width: int = 10) -> str:
    """Mirror of the server's 120 * level^1.45 cumulative curve."""
    if level >= 100:
        return "█" * width
    floor = 120 * level ** 1.45
    span = 120 * (level + 1) ** 1.45 - floor
    ratio = 0.0 if span <= 0 else max(0.0, min(1.0, (xp - floor) / span))
    filled = max(0, min(width, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


class _Profile:
    """A parsed ``mc_profiles`` row with the Discord XP pool folded in."""

    __slots__ = ("username", "minecraft_xp", "discord_xp", "level", "power", "money",
                 "playtime_seconds", "active_seconds", "claims", "squads", "jobs")

    def __init__(self, row: dict, discord_xp: int):
        self.username = row.get("username") or "?"
        self.minecraft_xp = int(row.get("gameplay_xp") or 0)
        self.discord_xp = int(discord_xp or 0)
        combined = self.minecraft_xp + self.discord_xp
        self.level = int((combined // 100) ** 0.5)
        self.power = int(row.get("power") or 0)
        self.money = int(row.get("money") or 0)
        self.playtime_seconds = int(row.get("playtime_seconds") or 0)
        self.active_seconds = int(row.get("active_seconds") or 0)
        self.claims = int(row.get("claims") or 0)
        self.squads = int(row.get("squads") or 0)
        self.jobs = {
            key: (int(row.get(f"lv_{key}") or 0), int(row.get(f"xp_{key}") or 0))
            for key, _ in JOBS
        }


class MCEconomy(commands.Cog, name="MCEconomy"):
    """Profile, leaderboard and market commands for RoboticsCMC.

Every command lives under the ``/mc`` group. The global slash-command tree sits
at 99 of its 100 slots, and a group costs exactly one top-level slot however
many children it holds, so everything fits in one. ``/minecraft`` still opens
the RoboticsCMC server menu (link, unlink, server control).
"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # A group costs exactly one of the 100 global slash-command slots however
    # many children it holds, which is why everything lives under it. It must be
    # a class attribute: CogMeta only collects app commands found in the class
    # body, and it skips anything that already has a parent (the children).
    mc = app_commands.Group(
        name="mc",
        description="RoboticsCMC — profiles, leaderboards, market, orders and server status.",
    )

    @mc.command(name="status",
                description="Server status, address and the BlueMap link.")
    async def mc_status(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Minecraft")
        if cog is None:
            await interaction.response.send_message(
                "The Minecraft cog is not loaded.", ephemeral=True)
            return
        await interaction.response.defer()
        status, error = await cog._fetch_status("en")
        embed = await cog._status_embed("en", status)
        if error:
            embed.add_field(name="⚠️", value=error, inline=False)
        await interaction.followup.send(embed=embed)

    async def _resolve_discord_id(self, user) -> str | None:
        """Resolve through the bot's own Minecraft link record."""
        record = (await store.get_member(user.id)) or {}
        username = record.get("mc_username") or ""
        if not username:
            return None
        account = await store.get_minecraft_account_by_username(username)
        if not account:
            return None
        link = await store.mc_active_link_for_user(user.id)
        if link and link.get("discord_user"):
            return str(link["discord_user"])
        return str(account.get("discord_user")) if account.get("discord_user") else None

    async def _profile_for(self, discord_id: str) -> _Profile | None:
        row = await store.get_mc_profile(discord_id)
        if not row:
            return None
        return _Profile(row, await store.get_discord_xp(discord_id))

    # ------------------------------------------------------------------
    @mc.command(name="profile", description="Show a Minecraft player profile.")
    @app_commands.describe(member="Whose profile to show (defaults to you)")
    async def mc_profile(self, interaction: discord.Interaction,
                         member: discord.Member | None = None):
        target = member or interaction.user
        discord_id = await self._resolve_discord_id(target)
        if not discord_id:
            await interaction.response.send_message(
                "No active Minecraft link for that account. Link it in-game first.",
                ephemeral=True)
            return
        profile = await self._profile_for(discord_id)
        if not profile:
            await interaction.response.send_message(
                "No synced profile yet — profiles appear within 30 minutes of first play.",
                ephemeral=True)
            return

        embed = discord.Embed(title=profile.username, colour=discord.Colour.teal())
        embed.add_field(name="Level", value=f"**{profile.level}**", inline=True)
        embed.add_field(name="Power", value=f"**{profile.power}**", inline=True)
        embed.add_field(name="Balance", value=f"{profile.money:,} R", inline=True)
        embed.add_field(name="Playtime", value=_seconds(profile.playtime_seconds), inline=True)
        embed.add_field(name="Active", value=_seconds(profile.active_seconds), inline=True)
        embed.add_field(name="XP", value=f"{profile.minecraft_xp:,} MC + "
                                         f"{profile.discord_xp:,} Discord", inline=True)
        embed.add_field(name="Claims / squads", value=f"{profile.claims} / {profile.squads}",
                        inline=True)
        embed.add_field(
            name="Jobs",
            value="\n".join(
                f"`{label:<12}` lv **{profile.jobs[key][0]:<3}** "
                f"{_progress(profile.jobs[key][1], profile.jobs[key][0])} {profile.jobs[key][1]:,}"
                for key, label in JOBS),
            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @mc.command(name="top", description="Minecraft leaderboards.")
    async def mc_top(self, interaction: discord.Interaction, board: str = "playtime"):
        """``board`` is populated from the autocomplete callback below."""
        await interaction.response.defer(ephemeral=True)
        if board in BOARDS:
            label, column = BOARDS[board][1], BOARDS[board][0]
            duration = board in ("playtime", "active")
        else:
            label = dict(JOBS).get(board, board)
            column = f"xp_{board}"
            duration = False

        rows = await store.top_mc_profiles(column, PAGE_SIZE)
        if not rows:
            await interaction.followup.send("No entries yet.", ephemeral=True)
            return

        lines = []
        for index, row in enumerate(rows, start=1):
            medal = {1: "1.", 2: "2.", 3: "3."}.get(index, f"{index}.")
            name = row.get("username") or "?"
            value = row.get(column) or 0
            lines.append(f"**{medal}** `{name}` — {_seconds(value) if duration else f'{int(value):,}'}")
        embed = discord.Embed(title=f"{label} leaderboard", colour=discord.Colour.gold())
        embed.description = "\n".join(lines)
        embed.set_footer(text="Mirror refreshes every 30 minutes.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @mc_top.autocomplete("board")
    async def _mc_top_board_autocomplete(self, interaction: discord.Interaction,
                                         current: str):
        options = [(label, key) for key, (_, label) in BOARDS.items()]
        options += [(label, key) for key, label in JOBS]
        choices = [app_commands.Choice(name=label, value=key)
                   for label, key in options if current.lower() in label.lower() or current.lower() in key]
        await interaction.response.send_autocomplete(choices[:25])

    # ------------------------------------------------------------------
    @mc.command(name="shop", description="Browse the RoboticsCMC market.")
    @app_commands.describe(category="Filter by category (or leave as all)")
    async def mc_shop(self, interaction: discord.Interaction,
                      category: str = "all"):
        await interaction.response.defer(ephemeral=True)
        items = [i for i in await store.list_mc_shop_items() if i.get("tradable")]
        if category.lower() != "all":
            items = [i for i in items
                     if str(i.get("category", "")).lower() == category.lower()]
        if not items:
            await interaction.followup.send("The market is empty right now.", ephemeral=True)
            return

        embed = discord.Embed(title="RoboticsCMC market", colour=discord.Colour.green())
        for item in items[:10]:
            key = item.get("item_key", "?")
            label = str(key).replace("_", " ").title()
            price = int(item.get("buy_price") or 0)
            embed.add_field(name=label,
                            value=f"`{price:,} R`\n`/mcbuy {key} 1`", inline=True)
        if len(items) > 10:
            embed.set_footer(text=f"Showing 10 of {len(items)} items.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @mc.command(
        name="buy",
        description="Order an item. Delivered the next time you log in.")
    @app_commands.describe(item="Item key, e.g. iron_ingot", quantity="How many (1-64)")
    async def mc_buy(self, interaction: discord.Interaction, item: str, quantity: int = 1):
        if not 1 <= quantity <= MAX_QUANTITY:
            await interaction.response.send_message(
                f"Quantity must be between 1 and {MAX_QUANTITY}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        item_key = item.strip().lower()
        listing = await store.get_mc_shop_item(item_key)
        if not listing:
            await interaction.followup.send("That item is not on the market.", ephemeral=True)
            return
        if not listing.get("tradable"):
            await interaction.followup.send(
                f"`{item_key}` is not currently for sale.", ephemeral=True)
            return

        discord_id = await self._resolve_discord_id(interaction.user)
        if not discord_id:
            await interaction.followup.send(
                "No active Minecraft link, so an order cannot be delivered to you.",
                ephemeral=True)
            return

        unit_price = int(listing.get("buy_price") or 0)
        total = unit_price * quantity
        profile = await self._profile_for(discord_id)
        if profile and profile.money < total:
            await interaction.followup.send(
                f"That costs **{total:,} R** but your mirrored balance is "
                f"**{profile.money:,} R**. Mirrors can lag 30 minutes, so this may "
                "still go through — the server has the final say.", ephemeral=True)
            return

        order_id = str(uuidlib.uuid4())
        now = discord.utils.utcnow()
        await store.create_mc_order({
            "order_id": order_id,
            "discord_user_id": discord_id,
            "username": profile.username if profile else "",
            "item_key": item_key,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_price": total,
            "status": "PENDING",
            "reason": "",
            "created_at": discord.utils.isoformat(now),
            "expires_at": discord.utils.isoformat(now + discord.timedelta(hours=24)),
        })

        embed = discord.Embed(
            title="Order placed",
            description=(f"`{quantity}x {item_key}` for **{total:,} R**\n\n"
                         "**Nothing has been charged yet.** The server checks your "
                         "balance and the live price when you next log in. If either "
                         "changed, the order is rejected and you are not charged."),
            colour=discord.Colour.blue())
        embed.add_field(name="Order", value=f"`{order_id[:8]}`")
        embed.add_field(name="Expires", value=discord.utils.format_dt(
            now + discord.timedelta(hours=24), "R"))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MCEconomy(bot))
