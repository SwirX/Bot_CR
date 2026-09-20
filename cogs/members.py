"""Club member system: linking, profiles, hierarchy, notifications, dashboard.

Discord account -> linked club account (club_id / club_role / cell) is the
bridge between the app and the bot. Role/cell are set by staff via
``/setprofile`` so a member can never self-promote; the linked account feeds
the permission-scope resolver in cogs/_scopes.py.
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from data.store import store
from data.store import StoreError
from cogs._scopes import (CLUB_ROLE_LABELS, SCOPE_LABELS, scopes_for,
                          scopes_for_author, require_scope)
from cogs._dates import days_until, fmt_date
from cogs._ui import OwnerView, PaginatorView
from cogs.minecraft import LinkChoiceView, UnlinkConfirmView, mc_link_card_embed
from i18n.core import resolve_member_lang, t

LOG = logging.getLogger("bot.members")

ROLE_CHOICES = [
    app_commands.Choice(name="Core Member", value="core_member"),
    app_commands.Choice(name="Cell Member", value="cell_member"),
    app_commands.Choice(name="Cell Chief", value="cell_chief"),
    app_commands.Choice(name="Vice President", value="vice_president"),
    app_commands.Choice(name="President", value="president"),
    app_commands.Choice(name="Archon", value="archon"),
]

NOTIF_KEYS = {
    "tasks": ("notify_tasks", "📋 Tasks"),
    "events": ("notify_events", "📅 Events"),
    "competitions": ("notify_competitions", "🏆 Competitions"),
    "announcements": ("notify_announcements", "📢 Announcements"),
}

HIERARCHY_CHART = (
    "```\n"
    "                ARCHON\n"
    "                   │\n"
    "             ┌─────┴─────┐\n"
    "          PRESIDENT     VICE PRESIDENT\n"
    "             ┌─────┴─────┐\n"
    "           CELL CHIEFS\n"
    "             ┌─────┴─────┐\n"
    "   CELL MEMBERS      CORE MEMBERS\n"
    "```"
)


def level_from_xp(xp: int) -> int:
    """Level for cumulative XP using the club curve 100·L²."""
    xp = max(0, int(xp or 0))
    level = 0
    while 100 * (level + 1) * (level + 1) <= xp:
        level += 1
    return level


class NotifView(discord.ui.View):
    """Toggles for /notifications — stored per member in Appwrite."""

    def __init__(self, cog, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="📋 Tasks", style=discord.ButtonStyle.secondary,
                       custom_id="notif:tasks")
    async def tasks(self, interaction, button):
        await self._toggle(interaction, "tasks", button)

    @discord.ui.button(label="📅 Events", style=discord.ButtonStyle.secondary,
                       custom_id="notif:events")
    async def events(self, interaction, button):
        await self._toggle(interaction, "events", button)

    @discord.ui.button(label="🏆 Competitions", style=discord.ButtonStyle.secondary,
                       custom_id="notif:competitions")
    async def competitions(self, interaction, button):
        await self._toggle(interaction, "competitions", button)

    @discord.ui.button(label="📢 Announcements", style=discord.ButtonStyle.secondary,
                       custom_id="notif:announcements")
    async def announcements(self, interaction, button):
        await self._toggle(interaction, "announcements", button)

    @discord.ui.button(label="✓ Save", style=discord.ButtonStyle.success,
                       custom_id="notif:done")
    async def done(self, interaction, button):
        await interaction.response.edit_message(
            content="🔔 Notification preferences saved.", view=None)

    async def _toggle(self, interaction, key, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "🔒 This panel belongs to someone else.", ephemeral=True)
            return
        attr, _label = NOTIF_KEYS[key]
        record = (await store.get_member(self.user_id)) or {}
        current = bool(record.get(attr, True))
        await store.merge_member(self.user_id, {attr: not current})
        await interaction.response.defer()
        await self.cog._refresh_notif_panel(interaction, self)


class DashboardView(OwnerView, discord.ui.View):
    """Quick actions for /dashboard — lightweight embeds, same backend."""

    def __init__(self, cog, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

    def _owner_deny_message(self, _interaction) -> str:
        return ("🔒 This dashboard belongs to someone else — run `/dashboard` "
                "yourself for your own panels.")

    @discord.ui.button(label="📋 Tasks", style=discord.ButtonStyle.primary,
                       custom_id="dash:tasks")
    async def tasks(self, interaction, button):
        await self._section(interaction, self.cog._tasks_text(self.user_id), "📋 Tasks")

    @discord.ui.button(label="🏆 Competitions", style=discord.ButtonStyle.primary,
                       custom_id="dash:competitions")
    async def competitions(self, interaction, button):
        await self._section(interaction, self.cog._competitions_text(self.user_id),
                            "🏆 Competitions")

    @discord.ui.button(label="📅 Events", style=discord.ButtonStyle.primary,
                       custom_id="dash:events")
    async def events(self, interaction, button):
        await self._section(interaction, self.cog._events_text(self.user_id), "📅 Events")

    @discord.ui.button(label="👤 Profile", style=discord.ButtonStyle.secondary,
                       custom_id="dash:profile")
    async def profile(self, interaction, button):
        embed = await self.cog._profile_embed(interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔔 Notifications", style=discord.ButtonStyle.secondary,
                       custom_id="dash:notifications")
    async def notifications(self, interaction, button):
        embed = await self.cog._notif_embed(interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _section(self, interaction, text, title):
        if not await self._owned(interaction):
            return
        if not text:
            text = "Nothing here yet."
        embed = discord.Embed(title=title, description=text, color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ProfileHubView(OwnerView, discord.ui.View):
    """/profile identity hub: Overview / Minecraft / Robotics tabs (Dank-Memer style)."""

    def __init__(self, cog: "Members", lang: str, member: discord.Member,
                 *, user: discord.Member = None, timeout: float = 180.0):
        """``user`` is the invoker — only they may navigate or close the view."""
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member = cog, lang, member
        self.user_id = user.id if user is not None else member.id
        self.close.label = t("settings.close", lang)
        tabs = discord.ui.Select(
            placeholder=t("profile.tab.pick", lang), row=0,
            options=[
                discord.SelectOption(value="overview", emoji="🏠",
                                     label=t("profile.tab.overview", lang)),
                discord.SelectOption(value="minecraft", emoji="⛏️",
                                     label=t("profile.tab.minecraft", lang)),
                discord.SelectOption(value="robotics", emoji="🔬",
                                     label=t("profile.tab.robotics", lang)),
            ],
        )
        tabs.callback = self._tab_select
        self.add_item(tabs)

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This profile view belongs to the command author — "
                "run `/profile` yourself to use its tabs.")

    async def _tab_select(self, interaction: discord.Interaction):
        if not await self._owned(interaction):
            return
        tab = interaction.values[0]
        if tab == "overview":
            embed = await self.cog._profile_embed(self.member)
            await interaction.response.edit_message(embed=embed, view=self)
        elif tab == "minecraft":
            await self._go_minecraft(interaction)
        else:
            embed = discord.Embed(
                title=t("profile.tab.robotics", self.lang),
                description=t("profile.robotics.soon", self.lang),
                color=discord.Color.dark_teal(),
            )
            await interaction.response.edit_message(embed=embed, view=self)

    async def _go_minecraft(self, interaction: discord.Interaction):
        mc_cog = self.cog.bot.get_cog("Minecraft")
        if mc_cog is None:
            embed = discord.Embed(description=t("mc.unconfigured", self.lang),
                                  color=discord.Color.red())
            await interaction.response.edit_message(embed=embed, view=self)
            return
        can_manage = interaction.user.id == self.member.id or mc_cog._is_mc_operator(interaction.user)
        embed = await mc_link_card_embed(mc_cog, self.member, self.lang,
                                         full=can_manage)
        linked = None
        link = mc_cog._mc_link_of(await mc_cog._record_for(self.member.id))
        if link is not None and link.get("type") == "linked":
            linked = link.get("username")
        view = ProfileMinecraftTabView(self.cog, self.lang, self.member, mc_cog,
                                       viewer=interaction.user,
                                       linked_username=linked)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await interaction.response.edit_message(
            content=t("profile.closed", self.lang), embed=None, view=None)


class ProfileMinecraftTabView(OwnerView, discord.ui.View):
    """Minecraft tab inside /profile: link status + quick link/unlink actions."""

    def __init__(self, cog: "Members", lang: str, member: discord.Member,
                 mc_cog: "Minecraft", *, viewer: discord.Member,
                 linked_username: str | None = None,
                 timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member, self.mc_cog = cog, lang, member, mc_cog
        self.viewer = viewer
        self.user_id = viewer.id if viewer is not None else member.id
        self.linked_username = linked_username
        self.link.label = t("mc.hub.link", lang)
        self.unlink.label = t("mc.hub.unlink", lang)
        self.back.label = t("mc.hub.back", lang)
        self.close.label = t("settings.close", lang)
        self.mcpass.label = t("mc.hub.mcpass", lang)
        self.devices.label = t("mc.hub.devices", lang)
        if not (viewer.id == member.id or mc_cog._is_mc_operator(viewer)):
            self.remove_item(self.link)
            self.remove_item(self.unlink)
        # mc-link extras need a real mc_auth profile AND owner/operator access
        # (device IPs are privacy-sensitive even inside the profile tab).
        if linked_username is None or not (
                viewer.id == member.id or mc_cog._is_mc_operator(viewer)):
            self.remove_item(self.mcpass)
            self.remove_item(self.devices)

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This profile view belongs to the command author — "
                "run `/profile` yourself to use its tabs.")

    async def _home(self) -> tuple[discord.Embed, "ProfileMinecraftTabView"]:
        can_manage = (self.viewer.id == self.member.id
                      or self.mc_cog._is_mc_operator(self.viewer))
        embed = await mc_link_card_embed(self.mc_cog, self.member, self.lang,
                                         full=can_manage)
        linked = None
        link = self.mc_cog._mc_link_of(
            await self.mc_cog._record_for(self.member.id))
        if link is not None and link.get("type") == "linked":
            linked = link.get("username")
        return embed, ProfileMinecraftTabView(
            self.cog, self.lang, self.member, self.mc_cog, viewer=self.viewer,
            linked_username=linked)

    @discord.ui.button(emoji="🔗", style=discord.ButtonStyle.primary, row=0)
    async def link(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        view = LinkChoiceView(self.mc_cog, self.lang, self.member,
                              home_factory=self._home)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger, row=0)
    async def unlink(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        view = UnlinkConfirmView(self.mc_cog, self.lang, self.member,
                                 home_factory=self._home)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        hub = ProfileHubView(self.cog, self.lang, self.member, user=self.viewer)
        embed = await self.cog._profile_embed(self.member)
        await interaction.response.edit_message(embed=embed, view=hub)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await interaction.response.edit_message(
            content=t("profile.closed", self.lang), embed=None, view=None)

    @discord.ui.button(emoji="🔑", style=discord.ButtonStyle.primary, row=2)
    async def mcpass(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        mclink = self.cog.bot.get_cog("McLink")
        if mclink is None or not getattr(self, "linked_username", None):
            await interaction.response.defer()
            return
        await mclink.open_mcpass_modal(interaction, self.lang,
                                       self.linked_username)

    @discord.ui.button(emoji="📱", style=discord.ButtonStyle.secondary, row=2)
    async def devices(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        mclink = self.cog.bot.get_cog("McLink")
        if mclink is None or not getattr(self, "linked_username", None):
            await interaction.response.defer()
            return
        await mclink.open_devices_view(interaction, self.lang,
                                       self.linked_username,
                                       home_factory=self._home)


class Members(commands.Cog):
    """Linking, profiles, hierarchy, notifications and the dashboard."""

    def __init__(self, bot):
        self.bot = bot

    # ── helpers ─────────────────────────────────────────────────
    async def _record(self, user_id: int) -> dict:
        try:
            return (await store.get_member(user_id)) or {}
        except StoreError:
            return {}

    def _role_label(self, record: dict) -> str:
        key = str(record.get("club_role") or "").lower()
        return CLUB_ROLE_LABELS.get(key, "Core Member" if not key else key)

    async def _profile_embed(self, member: discord.Member) -> discord.Embed:
        record = await self._record(member.id)
        name = record.get("real_name") or member.display_name
        scopes = scopes_for(str(record.get("club_role") or "").lower(),
                            is_member=member)
        embed = discord.Embed(
            title=f"👤 {name}",
            color=discord.Color.teal(),
            description=f"<@{member.id}> · Discord identity linked to the club account.",
        )
        embed.add_field(name="Club role",
                        value=self._role_label(record), inline=True)
        embed.add_field(name="Cell",
                        value=record.get("cell") or "—", inline=True)
        embed.add_field(name="Club ID",
                        value=record.get("club_id") or "—", inline=True)
        xp = int(record.get("xp") or 0)
        embed.add_field(name="XP / Level",
                        value=f"{xp:,} XP · Level {level_from_xp(xp)}", inline=True)
        voice = float(record.get("voice_seconds") or 0)
        embed.add_field(name="Voice time",
                        value=f"{int(voice // 3600)}h {int((voice % 3600) // 60)}m"
                              if voice else "0s", inline=True)
        if "internal.read" in scopes:
            embed.add_field(name="⚠️ Warnings",
                            value=str(record.get("warnings") or 0), inline=True)
        else:
            embed.add_field(name="Status", value="Active", inline=True)
        return embed

    async def _notif_embed(self, member: discord.Member) -> discord.Embed:
        record = await self._record(member.id)
        lines = []
        for key, (attr, label) in NOTIF_KEYS.items():
            on = bool(record.get(attr, True))
            lines.append(f"{'🟢' if on else '⚪'} {label}: {'ON' if on else 'OFF'}")
        embed = discord.Embed(
            title="🔔 Notification preferences",
            description="\n".join(lines) + "\n\nUse `/notifications` to change these.",
            color=discord.Color.blurple(),
        )
        return embed

    async def _refresh_notif_panel(self, interaction, view):
        embed = await self._notif_embed(interaction.user)
        await interaction.edit_original_response(embed=embed, view=view)

    # ── text summaries (shared with the dashboard buttons) ─────
    async def _tasks_text(self, user_id: int) -> str:
        try:
            tasks = [t for t in await store.list_tasks()
                     if str(t.get("assignee")) == str(user_id)
                     and t.get("status") not in ("done", "cancelled")]
        except StoreError:
            return ""
        if not tasks:
            return ""
        lines = []
        for t in sorted(tasks, key=lambda t: t.get("due") or "9999"):
            color = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                str(t.get("priority") or "medium"), "🟡")
            due = fmt_date(t.get("due"))
            status = "✅ done" if t.get("status") == "done" else (
                "⚠️ overdue" if t.get("status") not in ("done", "cancelled")
                and days_until(t.get("due")) is not None
                and days_until(t.get("due")) < 0 else t.get("status") or "open")
            lines.append(f"{color} **{t.get('title')}** — {due} ({status})\n"
                         f"   `{t.get('task_id')}`")
        return "\n".join(lines[:8])

    async def _competitions_text(self, user_id: int) -> str:
        try:
            comps = await store.list_competitions()
        except StoreError:
            return ""
        upcoming = [c for c in comps
                    if days_until(c.get("date")) is None or days_until(c.get("date")) >= 0]
        upcoming.sort(key=lambda c: str(c.get("date") or "9999"))
        regs = {str(user_id) in (c.get("registered") or []) for c in comps}
        lines = []
        for c in upcoming[:6]:
            reg = "✅" if str(user_id) in (c.get("registered") or []) else "⬜"
            cap = f"/{c['capacity']}" if c.get("capacity") else ""
            lines.append(f"{reg} **{c.get('name')}** · {fmt_date(c.get('date'))} "
                         f"· 📍 {c.get('location') or 'TBD'} · "
                         f"👥 {len(c.get('registered') or [])}{cap}")
        return "\n".join(lines)

    async def _events_text(self, user_id: int) -> str:
        try:
            events = await store.list_events()
        except StoreError:
            return ""
        upcoming = [e for e in events
                    if days_until(e.get("date")) is None or days_until(e.get("date")) >= 0]
        upcoming.sort(key=lambda e: str(e.get("date") or "9999"))
        lines = []
        for e in upcoming[:6]:
            att = str(user_id) in (e.get("attendees") or [])
            mark = "✅" if att else "⬜"
            lines.append(f"{mark} **{e.get('title')}** · {fmt_date(e.get('date'))} "
                         f"{e.get('time') or ''} · 📍 {e.get('location') or 'TBD'}")
        return "\n".join(lines)

    # ── commands ───────────────────────────────────────────────
    @commands.hybrid_command(name="link", description="Link your Discord to your club account.")
    @commands.guild_only()
    async def link(self, ctx, club_id: str, real_name: str = None):
        """Store your club-account ID (the app/website's identifier for you)."""
        payload = {"club_id": club_id.strip()}
        if real_name:
            payload["real_name"] = real_name.strip()
        try:
            await store.merge_member(ctx.author.id, payload)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't save the link: {exc}")
            return
        await ctx.send(f"🔗 Linked Discord → club account **{club_id.strip()}**.\n"
                       "Your club role and cell are set by leadership via "
                       "`/setprofile` (you can't assign them yourself).")

    @commands.hybrid_command(name="unlink", description="Remove your club-account link.")
    @commands.guild_only()
    async def unlink(self, ctx):
        """Forget club_id/role/cell — Discord-only member again."""
        try:
            await store.merge_member(ctx.author.id,
                                     {"club_id": "", "club_role": "", "cell": ""})
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't unlink: {exc}")
            return
        await ctx.send("🔓 Unlinked your club account.")

    @commands.hybrid_command(name="profile", description="Show a member's club profile.")
    @commands.guild_only()
    async def profile(self, ctx, member: discord.Member = None):
        """Your profile (or a public one) straight from the Appwrite source of truth."""
        member = member or ctx.author
        lang = await resolve_member_lang(ctx.author.id, None)
        embed = await self._profile_embed(member)
        await ctx.send(embed=embed, view=ProfileHubView(self, lang, member,
                                                        user=ctx.author),
                       ephemeral=True)

    @commands.hybrid_command(name="whois", description="Internal profile for staff.")
    @commands.guild_only()
    @require_scope("members.read")
    async def whois(self, ctx, member: discord.Member):
        """Full internal record — linking, cell, warnings, notifications prefs."""
        record = await self._record(member.id)
        embed = await self._profile_embed(member)
        embed.description = f"<@{member.id}> · internal record (staff view)."
        embed.add_field(name="📋 Linked club ID",
                        value=record.get("club_id") or "not linked", inline=True)
        embed.add_field(name="🔔 Notifications",
                        value=", ".join(
                            k for k, (attr, _l) in NOTIF_KEYS.items()
                            if bool(record.get(attr, True))) or "none",
                        inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="roles", description="What your club role gives you access to.")
    @commands.guild_only()
    async def roles(self, ctx):
        """Explains the permission scopes your current role unlocks."""
        scopes = await scopes_for_author(ctx)
        lines = [f"{SCOPE_LABELS.get(scope, scope)}"
                 for scope in sorted(scopes)
                 if scope in SCOPE_LABELS]
        embed = discord.Embed(
            title=f"🔑 Access for {ctx.author.display_name}",
            description="\n".join(f"• {line}" for line in lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Scopes come from your linked club account + Discord roles.")
        if len(lines) > 25:
            await ctx.send(embed=embed, view=PaginatorView(
                [f"{' '.join(lines[i:i + 25])}" for i in range(0, len(lines), 25)],
                user=ctx.author))
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="hierarchy", description="The club's structure.")
    @commands.guild_only()
    async def hierarchy(self, ctx):
        """Static org chart — Archon → President/VP → Cell Chiefs → cells."""
        embed = discord.Embed(
            title="🤖 Robotics Club hierarchy", color=discord.Color.dark_teal())
        embed.description = HIERARCHY_CHART
        embed.add_field(
            name="Reading it",
            value="Scopes cascade down: a Cell Chief has their cell's task rights, "
                  "Vice Presidents and the President span cells, the Archon governs.",
            inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setprofile",
                             description="Set a member's club role / cell / club ID.")
    @commands.guild_only()
    @require_scope("members.manage")
    async def setprofile(self, ctx, member: discord.Member = None,
                         club_role: app_commands.Choice[str] = None,
                         cell: str = None, club_id: str = None):
        """Leadership tool: only staff can promote/place members into cells.

        Slash invocation opens a modal form; prefix keeps the inline path.
        """
        if ctx.interaction is not None:
            role_value = getattr(club_role, "value", "") if club_role else ""
            await ctx.interaction.response.send_modal(SetProfileModal(
                self, member=member, club_role=role_value,
                cell=cell or "", club_id=club_id or ""))
            return
        if member is None:
            await ctx.send("⚠️ Pass the member to update, e.g. "
                           "`!setprofile @member role=cell_chief cell=Alpha`.")
            return
        await self._set_profile(ctx, member=member, club_role=club_role,
                                cell=cell, club_id=club_id, respond=ctx.send)

    async def _set_profile(self, ctx, *, member: discord.Member, club_role=None,
                           cell: str = None, club_id: str = None, respond) -> None:
        """Shared core for /setprofile and its modal form.

        ``respond`` is ``ctx.send`` for prefix invocations and an interaction
        followup for modals.
        """
        if member == self.bot.user:
            await respond("Nice try. I manage my own account. 🤖")
            return
        payload = {}
        if club_role:
            key = str(club_role.value if hasattr(club_role, "value")
                      else club_role).strip().lower()
            if key not in CLUB_ROLE_LABELS:
                await respond("⚠️ Unknown club role **%s** — pick one of: %s."
                              % (key, ", ".join(CLUB_ROLE_LABELS.values())))
                return
            payload["club_role"] = key
        if cell:
            payload["cell"] = str(cell).strip()
        if club_id:
            payload["club_id"] = str(club_id).strip()
        if not payload:
            await respond("⚠️ Nothing to change — give a `club_role`, `cell` or `club_id`.")
            return
        try:
            await store.merge_member(member.id, payload)
        except StoreError as exc:
            await respond(f"⚠️ Couldn't update the profile: {exc}")
            return
        role_label = CLUB_ROLE_LABELS.get(
            str(payload.get("club_role", "")).lower(), "")
        piece = ", ".join(f"{k}={v}" for k, v in payload.items())
        await respond(f"✅ Updated **{member.display_name}**: {piece}"
                      + (f" → **{role_label}**" if role_label else ""))

    @commands.hybrid_command(name="notifications",
                             description="Configure what the bot notifies you about.")
    @commands.guild_only()
    async def notifications(self, ctx):
        """Toggle task/event/competition/announcement notifications (stored)."""
        embed = await self._notif_embed(ctx.author)
        await ctx.send(embed=embed, view=NotifView(self, ctx.author.id),
                       ephemeral=True)

    @commands.hybrid_command(name="dashboard", description="Your whole club life in one view.")
    @commands.guild_only()
    async def dashboard(self, ctx):
        """Permission-aware overview — scope-dependent sections + quick actions."""
        record = await self._record(ctx.author.id)
        scopes = await scopes_for_author(ctx)
        name = record.get("real_name") or ctx.author.display_name
        embed = discord.Embed(
            title="🤖 ROBOTICS CLUB",
            description=f"Welcome, **{name}**.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Role", value=self._role_label(record), inline=True)
        embed.add_field(name="Cell", value=record.get("cell") or "—", inline=True)
        if "tasks.read" in scopes and "tasks.claim" in scopes:
            active = [t for t in (await self._safe_task_list())
                      if str(t.get("assignee")) == str(ctx.author.id)
                      and t.get("status") not in ("done", "cancelled")]
            due_soon = [t for t in active
                        if (d := days_until(t.get("due"))) is not None and 0 <= d <= 3]
            embed.add_field(
                name="📋 Tasks",
                value=f"{len(active)} active · {len(due_soon)} due soon",
                inline=True)
        comps = await self._competitions_text(ctx.author.id)
        if comps:
            registered = sum(1 for c in await self._safe_comp_list()
                             if str(ctx.author.id) in (c.get("registered") or []))
            lines = comps.splitlines()
            embed.add_field(name="🏆 Competitions",
                            value=f"{len(lines)} upcoming · {registered} registered",
                            inline=True)
        events = await self._events_text(ctx.author.id)
        if events:
            attending = sum(1 for e in await self._safe_event_list()
                            if str(ctx.author.id) in (e.get("attendees") or []))
            embed.add_field(name="📅 Events",
                            value=f"{len(events.splitlines())} upcoming · "
                                  f"{attending} attending", inline=True)
        embed.set_footer(text="Appwrite = source of truth · press a button for details")
        await ctx.send(embed=embed, view=DashboardView(self, ctx.author.id))

    # ── safe list helpers (used by the dashboard) ──────────────
    async def _safe_task_list(self):
        try:
            return await store.list_tasks()
        except StoreError:
            return []

    async def _safe_comp_list(self):
        try:
            return await store.list_competitions()
        except StoreError:
            return []

    async def _safe_event_list(self):
        try:
            return await store.list_events()
        except StoreError:
            return []


class SetProfileModal(discord.ui.Modal):
    """Pop-up form for /setprofile (staff) — pre-filled from slash args."""

    def __init__(self, cog: "Members", *, member: discord.Member = None,
                 club_role: str = "", cell: str = "", club_id: str = ""):
        super().__init__(title="🪪 Set profile")
        self.cog = cog
        self.member_input = discord.ui.TextInput(
            label="Member (mention or numeric ID)", max_length=32,
            default=member.mention if member is not None else None,
            required=True)
        self.role_input = discord.ui.TextInput(
            label="Club role (core_member … archon)", max_length=32,
            default=club_role or None, required=False)
        self.cell_input = discord.ui.TextInput(
            label="Cell", max_length=64, default=cell or None, required=False)
        self.clubid_input = discord.ui.TextInput(
            label="Club account ID", max_length=64,
            default=club_id or None, required=False)
        for item in (self.member_input, self.role_input, self.cell_input,
                     self.clubid_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        raw = (self.member_input.value or "").strip()
        digits = re.sub(r"\D", "", raw)
        member = (interaction.guild.get_member(int(digits))
                  if digits and interaction.guild is not None else None)
        if member is None:
            await interaction.followup.send(
                "⚠️ Couldn't find that member in this server — paste their "
                "mention or numeric ID.", ephemeral=True)
            return
        await self.cog._set_profile(
            interaction, member=member,
            club_role=self.role_input.value or "",
            cell=self.cell_input.value or "",
            club_id=self.clubid_input.value or "",
            respond=lambda text: interaction.followup.send(text))


async def setup(bot):
    await bot.add_cog(Members(bot))