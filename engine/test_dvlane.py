"""engine/dvlane.py — the lane that replaces profile 7 movies on the NAS. Its pure decisions (the disk
budget, the stream throttle, recognizing a swap that already happened) and the two steps that
decide whether an original is replaced, with every NAS and conversion call mocked."""
import os
import tempfile
import threading
import unittest
from unittest import mock

import dvbook
import dvlane
import dvp7
import nas_ssh

GB = 1024 ** 3
HOST = "/volume1/Media/Movies/Temple (1984) [2160p DV].mkv"
NAME = os.path.basename(HOST)


class Pure(unittest.TestCase):
    def test_work_lives_outside_the_pipelines_scratch(self):
        self.assertNotIn("topaz-scratch", dvlane.WORK_ROOT)

    def test_the_lane_never_starts_under_test(self):
        lane = dvlane.Lane()
        lane.start()
        self.assertEqual(lane._threads, {})

    def test_the_budget_keeps_the_pipelines_floor(self):
        # 60 GB movie -> 168 GB footprint; 580 free - 168 = 412 >= 400
        self.assertTrue(dvlane.fits(60 * GB, 0, 580 * GB, 400 * GB))
        self.assertFalse(dvlane.fits(70 * GB, 0, 580 * GB, 400 * GB))
        # bytes already on disk are already spent
        self.assertTrue(dvlane.fits(70 * GB, 30 * GB, 580 * GB, 400 * GB))
        self.assertEqual(dvlane.disk_need(10, 100), 0)

    def test_any_session_or_an_unreachable_plex_throttles(self):
        self.assertEqual(dvlane.throttle_for({"count": 1, "files": set()}), dvlane.THROTTLE_BPS)
        self.assertEqual(dvlane.throttle_for(None), dvlane.THROTTLE_BPS)
        self.assertIsNone(dvlane.throttle_for({"count": 0, "files": set()}))

    def test_a_finished_rename_is_recognized_only_from_both_signs(self):
        self.assertTrue(dvlane.swapped_already(90, False, 100, 90))
        self.assertFalse(dvlane.swapped_already(90, True, 100, 90))      # still staged
        self.assertFalse(dvlane.swapped_already(100, False, 100, 90))    # original still there
        self.assertFalse(dvlane.swapped_already(None, False, 100, 90))   # gone: not ours


