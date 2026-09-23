"""Music streaming for Bot_CR (multi-source: YouTube, Audius, internet radio).

Joins the author's voice channel and streams the first playable source for a
query (YouTube via yt-dlp first, Audius as the automatic keyless fallback,
public radio stations on request), with a queue, a ``/queue auto`` YTMusic-radio
generator, majority vote-skip, per-track loop, volume control and an
interactive now-playing panel that also looks up lyrics on LRCLIB. The bot
auto-disconnects after being idle for a bit.

The playback state machine lives in :class:`MusicPlayer` and is deliberately
voice-independent where possible (``voice`` is injected), so the queue / vote /
loop math is unit-testable without a real Discord voice connection.
"""

import asyncio
import concurrent.futures
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

from cogs._perms import is_bot_admin
from cogs.music_sources import radio as radio_provider
from cogs.music_sources import resolve_playable, youtube as youtube_provider
from cogs.music_sources.model import AllSourcesFailed, Playable, SourceUnavailable

LOG = logging.getLogger("bot.music")

USER_AGENT = "Bot_CR/1.0 (robotics-club Discord bot; contact: server staff)"

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


@dataclass
class Track:
    """One queued song. Votes reset when the track starts playing."""

    title: str
    url: str
    webpage_url: str = ""
    video_id: str = ""
    duration: int | None = None
    thumbnail: str = ""
    artist: str = ""
    requester_id: int | None = None
    headers: dict = field(default_factory=dict)
    votes: set = field(default_factory=set)
    # Decrypted local audio file (Deezer); when set, playback reads this path
    # instead of `url`.
    local_path: str = ""


def _ffmpeg_kwargs(track: Track) -> dict:
    """The ffmpeg options for a track: HTTP knobs only for streamed URLs.

    The ``-reconnect`` family belongs to ffmpeg's HTTP input layer. Pointing
    them at a local file makes ffmpeg abort with "Option reconnect not found"
    (exit 8) — exactly the "resolved but nothing plays" bug, because Deezer's
    decrypted files used to get the same before-options as a URL. A local file
    needs no before-options at all.
    """
    if track.local_path:
        return {"options": "-vn"}
    before = FFMPEG_BEFORE
    if track.headers:
        before = f"{before} {_header_option(track.headers)}"
    return {"before_options": before, "options": "-vn"}


def _header_option(headers: dict) -> str:
    """Render yt-dlp's ``http_headers`` as an ffmpeg ``-headers`` argument.

    ffmpeg wants CRLF-terminated ``Name: value`` pairs in a single argument.
    discord.py shlex-splits ``before_options``, so the surrounding quotes keep
    the pairs together and the embedded newlines reach ffmpeg intact. Passing
    the same headers yt-dlp used avoids googlevideo 403s mid-stream.
    """
    pairs = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return f'-headers "{pairs}"'

IDLE_LEAVE_SECONDS = 60
IDLE_CHECK_SECONDS = 10


