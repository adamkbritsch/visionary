import os
import tempfile
import unittest
from unittest import mock

import movies


class Helpers(unittest.TestCase):
    def test_is_movie_video_skips_junk(self):
        self.assertTrue(movies._is_movie_video("A Movie (2010).mkv"))
        self.assertTrue(movies._is_movie_video("B.mp4"))
        self.assertFalse(movies._is_movie_video("poster.jpg"))
        self.assertFalse(movies._is_movie_video("62893001_1_1744956204651.ug-tmp"))

    def test_movie_title_strips_tags_and_edition(self):
        self.assertEqual(movies.movie_title("12 Years a Slave (2013) [1080p BluRay HEVC].mkv"),
                         "12 Years a Slave (2013)")
        self.assertEqual(movies.movie_title("John Wick - Chapter 4 (2023) {edition-60 FPS} [2160p].mkv"),
                         "John Wick - Chapter 4 (2023)")
        self.assertEqual(movies.movie_title("Plain Name.mkv"), "Plain Name")


ENTRIES = [
    {"name": "Alpha (2010) [1080p BluRay x264].mkv", "dir": "/Media/Movies"},
    {"name": "Beta (2011) [2160p UHD HDR10 DV HEVC].mkv", "dir": "/Media/Movies"},   # DV by name mark
    {"name": "Gamma (2012) [1080p WEB-DL].mkv", "dir": "/Media/Movies/Gamma (2012)"},
]


class Parse(unittest.TestCase):
    def test_flags_dv_and_sorts_by_title(self):
        ms = movies.parse_movies(ENTRIES)
        self.assertEqual([m["title"] for m in ms], ["Alpha (2010)", "Beta (2011)", "Gamma (2012)"])
        self.assertTrue([m for m in ms if m["title"] == "Beta (2011)"][0]["has_dv"])     # name mark
        self.assertFalse([m for m in ms if m["title"] == "Alpha (2010)"][0]["has_dv"])

    def test_dv_manifest_marks_done_without_name_mark(self):
        ms = movies.parse_movies(ENTRIES, dv_map={"Alpha (2010) [1080p BluRay x264].mkv": 1})
        self.assertTrue([m for m in ms if m["title"] == "Alpha (2010)"][0]["has_dv"])

    def test_keeps_each_movies_own_dir(self):
        ms = {m["title"]: m for m in movies.parse_movies(ENTRIES)}
        self.assertEqual(ms["Gamma (2012)"]["dir"], "/Media/Movies/Gamma (2012)")