class Steps(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.patches = [mock.patch.object(dvbook, "PROFILES_FILE", os.path.join(d, "p.json")),
                        mock.patch.object(dvbook, "QUEUE_FILE", os.path.join(d, "q.json")),
                        mock.patch.object(dvlane, "WORK_ROOT", os.path.join(d, "work")),
                        mock.patch.object(dvlane.logbook, "event"),
                        mock.patch.object(dvlane.logbook, "failure")]
        for p in self.patches:
            p.start()
        dvbook.seed([{"nas_path": HOST, "size_bytes": 100, "enhancement_layer": "FEL",
                      "plex_rating_key": "5", "plex_section": "2"}])
        dvbook.add([{"name": NAME, "dir": "/Media/Movies", "title": "Temple", "bytes": 100}])
        self.lane = dvlane.Lane()
        self.ev = threading.Event()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def e(self):
        return dvbook.entry(NAME)

    def _fake_download(self, host, local, size, **kw):
        with open(local, "wb") as fh:
            fh.write(b"s" * size)

    def _fake_convert(self, src, out, work, **kw):
        with open(out, "wb") as fh:
            fh.write(b"n" * 90)
        return {"el": "FEL", "dual_track": False, "frames": 10, "size_out": 90}

    def test_fetch_converts_and_hands_off_without_keeping_the_source(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(100, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "download", side_effect=self._fake_download), \
             mock.patch.object(dvp7, "convert", side_effect=self._fake_convert):
            self.lane._fetch(self.e(), self.ev)
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["size_out"], e["expect_mtime"]),
                         (dvbook.ACTIVE, "converted", 90, 7))
        d = dvlane.work_dir(HOST)
        self.assertFalse(os.path.exists(os.path.join(d, "source.mkv")))
        self.assertTrue(os.path.exists(os.path.join(d, "p81.mkv")))

    def test_a_file_that_changed_on_the_nas_is_not_touched(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(123, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=7), \
             mock.patch.object(nas_ssh, "download", side_effect=AssertionError("no download")):
            with self.assertRaisesRegex(RuntimeError, "not the 100"):
                self.lane._fetch(self.e(), self.ev)

    def test_an_already_converted_file_is_done_not_failed(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(90, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ssh, "download", side_effect=AssertionError("no download")):
            self.lane._fetch(self.e(), self.ev)
        self.assertEqual(self.e()["state"], dvbook.DONE)
        self.assertFalse(dvbook.is_p7(NAME))

    def test_a_file_that_is_not_p7_after_all_fails_with_the_reason(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(100, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "download", side_effect=self._fake_download), \
             mock.patch.object(dvp7, "convert", side_effect=dvp7.NotP7("reads profile 5")):
            with self.assertRaisesRegex(RuntimeError, "profile 5"):
                self.lane._fetch(self.e(), self.ev)
        self.assertFalse(dvbook.is_p7(NAME))          # never offered again

    def _converted(self):
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "p81.mkv"), "wb") as fh:
            fh.write(b"n" * 90)
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted", size_in=100, size_out=90,
                      expect_mtime=7)

    def test_ship_uploads_checks_and_swaps_then_records_profile_8(self):
        self._converted()
        idle = {"count": 0, "files": set()}
        with mock.patch.object(nas_ssh, "upload") as up, \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ssh, "swap") as sw, \
             mock.patch.object(dvlane.plex, "session_detail", return_value=idle), \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=True) as rf, \
             mock.patch.object(dvlane.plex, "analyze", return_value=True) as an:
            self.lane._ship(self.e(), self.ev)
        up.assert_called_once()
        self.assertEqual(sw.call_args.kwargs, {"expect_size": 100, "expect_mtime": 7,
                                               "new_size": 90})
        e = self.e()
        self.assertEqual((e["state"], e.get("plex_pending")), (dvbook.DONE, False))
        rf.assert_called_once_with("2", "/media/Movies")
        an.assert_called_once_with("5")
        self.assertEqual(dvbook.profile_of(NAME)["profile"], 8)
        self.assertFalse(os.path.exists(dvlane.work_dir(HOST)))
        self.assertEqual(dvbook.summary()["saved_bytes"], 10)

    def test_a_staged_file_the_nas_does_not_read_as_8_is_never_swapped(self):
        self._converted()
        with mock.patch.object(nas_ssh, "upload"), \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=7), \
             mock.patch.object(nas_ssh, "discard_stage") as disc, \
             mock.patch.object(nas_ssh, "swap", side_effect=AssertionError("must not swap")):
            with self.assertRaisesRegex(RuntimeError, "profile 7"):
                self.lane._ship(self.e(), self.ev)
        disc.assert_called_once_with(HOST)

    def test_never_swaps_while_that_movie_is_playing(self):
        self._converted()
        dvbook.update(NAME, phase="swap")
        playing = {"count": 1, "files": {NAME}}
        stop = threading.Event()
        with mock.patch.object(nas_ssh, "stat", side_effect=lambda p: (90, 1, 0, 0, "644")
                               if p.endswith(".part") else (100, 7, 911, 10, "644")), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=playing), \
             mock.patch.object(nas_ssh, "swap", side_effect=AssertionError("must not swap")), \
             mock.patch.object(self.lane, "_wait", side_effect=lambda s: True):
            with self.assertRaises(nas_ssh.Stopped):
                self.lane._ship(self.e(), stop)
        self.assertEqual(self.e()["phase"], "swap")

    def test_plex_is_only_told_once_nobody_is_streaming(self):
        self._converted()
        watching = {"count": 1, "files": {"Other.mkv"}}
        with mock.patch.object(nas_ssh, "stat", return_value=None), \
             mock.patch.object(nas_ssh, "upload"), \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ssh, "swap"), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=watching), \
             mock.patch.object(dvlane.plex, "refresh_folder",
                               side_effect=AssertionError("not while streaming")):
            self.lane._ship(self.e(), self.ev)          # another movie playing: swap is fine
        self.assertEqual((self.e()["state"], self.e()["plex_pending"]), (dvbook.DONE, True))

    def test_a_crash_after_the_rename_resumes_as_done(self):
        self._converted()
        dvbook.update(NAME, phase="swap")
        with mock.patch.object(nas_ssh, "stat", side_effect=lambda p: None if p.endswith(".part")
                               else (90, 9, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "upload", side_effect=AssertionError("no re-upload")), \
             mock.patch.object(nas_ssh, "swap", side_effect=AssertionError("no second swap")), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=None):
            self.lane._ship(self.e(), self.ev)
        self.assertEqual(self.e()["state"], dvbook.DONE)

    def test_a_lost_local_file_goes_back_to_be_converted_again(self):
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted", size_in=100, size_out=90)
        with mock.patch.object(nas_ssh, "upload", side_effect=AssertionError("nothing to send")):
            self.lane._ship(self.e(), self.ev)
        self.assertIsNone(self.e()["phase"])

    def test_the_fetch_waits_for_disk_rather_than_crowd_the_pipeline(self):
        with mock.patch.object(dvlane.shutil, "disk_usage",
                               return_value=mock.Mock(free=400 * GB + 100)), \
             mock.patch.object(dvlane, "_floor_bytes", return_value=400 * GB):
            self.assertIsNone(self.lane._pick_fetch())
        self.assertIn("waiting for disk", self.lane._note)

    def test_remove_cleans_up_and_leaves_the_original_alone(self):
        self._converted()
        with mock.patch.object(nas_ssh, "discard_stage") as disc, \
             mock.patch.object(nas_ssh, "swap", side_effect=AssertionError("never")):
            self.assertTrue(self.lane.remove(NAME))
        disc.assert_not_called()                         # nothing was staged yet
        self.assertIsNone(dvbook.entry(NAME))
        self.assertFalse(os.path.exists(dvlane.work_dir(HOST)))

    def test_status_is_local_only(self):
        with mock.patch.object(nas_ssh, "remote", side_effect=AssertionError("no NAS I/O")), \
             mock.patch.object(dvlane.plex, "session_detail",
                               side_effect=AssertionError("no Plex I/O")):
            st = self.lane.status()
        self.assertEqual(st["queue"][0]["name"], NAME)
        self.assertEqual(st["summary"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
