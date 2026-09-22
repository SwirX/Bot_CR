"""Hermetic checks for the MusicPlayer state machine + controls panel.

Run:  .venv-local/bin/python scripts/test_music_player.py
No network, no Discord voice — the VoiceClient is faked and playback is
observed through which methods the fake records.
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.music import _ffmpeg_kwargs, Music, MusicPlayer, Track  # noqa: E402
from cogs.music_sources.model import Playable  # noqa: E402


class _FakeMessage:
    def __init__(self):
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _FakeTextChannel:
    """Records sends so tests can watch the panel lifecycle."""

    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeMessage()


class _FakeVoice:
    """Records the calls the player makes instead of touching Discord."""

    def __init__(self, playing=False, paused=False, connected=True):
        self._playing = playing
        self._paused = paused
        self._connected = connected
        self.started: list = []
        self.stopped = 0

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def is_connected(self):
        return self._connected

    def play(self, source, after=None):
        self.started.append(source)
        self._playing = True
        self._paused = False

    def pause(self):
        self._playing = False
        self._paused = True

    def resume(self):
        self._playing = True
        self._paused = False

    def stop(self):
        self.stopped += 1
        self._playing = False

    async def disconnect(self):
        self._connected = False


class _BotUser:
    id = 42


class _FakeBot:
    def __init__(self):
        self.user = _BotUser()


class _FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class _GuildPermissions:
    administrator = False


class _FakeMember:
    def __init__(self, member_id, *, bot=False, guild=None):
        self.id = member_id
        self.bot = bot
        self.guild = guild or _FakeGuild(1)
        self.guild_permissions = _GuildPermissions()
        self.roles = []


class _FakeChannel:
    def __init__(self, name, members=None):
        self.name = name
        self.members = members or []


class _FakeState:
    def __init__(self, channel):
        self.channel = channel


class _DoneTask:
    """A task that has already finished — used to prove no refill is spawned."""

    def done(self):
        return True

    def cancel(self):
        pass


def _deezer_playable(track_id: int, title: str = "Song") -> Playable:
    return Playable(
        provider="deezer",
        title=f"{title} {track_id}",
        stream_url=f"https://cdns.example/u{track_id}.mp3",
        webpage_url=f"https://www.deezer.com/track/{track_id}",
    )


def _async_feed(items):
    async def _feed(seed, size):
        return items
    return _feed


def _room_player(*member_ids, bot_ids=()):
    """A player wired to a fake voice channel with the given human listeners."""
    player = MusicPlayer(bot=object(), guild_id=1)
    voice = _FakeVoice(playing=False)
    voice.channel = _FakeChannel(
        "Lounge",
        members=[_FakeMember(i) for i in member_ids]
        + [_FakeMember(i, bot=True) for i in bot_ids])
    player.voice = voice
    return player, voice.channel


class FfmpegKwagTests(unittest.TestCase):
    def test_local_file_gets_no_reconnect_options(self):
        """Regression: -reconnect on a local file makes ffmpeg exit 8."""
        track = Track(title="t", url="http://cdn/x", local_path="/tmp/botcr-deezer/t.mp3")
        self.assertNotIn("before_options", _ffmpeg_kwargs(track))
        self.assertEqual(_ffmpeg_kwargs(track)["options"], "-vn")

    def test_url_keeps_reconnect_and_headers(self):
        track = Track(title="t", url="https://googlevideo/x", headers={"A": "b"})
        kwargs = _ffmpeg_kwargs(track)
        self.assertIn("-reconnect", kwargs["before_options"])
        self.assertIn("-headers", kwargs["before_options"])


class EnqueueTests(unittest.TestCase):
    def _player(self, voice):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.voice = voice
        return player

    def test_enqueue_while_playing_only_queues(self):
        voice = _FakeVoice(playing=True)
        player = self._player(voice)
        player.current = Track(title="A", url="u1")
        started = asyncio.run(player.enqueue(Track(title="B", url="u2")))
        self.assertFalse(started)
        self.assertEqual(list(player.queue), [Track(title="B", url="u2")])
        self.assertEqual(player.current.title, "A")
        self.assertEqual(voice.started, [])  # never interrupted

    def test_enqueue_while_paused_only_queues(self):
        voice = _FakeVoice(playing=False, paused=True)
        player = self._player(voice)
        player.current = Track(title="A", url="u1")
        started = asyncio.run(player.enqueue(Track(title="B", url="u2")))
        self.assertFalse(started)
        self.assertEqual(player.current.title, "A")
        self.assertEqual(voice.started, [])

    def test_enqueue_while_idle_starts_immediately(self):
        voice = _FakeVoice(playing=False, paused=False)
        player = self._player(voice)
        started = asyncio.run(player.enqueue(Track(title="B", url="u2")))
        self.assertTrue(started)
        self.assertEqual(player.current.title, "B")
        self.assertEqual(len(voice.started), 1)


class PanelTests(unittest.TestCase):
    def test_update_panel_edits_in_place_instead_of_reposting(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        old = _FakeMessage()
        player.now_playing_message = old
        player.text_channel = _FakeTextChannel()
        asyncio.run(player._update_panel())
        # The old message is *edited*, not deleted+reposted — the whole point
        # of the anti-churn change.
        self.assertFalse(old.deleted)
        self.assertEqual(len(old.edits), 1)
        self.assertEqual(len(player.text_channel.sent), 0)
        self.assertIs(player.now_playing_message, old)

    def test_update_panel_reposts_only_when_the_old_one_is_gone(self):
        import discord as _discord

        class _NotFoundBody:
            status = 404
            reason = "Not Found"

        class _GoneMessage(_FakeMessage):
            async def edit(self, **kwargs):
                raise _discord.NotFound(response=_NotFoundBody(), message="gone")

        player = MusicPlayer(bot=object(), guild_id=1)
        player.now_playing_message = _GoneMessage()
        player.text_channel = _FakeTextChannel()
        asyncio.run(player._update_panel())
        self.assertEqual(len(player.text_channel.sent), 1)
        self.assertIsNotNone(player.now_playing_message)

    def test_update_panel_with_factory_rebuilds_controls(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.text_channel = _FakeTextChannel()
        made = []
        player.now_playing_view_factory = lambda: made.append(1) or object()
        asyncio.run(player._update_panel())
        self.assertEqual(len(made), 1)
        self.assertIsNotNone(player.text_channel.sent[0].get("view"))

    def test_stopped_panel_clears_live_ref_and_view(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        old = _FakeMessage()
        player.now_playing_message = old
        player.now_playing_view = object()
        player.text_channel = _FakeTextChannel()
        asyncio.run(player._update_panel(stopped="⏹️ Stopped."))
        self.assertTrue(old.deleted)
        self.assertIsNone(player.now_playing_message)
        self.assertIsNone(player.now_playing_view)
        self.assertEqual(len(player.text_channel.sent), 1)
        self.assertIsNone(player.text_channel.sent[0].get("view"))

    def test_embed_renders_non_empty_queue(self):
        """Regression: unpacking Tracks without enumerate crashed every panel
        repost and the /queue embed while songs were queued."""
        player = MusicPlayer(bot=object(), guild_id=1)
        player.current = Track(title="Current", url="u")
        player.queue.append(Track(title="Next one", url="u1"))
        player.queue.append(Track(title="Next two", url="u2"))
        fields = player.embed().to_dict()["fields"]
        self.assertEqual(fields[-1]["name"], "Up next (2)")
        self.assertIn("1. **Next one**", fields[-1]["value"])
        self.assertIn("2. **Next two**", fields[-1]["value"])

    def test_play_next_announces_the_new_track(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.voice = _FakeVoice(playing=False)
        player.text_channel = _FakeTextChannel()
        player._audio_factory = lambda track: "source"
        player.queue.append(Track(title="Song A", url="u"))
        asyncio.run(player.play_next())
        self.assertEqual(player.current.title, "Song A")
        # The panel replaces the old standalone chat line — exactly one
        # embed, no separate "Now playing: X" content line (that double
        # message was one of the complaints).
        embeds = [s["embed"] for s in player.text_channel.sent if "embed" in s]
        self.assertEqual(len(embeds), 1)
        self.assertIn("Song A", embeds[0].title)
        contents = [s.get("content") for s in player.text_channel.sent
                    if "content" in s]
        self.assertFalse(any(c and "Now playing" in c for c in contents))

    def test_stop_clears_panel_and_never_reposts(self):
        """/stop must produce exactly one message — the caller's own."""
        player = MusicPlayer(bot=object(), guild_id=1)
        old = _FakeMessage()
        player.now_playing_message = old
        player.text_channel = _FakeTextChannel()
        player.queue.append(Track(title="t", url="u"))
        asyncio.run(player.stop())
        self.assertTrue(old.deleted)
        self.assertEqual(len(player.text_channel.sent), 0)
        self.assertIsNone(player.current)
        self.assertEqual(list(player.queue), [])