def skip_threshold(listener_count: int) -> int:
    """Votes required to skip: a majority, at least 2 — or 1 when alone."""
    if listener_count <= 1:
        return 1
    return max(2, listener_count // 2 + 1)


def fmt_duration(seconds) -> str:
    if not seconds:
        return "∞"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class MusicPlayer:
    """Per-guild queue + playback state machine (voice injected for tests)."""

    def __init__(self, bot, guild_id, text_channel=None, *, audio_factory=None):
        self.bot = bot
        self.guild_id = guild_id
        self.text_channel = text_channel
        self.voice = None
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop = False
        self.volume = 0.5
        # The member who started this playback session — destructive controls
        # (stop/loop/volume/pause) are limited to them, staff, the requester of
        # the current track, or whoever is left alone in the voice channel.
        self.host_id: int | None = None
        self.now_playing_message = None
        self.now_playing_view = None
        # Set by the owning cog; used to rebuild the control view each time
        # the panel is (re)posted so buttons never go stale on an old message.
        self.now_playing_view_factory = None
        self._audio_factory = audio_factory
        self._watchdog_task: asyncio.Task | None = None
        self._last_active = time.monotonic()

    def _touch(self):
        """Note recent activity so the idle watchdog doesn't disconnect."""
        self._last_active = time.monotonic()

    def start_watchdog(self):
        """Ensure the idle-leave watchdog is running for this voice session."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self):
        """Leave the channel after IDLE_LEAVE_SECONDS with nothing queued/playing.

        Unlike a one-shot sleep task, this also covers joining without a
        playable track (e.g. a failed search) and survives track hand-offs.
        """
        try:
            while True:
                await asyncio.sleep(IDLE_CHECK_SECONDS)
                if self.voice is None or not self.voice.is_connected():
                    return
                if self.voice.is_playing() or self.voice.is_paused():
                    continue  # actively playing (or deliberately paused) — not idle
                if self.queue or self.current is not None:
                    continue  # something is waiting; don't interrupt it
                if time.monotonic() - self._last_active >= IDLE_LEAVE_SECONDS:
                    await self.voice.disconnect()
                    await self._update_panel(
                        stopped="Left the voice channel after being idle.")
                    return
        except asyncio.CancelledError:
            return

    async def _cancel_watchdog(self):
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    # ── queue / playback ────────────────────────────────────────
    async def enqueue(self, track: Track):
        """Add a track; start playback only when nothing else is busy.

        A paused track counts as busy — auto-starting over it would silently
        drop what the user paused.
        """
        self.queue.append(track)
        self._touch()
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            return False
        await self.play_next()
        return True

    async def play_next(self):
        """Start the next track — queue head, or loop the current one."""
        if self.voice is None or not self.voice.is_connected() or self.voice.is_playing():
            return
        if self.loop and self.current is not None:
            track = self.current
        elif self.queue:
            track = self.queue.popleft()
        else:
            track = None
        if track is None:
            self.current = None
            self._touch()  # count the idle window from when the last track ended
            return
        self.current = track
        self.current.votes.clear()
        source = self._make_source(track)
        self.voice.play(source, after=self._after_hook)
        await self._update_panel()

    def _make_source(self, track: Track):
        if self._audio_factory is not None:
            return self._audio_factory(track)
        kwargs = _ffmpeg_kwargs(track)
        audio = discord.FFmpegPCMAudio(
            track.local_path or track.url, **kwargs)
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    def _after_hook(self, error):
        if error:
            LOG.warning("Playback error: %s", error)
        future = asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            pass  # bot/loop shutting down mid-track
        except Exception:  # pragma: no cover - defensive
            LOG.exception("after-hook crashed")

    # ── control ─────────────────────────────────────────────────
    async def stop(self, label: str = "⏹️ Stopped."):
        await self._cancel_watchdog()
        self.queue.clear()
        self.current = None
        if self.voice and self.voice.is_playing():
            self.voice.stop()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect()
        await self._update_panel(stopped=label)

    async def toggle_loop(self):
        self.loop = not self.loop
        await self._update_panel()
        return self.loop

    async def set_volume(self, percent: float):
        self.volume = max(0.05, min(1.0, percent / 100))
        if self.voice and isinstance(getattr(self.voice, "source", None), discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume
        return self.volume

    def _listeners(self) -> int:
        if self.voice is None or self.voice.channel is None:
            return 0
        return sum(1 for member in self.voice.channel.members if not member.bot)

    async def vote_skip(self, author: discord.Member) -> tuple[bool, int, int]:
        """Request a skip. Returns (skipped_now, needed, votes_now).

        The requester and bot staff skip instantly; otherwise the track skips
        once a majority (>=2, or 1 when alone) of listeners votes.
        """
        if self.current is None:
            return False, 0, 0
        needed = skip_threshold(self._listeners())
        if author.id == self.current.requester_id or is_bot_admin(author):
            await self._skip_now()
            return True, needed, needed
        self.current.votes.add(author.id)
        votes = len(self.current.votes)
        if votes >= needed:
            await self._skip_now()
            return True, needed, votes
        return False, needed, votes

    async def _skip_now(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()  # play_next runs via the after-hook
        else:
            await self.play_next()

    # ── panel ───────────────────────────────────────────────────
    def embed(self, stopped: str = ""):
        embed = discord.Embed(color=discord.Color.red())
        if stopped:
            embed.title = stopped
            return embed
        track = self.current
        if track is None:
            embed.title = "🎵 Nothing playing."
            embed.description = "Use `/play <song>` to start something."
            return embed
        embed.title = f"🎵 {track.title}"
        embed.url = track.webpage_url or None
        desc = track.artist or ""
        if track.requester_id:
            desc = (desc + "\n" if desc else "") + f"Requested by <@{track.requester_id}>"
        embed.description = desc or None
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        embed.add_field(name="Duration", value=fmt_duration(track.duration), inline=True)
        embed.add_field(name="Loop", value="🔂 On" if self.loop else "Off", inline=True)
        if self.current and self.current.votes:
            votes = len(self.current.votes)
            embed.add_field(name="Skip votes",
                            value=f"{votes}/{skip_threshold(self._listeners())}", inline=True)
        if self.queue:
            lines = "\n".join(
                f"{i + 1}. **{t.title}**" for i, t in list(self.queue)[:6])
            more = f"\n*+{len(self.queue) - 6} more*" if len(self.queue) > 6 else ""
            embed.add_field(name=f"Up next ({len(self.queue)})", value=lines + more, inline=False)
        else:
            embed.add_field(name="Up next", value="—", inline=False)
        return embed

    async def _update_panel(self, stopped: str = ""):
        """(Re)post the now-playing panel so it stays the newest chat message.

        The old panel is deleted and a fresh copy — control view included —
        is sent to the player's text channel: no buttons ever sit on a buried
        message, and the panel is always one message the user can act on.
        ``stopped`` posts a plain notice without controls instead.
        """
        old = self.now_playing_message
        self.now_playing_message = None
        self.now_playing_view = None
        if old is not None:
            try:
                await old.delete()
            except (discord.HTTPException, discord.NotFound):
                pass
        if self.text_channel is None:
            return
        view = None
        if not stopped and self.now_playing_view_factory is not None:
            view = self.now_playing_view_factory()
        embed = self.embed(stopped=stopped)
        try:
            message = await self.text_channel.send(embed=embed, view=view)
        except (discord.HTTPException, discord.NotFound):
            return
        if not stopped:
            self.now_playing_message = message
            self.now_playing_view = view


class NowPlayingView(discord.ui.View):
    """Interactive panel: pause/resume, vote-skip, loop, stop, lyrics.

    Every action defers immediately, lets the player (re)post the panel as
    the newest message, then answers the user privately — the controls never
    edit a message that may have just been replaced.
    """

    def __init__(self, cog, player: MusicPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.voice is None or player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if not self.cog._can_control(interaction.user, player):
            await interaction.response.send_message(
                "🔒 Only the session host, the requester, or staff can control the player.",
                ephemeral=True)
            return
        if player.voice.is_paused():
            player.voice.resume()
            feedback = "▶️ Resumed"
        elif player.voice.is_playing():
            player.voice.pause()
            feedback = "⏸️ Paused"
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer()
        await player._update_panel()
        await interaction.followup.send(feedback, ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        await interaction.response.defer()
        skipped, needed, votes = await player.vote_skip(interaction.user)
        if not skipped:
            await player._update_panel()  # skipping reposts via the after-hook
        if skipped:
            await interaction.followup.send(
                f"⏭️ Skipped by {interaction.user.mention}.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⏭️ Skip vote {votes}/{needed}.", ephemeral=True)

    @discord.ui.button(emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._can_control(interaction.user, self.player):
            await interaction.response.send_message(
                "🔒 Only the session host, the requester, or staff can toggle loop.",
                ephemeral=True)
            return
        await interaction.response.defer()
        state = await self.player.toggle_loop()  # reposts the panel itself
        await interaction.followup.send(
            f"🔂 Loop {'on' if state else 'off'}.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._can_control(interaction.user, self.player):
            await interaction.response.send_message(
                "🔒 Only the session host, the requester, or staff can stop the player.",
                ephemeral=True)
            return
        await interaction.response.defer()
        await self.player.stop()
        await interaction.followup.send("⏹️ Stopped and left the channel.", ephemeral=True)

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.success, custom_id="music:lyrics")
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.cog._lyrics_for(
            player.current.title, player.current.artist)
        if result is None:
            await interaction.followup.send(
                "🎤 No lyrics found for the current track.", ephemeral=True)
            return
        lyrics, title, artist = result
        embed = discord.Embed(
            title=f"🎤 {artist} — {title}",
            description=lyrics[:3500],
            color=discord.Color.pink(),
        )
        embed.set_footer(text="via LRCLIB")
        await interaction.followup.send(embed=embed, ephemeral=True)


class Music(commands.Cog):
    """Queue music from any source — /play, /queue auto, /radio, /skip and more."""

    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self._http: aiohttp.ClientSession | None = None

    def _player(self, guild_id: int, text_channel=None) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(self.bot, guild_id, text_channel)
            player.now_playing_view_factory = lambda: NowPlayingView(self, player)
            self.players[guild_id] = player
        elif text_channel is not None:
            player.text_channel = text_channel
        return player

    def _cleanup(self, guild_id: int):
        player = self.players.pop(guild_id, None)
        if player is not None and player._watchdog_task is not None:
            player._watchdog_task.cancel()

    def _can_control(self, member: discord.Member, player: MusicPlayer) -> bool:
        """Who may use destructive controls on this guild's shared player.

        Bot staff, the session host, the current track's requester, or the
        only person left listening. Vote-skip stays democratic for everyone.
        """
        if is_bot_admin(member):
            return True
        if player.host_id is not None and member.id == player.host_id:
            return True
        if player.current is not None and player.current.requester_id == member.id:
            return True
        return player._listeners() <= 1

    async def cog_unload(self):
        for player in list(self.players.values()):
            if player.voice and player.voice.is_connected():
                await player.voice.disconnect()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ── source wiring ────────────────────────────────────────────
    @staticmethod
    def _track_from_playable(playable: Playable, requester_id: int) -> Track:
        """Map a provider-resolved Playable onto the playback Track type."""
        return Track(
            title=playable.title,
            url=playable.stream_url,
            webpage_url=playable.webpage_url,
            video_id=playable.video_id,
            duration=playable.duration,
            thumbnail=playable.thumbnail,
            artist=playable.artist,
            requester_id=requester_id,
            headers=playable.headers,
            local_path=playable.local_path,
        )

    async def _ensure_voice(self, ctx) -> MusicPlayer | None:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("🎧 Join a voice channel first.")
            return None
        channel = ctx.author.voice.channel
        player = self._player(ctx.guild.id, ctx.channel)
        existing = discord.utils.get(self.bot.voice_clients, guild=ctx.guild)
        if existing is not None and existing.is_connected():
            if existing.channel != channel:
                await ctx.send(f"⚠️ I'm already playing in {existing.channel.mention}.")
                return None
            player.voice = existing
            player.start_watchdog()
            player._touch()
            return player
        try:
            player.voice = await channel.connect()
        except (discord.Forbidden, discord.ClientException) as exc:
            await ctx.send(f"⛔ Couldn't join the channel: {exc}")
            return None
        player.start_watchdog()
        player._touch()
        return player

    # ── commands ────────────────────────────────────────────────
    @commands.hybrid_command(name="play", description="Play a song (search or URL).")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def play(self, ctx, *, query: str):
        """Stream the first playable source for the query, in provider order.

        While anything is busy (playing *or* paused) the song is only queued —
        playback is never interrupted by an extra `/play`.
        """
        # Slash interactions expire after ~3s; joining voice and resolving a
        # source can easily take longer, so acknowledge before any awaits.
        # On prefix invocations ctx.defer() is a no-op.
        await ctx.defer()
        player = await self._ensure_voice(ctx)
        if player is None:
            return
        try:
            playable = await resolve_playable(query)
        except AllSourcesFailed as exc:
            LOG.warning("No source could play %r: %s", query, exc)
            hint = exc.best_hint()
            await ctx.send(f"⚠️ {hint}" if hint
                           else "⚠️ Couldn't find anything playable for that query.")
            return
        track = Music._track_from_playable(playable, ctx.author.id)
        fresh = player.current is None and not player.queue
        started = await player.enqueue(track)
        if fresh:
            player.host_id = ctx.author.id
        if started:
            await ctx.send(f"🎵 Now playing: **{track.title}**")
        else:
            await ctx.send(
                f"➕ **{track.title}** added to the queue (#{len(player.queue)}).")
        # Re-post the controls panel last so it stays the newest message.
        await player._update_panel()

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    @commands.guild_only()
    async def pause(self, ctx):
        player = self._player(ctx.guild.id)
        if not player.voice or not player.voice.is_playing():
            await ctx.send("🎵 Nothing is playing to pause.")
            return
        if not self._can_control(ctx.author, player):
            await ctx.send("🔒 Only the session host, the current requester, or staff "
                           "can control the player.")
            return
        player.voice.pause()
        await player._update_panel()
        await ctx.send("⏸️ Paused.", delete_after=8)

    @commands.hybrid_command(name="resume", description="Resume the current track.")
    @commands.guild_only()
    async def resume(self, ctx):
        player = self._player(ctx.guild.id)
        if not player.voice or not player.voice.is_paused():
            await ctx.send("🎵 Nothing is paused.")
            return
        if not self._can_control(ctx.author, player):
            await ctx.send("🔒 Only the session host, the current requester, or staff "
                           "can control the player.")
            return
        player.voice.resume()
        await player._update_panel()
        await ctx.send("▶️ Resumed.", delete_after=8)

    @commands.hybrid_command(name="skip", description="Vote to skip the current track.")
    @commands.guild_only()
    async def skip(self, ctx):
        """Requester/staff skip instantly; everyone else needs a majority vote."""
        player = self._player(ctx.guild.id)
        if player.voice is None or player.current is None:
            await ctx.send("🎵 Nothing is playing to skip.")
            return
        skipped, needed, votes = await player.vote_skip(ctx.author)
        if not skipped:
            await player._update_panel()  # skipping reposts via the play hook
        if skipped:
            await ctx.send("⏭️ Skipped.")
        else:
            await ctx.send(f"⏭️ Skip vote {votes}/{needed}.")

    @commands.hybrid_command(name="stop", description="Stop playback and leave the channel.")
    @commands.guild_only()
    async def stop(self, ctx):
        player = self._player(ctx.guild.id)
        if player.voice is None or not player.voice.is_connected():
            await ctx.send("🎵 I'm not in a voice channel.")
            return
        if not self._can_control(ctx.author, player):
            await ctx.send("🔒 Only the session host, the current requester, or staff "
                           "can stop the player.")
            return
        await player.stop()
        await ctx.send("⏹️ Stopped and left.")

    @commands.hybrid_command(name="loop", description="Toggle repeat for the current track.")
    @commands.guild_only()
    async def loop(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None or player.now_playing_message is None:
            await ctx.send("🎵 Queue a song first (`/play`).")
            return
        if not self._can_control(ctx.author, player):
            await ctx.send("🔒 Only the session host, the current requester, or staff "
                           "can toggle loop.")
            return
        state = await player.toggle_loop()
        await ctx.send(f"🔂 Loop {'on' if state else 'off'}.")

    @commands.hybrid_command(name="volume", description="Set playback volume (1–100).")
    @commands.guild_only()
    async def volume(self, ctx, percent: int):
        player = self._player(ctx.guild.id)
        if not self._can_control(ctx.author, player):
            await ctx.send("🔒 Only the session host, the current requester, or staff "
                           "can change the volume.")
            return
        level = await player.set_volume(max(1, min(percent, 100)))
        await ctx.send(f"🔊 Volume set to {round(level * 100)}%.")

    @commands.hybrid_command(name="queue",
                             description="Show the queue — or pass `auto` to generate a radio queue.")
    @commands.guild_only()
    async def queue(self, ctx, mode: str | None = None):
        """Show the queue, or generate a 📻 radio queue from the current track.

        `!queue auto` (or `/queue auto`) pulls a related-tracks radio for the
        song that's currently playing and adds it to the queue.
        """
        if mode is not None and mode.strip().lower() in ("auto", "radio", "seed"):
            await self._queue_auto(ctx, self._player(ctx.guild.id))
            return
        player = self._player(ctx.guild.id)
        if player.current is None and not player.queue:
            await ctx.send("🎵 The queue is empty. Add songs with `/play`.")
            return
        await ctx.send(embed=player.embed())

    async def _queue_auto(self, ctx, player: MusicPlayer, size: int = 8):
        """Generate a radio queue from the currently playing track."""
        if player.voice is None or not player.voice.is_connected():
            await ctx.send("🎧 I need to be in a voice channel first — use `/play`.")
            return
        if player.current is None or not player.current.video_id:
            await ctx.send("🎵 Play something first, then run `/queue auto` "
                           "to generate a 📻 radio queue.")
            return
        await ctx.defer()
        current = player.current
        try:
            seed_ids = await asyncio.to_thread(
                youtube_provider.radio_seed_ids, current.video_id, size)
        except Exception as exc:
            LOG.warning("Auto-queue seed failed for %r: %s", current.title, exc)
            await ctx.send("⚠️ Couldn't generate a radio for the current track.")
            return
        if not seed_ids:
            await ctx.send("⚠️ No related tracks found for the current track.")
            return
        await ctx.send(f"📻 Building a radio queue from **{current.title}**…")
        playables = await youtube_provider.resolve_parallel(seed_ids)
        if not playables:
            await ctx.send("⚠️ Couldn't resolve any of the radio tracks.")
            return
        for playable in playables:
            await player.enqueue(
                Music._track_from_playable(playable, ctx.author.id))
        await player._update_panel()
        await ctx.send(
            f"➕ **{len(playables)}** songs from the radio of **{current.title}** "
            f"added to the queue.")

    @commands.hybrid_command(name="nowplaying", description="Show the current track.")
    @commands.guild_only()
    async def nowplaying(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None:
            await ctx.send("🎵 Nothing is playing.")
            return
        if player.now_playing_message is None:
            await self._send_panel(ctx, player)
        else:
            await player._update_panel()
            await ctx.send(embed=player.embed())

    @commands.hybrid_command(name="radio",
                             description="Play a public internet radio station.")
    @commands.guild_only()
    async def radio(self, ctx, station: str = "groovesalad"):
        """Stream a public radio station — no search involved."""
        await ctx.defer()
        player = await self._ensure_voice(ctx)
        if player is None:
            return
        try:
            playable = radio_provider.resolve(station)
        except SourceUnavailable as exc:
            await ctx.send(f"⚠️ {exc.message}")
            return
        track = Music._track_from_playable(playable, ctx.author.id)
        fresh = player.current is None and not player.queue
        started = await player.enqueue(track)
        if fresh:
            player.host_id = ctx.author.id
        if started:
            await ctx.send(f"🎵 Now playing: **{track.title}**")
        else:
            await ctx.send(f"➕ **{track.title}** added to the queue.")
        await player._update_panel()  # keep the controls panel as the newest message

    # ── lyrics (LRCLIB) ─────────────────────────────────────────
    async def _lyrics_for(self, title: str, artist: str):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": USER_AGENT},
            )
        query = f"{artist} {title}".strip() or title
        try:
            async with self._http.get(
                    "https://lrclib.net/api/search", params={"q": query}) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return None
        if not data:
            return None
        track = data[0]
        text = track.get("plainLyrics") or track.get("syncedLyrics")
        if not text:
            return None
        return text, track.get("trackName") or title, track.get("artistName") or artist

    # ── lifecycle ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id != self.bot.user.id:
            return
        if after.channel is None:
            self._cleanup(member.guild.id)


async def setup(bot):
    await bot.add_cog(Music(bot))