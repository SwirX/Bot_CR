"""Hermetic checks for the MusicPlayer state machine + controls panel.

Run:  .venv-local/bin/python scripts/test_music_player.py
No network, no Discord voice — the VoiceClient is faked and playback is
observed through which methods the fake records.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.music import _ffmpeg_kwargs, MusicPlayer, Track  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)