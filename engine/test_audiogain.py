import os
import tempfile
import unittest
from unittest import mock

import audiogain as ag


class Season(unittest.TestCase):
    def test_season_from_episode_id(self):
        self.assertEqual(ag.season_of("S04E12"), "S04")
        self.assertEqual(ag.season_of("s4e2"), "S04")        # normalised, so S4 and S04 agree
        self.assertEqual(ag.season_of("S00E01"), "S00")      # specials are their own season
        self.assertEqual(ag.season_of("nonsense"), "")


class Signature(unittest.TestCase):
    """Only a CLEAR indicator in the filename may split a season. A false split silently
    re-measures and the volume steps — the exact bug this exists to prevent."""

    LOST_11 = "Lost (2004) - S04E11 - Cabin Fever (1080p BluRay x265 Silence).mkv"
    LOST_12 = "Lost (2004) - S04E12 - There's No Place Like Home (1) (1080p BluRay x265 Silence).mkv"

    def test_same_release_same_signature(self):
        self.assertEqual(ag.source_signature(self.LOST_11), ag.source_signature(self.LOST_12))

    def test_a_different_master_splits(self):
        web = "Lost (2004) - S04E13 - Filler (1080p WEB-DL DDP5.1 H.264-NTb).mkv"
        self.assertNotEqual(ag.source_signature(self.LOST_11), ag.source_signature(web))

    def test_group_is_read_off_both_common_layouts(self):
        self.assertEqual(ag.release_group(self.LOST_11), "silence")          # "… x265 Silence)"
        self.assertEqual(ag.release_group("Show.S01E01.1080p.WEB-DL.H.264-NTb.mkv"), "ntb")
        # a codec glued to a group keeps only the group half
        self.assertEqual(ag.release_group("Lost - S04E13 (1080p WEB-DL H.264-NTb).mkv"), "ntb")

    def test_plain_titles_carry_no_source_evidence(self):
        # no tech tokens -> empty signature -> the whole season groups together
        for n in ("The Office (US) - S02E10 - Christmas Party.mkv",
                  "Show - S01E01 - Spider-Man.mkv",
                  "Show - S01E02 - Title (Part 1).mkv"):
            self.assertEqual(ag.source_signature(n), "", n)
        self.assertEqual(ag.source_signature("The Office (US) - S02E10 - Christmas Party.mkv"),
                         ag.source_signature("The Office (US) - S02E11 - Booze Cruise.mkv"))

    def test_episode_title_alone_never_splits(self):
        a = "Lost (2004) - S04E11 - Cabin Fever (1080p BluRay x265 Silence).mkv"
        b = "Lost (2004) - S04E12 - Something Totally Different (1080p BluRay x265 Silence).mkv"
        self.assertEqual(ag.source_signature(a), ag.source_signature(b))


class Keys(unittest.TestCase):
    def test_key_is_show_season_and_source(self):
        k = ag.key_for("Lost (2004)", "S04E12", "x (1080p BluRay x265 Silence).mkv")
        self.assertTrue(k.startswith("Lost (2004)|S04|"))

    def test_seasons_and_shows_do_not_share(self):
        n = "x (1080p BluRay x265 Silence).mkv"
        self.assertNotEqual(ag.key_for("Lost", "S04E01", n), ag.key_for("Lost", "S05E01", n))
        self.assertNotEqual(ag.key_for("Lost", "S04E01", n), ag.key_for("Fringe", "S04E01", n))

    def test_no_season_means_no_key(self):
        # movies / YouTube fall through to per-item measurement, exactly as before
        self.assertEqual(ag.key_for("A Movie", "", "A Movie (2020) 1080p BluRay.mkv"), "")
        self.assertEqual(ag.key_for("", "S01E01", "x.mkv"), "")


class Book(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = mock.patch.object(ag, "BOOK_FILE", os.path.join(d, "gain.json"))
        p.start(); self.addCleanup(p.stop)

    def test_first_episode_decides_and_later_ones_reuse(self):
        k = ag.key_for("Lost", "S04E01", "a (1080p BluRay x265 Silence).mkv")
        self.assertIsNone(ag.remembered(k))
        ag.remember(k, 3.2, -19.2, "a (1080p BluRay x265 Silence).mkv", target=-16)
        self.assertEqual(ag.remembered(k), 3.2)

    def test_the_first_decision_is_never_overwritten(self):
        k = ag.key_for("Lost", "S04E01", "a (1080p BluRay x265 Silence).mkv")
        ag.remember(k, 3.2, -19.2, "a.mkv")
        ag.remember(k, 9.9, -25.0, "b.mkv")          # a later episode must not re-decide
        self.assertEqual(ag.remembered(k), 3.2)

    def test_forget_scopes_to_one_show(self):
        n = "a (1080p BluRay x265 Silence).mkv"
        ag.remember(ag.key_for("Lost", "S04E01", n), 3.0, -19.0, n)
        ag.remember(ag.key_for("Lost", "S05E01", n), 4.0, -20.0, n)
        ag.remember(ag.key_for("Fringe", "S01E01", n), 5.0, -21.0, n)
        self.assertEqual(ag.forget("Lost"), 2)
        self.assertIsNone(ag.remembered(ag.key_for("Lost", "S04E01", n)))
        self.assertEqual(ag.remembered(ag.key_for("Fringe", "S01E01", n)), 5.0)

    def test_empty_key_is_a_no_op(self):
        ag.remember("", 3.0, -19.0, "x.mkv")
        self.assertIsNone(ag.remembered(""))
        self.assertEqual(ag.view(), [])

    def test_view_reports_rows(self):
        n = "a (1080p BluRay x265 Silence).mkv"
        ag.remember(ag.key_for("Lost", "S04E01", n), 3.25, -19.2, n, target=-16)
        row = ag.view("Lost")[0]
        self.assertEqual((row["series"], row["season"], row["gain"]), ("Lost", "S04", 3.25))


if __name__ == "__main__":
    unittest.main()
