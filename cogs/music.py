"""Music streaming for Bot_CR (multi-source: YouTube, Audius, internet radio).

Joins the author's voice channel and streams the first playable source for a
query (YouTube via yt-dlp first, Audius as the automatic keyless fallback,
public radio stations on request), with a queue, a ``/queue auto`` radio feed
that refills itself around the last played song and keeps sessions going,
majority vote-skip with a deadline tie-break, per-track loop, volume control
and an interactive now-playing panel that also looks up lyrics on LRCLIB.
The bot auto-disconnects after being idle for a bit.

The playback state machine lives in :class:`MusicPlayer` and is deliberately
voice-independent where possible (``voice`` is injected), so the queue / vote /
loop math is unit-testable without a real Discord voice connection.
"""

import asyncio
import concurrent.futures
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

from cogs._perms import is_bot_admin
from cogs.music_sources import deezer as deezer_provider
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
    provider: str = ""
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
RADIO_REFILL_AT = 6         # top the queue back up when fewer than this many songs wait
RADIO_REFILL_SIZE = 8       # songs to add per refill
RADIO_REFILL_FETCH = 12     # fetch a little extra so history-dedup still finds fresh songs
RADIO_REFILL_COOLDOWN = 30  # minimum seconds between refills
VOTE_SECONDS = 20           # how long a skip/remove vote stays open


