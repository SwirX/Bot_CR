"""Hermetic checks for the api.deezer.com auto-queue feed.

Run:  .venv-local/bin/python scripts/test_deezer_radio.py
No network: only URL parsing, mapping and feed-filtering logic.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.music_sources import deezer_radio  # noqa: E402


class TrackIdFromUrlTests(unittest.TestCase):
    def test_parses_deezer_track_url(self):
        self.assertEqual(
            deezer_radio.track_id_from_url("https://www.deezer.com/track/1484264492"),
            1484264492)

    def test_parses_url_with_trailing_slash(self):
        self.assertEqual(
            deezer_radio.track_id_from_url("https://t.deezer.com/track/42/"),
            42)

    def test_rejects_non_track_urls(self):
        self.assertIsNone(deezer_radio.track_id_from_url("https://www.deezer.com/"))
        self.assertIsNone(deezer_radio.track_id_from_url("not a url"))
        self.assertIsNone(deezer_radio.track_id_from_url("https://x/track/abc"))


class MapApiTrackTests(unittest.TestCase):
    def test_maps_to_gw_light_shape(self):
        api = {
            "id": 42,
            "title": "Shivers",
            "duration": 207,
            "artist": {"id": 384236, "name": "Ed Sheeran"},
            "album": {"md5_image": "38f1360bb17de383d44962a2a07d214f"},
        }
        mapped = deezer_radio._map_api_track(api)
        self.assertEqual(mapped["SNG_ID"], 42)
        self.assertEqual(mapped["SNG_TITLE"], "Shivers")
        self.assertEqual(mapped["ART_NAME"], "Ed Sheeran")
        self.assertEqual(mapped["ARTISTS"], [{"ART_NAME": "Ed Sheeran"}])
        self.assertEqual(mapped["ALB_PICTURE"], "38f1360bb17de383d44962a2a07d214f")
        self.assertEqual(mapped["DURATION"], 207)

    def test_handles_missing_artist_and_album(self):
        mapped = deezer_radio._map_api_track({"id": 7, "title": "x"})
        self.assertEqual(mapped["ART_NAME"], "")
        self.assertEqual(mapped["ARTISTS"], [])
        self.assertEqual(mapped["ALB_PICTURE"], "")


class FilterFeedTests(unittest.TestCase):
    def _feed(self):
        return [
            {"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 1},
        ]

    def test_excludes_seed_and_duplicates(self):
        picked = deezer_radio.filter_feed(self._feed(), exclude_id=2, limit=10)
        self.assertEqual([t["id"] for t in picked], [1, 3, 4])

    def test_caps_to_limit(self):
        picked = deezer_radio.filter_feed(self._feed(), exclude_id=9, limit=2)
        self.assertEqual([t["id"] for t in picked], [1, 2])


if __name__ == "__main__":
    unittest.main()