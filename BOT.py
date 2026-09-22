import asyncio
import logging
import os
import signal

import discord
from discord.ext import commands

import config
from data.store import store
from KeepAlive import keep_alive, mark_not_ready, mark_ready

LOG = logging.getLogger("bot")

# Create a bot instance
intents = discord.Intents.default()
intents.message_content = True  # Privileged intent
intents.presences = True  # Track online status
intents.members = True  # Required for the on_member_join event
intents.voice_states = True  # Track voice state changes

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)


def configure_logging() -> None:
    """Set up human-readable logging for the bot and its dependencies."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The Appwrite SDK logs through urllib3 — keep it quiet unless it matters.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# Event: When the bot is ready
@bot.event
async def on_ready():
    LOG.info("Logged in as %s (latency %.1f ms)", bot.user, bot.latency * 1000)
    mark_ready()  # keepalive readiness probe
    await sync_commands()


async def sync_commands():
    """Register hybrid slash commands with Discord.

    With GUILD_ID set, commands sync instantly to that server (great for
    testing); otherwise they sync globally and can take up to an hour.
    """
    if config.GUILD_ID:
        guild = discord.Object(id=config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        synced = await bot.tree.sync()
    LOG.info("Synced %d slash command(s)", len(synced))


async def _respond(ctx: commands.Context, content: str, ephemeral: bool = True):
    """Reply gracefully to both prefix and slash invocations."""
    if ctx.interaction:
        await ctx.interaction.response.send_message(content, ephemeral=ephemeral)
    else:
        await ctx.send(content)


# Event: Prefix-based errors
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"⛔ You need `{', '.join(error.missing_permissions)}` to use that.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Slow down! Try again in {error.retry_after:.0f}s.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
        return
    LOG.error("Command %r failed: %s", ctx.command, error)
    await ctx.send("⚠️ Something went wrong.")


# Event: Slash/hybrid errors
@bot.event
async def on_application_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
):
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        content = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, discord.app_commands.MissingPermissions):
        content = f"⛔ You need `{', '.join(error.missing_permissions)}` to use that."
    elif (hint := _argument_hint(error)) is not None:
        # Bad *input* is not an internal failure and must not read as one: the
        # user can fix it, and "something went wrong" tells them nothing about
        # what to change. Only the conversion layer's own message is shown --
        # never the wrapped command's internals.
        content = f"❓ {hint}"
    else:
        LOG.error("Slash command %r failed: %s", interaction.command, error)
        content = "⚠️ Something went wrong."
    try:
        await interaction.response.send_message(content, ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(content, ephemeral=True)


def _argument_hint(error: Exception) -> str | None:
    """A user-facing message for a failed argument conversion, else ``None``.

    Hybrid commands run their slash arguments through the same converters as
    text ones, so ``BadArgument`` arrives wrapped in ``CommandInvokeError`` with
    the real cause a few layers down. The most common way to hit it is passing
    something Discord's own UI cannot resolve -- typing ``@alice`` into an
    option that arrived as a plain string, because Discord has no multi-user
    option type for the converter to work from.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, commands.BadArgument):
            text = str(error)
            return text or "I couldn't read one of the values you entered."
        # ``ext.commands`` chains via __cause__, ``app_commands`` keeps it on
        # ``.original`` -- following only one of them means the hint silently
        # stops appearing whenever the other flavour raises.
        error = (getattr(error, "__cause__", None)
                 or getattr(error, "original", None)
                 or getattr(error, "original_error", None))
    return None


