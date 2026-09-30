"""engine/dvbook.py — the profile book and the profile 7 conversion queue. The book decides what the
Movies pane offers for conversion and what the lane is allowed to replace on the NAS, so the tests
pin the refusals: a stale size, an unknown profile and a converted file must never read as P7."""
import os
import tempfile
import unittest
from unittest import mock

import dvbook

HOST = "/volume3/MediaVolume3/Movies/Drive (2011)/Drive (2011) [2160p REMUX DV].mkv"
NAME = os.path.basename(HOST)
SEED = [{"nas_path": HOST, "size_bytes": 50_000_000_000, "enhancement_layer": "FEL",
         "plex_rating_key": "123", "plex_section": "2"},
        {"nas_path": "/volume1/Media/Movies/Spectre (2015).mkv", "size_bytes": 60_000_000_000,
         "enhancement_layer": "dual-track (EL+RPU in a separate 1080p HEVC track)",
         "plex_rating_key": 9, "plex_section": 2}]


class Book(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self._p = [mock.patch.object(dvbook, "PROFILES_FILE", os.path.join(d, "p.json")),
                   mock.patch.object(dvbook, "QUEUE_FILE", os.path.join(d, "q.json"))]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()


class Profiles(Book):
    def test_the_live_book_is_never_the_test_book(self):
        self.assertNotIn(os.path.expanduser("~/.topaz-pipeline"), dvbook.BOOK_DIR)

    def test_seed_records_profile_7_with_where_it_lives(self):
        self.assertEqual(dvbook.seed(SEED), 2)
        e = dvbook.profile_of(NAME)
        self.assertEqual((e["profile"], e["el"], e["src"], e["host"], e["rk"], e["section"]),
                         (7, "FEL", "rpu", HOST, "123", "2"))
        self.assertEqual(dvbook.profile_of("Spectre (2015).mkv")["el"], "dual")
        self.assertEqual(dvbook.profile_of("Spectre (2015).mkv")["rk"], "9")

    def test_a_different_size_is_a_different_file(self):
        dvbook.seed(SEED)
        self.assertTrue(dvbook.is_p7(NAME, 50_000_000_000))
        self.assertFalse(dvbook.is_p7(NAME, 49_000_000_000))       # replaced since: not proven
        self.assertTrue(dvbook.is_p7(NAME))                         # no size given: name only

    def test_a_converted_file_stops_reading_as_p7(self):
        dvbook.seed(SEED)
        dvbook.record_profile(NAME, 41_000_000_000, 8, src="rpu", host=HOST)
        self.assertFalse(dvbook.is_p7(NAME))
        self.assertFalse(dvbook.is_p7(NAME, 50_000_000_000))
        self.assertEqual(dvbook.profile_of(NAME)["host"], HOST)     # where it lives is kept
        self.assertNotIn(NAME, dvbook.p7_names())

    def test_a_header_probe_never_overrides_an_rpu_reading_of_the_same_file(self):
        dvbook.seed(SEED)
        dvbook.record_profile(NAME, 50_000_000_000, 8, src="probe")
        self.assertEqual(dvbook.profile_of(NAME)["profile"], 7)

    def test_a_probe_of_a_new_file_does_replace_it(self):
        dvbook.seed(SEED)
        dvbook.record_profile(NAME, 41_000_000_000, 8, src="probe")
        self.assertEqual(dvbook.profile_of(NAME)["profile"], 8)


class Queue(Book):
    def setUp(self):
        super().setUp()
        dvbook.seed(SEED)

    def row(self, name=NAME, size=50_000_000_000):
        return {"name": name, "dir": "/MediaVolume3/Movies/Drive (2011)", "title": "Drive (2011)",
                "bytes": size}

    def test_only_known_profile_7_movies_are_queued(self):
        self.assertEqual(dvbook.add([self.row(), self.row("Unknown (2000).mkv"),
                                     self.row(size=1)]), 1)
        (e,) = dvbook.queue()
        self.assertEqual((e["name"], e["host"], e["state"], e["phase"], e["rk"]),
                         (NAME, HOST, dvbook.PENDING, None, "123"))

    def test_a_movie_is_queued_once(self):
        dvbook.add([self.row()])
        self.assertEqual(dvbook.add([self.row()]), 0)
        self.assertEqual(len(dvbook.queue()), 1)

    def test_a_failed_movie_is_requeued_fresh(self):
        dvbook.add([self.row()])
        dvbook.update(NAME, state=dvbook.FAILED, error="boom", phase="upload")
        self.assertEqual(dvbook.add([self.row()]), 1)
        (e,) = dvbook.queue()
        self.assertEqual((e["state"], e["phase"], e.get("error")), (dvbook.PENDING, None, None))

    def test_a_probe_found_movie_needs_a_host_from_the_caller(self):
        dvbook.record_profile("New (2025).mkv", 10, 7, src="probe")
        self.assertEqual(dvbook.add([self.row("New (2025).mkv", 10)]), 0)
        self.assertEqual(dvbook.add([{**self.row("New (2025).mkv", 10),
                                      "host": "/volume1/Media/Movies/New (2025).mkv"}]), 1)

    def test_open_entries_resume_an_active_one_and_filter_by_phase(self):
        dvbook.add([self.row(), self.row("Spectre (2015).mkv", 60_000_000_000)])
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted")
        self.assertEqual([e["name"] for e in dvbook.open_entries(phases=("converted",))], [NAME])
        self.assertEqual([e["name"] for e in dvbook.open_entries(phases=(None,))],
                         ["Spectre (2015).mkv"])
        self.assertEqual(dvbook.next_pending()["name"], NAME)
        dvbook.update(NAME, state=dvbook.DONE)
        self.assertEqual(dvbook.next_pending()["name"], "Spectre (2015).mkv")

    def test_summary_counts_space_saved_by_finished_movies_only(self):
        dvbook.add([self.row(), self.row("Spectre (2015).mkv", 60_000_000_000)])
        dvbook.update(NAME, state=dvbook.DONE, size_in=50_000_000_000, size_out=42_000_000_000)
        s = dvbook.summary()
        self.assertEqual(s["saved_bytes"], 8_000_000_000)
        self.assertEqual(s["by_state"], {dvbook.DONE: 1, dvbook.PENDING: 1})
        self.assertEqual(s["bytes_left"], 60_000_000_000)

    def test_remove(self):
        dvbook.add([self.row()])
        self.assertTrue(dvbook.remove(NAME))
        self.assertFalse(dvbook.remove(NAME))
        self.assertIsNone(dvbook.entry(NAME))


if __name__ == "__main__":
    unittest.main()
