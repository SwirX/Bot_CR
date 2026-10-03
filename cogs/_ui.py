"""Reusable interactive Discord components for Bot_CR.

This file is NOT a cog: the underscore prefix keeps the auto-loader in
BOT.py (and scripts/smoke_test.py) from treating it as an extension. Cog
modules import these views with ``from cogs._ui import ...``.
"""

import asyncio
import logging

import discord

LOG = logging.getLogger("bot.ui")

__all__ = ["ConfirmView", "MemberPickerView", "PaginatorView", "OwnerView",
           "LoggedView", "close_panel", "select_value"]


class OwnerView:
    """Shared author-scoping for interactive views.

    Subclasses set ``self.user_id`` (int) on construction; ``owned`` refuses
    button presses from anyone else with an ephemeral notice. ``user_id=None``
    leaves the view open to everyone (shared panels such as polls). Override
    ``_owner_deny_message`` to tailor the refusal text.
    """

    async def owned(self, interaction: discord.Interaction) -> bool:
        user_id = getattr(self, "user_id", None)
        if user_id is None or interaction.user.id == user_id:
            return True
        await interaction.response.send_message(
            self._owner_deny_message(interaction), ephemeral=True)
        return False

    # Alias used by view callbacks; kept so refactors stay mechanical.
    async def _owned(self, interaction: discord.Interaction) -> bool:
        return await self.owned(interaction)

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This view belongs to the command author — run the "
                "command yourself to interact with it.")


