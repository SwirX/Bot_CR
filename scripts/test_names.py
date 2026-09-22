"""Unit checks for the name-matcher + the XP level curve.

Run:  .venv-local/bin/python scripts/test_names.py
Hermetic: no network, no Appwrite, no config import needed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.names import (  # noqa: E402
    AUTO_LINK_THRESHOLD, best_match, decursive, name_similarity,
    normalize_name, top_matches,
)
from data.levels import level_from_xp, xp_for_level, xp_progress  # noqa: E402

CURSIVE = "𝓕𝓪𝓽𝓲𝓶𝓪 𝓑𝓸𝓾𝔃𝓪𝓻𝓫𝓲𝓪"


class DecursiveTests(unittest.TestCase):
    def test_capitals_and_lowercase(self):
        self.assertEqual(decursive(CURSIVE), "Fatima Bouzarbia")

    def test_whole_alphabet_round_trip(self):
        upper = "".join(chr(0x1D4D0 + i) for i in range(26))
        lower = "".join(chr(0x1D4EA + i) for i in range(26))
        self.assertEqual(decursive(upper), "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.assertEqual(decursive(lower), "abcdefghijklmnopqrstuvwxyz")

    def test_non_cursive_passes_through(self):
        self.assertEqual(decursive("حمدي أحمد 123"), "حمدي أحمد 123")
        self.assertEqual(decursive(""), "")


class NormalizeTests(unittest.TestCase):
    def test_decursives_lowercases_collapses(self):
        self.assertEqual(normalize_name(CURSIVE), "fatima bouzarbia")

    def test_punctuation_diacritics(self):
        self.assertEqual(normalize_name("Bouzarbia, Fatima-Élise"),
                         "bouzarbia fatima elise")

    def test_arabic_kept(self):
        self.assertNotEqual(normalize_name("حمدي أحمد"), "")


class SimilarityTests(unittest.TestCase):
    def test_exact_and_reversed(self):
        self.assertEqual(name_similarity("Fatima Bouzarbia",
                                         "Fatima Bouzarbia"), 1.0)
        self.assertEqual(name_similarity("Bouzarbia Fatima",
                                         "Fatima Bouzarbia"), 1.0)

    def test_cursive_matches_plain(self):
        self.assertEqual(name_similarity(CURSIVE, "Fatima Bouzarbia"), 1.0)

    def test_initial_shorthand(self):
        self.assertEqual(name_similarity("Fatima B.", "Fatima Bouzarbia"),
                         1.0)

    def test_unrelated(self):
        self.assertEqual(name_similarity("Yasser Yyy", "Fatima Bouzarbia"),
                         0.0)

    def test_partial_overlap(self):
        self.assertGreater(name_similarity("Marouane", "Marouane Bouglace"),
                           0.0)
        self.assertLess(name_similarity("Marouane", "Marouane Bouglace"),
                        1.0)

    def test_empty_sides(self):
        self.assertEqual(name_similarity("", "Fatima"), 0.0)
        self.assertEqual(name_similarity("Fatima", ""), 0.0)


class BestMatchTests(unittest.TestCase):
    def _candidates(self):
        return [("m1", "Fatima Bouzarbia"),
                ("m2", "Marouane Bouglace"),
                ("m3", "Khalid Bahmad")]

    def test_confident_cursive_match(self):
        match = best_match(CURSIVE, self._candidates())
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "m1")
        self.assertGreaterEqual(match[2], AUTO_LINK_THRESHOLD)

    def test_unknown_name_rejected(self):
        self.assertIsNone(best_match("Nobody Person Here", self._candidates()))

    def test_below_threshold_rejected(self):
        self.assertIsNone(best_match("Fatima", self._candidates()))

    def test_ambiguous_tie_rejected(self):
        cands = [("a", "Fatima Bouzarbia"), ("b", "Fatima Bougotov")]
        self.assertIsNone(best_match("Fatima B.", cands,
                                     threshold=AUTO_LINK_THRESHOLD))

    def test_top_matches_order(self):
        top = top_matches("bouzarbia", self._candidates())
        self.assertEqual(top[0][0], "m1")
        self.assertEqual(len(top), 3)


class LevelCurveTests(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(level_from_xp(0), 0)
        self.assertEqual(level_from_xp(99), 0)
        self.assertEqual(level_from_xp(100), 1)
        self.assertEqual(level_from_xp(1234), 3)
        self.assertEqual(xp_for_level(3), 900)

    def test_progress(self):
        level, into, need = xp_progress(1234)
        self.assertEqual((level, into, need), (3, 334, 700))

    def test_negative_tolerated(self):
        self.assertEqual(level_from_xp(-5), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)