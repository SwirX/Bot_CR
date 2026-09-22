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
from cogs.music_sources import links, radio  # noqa: E402
from cogs.music_sources.model import Candidate  # noqa: E402
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


class CandidateMergeTests(unittest.TestCase):
    """The picker path: metadata-only, no eager stream downloads."""

    def setUp(self):
        self._saved = list(sources._CANDIDATE_PROVIDERS)

    def tearDown(self):
        sources._CANDIDATE_PROVIDERS = self._saved

    def _provider(self, hits: list[Candidate]):
        async def provide(query, limit):
            return hits[:limit]
        return provide

    def test_hits_from_both_providers_are_merged_and_capped(self):
        from cogs.music_sources.model import Candidate
        deezer_hits = [Candidate("deezer", f"Song {i}", "Artist A") for i in range(4)]
        audius_hits = [Candidate("audius", f"Track {i}", "Artist B") for i in range(4)]
        sources._CANDIDATE_PROVIDERS = (self._provider(deezer_hits),
                                        self._provider(audius_hits))
        results = asyncio.run(sources.search_candidates("q", limit=5))
        self.assertEqual(len(results), 5)
        self.assertEqual({c.provider for c in results}, {"deezer", "audius"})

    def test_same_artist_title_is_deduplicated(self):
        from cogs.music_sources.model import Candidate
        dup = Candidate("audius", "Shape of You", "Ed Sheeran")
        first = Candidate("deezer", "shape of you", "ed sheeran")
        sources._CANDIDATE_PROVIDERS = (self._provider([first]),
                                        self._provider([dup]))
        results = asyncio.run(sources.search_candidates("q"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider, "deezer")

    def test_a_dead_provider_shrinks_but_does_not_kill_results(self):
        from cogs.music_sources.model import Candidate

        async def boom(query, limit):
            raise RuntimeError("deezer is down")

        alive = [Candidate("audius", "Song", "Artist")]
        sources._CANDIDATE_PROVIDERS = (boom, self._provider(alive))
        results = asyncio.run(sources.search_candidates("q"))
        self.assertEqual([c.provider for c in results], ["audius"])

    def test_link_queries_return_no_candidates(self):
        # A link names one track; offering a list of search hits would be wrong.
        results = asyncio.run(
            sources.search_candidates("https://youtu.be/dQw4w9WgXcQ"))
        self.assertEqual(results, [])

    def test_materialize_routes_by_provider(self):
        from cogs.music_sources.model import Candidate

        marker = {}

        async def fake_materialize(candidate):
            marker["provider"] = candidate.provider
            return Playable(provider=candidate.provider, title="T", stream_url="u")

        import cogs.music_sources.deezer as dz
        import cogs.music_sources.audius as au
        old_dz, old_au = dz.materialize, au.materialize
        dz.materialize = fake_materialize
        au.materialize = fake_materialize
        try:
            for provider in ("deezer", "audius"):
                result = asyncio.run(
                    sources.materialize(Candidate(provider, "T", payload={"SNG_ID": 1})))
                self.assertEqual(marker["provider"], provider)
                self.assertEqual(result.title, "T")
            with self.assertRaises(SourceUnavailable):
                asyncio.run(sources.materialize(Candidate("mystery", "T")))
        finally:
            dz.materialize = old_dz
            au.materialize = old_au


class LinkRoutingTests(unittest.TestCase):
    """A pasted link must become an ordinary provider search, never a fetch."""

    def setUp(self):
        self._saved = list(sources._PROVIDERS)
        self.seen: list[str] = []

    def tearDown(self):
        sources._PROVIDERS = self._saved

    def _capture(self):
        seen = self.seen

        async def capture(query):
            seen.append(query)
            return Playable(provider="deezer", title="Shape of You",
                            stream_url="u", artist="Ed Sheeran")

        sources._PROVIDERS = (capture,)
        return capture

    def test_youtube_link_is_parsed_then_searched(self):
        # Stub fetch_parsed: the live oEmbed call is exercised on the host, not here.
        async def fake_parse(intent, session):
            return links.ParsedTrack(
                title="Shape of You (Official Video)",
                artist="Ed Sheeran - Topic",
                webpage_url=intent.webpage_url, provider="youtube")

        original = links.fetch_parsed
        links.fetch_parsed = fake_parse
        try:
            self._capture()
            result = asyncio.run(
                sources.resolve_playable("https://youtu.be/xTvyyoF_LZY"))
        finally:
            links.fetch_parsed = original
        self.assertEqual(self.seen, ["Ed Sheeran Shape of You"])
        self.assertEqual(result.title, "Shape of You")

    def test_non_link_query_bypasses_the_link_parser_entirely(self):
        async def boom(intent, session):
            raise AssertionError("plain search text must not hit the link parser")

        original = links.fetch_parsed
        links.fetch_parsed = boom
        try:
            self._capture()
            asyncio.run(sources.resolve_playable("shape of you ed sheeran"))
        finally:
            links.fetch_parsed = original
        self.assertEqual(self.seen, ["shape of you ed sheeran"])

    def test_unparseable_link_surfaces_a_useful_message(self):
        async def fail(intent, session):
            raise SourceUnavailable(intent.provider, "link_unreadable",
                                    "Couldn't read that link — paste the title.")

        original = links.fetch_parsed
        links.fetch_parsed = fail
        try:
            self._capture()
            with self.assertRaises(SourceUnavailable) as ctx:
                asyncio.run(sources.resolve_playable("https://youtu.be/xTvyyoF_LZY"))
        finally:
            links.fetch_parsed = original
        self.assertIn("paste the title", ctx.exception.message)

    def test_playlist_link_is_refused_with_guidance(self):
        with self.assertRaises(SourceUnavailable) as ctx:
            asyncio.run(sources.resolve_playable(
                "https://www.youtube.com/playlist?list=PL1234567890"))
        self.assertEqual(ctx.exception.reason_code, "unsupported_link")

    def test_ssrf_url_is_treated_as_plain_text_not_a_link(self):
        # The core guarantee: an unrecognised host never reaches the parser.
        async def boom(intent, session):
            raise AssertionError("SSRF URL must not be parsed as a link")

        original = links.fetch_parsed
        links.fetch_parsed = boom
        try:
            self._capture()
            asyncio.run(sources.resolve_playable("http://169.254.169.254/latest/"))
        finally:
            links.fetch_parsed = original
        self.assertEqual(self.seen, ["http://169.254.169.254/latest/"])


class RadioTests(unittest.TestCase):
    def test_known_station_resolves_instantly(self):
        playable = asyncio.run(radio.resolve_station("GrooveSalad"))
        self.assertEqual(playable.provider, "radio")
        self.assertIn("somafm.com", playable.stream_url)

    def test_pick_station_prefers_exact_name_over_popular_partial(self):
        stations = [
            {"name": "Hitradio Austria", "url": "u1", "clickcount": 5},
            {"name": "HitRadio", "url": "u2", "clickcount": 100},
        ]
        picked = radio.pick_station(stations, "hitradio")
        self.assertEqual(picked["name"], "HitRadio")

    def test_pick_station_matches_compact_names(self):
        stations = [{"name": "Radio Mars FM", "url": "u", "clickcount": 7}]
        picked = radio.pick_station(stations, "radiomars")
        self.assertEqual(picked["name"], "Radio Mars FM")

    def test_pick_station_skips_entries_without_stream(self):
        stations = [
            {"name": "Hitradio", "url": "", "url_resolved": "", "clickcount": 99},
            {"name": "Hitradio Retro", "url": "u", "clickcount": 9},
        ]
        picked = radio.pick_station(stations, "hitradio")
        self.assertEqual(picked["name"], "Hitradio Retro")

    def test_pick_station_returns_none_when_feed_is_empty(self):
        self.assertIsNone(radio.pick_station([], "hitradio"))

    def test_build_station_playable_carries_stream_metadata(self):
        playable = radio._build_station_playable({
            "name": "Radio Mars",
            "country": "Belgium",
            "url_resolved": "http://x/stream",
            "favicon": "http://x/fav.png",
        })
        self.assertEqual(playable.provider, "radio")
        self.assertEqual(playable.stream_url, "http://x/stream")
        self.assertTrue(playable.headers["User-Agent"])
        self.assertIn("Belgium", playable.title)

    def test_prefix_variants_recover_concatenated_names(self):
        self.assertEqual(radio._prefix_variants("radiomars"), ["mars"])
        self.assertEqual(radio._prefix_variants("radio"), [])
        self.assertEqual(radio._prefix_variants("hitradio"), [])


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
            _BF_IV, blowfish_key, decrypt_chunks,
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
            _BF_IV, blowfish_key, decrypt_stream,
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

    def test_registry_orders_deezer_audius(self):
        """Deezer first, Audius fallback — no YouTube.

        yt-dlp was reachable with member-supplied URLs (blind SSRF), so the
        provider was removed rather than merely reordered.
        """
        from cogs.music_sources import audius, deezer
        self.assertEqual(sources._PROVIDERS, (deezer.resolve, audius.resolve))
        self.assertFalse(any("youtube" in str(p) for p in sources._PROVIDERS))


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