async def load_cogs() -> int:
    """Auto-discover and load every cog in the cogs/ package.

    Drop a new file in cogs/ and it is picked up automatically; no wiring
    needed in this launcher.

    The directory is resolved from ``config.BASE_DIR`` rather than the
    process CWD: a relative ``Path("cogs")`` globs to nothing when systemd
    starts us anywhere but the repo root, which loaded *zero* cogs silently —
    the bot connected, synced no commands and answered health checks while
    every feature was simply absent. The count is asserted below so that can
    never fail quietly again.
    """
    cogs_dir = config.BASE_DIR / "cogs"
    if not cogs_dir.is_dir():
        raise RuntimeError(f"Cog directory not found: {cogs_dir}")
    loaded = 0
    for path in sorted(cogs_dir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        extension = f"cogs.{path.stem}"
        await bot.load_extension(extension)
        loaded += 1
        LOG.info("Loaded cog: %s", extension)
    if loaded < 20:
        raise RuntimeError(
            f"Only {loaded} cog(s) loaded — expected the full set. "
            "Refusing to start with a partial feature set."
        )
    LOG.info("Loaded %d cog(s) total", loaded)
    return loaded


async def shutdown(signal_name: str = "") -> None:
    """Graceful shutdown. Installed on SIGTERM/SIGINT by main().

    Previously there was no shutdown path at all, so Python's default handler
    killed the process outright on every deploy. That left:
      * voice connections open — members stuck in a channel with a bot still
        "hearing" it until Discord's gateway timeout, and a /play panel that
        never got a stop notice;
      * up to one flush interval (DASHBOARD_REFRESH_SECONDS, default 60s) of
        message/voice counts and XP discarded;
      * aiohttp sessions and the Deezer cache orphaned.
    """
    mark_not_ready()
    LOG.info("shutdown requested (%s) — closing voice and flushing…", signal_name)
    try:
        for vc in list(bot.voice_clients):
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001 - best-effort on the way out
                LOG.debug("voice disconnect failed for %s", vc, exc_info=True)
    finally:
        # Push any buffered activity before the loop closes.
        for cog in list(bot.cogs):
            flush = getattr(cog, "flush_now", None)
            if callable(flush):
                try:
                    await flush()
                except Exception:  # noqa: BLE001 - never block the exit
                    LOG.debug("flush failed for %s", type(cog).__name__,
                              exc_info=True)
        await bot.close()


async def self_check() -> None:
    """Log what the bot actually resolved to, so a misconfiguration is visible.

    Every knob here fails *silently* by default: a wrong channel/role name just
    means a feature quietly does nothing, and ``Minecraft._configured()``
    returning False removes the whole ``/mc*`` surface with no log line at all.
    Worse, a staff identity that silently stops matching only shows up as
    "Bot admins only." at the moment somebody needs it.

    This prints the resolved admin set and a capability table at startup so
    both are checkable without waiting for a failure.
    """
    from cogs._perms import is_bot_admin  # local import: avoids a cycle

    LOG.info("── bot identity ──────────────────────────────")
    LOG.info("  guild sync       : %s", config.GUILD_ID or "GLOBAL (slow to propagate)")
    LOG.info("  prefix           : %s", config.PREFIX)
    LOG.info("  bot admins (ids) : %s",
             sorted(config.BOT_ADMIN_USER_IDS) or "— none (roles only)")
    LOG.info("  MC power (ids)   : %s",
             sorted(config.MC_CONTROL_USER_IDS) or "— none")
    LOG.info("  meeting admin    : ids=%s roles=%s",
             sorted(config.MEETING_ADMIN_USER_IDS) or "— none",
             sorted(config.MEETING_ADMIN_ROLES) or "— none")

    LOG.info("── capabilities ───────────────────────────────")
    checks = [
        ("appwrite endpoint", bool(config.APPWRITE_ENDPOINT)),
        ("appwrite project/db", bool(config.APPWRITE_PROJECT_ID
                                      and config.APPWRITE_DATABASE_ID)),
        ("pterodactyl client key", bool(config.MC_PTERO_CLIENT_KEY)),
        ("minecraft server id", bool(config.MC_SERVER_ID)),
        ("minecraft address", bool(config.MC_ADDRESS)),
        ("deezer ARL (radio/fallback)", bool(os.environ.get("DEEZER_ARL"))),
        ("level role rewards", bool(config.LEVEL_ROLE_REWARDS)),
    ]
    for label, ok in checks:
        LOG.info("  %-26s %s", label, "ok" if ok else "MISSING — feature disabled")

    # Which configured role/channel names actually exist in the guild?
    if config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild is None:
            LOG.warning("  guild %s not available yet; skipping name checks",
                        config.GUILD_ID)
            return
        role_names = [("ROLE_ARCHON", config.ROLE_ARCHON),
                      ("ROLE_VERIFIED", config.ROLE_VERIFIED),
                      ("ROLE_MEMBER", config.ROLE_MEMBER),
                      ("MC_PLAYER_ROLE", config.MC_PLAYER_ROLE)]
        for label, name in role_names:
            found = discord.utils.get(guild.roles, name=name)
            LOG.info("  %-26s %s", label,
                     "ok" if found else f"MISSING role named {name!r}")
        channel_names = [("CHANNEL_ANNOUNCEMENTS", config.CHANNEL_ANNOUNCEMENTS),
                         ("CHANNEL_DASHBOARD", config.CHANNEL_DASHBOARD),
                         ("CHANNEL_BOTLOG", config.CHANNEL_BOTLOG)]
        for label, name in channel_names:
            found = discord.utils.get(guild.text_channels, name=name)
            LOG.info("  %-26s %s", label,
                     "ok" if found else f"MISSING channel named {name!r}")

        # Confirm the configured operator IDs are actually members here.
        for uid in sorted(config.BOT_ADMIN_USER_IDS):
            member = guild.get_member(uid)
            state = ("ok, is_bot_admin="
                     + str(is_bot_admin(member)) if member else
                     "NOT A MEMBER of this guild — admin powers will not apply")
            LOG.info("  operator %-18s %s", uid, state)


async def main():
    configure_logging()
    keep_alive()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        def _on_signal(name=sig.name):
            asyncio.create_task(shutdown(name))
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass
    # Fail fast on a dead store. Booting without persistence was worse than
    # not booting: every command answered from an empty database, OTP logins
    # "succeeded" without being written, and XP/stat flushes vanished — with
    # nothing but a CRITICAL line in a journal nobody reads. Retry briefly so a
    # blip on Appwrite's side doesn't need a human restart, then give up loudly.
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            await store.init()
            break
        except Exception as exc:  # noqa: BLE001 - retried, then fatal below
            last = exc
            wait = 2 ** attempt
            LOG.warning("Appwrite init failed (attempt %d/5): %s — retrying in %ds",
                        attempt, exc, wait)
            await asyncio.sleep(wait)
    else:
        LOG.critical("Appwrite store unavailable after 5 attempts: %s", last)
        raise SystemExit(1)
    await load_cogs()
    bot.add_listener(
        lambda: asyncio.create_task(self_check()), "on_ready")
    await bot.start(config.BOT_TOKEN)

# Run the bot
if __name__ == "__main__":
    asyncio.run(main())