"""Unit checks for the music source registry + model (no network).

Run:  .venv-local/bin/python scripts/test_music_sources.py
Hermetic: providers are faked; nothing touches YouTube, Audius or Discord.
"""

import asyncio
import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cogs.music_sources as sources  # noqa: E402
from cogs.music_sources import radio  # noqa: E402
from cogs.music_sources.model import (  # noqa: E402
    AllSourcesFailed, Playable, SourceFailure, SourceUnavailable,
)


class PlayableTests(unittest.TestCase):
    def test_playable_exposes_stream_fields(self):
        playable = Playable(provider="audius", title="t", stream_url="https://x/audio")
        self.assertEqual(playable.provider, "audius")
        self.assertEqual(playable.headers, {})

    def test_playable_is_immutable(self):
        playable = Playable(provider="youtube", title="t", stream_url="u")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            playable.title = "other"  # type: ignore[misc]


class FailureModelTests(unittest.TestCase):
    def test_source_unavailable_carries_provider_and_code(self):
        exc = SourceUnavailable("youtube", "blocked", "cookies needed")
        self.assertEqual(exc.provider, "youtube")
        self.assertEqual(exc.reason_code, "blocked")

    def test_best_hint_prefers_blocked_over_plain_missing(self):
        failures = [
            SourceFailure("youtube", "blocked", "cookies needed"),
            SourceFailure("audius", "not_found", "no tracks"),
        ]
        self.assertEqual(AllSourcesFailed(failures).best_hint(), "cookies needed")

    def test_best_hint_falls_back_to_last_message(self):
        failures = [SourceFailure("audius", "not_found", "no tracks")]
        self.assertEqual(AllSourcesFailed(failures).best_hint(), "no tracks")

    def test_best_hint_empty_without_failures(self):
        self.assertEqual(AllSourcesFailed([]).best_hint(), "")


class RegistryTests(unittest.TestCase):
    """Ordered fallback logic exercised with fake async providers."""

    def setUp(self):
        self._saved = list(sources._PROVIDERS)

    def tearDown(self):
        sources._PROVIDERS = self._saved

    def _set_providers(self, *providers):
        sources._PROVIDERS = providers

    def test_first_success_wins(self):
        async def first(query):
            return Playable(provider="first", title="a", stream_url="u1")

        async def second(query):
            raise AssertionError("second provider must not run")

        self._set_providers(first, second)
        result = asyncio.run(sources.resolve_playable("q"))
        self.assertEqual(result.provider, "first")

    def test_blocked_provider_falls_through_to_next(self):
        async def first(query):
            raise SourceUnavailable("first", "blocked", "cookies")

        async def second(query):
            return Playable(provider="second", title="b", stream_url="u2")

        self._set_providers(first, second)
        result = asyncio.run(sources.resolve_playable("q"))
        self.assertEqual(result.provider, "second")

    def test_unexpected_exception_recorded_and_falls_through(self):
        async def first(query):
            raise ValueError("boom")

        async def second(query):
            return Playable(provider="second", title="b", stream_url="u2")

        self._set_providers(first, second)
        result = asyncio.run(sources.resolve_playable("q"))
        self.assertEqual(result.provider, "second")

    def test_all_fail_raises_with_failures_in_provider_order(self):
        async def first(query):
            raise SourceUnavailable("first", "blocked", "cookies")

        async def second(query):
            raise SourceUnavailable("second", "not_found", "empty")

        self._set_providers(first, second)
        with self.assertRaises(AllSourcesFailed) as ctx:
            asyncio.run(sources.resolve_playable("q"))
        self.assertEqual([f.provider for f in ctx.exception.failures],
                         ["first", "second"])


class RadioTests(unittest.TestCase):
    def test_known_station_resolves(self):
        playable = radio.resolve("GrooveSalad")
        self.assertEqual(playable.provider, "radio")
        self.assertIn("somafm.com", playable.stream_url)

    def test_unknown_station_raises(self):
        with self.assertRaises(SourceUnavailable):
            radio.resolve("nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)