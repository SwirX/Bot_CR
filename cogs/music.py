"""Music streaming for Bot_CR (yt-dlp + ffmpeg).

Joins the author's voice channel and streams the best audio source from YouTube
(a query is searched, a URL is used directly), with a queue, majority vote-skip,
per-track loop, volume control and an interactive now-playing panel that also
looks up lyrics on LRCLIB.

The playback state machine lives in :class:`MusicPlayer` and is deliberately
voice-independent where possible (``voice`` is injected), so the queue / vote /
loop math is unit-testable without a real Discord voice connection.
"""

import asyncio
import concurrent.futures
import logging
import re
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised at deploy time
    yt_dlp = None

from cogs._perms import is_bot_admin

LOG = logging.getLogger("bot.music")

USER_AGENT = "Bot_CR/1.0 (robotics-club Discord bot; contact: server staff)"

URL_RE = re.compile(r"^https?://", re.I)

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

IDLE_LEAVE_SECONDS = 60


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


@dataclass
class Track:
    """One queued song. Votes reset when the track starts playing."""

    title: str
    url: str
    webpage_url: str = ""
    duration: int | None = None
    thumbnail: str = ""
    artist: str = ""
    requester_id: int | None = None
    votes: set = field(default_factory=set)


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
        self.now_playing_message = None
        self.now_playing_view = None
        self._audio_factory = audio_factory
        self._leave_task: asyncio.Task | None = None

    # ── queue / playback ────────────────────────────────────────
    async def enqueue(self, track: Track):
        """Add a track; start playback immediately if nothing is playing."""
        self.queue.append(track)
        if self.voice and self.voice.is_playing():
            return False
        await self.play_next()
        return True

    async def play_next(self):
        """Start the next track — queue head, or loop the current one."""
        if self.voice is None or not self.voice.is_connected() or self.voice.is_playing():
            return
        await self._cancel_leave()
        if self.loop and self.current is not None:
            track = self.current
        elif self.queue:
            track = self.queue.popleft()
        else:
            track = None
        if track is None:
            await self._idle_and_leave()
            return
        self.current = track
        self.current.votes.clear()
        source = self._make_source(track)
        self.voice.play(source, after=self._after_hook)
        await self._update_panel()

    def _make_source(self, track: Track):
        if self._audio_factory is not None:
            return self._audio_factory(track)
        audio = discord.FFmpegPCMAudio(
            track.url,
            before_options=FFMPEG_BEFORE,
            options="-vn",
        )
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

    async def _idle_and_leave(self):
        """Queue empty — sit tight briefly, then leave if nobody queues more."""
        self.current = None
        if self._leave_task is None:
            self._leave_task = asyncio.create_task(self._leave_soon())

    async def _leave_soon(self):
        await asyncio.sleep(IDLE_LEAVE_SECONDS)
        if self.voice and self.voice.is_connected() and not self.voice.is_playing():
            self.current = None
            await self.voice.disconnect()
            await self._update_panel(stopped="Queue finished — left the voice channel.")

    async def _cancel_leave(self):
        if self._leave_task is not None:
            self._leave_task.cancel()
            self._leave_task = None

    # ── control ─────────────────────────────────────────────────
    async def stop(self, label: str = "⏹️ Stopped."):
        await self._cancel_leave()
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
        message = self.now_playing_message
        if message is None:
            return
        view = self.now_playing_view
        embed = self.embed(stopped=stopped)
        try:
            await message.edit(embed=embed, view=view if not stopped else None)
        except discord.NotFound:
            self.now_playing_message = None
            self.now_playing_view = None


