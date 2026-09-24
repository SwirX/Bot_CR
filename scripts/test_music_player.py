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

    async def delete(self):
        self.deleted = True


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
    def test_update_panel_deletes_old_and_reposts(self):
        player = MusicPlayer(bot=object(), guild_id=1)
        old = _FakeMessage()
        player.now_playing_message = old
        player.text_channel = _FakeTextChannel()
        asyncio.run(player._update_panel())
        self.assertTrue(old.deleted)  # the old panel is removed...
        self.assertEqual(len(player.text_channel.sent), 1)  # ...and a fresh one
        self.assertIsNotNone(player.now_playing_message)    # is now the live ref

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
        contents = [s.get("content") for s in player.text_channel.sent
                    if "content" in s]
        self.assertTrue(any("Now playing" in c and "Song A" in c for c in contents))

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
        embeds = [s["embed"] for s in player.text_channel.sent if "embed" in s]
        self.assertEqual(len(embeds), 1)
        self.assertIn("Quiet", embeds[0].title)
        self.assertIn("Lounge", embeds[0].title)


class VoteTests(unittest.TestCase):
    def _track(self, title, requester_id, key=""):
        return Track(title=title, url=f"u-{title}", requester_id=requester_id,
                     webpage_url=key)

    def test_requester_skips_instantly(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Mine", 11)
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(11)))
        self.assertEqual(status, "passed")
        self.assertIsNone(player._vote)
        self.assertIsNone(player.current)  # skipped into an empty queue

    def test_non_requester_opens_a_vote(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Theirs", 33)
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(11)))
        self.assertEqual(status, "vote")
        self.assertEqual((yes, no), (1, 0))
        self.assertIsNotNone(player._vote)
        self.assertEqual(player.current.title, "Theirs")  # still playing

    def test_split_vote_stays_pending_in_a_duo(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Theirs", 33)
        asyncio.run(player.cast_vote("skip", _FakeMember(11)))
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(22), want=False))
        self.assertEqual(status, "vote")
        self.assertEqual((yes, no), (1, 1))  # 1v1 — no instant majority

    def test_tie_resolves_toward_skip_at_the_deadline(self):
        """The troll deadlock: 1 yes vs 1 no must not hang forever."""
        import cogs.music as music_module
        old = music_module.VOTE_SECONDS
        music_module.VOTE_SECONDS = 3600  # keep the spawned closer quiet
        try:
            player, _ = _room_player(11, 22)
            player.text_channel = _FakeTextChannel()
            player.current = self._track("Theirs", 33)
            asyncio.run(player.cast_vote("skip", _FakeMember(11)))
            asyncio.run(player.cast_vote("skip", _FakeMember(22), want=False))
            vote = player._vote
            self.assertIsNotNone(vote)
            music_module.VOTE_SECONDS = 0.01
            asyncio.run(player._close_vote(vote))
        finally:
            music_module.VOTE_SECONDS = old
        self.assertIsNone(player._vote)  # the motion went through
        self.assertIsNone(player.current)

    def test_full_turnout_passes_in_a_duo(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Theirs", 33)
        asyncio.run(player.cast_vote("skip", _FakeMember(11)))
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(22)))
        self.assertEqual(status, "passed")
        self.assertIsNone(player._vote)

    def test_majority_of_listeners_passes_instantly(self):
        player, _ = _room_player(11, 22, 33, 44)
        player.current = self._track("Theirs", 99)
        asyncio.run(player.cast_vote("skip", _FakeMember(11)))
        asyncio.run(player.cast_vote("skip", _FakeMember(22)))
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(33)))
        self.assertEqual(status, "passed")  # 3 yes > half of 4 listeners
        self.assertIsNone(player._vote)

    def test_requester_removes_their_own_queue_entry_instantly(self):
        player, _ = _room_player(11, 22)
        key = "https://www.deezer.com/track/5"
        player.queue.append(self._track("Mine2", 11, key))
        status, yes, no = asyncio.run(player.cast_vote(
            "remove", _FakeMember(11), target_key=key, target_title="Mine2"))
        self.assertEqual(status, "passed")
        self.assertEqual(list(player.queue), [])

    def test_removing_someone_elses_song_opens_a_vote(self):
        player, _ = _room_player(11, 22)
        key = "https://www.deezer.com/track/5"
        player.queue.append(self._track("Theirs2", 33, key))
        status, yes, no = asyncio.run(player.cast_vote(
            "remove", _FakeMember(22), target_key=key, target_title="Theirs2"))
        self.assertEqual(status, "vote")
        self.assertEqual(len(player.queue), 1)
        self.assertEqual(player._vote.kind, "remove")

    def test_requester_veto_cancels_a_pending_vote(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Theirs", 33)
        asyncio.run(player.cast_vote("skip", _FakeMember(11)))
        self.assertIsNotNone(player._vote)
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(33), want=False))
        self.assertEqual(status, "kept")
        self.assertIsNone(player._vote)
        self.assertEqual(player.current.title, "Theirs")

    def test_keep_with_no_open_vote_is_idle(self):
        player, _ = _room_player(11)
        player.current = self._track("Mine", 11)
        status, yes, no = asyncio.run(
            player.cast_vote("skip", _FakeMember(22), want=False))
        self.assertEqual(status, "idle")

    def test_a_new_track_clears_pending_motions(self):
        player, _ = _room_player(11, 22)
        player.current = self._track("Theirs", 33)
        asyncio.run(player.cast_vote("skip", _FakeMember(11)))
        self.assertIsNotNone(player._vote)
        asyncio.run(player.play_next())
        self.assertIsNone(player._vote)


if __name__ == "__main__":
    unittest.main(verbosity=2)