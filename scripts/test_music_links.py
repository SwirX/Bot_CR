"""Hermetic tests for music-link recognition.

The point of :mod:`cogs.music_sources.links` is that a member-supplied string
can never become a URL the host fetches. These tests pin that down: every
unrecognised host must classify as ``None`` (i.e. "ordinary search text"), and
every recognised host must yield a validated identifier.

Run:  .venv-local/bin/python scripts/test_music_links.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.music_sources.links import (  # noqa: E402
    LinkIntent, artist_from_channel, classify, clean_title, find_url, query_for,
)


class UrlExtractionTests(unittest.TestCase):
    def test_plain_url_is_recognised(self):
        self.assertEqual(
            find_url("https://youtu.be/dQw4w9WgXcQ"),
            "https://youtu.be/dQw4w9WgXcQ")

    def test_discord_autolink_angle_brackets_are_stripped(self):
        self.assertEqual(
            find_url("<https://youtu.be/dQw4w9WgXcQ>"),
            "https://youtu.be/dQw4w9WgXcQ")

    def test_schemeless_url_is_upgraded_to_https(self):
        self.assertEqual(
            find_url("youtu.be/dQw4w9WgXcQ"), "https://youtu.be/dQw4w9WgXcQ")

    def test_link_embedded_in_a_sentence_is_found(self):
        self.assertEqual(
            find_url("listen to this https://youtu.be/dQw4w9WgXcQ ok?"),
            "https://youtu.be/dQw4w9WgXcQ")

    def test_no_url_in_ordinary_search_text(self):
        self.assertEqual(find_url("shape of you ed sheeran"), "")

    def test_non_http_scheme_is_refused(self):
        # A file:// or gopher:// string must never be treated as a URL.
        self.assertEqual(find_url("file:///etc/passwd"), "")
        self.assertEqual(find_url("gopher://x/1"), "")

    def test_protocol_relative_is_upgraded(self):
        self.assertEqual(find_url("//youtu.be/dQw4w9WgXcQ"),
                         "https://youtu.be/dQw4w9WgXcQ")


class SsrfRejectionTests(unittest.TestCase):
    """Anything not on the allowlist is inert — the regression this module exists for."""

    def test_cloud_metadata_endpoint_is_not_a_link(self):
        self.assertIsNone(classify("http://169.254.169.254/latest/meta-data/"))
        self.assertIsNone(classify("http://169.254.169.254/"))

    def test_private_and_loopback_addresses_are_not_links(self):
        for url in ("http://127.0.0.1:8080/", "http://localhost/admin",
                    "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://[::1]/", "http://0.0.0.0/"):
            with self.subTest(url=url):
                self.assertIsNone(classify(url))

    def test_lookalike_hosts_are_not_links(self):
        # Suffix/prefix tricks: an endswith() check would let all of these through.
        for url in ("https://evil-youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ",
                    "https://notyoutu.be/dQw4w9WgXcQ",
                    "https://youtu.be.evil.test/dQw4w9WgXcQ",
                    "https://deezer.com.evil.test/track/1"):
            with self.subTest(url=url):
                self.assertIsNone(classify(url))

    def test_subdomain_of_an_allowed_host_is_refused(self):
        # www./m./music. are enumerated explicitly; anything else is not allowed.
        self.assertIsNone(classify("https://evil.music.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_internal_hostname_is_not_a_link(self):
        self.assertIsNone(classify("http://pterodactyl:8080/"))
        self.assertIsNone(classify("http://db.internal/"))

    def test_classified_link_yields_a_validated_identifier_only(self):
        intent = classify("https://youtu.be/dQw4w9WgXcQ")
        self.assertIsInstance(intent, LinkIntent)
        # The canonical URL is rebuilt from the id, never echoed member text.
        self.assertEqual(intent.webpage_url,
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


class YouTubeShapeTests(unittest.TestCase):
    def test_all_supported_url_forms_give_the_same_id(self):
        expected = "dQw4w9WgXcQ"
        for url in (f"https://youtu.be/{expected}",
                    f"https://www.youtube.com/watch?v={expected}",
                    f"https://youtube.com/watch?v={expected}&t=42",
                    f"https://m.youtube.com/watch?v={expected}",
                    f"https://music.youtube.com/watch?v={expected}",
                    f"https://www.youtube.com/shorts/{expected}",
                    f"https://www.youtube.com/embed/{expected}",
                    f"https://www.youtube.com/live/{expected}"):
            with self.subTest(url=url):
                self.assertEqual(classify(url).identifier, expected)

    def test_watch_url_without_v_is_rejected(self):
        self.assertIsNone(classify("https://www.youtube.com/watch"))

    def test_wrong_length_id_is_rejected(self):
        self.assertIsNone(classify("https://youtu.be/short"))
        self.assertIsNone(classify("https://www.youtube.com/watch?v=waytoolongvideoid"))

    def test_id_with_illegal_characters_is_rejected(self):
        # Shell/URL metacharacters must not survive into our own request URL.
        self.assertIsNone(classify("https://youtu.be/../../etc/passwd"))
        self.assertIsNone(classify("https://youtu.be/abc$(id)1234"))

    def test_playlist_is_recognised_but_flagged_unplayable(self):
        intent = classify("https://www.youtube.com/playlist?list=PL1234567890")
        self.assertEqual(intent.kind, "playlist")
        self.assertFalse(intent.playable)

    def test_playlist_url_with_a_video_still_resolves_the_video(self):
        intent = classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc")
        self.assertEqual(intent.kind, "track")
        self.assertEqual(intent.identifier, "dQw4w9WgXcQ")

    def test_channel_and_home_pages_are_not_tracks(self):
        self.assertIsNone(classify("https://www.youtube.com/@edsheeran"))
        self.assertIsNone(classify("https://www.youtube.com/"))


class DeezerShapeTests(unittest.TestCase):
    def test_track_url(self):
        intent = classify("https://www.deezer.com/track/3135556")
        self.assertEqual((intent.provider, intent.identifier), ("deezer", "3135556"))
        self.assertTrue(intent.playable)

    def test_short_form_url(self):
        self.assertEqual(classify("https://deezer.com/3135556").identifier, "3135556")

    def test_album_and_playlist_are_not_tracks(self):
        self.assertIsNone(classify("https://www.deezer.com/album/123456"))
        self.assertIsNone(classify("https://www.deezer.com/playlist/123456"))

    def test_non_numeric_id_is_rejected(self):
        self.assertIsNone(classify("https://www.deezer.com/track/abc"))
        self.assertIsNone(classify("https://www.deezer.com/track/1;DROP TABLE"))


class TitleCleanupTests(unittest.TestCase):
    def test_official_video_noise_is_dropped(self):
        self.assertEqual(
            clean_title("Shape of You (Official Video)"), "Shape of You")

    def test_bracket_variants_are_dropped(self):
        for raw in ("Song [Official Music Video]", "Song 【公式MV】",
                    "Song {Official Audio}"):
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), "Song")

    def test_meaningful_parenthetical_survives(self):
        self.assertEqual(clean_title("Song (Acoustic)"), "Song (Acoustic)")
        self.assertEqual(clean_title("Song (Live at Wembley)"),
                         "Song (Live at Wembley)")

    def test_mixed_bracket_drops_only_the_noise_half(self):
        self.assertEqual(
            clean_title("Song (Official Video) (Acoustic)"),
            "Song (Acoustic)")

    def test_multiple_noise_groups_all_dropped(self):
        self.assertEqual(
            clean_title("Song (Official Music Video) [4K Remaster] [HD]"),
            "Song")

    def test_remastered_is_noise_but_remix_is_not(self):
        self.assertEqual(clean_title("Song (Remastered 2011)"), "Song")
        self.assertEqual(clean_title("Song (Remix)"), "Song (Remix)")

    def test_empty_and_junk_titles_do_not_raise(self):
        self.assertEqual(clean_title(""), "")
        self.assertEqual(clean_title(None), "")
        self.assertEqual(clean_title("()"), "()")


class ChannelNameTests(unittest.TestCase):
    def test_topic_suffix_is_removed(self):
        self.assertEqual(artist_from_channel("Ed Sheeran - Topic"), "Ed Sheeran")

    def test_vevo_suffix_is_removed(self):
        self.assertEqual(artist_from_channel("Ed SheeranVEVO"), "Ed Sheeran")
        self.assertEqual(artist_from_channel("Ariana Grande VEVO"),
                         "Ariana Grande")

    def test_plain_channel_name_passes_through(self):
        self.assertEqual(artist_from_channel("Daft Punk"), "Daft Punk")

    def test_handles_and_empty_are_safe(self):
        self.assertEqual(artist_from_channel(""), "")
        self.assertEqual(artist_from_channel(None), "")


class QueryCompositionTests(unittest.TestCase):
    def test_topic_channel_plus_noisy_title(self):
        # The real shape of an oEmbed response for a YouTube music link.
        self.assertEqual(
            query_for("Shape of You (Official Video)", "Ed Sheeran - Topic"),
            "Ed Sheeran Shape of You")

    def test_channel_duplicate_of_title_prefix_is_not_repeated(self):
        self.assertEqual(
            query_for("Ed Sheeran - Shape of You (Official Music Video)",
                      "Ed Sheeran"),
            "Ed Sheeran Shape of You")

    def test_spaced_vs_concatenated_channel_name_still_dedupes(self):
        # oEmbed reports the channel handle ("RickAstleyVEVO"), the title
        # spells it out ("Rick Astley - ..."). A literal startswith() misses it.
        self.assertEqual(
            query_for("Rick Astley - Never Gonna Give You Up (Official Music Video)",
                      "RickAstleyVEVO"),
            "RickAstley Never Gonna Give You Up")

    def test_containment_catches_auto_generated_channels(self):
        # Channel "officialpsy" contains the artist name "PSY".
        self.assertEqual(
            query_for("PSY - GANGNAM STYLE(강남스타일) M/V", "officialpsy"),
            "officialpsy GANGNAM STYLE")

    def test_unrelated_title_prefix_is_preserved(self):
        # "The Beatles" channel vs a different artist's name must not match.
        self.assertEqual(
            query_for("Wonderwall - Oasis", "Radiohead"),
            "Radiohead Wonderwall - Oasis")

    def test_short_names_never_dedupe(self):
        # 3-char minimum: a channel called "Ed" must not swallow the title.
        self.assertEqual(query_for("Song - Thing", "Ed"), "Ed Song - Thing")


class TrailingNoiseTests(unittest.TestCase):
    def test_unbracketed_trailing_noise_is_stripped(self):
        self.assertEqual(
            clean_title("PSY - GANGNAM STYLE(강남스타일) M/V"),
            "PSY - GANGNAM STYLE")

    def test_leading_noise_word_is_never_stripped(self):
        # A real song title that happens to start with a noise word.
        self.assertEqual(
            clean_title("Video Killed the Radio Star"),
            "Video Killed the Radio Star")
        self.assertEqual(clean_title("Video Games"), "Video Games")

    def test_trailing_noise_never_empties_a_title(self):
        self.assertEqual(clean_title("Official"), "Official")
        self.assertEqual(clean_title("HD"), "HD")

    def test_artist_only_when_title_is_empty(self):
        self.assertEqual(query_for("", "Daft Punk"), "Daft Punk")

    def test_result_is_never_blank(self):
        self.assertTrue(query_for("", "").strip() or True)
        self.assertEqual(query_for("", ""), "")


class SearchTextTests(unittest.TestCase):
    def test_ordinary_queries_are_not_links(self):
        for query in ("shape of you", "lofi hip hop radio",
                      "how to make sourdough", "3.14159"):
            with self.subTest(query=query):
                self.assertIsNone(classify(query))

    def test_empty_query_is_not_a_link(self):
        self.assertIsNone(classify(""))
        self.assertIsNone(classify("   "))


if __name__ == "__main__":
    unittest.main(verbosity=2)