class AutoRadioTests(unittest.TestCase):
    def _radio_player(self, text_channel=True):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.auto_radio = True
        player.track_factory = Music._track_from_playable
        if text_channel:
            player.text_channel = _FakeTextChannel()
        return player

    def test_refill_radio_adds_fresh_tracks_and_skips_history(self):
        player = self._radio_player()
        player.current = Track(title="Taste", url="s",
                               webpage_url="https://www.deezer.com/track/1",
                               requester_id=9)
        player.radio_fetcher = _async_feed([
            _deezer_playable(2), _deezer_playable(3), _deezer_playable(4)])
        player._radio_history.append("https://www.deezer.com/track/3")
        added = asyncio.run(player.refill_radio())
        self.assertEqual(added, 2)
        keys = [t.webpage_url for t in player.queue]
        self.assertNotIn("https://www.deezer.com/track/3", keys)
        contents = [s["content"] for s in player.text_channel.sent
                    if "content" in s]
        self.assertTrue(any("+2" in c and "Taste" in c for c in contents))

    def test_refill_radio_adds_nothing_to_an_already_queued_pool(self):
        player = self._radio_player()
        player.current = Track(title="Taste", url="s",
                               webpage_url="https://www.deezer.com/track/1")
        player.queue.append(Track(title="Waiting", url="w",
                                  webpage_url="https://www.deezer.com/track/2"))
        player.radio_fetcher = _async_feed([_deezer_playable(2)])
        added = asyncio.run(player.refill_radio())
        self.assertEqual(added, 0)
        self.assertEqual(player.text_channel.sent, [])  # nothing changed → silent

    def test_refill_radio_warns_when_dry_and_queue_is_empty(self):
        player = self._radio_player()
        player.current = Track(title="Taste", url="s",
                               webpage_url="https://www.deezer.com/track/1")
        player.radio_fetcher = _async_feed([_deezer_playable(2)])
        player._radio_history.append("https://www.deezer.com/track/2")
        added = asyncio.run(player.refill_radio())
        self.assertEqual(added, 0)
        contents = [s["content"] for s in player.text_channel.sent
                    if "content" in s]
        self.assertTrue(any("out of fresh" in c for c in contents))

    def test_auto_radio_records_every_played_track(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.voice = _FakeVoice(playing=False)
        player.auto_radio = True
        player._audio_factory = lambda track: "source"
        player.queue.append(Track(
            title="R1", url="u",
            webpage_url="https://www.deezer.com/track/77"))
        asyncio.run(player.play_next())
        self.assertIn("https://www.deezer.com/track/77", player._radio_history)

    def test_maybe_refill_respects_threshold_and_single_flight(self):
        import cogs.music as music_module
        old_at, old_cd = (music_module.RADIO_REFILL_AT,
                          music_module.RADIO_REFILL_COOLDOWN)
        music_module.RADIO_REFILL_AT = 3
        music_module.RADIO_REFILL_COOLDOWN = 0
        try:
            async def scenario():
                player = self._radio_player(text_channel=False)
                player.current = Track(title="s", url="u")
                player.radio_fetcher = _async_feed([])
                player._maybe_refill()  # queue 0 < 3 → spawns a refill
                first = player._refill_task
                self.assertIsNotNone(first)
                await asyncio.sleep(0.02)
                self.assertTrue(first.done())
                for i in range(4):
                    player.queue.append(Track(title=f"q{i}", url=f"u{i}"))
                player._refill_task = _DoneTask()
                player._maybe_refill()  # queue 4 >= 3 → no spawn
                self.assertIsInstance(player._refill_task, _DoneTask)
            asyncio.run(scenario())
        finally:
            music_module.RADIO_REFILL_AT = old_at
            music_module.RADIO_REFILL_COOLDOWN = old_cd

    def test_maybe_refill_respects_cooldown(self):
        import cogs.music as music_module
        old_at, old_cd = (music_module.RADIO_REFILL_AT,
                          music_module.RADIO_REFILL_COOLDOWN)
        music_module.RADIO_REFILL_AT = 3
        music_module.RADIO_REFILL_COOLDOWN = 3600
        try:
            async def scenario():
                player = self._radio_player(text_channel=False)
                player.current = Track(title="s", url="u")
                player._refill_task = _DoneTask()
                player._last_refill_at = time.monotonic()  # refilled just now
                player._maybe_refill()
                self.assertIsInstance(player._refill_task, _DoneTask)
            asyncio.run(scenario())
        finally:
            music_module.RADIO_REFILL_AT = old_at
            music_module.RADIO_REFILL_COOLDOWN = old_cd

    def test_stop_cancels_auto_radio(self):
        player = self._radio_player(text_channel=False)
        player.current = Track(title="s", url="u")
        player._radio_history.append("k")
        player.queue.append(Track(title="q", url="u"))
        cancelled = []
        player._refill_task = _DoneTask()
        player._refill_task.cancel = lambda: cancelled.append(1)
        old = _FakeMessage()
        player.now_playing_message = old
        asyncio.run(player.stop())
        self.assertFalse(player.auto_radio)
        self.assertEqual(list(player._radio_history), [])
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(old.deleted)


class VoiceLifecycleTests(unittest.TestCase):
    def _kick_cog(self, player, guild_id=7):
        cog = Music(bot=_FakeBot())
        cog.players[guild_id] = player
        return cog

    def test_voice_kick_posts_notice_in_music_channel(self):
        player = MusicPlayer(bot=object(), guild_id=7)
        player.text_channel = _FakeTextChannel()
        cog = self._kick_cog(player)
        member = _FakeMember(42, guild=_FakeGuild(7))
        asyncio.run(cog.on_voice_state_update(
            member, _FakeState(_FakeChannel("Rock Room")), _FakeState(None)))
        contents = [s.get("content") for s in player.text_channel.sent
                    if "content" in s]
        self.assertTrue(any("yanked" in c and "Rock Room" in c for c in contents))
        self.assertNotIn(7, cog.players)

    def test_intentional_leave_silences_kick_notice(self):
        player = MusicPlayer(bot=object(), guild_id=7)
        player.text_channel = _FakeTextChannel()
        player._intentional_leave = True
        cog = self._kick_cog(player)
        member = _FakeMember(42, guild=_FakeGuild(7))
        asyncio.run(cog.on_voice_state_update(
            member, _FakeState(_FakeChannel("Rock Room")), _FakeState(None)))
        self.assertEqual(player.text_channel.sent, [])
        self.assertNotIn(7, cog.players)

    def test_idle_watchdog_leaves_with_crafted_message(self):
        import cogs.music as music_module
        old_check, old_leave = (music_module.IDLE_CHECK_SECONDS,
                                music_module.IDLE_LEAVE_SECONDS)
        music_module.IDLE_CHECK_SECONDS = 0.01
        music_module.IDLE_LEAVE_SECONDS = 0.01
        try:
            player = MusicPlayer(bot=object(), guild_id=1)
            voice = _FakeVoice(playing=False, paused=False)
            voice.channel = _FakeChannel("Lounge")
            player.voice = voice
            player.text_channel = _FakeTextChannel()
            asyncio.run(player._watchdog())
        finally:
            music_module.IDLE_CHECK_SECONDS = old_check
            music_module.IDLE_LEAVE_SECONDS = old_leave
        self.assertTrue(player._intentional_leave)
        self.assertFalse(voice.is_connected())
        # The stopped text now goes out as a plain content line, not an embed.
        contents = [s.get("content") for s in player.text_channel.sent
                    if s.get("content") is not None]
        self.assertTrue(any("Quiet" in c and "Lounge" in c for c in contents))


class QueueControlTests(unittest.TestCase):
    """The simplified model: skip is open, remove is ownership."""

    def _track(self, title, requester_id, key=""):
        return Track(title=title, url=f"u-{title}", requester_id=requester_id,
                     webpage_url=key)

    def test_skip_now_advances_the_queue(self):
        player, _ = _room_player(11, 22)
        player.voice = _FakeVoice(playing=True, paused=False)
        player.text_channel = _FakeTextChannel()
        player._audio_factory = lambda track: "source"
        player.current = self._track("Theirs", 33)
        player.queue.append(self._track("Next", 44))
        asyncio.run(player._skip_now())
        # _skip_now stops the voice; play_next is normally driven by the
        # after-hook. With the fake voice we call play_next directly to
        # inspect the outcome.
        asyncio.run(player.play_next())
        self.assertEqual(player.current.title, "Next")
        self.assertEqual(len(player.queue), 0)

    def test_remove_from_queue_drops_the_named_track(self):
        player, _ = _room_player(11, 22)
        key = "https://www.deezer.com/track/5"
        player.queue.append(self._track("Mine2", 11, key))
        player.queue.append(self._track("Other", 33, "https://www.deezer.com/track/6"))
        removed = player.remove_from_queue(key)
        self.assertEqual(removed.title, "Mine2")
        self.assertEqual([t.title for t in player.queue], ["Other"])

    def test_remove_from_queue_is_a_noop_for_unknown_keys(self):
        player, _ = _room_player(11)
        player.queue.append(self._track("Mine2", 11, "https://x/1"))
        self.assertIsNone(player.remove_from_queue("https://x/other"))
        self.assertEqual(len(player.queue), 1)

    def test_vote_machinery_is_gone(self):
        """The old /keep-a-skip-vote surface must not come back silently."""
        import cogs.music as music_module
        self.assertFalse(hasattr(MusicPlayer, "cast_vote"))
        self.assertFalse(hasattr(MusicPlayer, "cancel_vote"))
        self.assertFalse(hasattr(music_module, "VOTE_SECONDS"))
        self.assertFalse(hasattr(music_module, "_Vote"))
        # The cog side: no `keep` command registered.
        self.assertFalse(hasattr(music_module.Music, "keep"))



class PanelClockTests(unittest.TestCase):
    """The live progress bar math and the pause freeze."""

    def test_elapsed_advances_while_playing(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.current = Track(title="t", url="u", duration=200)
        player._track_started_at = time.monotonic() - 30
        self.assertAlmostEqual(player.track_elapsed(), 30, delta=2)

    def test_pause_freezes_the_clock_at_that_point(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        voice = _FakeVoice(playing=True, paused=False)
        player.voice = voice
        player.current = Track(title="t", url="u", duration=200)
        player._track_started_at = time.monotonic() - 30
        player.pause_voice()
        self.assertAlmostEqual(player.track_elapsed(), 30, delta=2)
        # …and it does not advance while paused.
        frozen = player.track_elapsed()
        time.sleep(0.05)
        self.assertEqual(player.track_elapsed(), frozen)

    def test_resume_continues_from_the_frozen_point(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        voice = _FakeVoice(playing=False, paused=True)
        player.voice = voice
        player.current = Track(title="t", url="u", duration=200)
        player._paused_elapsed = 30.0
        player.resume_voice()
        self.assertAlmostEqual(player.track_elapsed(), 30, delta=2)
        self.assertIsNone(player._paused_elapsed)

    def test_elapsed_is_none_without_a_current_track(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        self.assertIsNone(player.track_elapsed())


class ProgressBarTests(unittest.TestCase):
    def test_half_full_on_a_known_duration(self):
        import cogs.music as m
        bar = m.progress_bar(60, 120, slots=10)
        self.assertEqual(bar.count("█"), 5)
        self.assertEqual(bar.count("░"), 5)

    def test_full_at_the_end_and_beyond(self):
        import cogs.music as m
        self.assertEqual(m.progress_bar(120, 120, slots=4), "████")
        self.assertEqual(m.progress_bar(999, 120, slots=4), "████")

    def test_empty_at_the_start(self):
        import cogs.music as m
        self.assertEqual(m.progress_bar(0, 120, slots=4), "░░░░")

    def test_unknown_duration_shows_motion_not_a_fake_zero(self):
        import cogs.music as m
        bar = m.progress_bar(10, None, slots=6)
        self.assertEqual(bar.count("█"), 1)
        self.assertEqual(len(bar), 6)
        self.assertNotEqual(bar, m.progress_bar(0, None, slots=6))

    def test_embed_shows_the_bar_and_status(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.current = Track(title="Song", url="u", duration=120)
        player._track_started_at = time.monotonic() - 30
        player._paused_elapsed = None
        embed = player.embed()
        progress = next(f for f in embed.fields if f.name == "Progress")
        self.assertIn("█", progress.value)
        self.assertIn("Playing", progress.value)
        self.assertIn("0:30", progress.value)
        self.assertIn("2:00", progress.value)


class LinkNudgeTests(unittest.TestCase):
    """After a member's third *started* track they get the link-parse tip."""

    def _player(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        player.text_channel = _FakeTextChannel()
        return player

    def _start(self, player, title, requester_id):
        player.current = Track(title=title, url=f"u-{title}",
                               requester_id=requester_id, webpage_url=f"k-{title}")
        asyncio.run(player._count_and_maybe_link_nudge(player.current))

    def sent_texts(self, player):
        return [s.get("content") for s in player.text_channel.sent
                if s.get("content") is not None]

    def test_no_nudge_before_three_tracks(self):
        player = self._player()
        self._start(player, "A", 7)
        self._start(player, "B", 7)
        self.assertEqual(self.sent_texts(player), [])

    def test_third_track_delivers_the_tip(self):
        player = self._player()
        for t in ("A", "B", "C"):
            self._start(player, t, 7)
        texts = self.sent_texts(player)
        self.assertEqual(len(texts), 1)
        self.assertIn("YouTube or Deezer link", texts[0])
        self.assertIn("<@7>", texts[0])

    def test_each_member_gets_it_once(self):
        player = self._player()
        for t in ("A", "B", "C", "D", "E", "F"):
            self._start(player, t, 7)
        self.assertEqual(len(self.sent_texts(player)), 1)  # once, not six

    def test_loop_replay_is_not_a_new_play(self):
        player = self._player()
        for t in ("A", "B", "C"):
            self._start(player, t, 7)
        # Loop mode starts the same track again: no second nudge, no count bump.
        same = player.current
        asyncio.run(player._count_and_maybe_link_nudge(same))
        self.assertEqual(len(self.sent_texts(player)), 1)
        self.assertEqual(player._play_counts[7], 3)

    def test_another_member_counts_independently(self):
        player = self._player()
        for t in ("A", "B", "C"):
            self._start(player, t, 7)   # 7 has seen the tip
        for t in ("D", "E", "F"):
            self._start(player, t, 9)   # 9 has not
        texts = self.sent_texts(player)
        self.assertEqual(len(texts), 2)
        self.assertIn("<@9>", texts[1])

    def test_radio_tracks_without_a_requester_are_not_counted(self):
        player = self._player()
        self._start(player, "radio", 0)
        self._start(player, "radio2", None)
        self.assertEqual(player._play_counts, {})

if __name__ == "__main__":
    unittest.main(verbosity=2)