@dataclass
class _Vote:
    """An open skip/remove motion: who wants it, who wants it kept."""

    kind: str            # "skip" or "remove"
    target_key: str      # dedup key of the queued track for remove motions
    target_title: str    # human-readable name for messages
    yes: set = field(default_factory=set)
    no: set = field(default_factory=set)
    deadline: float = 0.0
    task: asyncio.Task | None = None


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

    def __init__(self, bot, guild_id, text_channel=None, *,
                 audio_factory=None, radio_fetcher=None, track_factory=None):
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
        # Auto-radio feed wiring injected by the owning cog: how to fetch
        # related playables for a seed, and how to map one onto a Track.
        self.radio_fetcher = radio_fetcher
        self.track_factory = track_factory
        # Continuous radio mode: `/queue auto` turns it on and the player
        # keeps topping the queue up from the last played song.
        self.auto_radio = False
        self._radio_history: deque[str] = deque(maxlen=400)
        self._refill_task: asyncio.Task | None = None
        self._last_refill_at = 0.0
        self._watchdog_task: asyncio.Task | None = None
        self._last_active = time.monotonic()
        # True when the player chose to leave (stop / idle / shutdown) rather
        # than being removed by a moderator — suppresses the "kicked" notice.
        self._intentional_leave = False
        # One open skip/remove motion at a time (new votes replace old ones).
        self._vote: _Vote | None = None

    def _touch(self):
        """Note recent activity so the idle watchdog doesn't disconnect."""
        self._last_active = time.monotonic()

    # ── auto-radio ────────────────────────────────────────────
    @staticmethod
    def _key_of(track: Track) -> str:
        """Stable identity for radio dedup (Deezer track URL >> yt id >> stream)."""
        return track.webpage_url or track.video_id or track.url or ""

    def note_radio_play(self, track: Track) -> None:
        """Remember a radio-fed track so auto-refills never replay it."""
        key = self._key_of(track)
        if key:
            self._radio_history.append(key)

    def _queue_keys(self) -> set[str]:
        keys = {self._key_of(self.current)} if self.current is not None else set()
        keys.update(self._key_of(t) for t in self.queue)
        return keys

    def _maybe_refill(self):
        """Start a radio refill when the queue is running low — one at a time."""
        if not self.auto_radio or self.current is None:
            return
        if self._refill_task is not None and not self._refill_task.done():
            return
        if len(self.queue) >= RADIO_REFILL_AT:
            return
        if time.monotonic() - self._last_refill_at < RADIO_REFILL_COOLDOWN:
            return
        self._refill_task = asyncio.create_task(self.refill_radio())

    async def refill_radio(self) -> int:
        """Pull a fresh radio batch around the last played song and announce it.

        Returns how many songs were added. A radio feed is a small fixed pool,
        so anything this session already heard — plus the current queue — is
        filtered out before enqueueing; otherwise refills would just replay.
        """
        seed = self.current
        self._last_refill_at = time.monotonic()
        if self.radio_fetcher is None or seed is None:
            return 0
        try:
            playables = await self.radio_fetcher(seed, RADIO_REFILL_FETCH)
        except Exception as exc:
            LOG.warning("Auto-radio refill failed for %r: %s", seed.title, exc)
            playables = []
        heard = set(self._radio_history) | self._queue_keys()
        fresh = [p for p in playables
                 if (p.webpage_url or p.video_id or p.stream_url or "") not in heard]
        fresh = fresh[:RADIO_REFILL_SIZE]
        requester = seed.requester_id or 0
        for playable in fresh:
            self.queue.append(self.track_factory(playable, requester))
        self._touch()
        await self._announce_refill(seed, len(fresh))
        return len(fresh)

    async def _announce_refill(self, seed: Track, added: int) -> None:
        if self.text_channel is None:
            return
        if added:
            text = (f"📻 +{added} songs from the radio of **{seed.title}** — "
                    "auto-radio keeps the queue topped up.")
        elif not self.queue and self.current is seed:
            text = (f"📻 The radio of **{seed.title}** is out of fresh songs and "
                    "the queue is empty — try `/play` for something specific.")
        else:
            return  # nothing changed, nothing to say
        try:
            await self.text_channel.send(content=text)
        except (discord.HTTPException, discord.NotFound):
            pass

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
                    place = (self.voice.channel.name if self.voice.channel
                             else "the voice channel")
                    self._intentional_leave = True
                    await self.voice.disconnect()
                    await self._update_panel(stopped=(
                        f"😴 Quiet for a minute, so I dipped out of **{place}**. "
                        "/play and I'll be right back!"))
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
        self.cancel_vote()  # a new track invalidates any pending motions
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
        if self.auto_radio:
            self.note_radio_play(track)
        source = self._make_source(track)
        self.voice.play(source, after=self._after_hook)
        await self._announce_current()
        await self._update_panel()
        self._maybe_refill()

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
    async def stop(self):
        """Stop playback, empty the queue, leave — and remove the panel.

        Callers send their own confirmation, so /stop and the panel button
        never double-post a message in a different style.
        """
        await self._cancel_watchdog()
        self.cancel_vote()
        self.auto_radio = False
        self._radio_history.clear()
        if self._refill_task is not None:
            self._refill_task.cancel()
        self.queue.clear()
        self.current = None
        if self.voice and self.voice.is_playing():
            self.voice.stop()
        if self.voice and self.voice.is_connected():
            self._intentional_leave = True
            await self.voice.disconnect()
        old = self.now_playing_message
        self.now_playing_message = None
        self.now_playing_view = None
        if old is not None:
            try:
                await old.delete()
            except (discord.HTTPException, discord.NotFound):
                pass

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

    def _motion_owner(self, kind: str, target_key: str) -> int | None:
        """Who owns the thing a motion targets — their word is instant."""
        if kind == "skip":
            return self.current.requester_id if self.current else None
        for track in self.queue:
            if self._key_of(track) == target_key:
                return track.requester_id
        return None

    def cancel_vote(self):
        """Drop any open motion and its deadline task."""
        if self._vote is not None:
            if self._vote.task is not None:
                self._vote.task.cancel()
            self._vote = None

    async def cast_vote(self, kind: str, author: discord.Member, *,
                        want: bool = True, target_key: str = "",
                        target_title: str = "") -> tuple[str, int, int]:
        """Vote on a skip/remove motion. Returns (status, yes, no).

        Status: "passed" — the motion fired; "vote" — recorded, still open;
        "kept" — the owner vetoed so the motion is cancelled; "idle" — no
        listeners, nothing to act on, or a "keep" with no open motion.

        The requester of the thing being voted on, the session host and staff
        act instantly. Everyone else votes: the motion passes right away once
        the yes side outnumbers the no side and holds a majority of the
        listeners present, and at the deadline unless a strict majority voted
        no — so one troll can't stalemate the vote forever.
        """
        listeners = self._listeners()
        if listeners <= 0:
            return "idle", 0, 0
        owner = self._motion_owner(kind, target_key)
        if owner is not None and (author.id in (owner, self.host_id)
                                  or is_bot_admin(author)):
            if want:
                await self._apply_motion(kind, target_key)
                return "passed", 0, 0
            self.cancel_vote()
            return "kept", 0, 0
        vote = self._vote
        if vote is None or vote.kind != kind or vote.target_key != target_key:
            if not want:
                return "idle", 0, 0
            self.cancel_vote()
            vote = _Vote(kind=kind, target_key=target_key,
                         target_title=target_title)
            vote.deadline = time.monotonic() + VOTE_SECONDS
            vote.task = asyncio.create_task(self._close_vote(vote))
            self._vote = vote
        if author.id in vote.yes or author.id in vote.no:
            return "vote", len(vote.yes), len(vote.no)
        (vote.yes if want else vote.no).add(author.id)
        yes, no = len(vote.yes), len(vote.no)
        if yes > no and (yes + no == listeners or yes * 2 > listeners):
            await self._apply_motion(kind, target_key)
            return "passed", yes, no
        return "vote", yes, no

    async def _close_vote(self, vote: _Vote):
        """Resolve a vote when its window closes: ties and silence pass it."""
        await asyncio.sleep(VOTE_SECONDS)
        if self._vote is not vote:
            return  # a newer motion replaced it, or the track ended
        yes, no = len(vote.yes), len(vote.no)
        if yes and no * 2 <= self._listeners():
            await self._apply_motion(vote.kind, vote.target_key)
            return
        self._vote = None
        await self._update_panel()  # clear the stale vote display
        await self._announce_action(
            f"🗳️ The vote didn't pass — **{vote.target_title or 'the song'}** stays.")

    async def _apply_motion(self, kind: str, target_key: str):
        """Carry out a passed motion and cancel the vote."""
        self.cancel_vote()
        if kind == "skip" and self.current is not None:
            title = self.current.title
            await self._skip_now()
            await self._announce_action(f"⏭️ Skipped **{title}**.")
        elif kind == "remove":
            removed = self.remove_from_queue(target_key)
            if removed is not None:
                await self._update_panel()
                await self._announce_action(
                    f"➖ Removed **{removed.title}** from the queue.")

    def remove_from_queue(self, target_key: str) -> Track | None:
        """Drop the first queued track matching ``target_key``; the rest shift up."""
        for index, track in enumerate(self.queue):
            if self._key_of(track) == target_key:
                del self.queue[index]
                return track
        return None

    async def _announce_action(self, text: str) -> None:
        """One public line in the music channel (outcome messages only)."""
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(content=text)
        except (discord.HTTPException, discord.NotFound):
            pass

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
        if self._vote is not None:
            vote = self._vote
            seconds = max(1, int(vote.deadline - time.monotonic()))
            embed.add_field(
                name="🗳️ Vote to skip" if vote.kind == "skip" else "🗳️ Vote to remove",
                value=f"👍 {len(vote.yes)} · 👎 {len(vote.no)} · {seconds}s to decide",
                inline=True)
        if self.queue:
            lines = "\n".join(
                f"{i + 1}. **{t.title}**" for i, t in enumerate(list(self.queue)[:6]))
            more = f"\n*+{len(self.queue) - 6} more*" if len(self.queue) > 6 else ""
            embed.add_field(name=f"Up next ({len(self.queue)})", value=lines + more, inline=False)
        else:
            embed.add_field(name="Up next", value="—", inline=False)
        return embed

    async def _announce_current(self) -> None:
        """One chat line announcing the track that just started."""
        if self.text_channel is None or self.current is None:
            return
        try:
            await self.text_channel.send(
                content=f"🎵 Now playing: **{self.current.title}**")
        except discord.HTTPException:
            pass

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
        status, yes, no = await player.cast_vote("skip", interaction.user)
        if status == "vote":
            await player._update_panel()  # show the live vote on the panel
            await interaction.followup.send(
                f"🗳️ Skip vote — 👍 {yes} · 👎 {no}. Use `/keep` to vote against.",
                ephemeral=True)
        else:
            # pass/kept/idle: the motion itself already posted its outcome
            # (and the after-hook reposted the panel when it skipped).
            word = "⏭️ Skipped by" if status == "passed" else "⏹️ No action"
            await interaction.followup.send(
                f"{word} {interaction.user.mention}.", ephemeral=True)

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
        # Public line in the music channel — not an ephemeral reply tied to a
        # panel message that is about to be deleted.
        await self.player._announce_action("⏹️ Stopped and left the channel.")

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.success, custom_id="music:lyrics")
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if player.current.provider == "radio":
            await interaction.response.send_message(
                "📻 This is a live radio stream — it has no lyrics.", ephemeral=True)
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
            player = MusicPlayer(
                self.bot, guild_id, text_channel,
                radio_fetcher=self.fetch_radio,
                track_factory=Music._track_from_playable)
            player.now_playing_view_factory = lambda: NowPlayingView(self, player)
            self.players[guild_id] = player
        elif text_channel is not None:
            player.text_channel = text_channel
        return player

    def _cleanup(self, guild_id: int):
        player = self.players.pop(guild_id, None)
        if player is None:
            return
        if player._watchdog_task is not None:
            player._watchdog_task.cancel()
        if player._refill_task is not None:
            player._refill_task.cancel()

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
            player._intentional_leave = True
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
            provider=playable.provider,
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
        if not started:
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

    @commands.hybrid_command(name="skip",
                             description="Skip the current track — instant if you requested it, else a vote.")
    @commands.guild_only()
    async def skip(self, ctx):
        """Requester/host/staff skip instantly; everyone else votes."""
        player = self._player(ctx.guild.id)
        if player.voice is None or player.current is None:
            await ctx.send("🎵 Nothing is playing to skip.")
            return
        status, yes, no = await player.cast_vote("skip", ctx.author)
        if status == "passed":
            return  # the motion already announced the outcome in chat
        if status == "vote":
            await player._update_panel()  # show the live vote
            await ctx.send(
                f"🗳️ Skip vote — 👍 {yes} · 👎 {no}. Use `/keep` to vote against.")
        else:
            await ctx.send("🎵 Nothing to skip right now.")

    @commands.hybrid_command(name="remove",
                             description="Remove a queued song by number — instant if you added it, else a vote.")
    @commands.guild_only()
    async def remove(self, ctx, position: int):
        """Drop queue entry #position; its requester removes it instantly."""
        player = self._player(ctx.guild.id)
        if not player.queue:
            await ctx.send("🎵 The queue is empty.")
            return
        if not 1 <= position <= len(player.queue):
            await ctx.send(f"🎵 #**{position}** isn't in the queue — "
                           "`/queue` shows the numbers.")
            return
        target = player.queue[position - 1]
        status, yes, no = await player.cast_vote(
            "remove", ctx.author, target_key=MusicPlayer._key_of(target),
            target_title=target.title)
        if status == "passed":
            return  # the motion already announced the removal in chat
        if status == "vote":
            await player._update_panel()  # show the live vote
            await ctx.send(
                f"🗳️ Vote to remove **{target.title}**: 👍 {yes} · 👎 {no} — "
                "`/keep` to keep it.")
        else:
            await ctx.send("🗳️ Cannot vote on that right now.")

    @commands.hybrid_command(name="keep",
                             description="Vote to keep the song while a skip/remove vote is open.")
    @commands.guild_only()
    async def keep(self, ctx):
        """Vote against an open skip/remove motion (the requester vetoes)."""
        player = self._player(ctx.guild.id)
        vote = player._vote
        if vote is None:
            await ctx.send("🗳️ No vote is open right now.")
            return
        status, yes, no = await player.cast_vote(
            vote.kind, ctx.author, want=False,
            target_key=vote.target_key, target_title=vote.target_title)
        if status == "kept":
            await player._update_panel()
            await ctx.send("✋ Kept — you're the requester of this one.")
        elif status == "vote":
            await player._update_panel()  # show the live vote
            await ctx.send(f"✋ Noted — 👍 {yes} · 👎 {no}.")
        else:
            await ctx.send("🗳️ Nothing to vote on right now.")

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

        `!queue auto` (or `/queue auto`) turns the 📻 auto-radio on: the queue
        is topped up from the last played song's radio whenever it runs low.
        """
        if mode is not None and mode.strip().lower() in ("auto", "radio", "seed"):
            await self._queue_auto(ctx, self._player(ctx.guild.id, ctx.channel))
            return
        player = self._player(ctx.guild.id)
        if player.current is None and not player.queue:
            await ctx.send("🎵 The queue is empty. Add songs with `/play`.")
            return
        await ctx.send(embed=player.embed())

    async def fetch_radio(self, seed: Track, size: int) -> list[Playable]:
        """Radio playables around a seed: Deezer artist-radio first, YTMusic
        as the fallback for YouTube-sourced seeds (they don't run from the
        datacenter IP, so Deezer is the real feed while an ARL is set)."""
        playables: list[Playable] = []
        if os.environ.get("DEEZER_ARL"):
            try:
                playables = await deezer_provider.radio_tracks(seed.title, size)
            except Exception as exc:
                LOG.warning("Auto-radio Deezer feed failed for %r: %s",
                            seed.title, exc)
                playables = []
        if not playables and seed.video_id:
            try:
                seed_ids = await asyncio.to_thread(
                    youtube_provider.radio_seed_ids, seed.video_id, size)
                if seed_ids:
                    playables = await youtube_provider.resolve_parallel(seed_ids)
            except Exception as exc:
                LOG.warning("Auto-radio seed failed for %r: %s", seed.title, exc)
                playables = []
        return playables

    async def _queue_auto(self, ctx, player: MusicPlayer):
        """Switch on continuous auto-radio and top the queue up immediately.

        One-shot batches die after a few songs. With auto-radio on, the queue
        is refilled around the *last played* song whenever it runs low, every
        refill is announced, and tracks this session already heard are never
        re-served.
        """
        if player.voice is None or not player.voice.is_connected():
            await ctx.send("🎧 I need to be in a voice channel first — use `/play`.")
            return
        if player.current is None:
            await ctx.send("🎵 Play something first, then run `/queue auto` "
                           "to generate a 📻 radio queue.")
            return
        await ctx.defer()
        player.auto_radio = True
        player.start_watchdog()
        added = await player.refill_radio()
        if added == 0 and player.queue:
            await ctx.send("📻 Couldn't add fresh radio songs right now — "
                           "the queue keeps what it has.")

    @commands.hybrid_command(name="nowplaying", description="Show the current track.")
    @commands.guild_only()
    async def nowplaying(self, ctx):
        player = self._player(ctx.guild.id, ctx.channel)
        if player.current is None:
            await ctx.send("🎵 Nothing is playing.")
            return
        await player._update_panel()

    @commands.hybrid_command(name="radio",
                             description="Search and play any live internet radio station by name.")
    @commands.guild_only()
    async def radio(self, ctx, station: str = "groovesalad"):
        """Search the world radio directory by name and start streaming."""
        await ctx.defer()
        player = await self._ensure_voice(ctx)
        if player is None:
            return
        try:
            playable = await radio_provider.resolve_station(station)
        except SourceUnavailable as exc:
            await ctx.send(f"⚠️ {exc.message}")
            return
        track = Music._track_from_playable(playable, ctx.author.id)
        fresh = player.current is None and not player.queue
        started = await player.enqueue(track)
        if fresh:
            player.host_id = ctx.author.id
        if not started:
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
        if after.channel is None and before.channel is not None:
            player = self.players.get(member.guild.id)
            if player is not None and not player._intentional_leave:
                await self._report_forced_leave(player, before.channel)
            self._cleanup(member.guild.id)

    async def _report_forced_leave(self, player: MusicPlayer, channel) -> None:
        """One honest line in the music channel when the bot is removed from
        voice against its will (kicked / yanked out of the channel)."""
        if player.text_channel is None:
            return
        try:
            await player.text_channel.send(
                content=f"👢 I was yanked out of **{channel.name}** — /play and "
                        "I'll be right back!")
        except (discord.HTTPException, discord.NotFound):
            pass


async def setup(bot):
    await bot.add_cog(Music(bot))