class ConfirmView(OwnerView, discord.ui.View):
    """Two-button confirm/cancel prompt for destructive actions.

    ``on_confirm`` must be an async callable taking the button interaction.
    Only the member who opened the prompt may confirm or cancel it; strangers
    get an ephemeral refusal and the buttons stay live for the owner. The
    buttons disable after the authorized press so the action can't repeat.
    """

    def __init__(self, on_confirm, *, user=None, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.on_confirm = on_confirm
        self.user_id = user.id if hasattr(user, "id") else user

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return "🔒 This prompt belongs to the command author — you can't act on it."

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Confirm")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        # The action callback sends its own followup on this interaction.
        await self.on_confirm(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Cancel")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=None
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class LoggedView:
    """Mixin: surface view interaction errors in the service logs.

    Without this, an exception inside a button/select callback makes Discord
    show a generic "interaction failed" with no trace anywhere — adding it to
    a view converts those silent failures into ``bot.ui`` log lines.
    """

    async def on_error(self, interaction: discord.Interaction, error: Exception,
                       item) -> None:
        LOG.error("%s interaction error (item=%s, user=%s): %s",
                  type(self).__name__, type(item).__name__,
                  getattr(getattr(interaction, "user", None), "id", "?"),
                  error)


def select_value(interaction: discord.Interaction) -> str:
    """The first selected value of a Select interaction, or "".

    discord.py 2.x removed ``Interaction.values`` — the selection now lives in
    ``interaction.data["values"]``. Callers must read it through this helper so
    a future version bump can't silently kill dropdown callbacks again.
    """
    data = interaction.data or {}
    values = data.get("values")
    return str(values[0]) if values else ""


class MemberPickerView(OwnerView, LoggedView, discord.ui.View):
    """◀ ▶ multi-select over the guild's members, with a ✅ confirm step.

    Discord has no command option that means "several users at once" — a command
    option is exactly one value of exactly one type — so any flow that needs
    several people chosen has to be a component instead. That is not a stylistic
    preference: ``commands.Greedy[discord.Member]`` serialises to a plain string
    option, because there is no app-command shape for it, and the members the
    user typed arrive as text that the converter then fails on.

    A select menu can hold 25 chosen values, but only out of the 25 options on
    the page it is showing, so a guild with more members than that needs paging
    *and* per-page selection state. ``_per_page`` is what makes a choice survive
    a page change instead of being silently replaced by whatever is on the new
    page — the alternative, rebuilding from the menu's current ``values``, loses
    every earlier page the moment the user turns the corner.

    ``on_confirm`` is an async callable taking ``(interaction, members)``.
    """

    #: Discord's hard cap on options in one select menu, and on values chosen
    #: from it. Overriding either raises inside ``to_components()`` — after the
    #: flow has been built and usually after someone has already pressed
    #: something.
    _MAX = 25

    def __init__(self, candidates: list[discord.Member], *,
                 on_confirm, user, page_size: int = _MAX,
                 timeout: float = 300.0,
                 placeholder: str = "🔎 Pick people…"):
        super().__init__(timeout=timeout)
        self.candidates = [m for m in candidates if not getattr(m, "bot", False)]
        self.on_confirm = on_confirm
        self.user_id = user.id if hasattr(user, "id") else user
        self.placeholder = placeholder
        # A zero-member page would be a select with no options, which discord.py
        # rejects outright; callers are expected to check first, but clamping to
        # at least one keeps a miscount from crashing a live interaction.
        self.page_size = max(1, min(page_size, self._MAX, len(self.candidates) or 1))
        self.pages = max(1, -(-len(self.candidates) // self.page_size))
        self._page = 0
        self._per_page: dict[int, set[str]] = {}
        self.select: discord.ui.Select | None = None
        self.confirmed = False
        self.cancelled = False
        self._rebuild()

    # ── selection state ────────────────────────────────────────────
    def selected(self) -> list[discord.Member]:
        """Chosen members, in the picker's own (stable, name-sorted) order.

        Deduplicated across pages: a member can only appear on one page, but
        building the union defensively means a future re-ordering of
        ``candidates`` can't hand the caller the same person twice.
        """
        chosen: dict[int, discord.Member] = {}
        for ids in self._per_page.values():
            for uid in ids:
                member = next((m for m in self.candidates if str(m.id) == uid), None)
                if member is not None:
                    chosen[member.id] = member
        return [m for m in self.candidates if m.id in chosen]

    def _page_members(self) -> list[discord.Member]:
        start = self._page * self.page_size
        return self.candidates[start:start + self.page_size]

    def _make_select(self) -> discord.ui.Select:
        picked = self._per_page.get(self._page, set())
        options = []
        for member in self._page_members():
            options.append(discord.SelectOption(
                label=(member.display_name or member.name)[:100],
                value=str(member.id),
                description=_role_hint(member)[:100],
                default=str(member.id) in picked,
            ))
        select = discord.ui.Select(
            placeholder=self.placeholder, min_values=0,
            # A multi-select needs headroom over the page size or a full page
            # could not be selected at once.
            max_values=min(self._MAX, max(1, len(options))),
            options=options, row=0)
        select.callback = self._on_select
        return select

    def _rebuild(self) -> None:
        """Swap in a select menu holding the current page's options."""
        if self.select is not None:
            self.remove_item(self.select)
        self.select = self._make_select()
        self.add_item(self.select)
        self.prev.disabled = self._page == 0
        self.next.disabled = self._page >= self.pages - 1
        self.page_label.label = f"{self._page + 1}/{self.pages}"
        self.confirm.disabled = not self.selected()

    # ── callbacks ──────────────────────────────────────────────────
    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This picker belongs to the command author — run the command "
                "yourself to pick from it.")

    async def _edit(self, interaction: discord.Interaction, **kwargs) -> None:
        try:
            await interaction.response.edit_message(view=self, **kwargs)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(interaction.message.id,
                                                    view=self, **kwargs)
        except discord.HTTPException:
            pass

    async def _notify(self, interaction: discord.Interaction, text: str) -> None:
        """Ephemeral notice for a refusal that must not replace the picker."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._owned(interaction):
            return
        # Read through the raw payload: this select is multi-value, so
        # select_value() (first value only) would keep exactly one person.
        values = (interaction.data or {}).get("values") or []
        self._per_page[self._page] = {str(v) for v in values}
        self._rebuild()
        await self._edit(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._page = max(0, self._page - 1)
        self._rebuild()
        await self._edit(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="1/1",
                       disabled=True, row=1)
    async def page_label(self, _interaction: discord.Interaction,
                         _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._page = min(self.pages - 1, self._page + 1)
        self._rebuild()
        await self._edit(interaction)

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        members = self.selected()
        if not members:
            # The button is disabled in this state, so this should be
            # unreachable — but the guard belongs here rather than in the
            # disabled flag, because an empty selection handed to a caller that
            # is about to start a meeting is a silent, confusing failure.
            await self._notify(interaction, "Pick at least one person first.")
            return
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await self._edit(interaction, content="✅ picked.", embed=None)
        # Releases anyone awaiting ``view.wait()``. Without this the wait only
        # ends at the timeout, so a caller that waits before acting would hang
        # for the full five minutes on an otherwise successful pick.
        self.stop()
        await self.on_confirm(interaction, members)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self.cancelled = True
        for child in self.children:
            child.disabled = True
        await self._edit(interaction, content="❌ Cancelled.", embed=None)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


def _role_hint(member: discord.Member) -> str:
    """One-line 'who is this' for a select option.

    The most senior role they hold, so a 200-person picker is still navigable
    by looking for "President" instead of comparing display names.
    """
    roles = [r for r in getattr(member, "roles", ())
             if getattr(r, "name", "") and not getattr(r, "managed", False)
             and r.name != "@everyone"]
    return roles[-1].name if roles else "no club role"


async def close_panel(interaction: discord.Interaction, *, text: str,
                      delay: float = 5.0) -> None:
    """✖️ close: swap the panel for ``text``, then delete it after ``delay`` s.

    Discord's original-response delete works even for ephemeral panels
    (where ``interaction.message`` is ``None``); the ``interaction.message``
    fallback covers prefix-invoked panels that have no interaction response.
    """
    try:
        await interaction.response.edit_message(content=text, embed=None, view=None)
    except (discord.HTTPException, discord.InteractionResponded):
        return
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
    try:
        await interaction.delete_original_response()
    except (discord.HTTPException, AttributeError):
        try:
            await interaction.message.delete()
        except (discord.HTTPException, AttributeError):
            pass


class PaginatorView(OwnerView, discord.ui.View):
    """◀ ▶ pager over a list of pages (strings and/or embeds).

    Pass ``user`` to scope the pager to its commanding member; without it the
    pager stays open to everyone (legacy callers).
    """

    def __init__(self, pages: list, *, timeout: float = 180.0, start: int = 0,
                 user: discord.Member = None):
        super().__init__(timeout=timeout)
        if not pages:
            raise ValueError("PaginatorView needs at least one page")
        self.pages = pages
        self.user_id = user.id if user is not None else None
        self._index = max(0, min(start, len(pages) - 1))
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev.disabled = self._index == 0
        self.next.disabled = self._index == len(self.pages) - 1
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This pager belongs to the command author — run the command "
                "yourself to page through it.")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._index = max(0, self._index - 1)
        await self._update(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="1/1", disabled=True)
    async def page_label(self, _interaction: discord.Interaction, _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._index = min(len(self.pages) - 1, self._index + 1)
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction):
        self._sync_buttons()
        page = self.pages[self._index]
        kwargs: dict = {"view": self, "content": None, "embed": None}
        if isinstance(page, discord.Embed):
            kwargs["embed"] = page
        else:
            kwargs["content"] = page
        try:
            await interaction.response.edit_message(**kwargs)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(interaction.message.id, **kwargs)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True