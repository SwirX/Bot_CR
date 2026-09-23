"""Unit checks for the music source registry + model (no network).

Run:  .venv-local/bin/python scripts/test_music_sources.py
Hermetic: providers are faked; nothing touches YouTube, Audius or Discord.
"""

import asyncio
import dataclasses
import hashlib
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


class DeezerDrmTests(unittest.TestCase):
    """Decryption mechanics, exercised without any network or ARL."""

    def test_blowfish_key_matches_documented_derivation(self):
        from cogs.music_sources.deezer_gw import _BF_SECRET, blowfish_key
        digest = hashlib.md5(b"31337").hexdigest().encode("ascii")
        expected = bytes(digest[i] ^ digest[i + 16] ^ _BF_SECRET[i]
                         for i in range(16))
        self.assertEqual(blowfish_key(31337), expected)

    def test_decrypt_restores_striped_audio(self):
        from Crypto.Cipher import Blowfish
        from cogs.music_sources.deezer_gw import (
            _BF_IV, CHUNK_BYTES, blowfish_key, decrypt_chunks,
        )

        async def _aiter(items):
            for item in items:
                yield item

        track_id = 7
        key = blowfish_key(track_id)
        full_chunk = bytes(range(256)) * 8  # 256 * 8 == 2048
        plain = [full_chunk for _ in range(7)]
        striped = []
        for index, chunk in enumerate(plain):
            if index % 3 == 0:
                cipher = Blowfish.new(key, Blowfish.MODE_CBC, _BF_IV)
                striped.append(cipher.encrypt(chunk))
            else:
                striped.append(chunk)
        striped.append(b"\x00trailing-partial-chunk")

        async def _decrypt_all():
            return [c async for c in decrypt_chunks(_aiter(striped), track_id)]

        self.assertEqual(
            b"".join(asyncio.run(_decrypt_all())),
            b"".join(plain) + b"\x00trailing-partial-chunk")

    def test_unknown_track_id_changes_key(self):
        from cogs.music_sources.deezer_gw import blowfish_key
        self.assertNotEqual(blowfish_key(1), blowfish_key(2))

    def test_decrypt_stream_survives_transport_chunk_sizes(self):
        """Stripe alignment must survive aiohttp's arbitrary read sizes.

        Regression for the mushy/stuttering playback: iter_chunked yields
        whatever the TCP buffer holds (1442, 1510, ... byte parcels), which
        used to drift the ``index % 3`` stripe and scramble everything past
        the first short chunk — the rest of the track decoded as muffled
        error-concealed garbage.
        """
        from Crypto.Cipher import Blowfish
        from cogs.music_sources.deezer_gw import (
            _BF_IV, CHUNK_BYTES, blowfish_key, decrypt_stream,
        )

        class _SlicingReader:
            """Mimic aiohttp.StreamReader.read(): at most n bytes at a time."""

            def __init__(self, data: bytes, sizes: list[int]):
                self._data = data
                self._sizes = sizes
                self._pos = 0

            async def read(self, n: int) -> bytes:
                if self._pos >= len(self._data):
                    return b""
                take = (max(1, min(n, self._sizes.pop(0)))
                        if self._sizes else n)
                out = self._data[self._pos:self._pos + take]
                self._pos += len(out)
                return out

        track_id = 7
        key = blowfish_key(track_id)
        full_chunk = bytes(range(256)) * 8  # 256 * 8 == 2048
        plain = b"".join(full_chunk for _ in range(7)) + b"\x00trailing"
        striped = bytearray()
        for index in range(7):
            if index % 3 == 0:
                cipher = Blowfish.new(key, Blowfish.MODE_CBC, _BF_IV)
                striped += cipher.encrypt(full_chunk)
            else:
                striped += full_chunk
        striped += b"\x00trailing"
        # Hostile mix: tiny, partial and bursty parcel sizes.
        reader = _SlicingReader(
            bytes(striped), [1, 2047, 1024, 3, 2045, 512, 1536, 2048, 7, 17])

        async def _decrypt_all():
            return b"".join(
                [c async for c in decrypt_stream(reader, track_id)])

        self.assertEqual(asyncio.run(_decrypt_all()), plain)


class DeezerGuardTests(unittest.TestCase):
    def test_missing_arl_disables_provider_without_network(self):
        from cogs.music_sources import deezer
        saved = os.environ.get("DEEZER_ARL")
        os.environ.pop("DEEZER_ARL", None)
        try:
            with self.assertRaises(SourceUnavailable) as ctx:
                asyncio.run(deezer.resolve("ed sheeran"))
            self.assertEqual(ctx.exception.reason_code, "no_config")
        finally:
            if saved is not None:
                os.environ["DEEZER_ARL"] = saved

    def test_missing_arl_disables_radio_without_network(self):
        from cogs.music_sources import deezer
        saved = os.environ.get("DEEZER_ARL")
        os.environ.pop("DEEZER_ARL", None)
        try:
            with self.assertRaises(SourceUnavailable) as ctx:
                asyncio.run(deezer.radio_tracks("ed sheeran"))
            self.assertEqual(ctx.exception.reason_code, "no_config")
        finally:
            if saved is not None:
                os.environ["DEEZER_ARL"] = saved

    def test_registry_orders_youtube_deezer_audius(self):
        from cogs.music_sources import audius, deezer, youtube
        self.assertEqual(sources._PROVIDERS,
                         (youtube.resolve, deezer.resolve, audius.resolve))


class DeezerLoginFormatTests(unittest.TestCase):
    """Format negotiation on login, faked without any network or ARL value.

    Locks in the behaviour behind the Premium-ARL upgrade: an account with
    the hq/lossless OPTIONS flags must land on MP3_320 or FLAC
    automatically, so swapping the ARL upgrades playback with no code change.
    """

    @staticmethod
    def _client(options: dict):
        from cogs.music_sources.deezer_gw import GwLightClient

        class _Stub(GwLightClient):
            async def _call(self, method, payload):
                return {
                    "USER": {"USER_ID": 42, "OPTIONS": options},
                    "checkForm": "tok",
                }

        return _Stub("arl", None)

    def test_premium_hq_selects_mp3_320(self):
        client = self._client({"web_hq": True})
        asyncio.run(client.login())
        self.assertEqual(client.format, "MP3_320")

    def test_lossless_account_prefers_flac(self):
        client = self._client({"web_hq": True, "web_lossless": True})
        asyncio.run(client.login())
        self.assertEqual(client.format, "FLAC")

    def test_free_account_falls_back_to_mp3_128(self):
        client = self._client({})
        asyncio.run(client.login())
        self.assertEqual(client.format, "MP3_128")

    def test_negotiated_format_ignores_unrelated_options(self):
        client = self._client({"web_hq": True, "web_lossless": False,
                               "streaming": True, "dgd_support": 1})
        asyncio.run(client.login())
        self.assertEqual(client.format, "MP3_320")


if __name__ == "__main__":
    unittest.main(verbosity=2)