class SelectedQueue(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(movies, "SELECTED_FILE", os.path.join(self.d, "movie_queue.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_add_dedupes_and_orders(self):
        movies.add_selected("a.mkv", "/Media/Movies", "A (2001)")
        movies.add_selected("b.mkv", "/Media/Movies/B", "B (2002)")
        movies.add_selected("a.mkv", "/Media/Movies", "A (2001)")   # dup ignored
        self.assertEqual([i["name"] for i in movies.get_selected()], ["a.mkv", "b.mkv"])

    def test_selected_view_next_and_skip(self):
        movies.add_selected("a.mkv", "/Media/Movies", "A (2001)")
        movies.add_selected("b.mkv", "/Media/Movies/B", "B (2002)")
        v = movies.selected_view()
        self.assertEqual(v["count"], 2)
        self.assertEqual(v["next"]["source_name"], "a.mkv")
        self.assertEqual(v["next"]["nas_dir"], "/Media/Movies")
        v2 = movies.selected_view(skip={"a.mkv"})       # a parked → b is next
        self.assertEqual(v2["next"]["title"], "B (2002)")

    def test_remove_and_clear(self):
        movies.add_selected("a.mkv", "/m", "A")
        movies.add_selected("b.mkv", "/m", "B")
        movies.remove_selected("a.mkv")
        self.assertEqual([i["name"] for i in movies.get_selected()], ["b.mkv"])
        movies.clear_selected()
        self.assertEqual(movies.get_selected(), [])
        self.assertIsNone(movies.selected_view()["next"])

    def test_added_movie_defaults_to_process_next(self):
        movies.add_selected("a.mkv", "/m", "A")
        self.assertEqual(movies._pos(movies.get_selected()[0]), 0)   # pos 0 = before any episode

    def test_move_movie_past_episodes_changes_slot_and_clamps(self):
        movies.add_selected("a.mkv", "/m", "A")                      # pos 0
        pos = lambda: movies._pos(movies.get_selected()[0])
        movies.move_in_queue("a.mkv", +1, ep_count=3)               # down past an episode → 1
        self.assertEqual(pos(), 1)
        movies.move_in_queue("a.mkv", +1, 3); movies.move_in_queue("a.mkv", +1, 3)  # → 2 → 3
        self.assertEqual(pos(), 3)
        movies.move_in_queue("a.mkv", +1, 3)                        # capped at ep_count → no-op
        self.assertEqual(pos(), 3)
        movies.move_in_queue("a.mkv", -1, 3)                        # back up → 2
        self.assertEqual(pos(), 2)
        for _ in range(5):
            movies.move_in_queue("a.mkv", -1, 3)                    # up past front → clamps at 0
        self.assertEqual(pos(), 0)

    def test_two_movies_same_slot_swap_order(self):
        movies.add_selected("a.mkv", "/m", "A")                      # pos 0
        movies.add_selected("b.mkv", "/m", "B")                      # pos 0
        movies.move_in_queue("a.mkv", +1, 3)                        # same slot → swap add-order
        self.assertEqual([i["name"] for i in movies.get_selected()], ["b.mkv", "a.mkv"])
        self.assertTrue(all(movies._pos(i) == 0 for i in movies.get_selected()))   # both stay slot 0

    def test_next_due_skips_future_movies_and_decrement_advances(self):
        movies.add_selected("a.mkv", "/m", "A")                      # pos 0 → due now
        movies.add_selected("b.mkv", "/m", "B")
        movies.move_in_queue("b.mkv", +1, 5)                        # B → after 1 episode
        self.assertEqual(movies.next_due()["source_name"], "a.mkv")
        movies.remove_selected("a.mkv")                              # A processed
        self.assertIsNone(movies.next_due())                        # B not due yet
        movies.decrement_positions()                                # an episode finished → B advances
        self.assertEqual(movies.next_due()["source_name"], "b.mkv")
        movies.decrement_positions()                                # floors at 0
        self.assertEqual(movies._pos(movies.get_selected()[0]), 0)


if __name__ == "__main__":
    unittest.main()


class ReleaseTags(unittest.TestCase):
    """Picker tags parsed from release names — approximate routing info (no probe)."""

    def test_wwdits_style_4k_webdl(self):
        t = movies.release_tags("What We Do in the Shadows - S02E01 - 2160p WEB-DL HULU [EAC3 5.1 h265-FLUX].mkv")
        self.assertEqual(t, ["4K", "HEVC"])
        self.assertEqual(movies.route_hint(t), "fast path ~2.5× runtime")

    def test_1080p_bluray_x264(self):
        t = movies.release_tags("12 Years a Slave (2013) [1080p BluRay x264].mkv")
        self.assertEqual(t, ["1080p", "H264"])
        self.assertEqual(movies.route_hint(t), "full upscale ~5× runtime")

    def test_hdr_dv_and_remux_markers(self):
        self.assertIn("HDR", movies.release_tags("Movie (2020) 2160p HDR10 WEB-DL HEVC.mkv"))
        self.assertIn("DV", movies.release_tags("Movie.2020.2160p.UHD.BluRay.DV.HDR10.x265.mkv"))
        t = movies.release_tags("Movie (2020) [2160p UHD BluRay REMUX HDR HEVC].mkv")
        self.assertEqual(t, ["4K", "HDR", "HEVC", "REMUX"])

    def test_no_false_dv_from_ordinary_words(self):
        self.assertNotIn("DV", movies.release_tags("Advent Movie (2019) 1080p WEB x264.mkv"))

    def test_untagged_name_yields_full_route(self):
        t = movies.release_tags("Plain Name.mkv")
        self.assertEqual(t, [])
        self.assertEqual(movies.route_hint(t), "full upscale ~5× runtime")

    def test_parse_movies_carries_tags_and_route(self):
        ms = movies.parse_movies([{"name": "Alpha (2010) [2160p WEB HEVC].mkv", "dir": "/M"}])
        self.assertEqual(ms[0]["tags"], ["4K", "HEVC"])
        self.assertEqual(ms[0]["route"], "fast path ~2.5× runtime")