class NowPlayingView(discord.ui.View):
    """Interactive panel: pause/resume, vote-skip, loop, stop, lyrics."""

    def __init__(self, cog, player: MusicPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player

    async def _reaction(self, interaction: discord.Interaction, feedback: str):
        await interaction.response.edit_message(
            embed=self.player.embed(), view=self.player.now_playing_view or self)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.voice is None or player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
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
        await self._reaction(interaction, feedback)
        await interaction.followup.send(feedback, ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        skipped, needed, votes = await player.vote_skip(interaction.user)
        await self._reaction(interaction, "")
        if skipped:
            await interaction.followup.send(
                f"⏭️ Skipped by {interaction.user.mention}.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⏭️ Skip vote {votes}/{needed}.", ephemeral=True)

    @discord.ui.button(emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self.player.toggle_loop()
        await self._reaction(interaction, "")
        await interaction.followup.send(
            f"🔂 Loop {'on' if state else 'off'}.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.stop()
        try:
            await interaction.response.edit_message(
                embed=self.player.embed("⏹️ Stopped."), view=None)
        except discord.NotFound:
            await interaction.response.send_message("⏹️ Stopped.", ephemeral=True)

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
    """Queue music from YouTube with yt-dlp — /play, /skip, /queue and more."""

    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self._http: aiohttp.ClientSession | None = None

    def _player(self, guild_id: int, text_channel=None) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(self.bot, guild_id, text_channel)
            self.players[guild_id] = player
        elif text_channel is not None:
            player.text_channel = text_channel
        return player

    def _cleanup(self, guild_id: int):
        player = self.players.pop(guild_id, None)
        if player is not None and player._leave_task is not None:
            player._leave_task.cancel()

    async def cog_unload(self):
        for player in list(self.players.values()):
            if player.voice and player.voice.is_connected():
                await player.voice.disconnect()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ── lookup helpers ──────────────────────────────────────────
    @staticmethod
    def _extract_audio(query: str) -> Track:
        """Blocking yt-dlp search/extract — run via asyncio.to_thread."""
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        target = query if URL_RE.match(query) else f"ytsearch1:{query}"
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            info = ydl.extract_info(target, download=False)
        if info.get("entries"):
            info = info["entries"][0]
        if not info or not info.get("url"):
            raise RuntimeError("no playable audio source found")
        return Track(
            title=info.get("title") or query,
            url=info["url"],
            webpage_url=info.get("webpage_url") or info.get("original_url") or "",
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail") or "",
            artist=info.get("artist") or info.get("channel") or info.get("uploader") or "",
        )

    async def _search(self, query: str) -> Track:
        return await asyncio.to_thread(self._extract_audio, query)

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
            return player
        try:
            player.voice = await channel.connect()
        except (discord.Forbidden, discord.ClientException) as exc:
            await ctx.send(f"⛔ Couldn't join the channel: {exc}")
            return None
        return player

    async def _send_panel(self, ctx, player: MusicPlayer):
        view = NowPlayingView(self, player)
        player.now_playing_view = view
        player.now_playing_message = await ctx.send(embed=player.embed(), view=view)

    # ── commands ────────────────────────────────────────────────
    @commands.hybrid_command(name="play", description="Play a song (YouTube search or URL).")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def play(self, ctx, *, query: str):
        """Search the top YouTube result, or stream a direct audio URL."""
        # Slash interactions expire after ~3s; joining voice and searching
        # YouTube can easily take longer, so acknowledge before any awaits.
        # On prefix invocations ctx.defer() is a no-op.
        await ctx.defer()
        player = await self._ensure_voice(ctx)
        if player is None:
            return
        async with ctx.typing():
            try:
                track = await self._search(query)
            except Exception as exc:
                LOG.warning("Search failed for %r: %s", query, exc)
                await ctx.send("⚠️ Couldn't find something playable for that query.")
                return
        track.requester_id = ctx.author.id
        started = await player.enqueue(track)
        if started and player.now_playing_message is None:
            await self._send_panel(ctx, player)
        elif not started:
            await ctx.send(f"➕ **{track.title}** added to the queue (#{len(player.queue)}).")

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    @commands.guild_only()
    async def pause(self, ctx):
        player = self._player(ctx.guild.id)
        if not player.voice or not player.voice.is_playing():
            await ctx.send("🎵 Nothing is playing to pause.")
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
        await player._update_panel()
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
        await player.stop()
        await ctx.send("⏹️ Stopped and left.")

    @commands.hybrid_command(name="loop", description="Toggle repeat for the current track.")
    @commands.guild_only()
    async def loop(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None or player.now_playing_message is None:
            await ctx.send("🎵 Queue a song first (`/play`).")
            return
        state = await player.toggle_loop()
        await ctx.send(f"🔂 Loop {'on' if state else 'off'}.")

    @commands.hybrid_command(name="volume", description="Set playback volume (1–100).")
    @commands.guild_only()
    async def volume(self, ctx, percent: int):
        player = self._player(ctx.guild.id)
        level = await player.set_volume(max(1, min(percent, 100)))
        await ctx.send(f"🔊 Volume set to {round(level * 100)}%.")

    @commands.hybrid_command(name="queue", description="Show the current queue.")
    @commands.guild_only()
    async def queue(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None and not player.queue:
            await ctx.send("🎵 The queue is empty. Add songs with `/play`.")
            return
        await ctx.send(embed=player.embed())

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