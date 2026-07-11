import json
import os
import tempfile
import unittest
from unittest import mock

import series
from series import parse_episodes, build_queue

NAMES = [
    "The Office  S01e01 Pilot (Extended Cut).mp4",
    "The Office  S01e02 Diversity Day (Extended Cut).mp4",
    "The Office  S01e02 Diversity Day (Extended Cut) HDR10 DV.mp4",   # e02 done
    "The Office  S01e03 Health Care (Extended Cut).mp4",
    "The Office  S01e10 Finale (Extended Cut).mp4",                   # numeric order vs e03
    "The Office  S01e04 only-master (Extended Cut) HDR10 DV.mp4",     # done, no source
    "season-poster.jpg",                                             # ignored (not video)
]


class Parse(unittest.TestCase):
    def test_groups_and_flags_per_episode(self):
        eps = {e["ep"]: e for e in parse_episodes(NAMES)}
        self.assertTrue(eps["S01E01"]["has_source"] and not eps["S01E01"]["has_dv"])
        self.assertTrue(eps["S01E02"]["has_source"] and eps["S01E02"]["has_dv"])   # both
        self.assertTrue(eps["S01E04"]["has_dv"] and not eps["S01E04"]["has_source"])
        self.assertEqual(eps["S01E01"]["source_name"], NAMES[0])

    def test_ignores_non_video(self):
        self.assertFalse(any("poster" in str(e) for e in parse_episodes(NAMES)))


class Queue(unittest.TestCase):
    def test_next_is_first_unprocessed_source(self):
        self.assertEqual(build_queue(NAMES)["next"]["ep"], "S01E01")

    def test_remaining_skips_done_and_sourceless_in_order(self):
        q = build_queue(NAMES)
        self.assertEqual(q["remaining"], ["S01E01", "S01E03", "S01E10"])  # e02/e04 excluded
        self.assertEqual(q["remaining_count"], 3)

    def test_numeric_ordering_e10_after_e03(self):
        self.assertEqual(build_queue(NAMES)["remaining"][-1], "S01E10")

    def test_remaining_items_carry_titles_and_exclude_parked(self):
        q = build_queue(NAMES, skip={"S01E03"})           # park E03
        self.assertEqual([it["ep"] for it in q["remaining_items"]], ["S01E01", "S01E10"])
        self.assertEqual(q["remaining_items"][0]["source_name"], NAMES[0])   # for the title

    def test_done_count(self):
        self.assertEqual(build_queue(NAMES)["done_count"], 2)   # e02, e04

    def test_empty_when_all_done(self):
        q = build_queue(["X S01e01 (Extended Cut) HDR10 DV.mp4"])
        self.assertIsNone(q["next"])
        self.assertEqual(q["remaining"], [])


class Watched(unittest.TestCase):
    def test_unwatched_first_then_watched_each_numeric(self):
        # e01 watched, e03 + e10 unwatched -> unwatched group (numeric) then watched
        wm = {NAMES[0]: True, NAMES[3]: False, NAMES[4]: False}
        q = build_queue(NAMES, watched_map=wm)
        self.assertEqual(q["remaining"], ["S01E03", "S01E10", "S01E01"])
        self.assertEqual(q["next"]["ep"], "S01E03")     # first UNWATCHED, not first numeric
        self.assertEqual(q["unwatched_count"], 2)

    def test_no_watched_map_is_plain_numeric(self):
        self.assertEqual(build_queue(NAMES)["remaining"], ["S01E01", "S01E03", "S01E10"])

    def test_all_watched_keeps_numeric(self):
        wm = {n: True for n in NAMES}
        self.assertEqual(build_queue(NAMES, watched_map=wm)["remaining"],
                         ["S01E01", "S01E03", "S01E10"])

    def test_parse_flags_watched_on_source(self):
        eps = {e["ep"]: e for e in parse_episodes(NAMES, watched_map={NAMES[0]: True})}
        self.assertTrue(eps["S01E01"]["watched"])
        self.assertFalse(eps["S01E03"]["watched"])      # not in the map → unwatched


class Mode(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_defaults_to_tv(self):
        self.assertEqual(series.get_mode(), "tv")

    def test_set_and_persist_mode(self):
        self.assertEqual(series.set_mode("movie"), "movie")
        self.assertEqual(series.get_mode(), "movie")
        self.assertEqual(series.set_mode("bogus"), "tv")   # unknown → tv
        self.assertEqual(series.get_mode(), "tv")

    def test_mode_and_selection_coexist(self):
        series.set_selection("My Show")
        series.set_mode("movie")
        self.assertEqual(series.get_selection(), "My Show")   # selection survives a mode write
        self.assertEqual(series.get_mode(), "movie")


class ActiveSeries(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_default_empty_then_set_is_single_primary(self):
        self.assertEqual(series.get_active_series(), [])
        self.assertIsNone(series.get_selection())
        series.set_selection("A")
        self.assertEqual(series.get_active_series(), ["A"])
        self.assertEqual(series.get_selection(), "A")          # primary = active[0]

    def test_add_dedupes_and_caps_at_three(self):
        for n in ("A", "B", "C", "D"):
            series.add_series(n)
        series.add_series("A")                                 # dup ignored
        self.assertEqual(series.get_active_series(), ["A", "B", "C"])   # D rejected (cap 3)

    def test_remove_then_set_resets_to_single(self):
        for n in ("A", "B", "C"):
            series.add_series(n)
        series.remove_series("B")
        self.assertEqual(series.get_active_series(), ["A", "C"])
        series.set_selection("Z")                              # 'set' = sole series, resets extras
        self.assertEqual(series.get_active_series(), ["Z"])

    def test_rotation_advances_wraps_and_clamps_on_remove(self):
        for n in ("A", "B", "C"):
            series.add_series(n)
        self.assertEqual(series.get_rotation(), 0)
        self.assertEqual(series.advance_rotation("A"), 1)      # A done → B next
        self.assertEqual(series.advance_rotation("B"), 2)      # B done → C next
        self.assertEqual(series.advance_rotation("C"), 0)      # C done → wrap to A
        series.advance_rotation("A")                           # rotation = 1
        series.remove_series("C")                              # → [A, B]; rotation must stay valid
        self.assertLess(series.get_rotation(), 2)
        self.assertEqual(series.advance_rotation("ZZZ"), series.get_rotation())  # unknown → no-op

    def test_legacy_single_series_field_migrates(self):
        with open(series.SELECTION_FILE, "w") as f:
            json.dump({"series": "Old", "mode": "tv"}, f)     # pre-round-robin file shape
        self.assertEqual(series.get_active_series(), ["Old"])
        self.assertEqual(series.get_selection(), "Old")

    def test_set_series_at_replaces_appends_and_dedupes(self):
        series.add_series("A"); series.add_series("B")        # [A, B]
        series.set_series_at(1, "C")                          # replace slot 1
        self.assertEqual(series.get_active_series(), ["A", "C"])
        series.set_series_at(2, "D")                          # empty slot → append
        self.assertEqual(series.get_active_series(), ["A", "C", "D"])
        series.set_series_at(0, "C")                          # C already in slot 1 → dedup
        self.assertEqual(series.get_active_series(), ["C", "D"])


if __name__ == "__main__":
    unittest.main()
