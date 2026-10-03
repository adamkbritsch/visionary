"""engine/dvlane.py — the lane that replaces profile 7 movies on the NAS. Its pure decisions (the disk
budget, the stream throttle, recognizing a swap that already happened) and the two steps that
decide whether an original is replaced, with every NAS and conversion call mocked."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import dvbook
import dvlane
import dvp7
import nas_ftp

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

    def test_the_plex_throttle_setting_off_means_full_speed(self):
        self.assertIsNone(dvlane.throttle_for({"count": 3, "files": set()}, enabled=False))
        self.assertIsNone(dvlane.throttle_for(None, enabled=False))
        lane = dvlane.Lane()
        with mock.patch("settings.plex_throttle", return_value=False), \
             mock.patch.object(dvlane.plex, "session_detail",
                               side_effect=AssertionError("off: Plex is not even asked")):
            self.assertIsNone(lane._limit())
        lane = dvlane.Lane()
        with mock.patch("settings.plex_throttle", return_value=True), \
             mock.patch.object(dvlane.plex, "session_detail", return_value={"count": 1, "files": set()}):
            self.assertEqual(lane._limit(), dvlane.THROTTLE_BPS)

    def test_any_session_or_an_unreachable_plex_throttles(self):
        self.assertEqual(dvlane.throttle_for({"count": 1, "files": set()}), dvlane.THROTTLE_BPS)
        self.assertEqual(dvlane.throttle_for(None), dvlane.THROTTLE_BPS)
        self.assertIsNone(dvlane.throttle_for({"count": 0, "files": set()}))

    def test_a_finished_rename_is_recognized_only_from_both_signs(self):
        self.assertTrue(dvlane.swapped_already(90, False, 100, 90))
        self.assertFalse(dvlane.swapped_already(90, True, 100, 90))      # still staged
        self.assertFalse(dvlane.swapped_already(100, False, 100, 90))    # original still there
        self.assertFalse(dvlane.swapped_already(None, False, 100, 90))   # gone: not ours


class _Lane(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.patches = [mock.patch.object(dvbook, "PROFILES_FILE", os.path.join(d, "p.json")),
                        mock.patch.object(dvbook, "QUEUE_FILE", os.path.join(d, "q.json")),
                        mock.patch.object(dvlane, "WORK_ROOT", os.path.join(d, "work")),
                        mock.patch.object(dvlane.logbook, "event"),
                        mock.patch.object(dvlane.logbook, "failure"),
                        mock.patch.object(nas_ftp, "discard_stage"),   # a test patches it to look
                        mock.patch.object(dvlane.plex, "heavy_activities", return_value=[])]  # never the live Plex
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

    def _converted(self):
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "p81.mkv"), "wb") as fh:
            fh.write(b"n" * 90)
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted", size_in=100, size_out=90,
                      expect_mtime=7)


class Steps(_Lane):
    def test_fetch_converts_and_hands_off_without_keeping_the_source(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(100, 7)), \
             mock.patch.object(nas_ftp, "download", side_effect=self._fake_download), \
             mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(dvp7, "convert", side_effect=self._fake_convert):
            self.lane._fetch(self.e(), self.ev)
        disc.assert_called_once_with(HOST)            # no stale staged copy survives a re-convert
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["size_out"], e["expect_mtime"]),
                         (dvbook.ACTIVE, "converted", 90, 7))
        d = dvlane.work_dir(HOST)
        self.assertFalse(os.path.exists(os.path.join(d, "source.mkv")))
        self.assertTrue(os.path.exists(os.path.join(d, "p81.mkv")))

    def test_a_file_that_changed_on_the_nas_is_not_touched(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(123, 7)), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=7), \
             mock.patch.object(nas_ftp, "download", side_effect=AssertionError("no download")):
            with self.assertRaisesRegex(RuntimeError, "not the 100"):
                self.lane._fetch(self.e(), self.ev)

    def test_an_already_converted_file_is_done_not_failed(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(90, 7)), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ftp, "download", side_effect=AssertionError("no download")):
            self.lane._fetch(self.e(), self.ev)
        self.assertEqual(self.e()["state"], dvbook.DONE)
        self.assertFalse(dvbook.is_p7(NAME))

    def test_a_file_that_is_not_p7_after_all_fails_with_the_reason(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(100, 7)), \
             mock.patch.object(nas_ftp, "download", side_effect=self._fake_download), \
             mock.patch.object(nas_ftp, "discard_stage"), \
             mock.patch.object(dvp7, "convert", side_effect=dvp7.NotP7("reads profile 5")):
            with self.assertRaisesRegex(RuntimeError, "profile 5"):
                self.lane._fetch(self.e(), self.ev)
        self.assertFalse(dvbook.is_p7(NAME))          # never offered again

    def test_ship_uploads_checks_and_swaps_then_records_profile_8(self):
        self._converted()
        idle = {"count": 0, "files": set()}
        with mock.patch.object(nas_ftp, "upload") as up, \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ftp, "swap") as sw, \
             mock.patch.object(dvlane.plex, "session_detail", return_value=idle), \
             mock.patch.object(dvlane.plex, "heavy_activities", return_value=[]), \
             mock.patch.object(dvlane.plex, "part_size", return_value=90), \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=True) as rf, \
             mock.patch.object(dvlane.plex, "analyze", return_value=True) as an:
            self.lane._ship(self.e(), self.ev)                  # Plex already has the new file:
        up.assert_called_once()                                 # settled at once, no rescan
        self.assertEqual(sw.call_args.kwargs, {"expect_size": 100, "expect_mtime": 7,
                                               "new_size": 90})
        e = self.e()
        self.assertEqual((e["state"], e.get("plex_pending")), (dvbook.DONE, False))
        rf.assert_not_called()
        an.assert_not_called()
        self.assertEqual(dvbook.profile_of(NAME)["profile"], 8)
        self.assertFalse(os.path.exists(dvlane.work_dir(HOST)))
        self.assertEqual(dvbook.summary()["saved_bytes"], 10)

    def test_a_staged_file_the_nas_does_not_read_as_8_is_never_swapped(self):
        self._converted()
        with mock.patch.object(nas_ftp, "upload"), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=7), \
             mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(nas_ftp, "swap", side_effect=AssertionError("must not swap")):
            with self.assertRaisesRegex(RuntimeError, "profile 7"):
                self.lane._ship(self.e(), self.ev)
        # once before this conversion's first upload (stale copies, the folder kept for the upload),
        # once for the bad copy
        self.assertEqual(disc.call_args_list, [mock.call(HOST, keep_dir=True), mock.call(HOST)])

    def test_the_first_upload_of_a_conversion_clears_a_stale_stage_first(self):
        # mkvmerge writes a new segment UID every time: resuming onto an older conversion's staged
        # copy would splice two files (review 2026-10-01: the pre-convert discard is best effort)
        self._converted()
        order = []
        with mock.patch.object(nas_ftp, "discard_stage", side_effect=lambda h, **k: order.append("discard")), \
             mock.patch.object(nas_ftp, "upload", side_effect=lambda *a, **k: order.append("upload")), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=7):
            with self.assertRaises(RuntimeError):
                self.lane._ship(self.e(), self.ev)
        self.assertEqual(order[:2], ["discard", "upload"])

    def test_a_resumed_upload_keeps_its_staged_bytes(self):
        self._converted()
        dvbook.update(NAME, phase="upload")
        with mock.patch.object(nas_ftp, "discard_stage", side_effect=AssertionError("resume")), \
             mock.patch.object(nas_ftp, "upload") as up, \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ftp, "swap"), \
             mock.patch.object(self.lane, "_plex_detail", return_value={"count": 0, "files": set()}), \
             mock.patch.object(self.lane, "_flush_plex"):
            self.lane._ship(self.e(), self.ev)
        up.assert_called_once()

    def test_a_blip_clearing_the_stage_before_convert_keeps_the_download(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(100, 7)), \
             mock.patch.object(nas_ftp, "download", side_effect=self._fake_download), \
             mock.patch.object(nas_ftp, "discard_stage", side_effect=OSError("blip")), \
             mock.patch.object(dvp7, "convert", side_effect=self._fake_convert):
            self.lane._fetch(self.e(), self.ev)
        self.assertEqual(self.e()["phase"], "converted")

    def test_never_swaps_while_that_movie_is_playing(self):
        self._converted()
        dvbook.update(NAME, phase="swap")
        playing = {"count": 1, "files": {NAME}}
        stop = threading.Event()
        with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: (90, 1, 0, 0, "644")
                               if p.endswith(".part") else (100, 7)), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=playing), \
             mock.patch.object(nas_ftp, "swap", side_effect=AssertionError("must not swap")), \
             mock.patch.object(self.lane, "_wait", side_effect=lambda s: True):
            with self.assertRaises(nas_ftp.Stopped):
                self.lane._ship(self.e(), stop)
        self.assertEqual(self.e()["phase"], "swap")

    def test_plex_is_only_told_once_nobody_is_streaming(self):
        self._converted()
        watching = {"count": 1, "files": {"Other.mkv"}}
        with mock.patch.object(nas_ftp, "stat", return_value=None), \
             mock.patch.object(nas_ftp, "upload"), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ftp, "swap"), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=watching), \
             mock.patch.object(dvlane.plex, "refresh_folder",
                               side_effect=AssertionError("not while streaming")):
            self.lane._ship(self.e(), self.ev)          # another movie playing: swap is fine
        self.assertEqual((self.e()["state"], self.e()["plex_pending"]), (dvbook.DONE, True))

    def test_a_crash_after_the_rename_resumes_as_done(self):
        self._converted()
        dvbook.update(NAME, phase="swap")
        with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: None if p.endswith(".part")
                               else (90, 9)), \
             mock.patch.object(nas_ftp, "upload", side_effect=AssertionError("no re-upload")), \
             mock.patch.object(nas_ftp, "swap", side_effect=AssertionError("no second swap")), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=None):
            self.lane._ship(self.e(), self.ev)
        self.assertEqual(self.e()["state"], dvbook.DONE)

    def test_a_lost_local_file_goes_back_to_be_converted_again(self):
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted", size_in=100, size_out=90)
        with mock.patch.object(nas_ftp, "upload", side_effect=AssertionError("nothing to send")):
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
        with mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(nas_ftp, "swap", side_effect=AssertionError("never")):
            self.assertTrue(self.lane.remove(NAME))
        disc.assert_not_called()                         # nothing was staged yet
        self.assertIsNone(dvbook.entry(NAME))
        self.assertFalse(os.path.exists(dvlane.work_dir(HOST)))

    def test_status_is_local_only(self):
        with mock.patch.object(nas_ftp, "_connect", side_effect=AssertionError("no NAS I/O")), \
             mock.patch.object(dvlane.plex, "session_detail",
                               side_effect=AssertionError("no Plex I/O")):
            st = self.lane.status()
        self.assertEqual(st["queue"][0]["name"], NAME)
        self.assertEqual(st["summary"]["total"], 1)


class EthernetOnly(_Lane):
    def test_the_note_follows_the_live_reason_and_the_movie_never_fails(self):
        reach = iter([False, False, False, True])       # three looks unreachable, then back
        links = iter([{"unavailable": True}, {"unavailable": False}])
        notes = []
        with mock.patch.object(nas_ftp, "reachable", side_effect=lambda: next(reach)), \
             mock.patch.object(nas_ftp, "link", side_effect=lambda: next(links)), \
             mock.patch.object(self.lane, "_wait",
                               side_effect=lambda s: notes.append(self.lane._note) or False):
            self.assertTrue(self.lane._offline(nas_ftp.NoLink("no cable")))
        self.assertIn("waiting for an Ethernet link to the NAS", notes[0])     # no cable
        self.assertEqual(notes[1], "waiting for the NAS (it does not answer over FTP)")  # cable back
        self.assertEqual(self.e()["state"], dvbook.PENDING)          # not failed

    def test_a_configuration_reason_is_shown_for_as_long_as_it_waits(self):
        reach = iter([False, False, False, True])
        notes = []
        why = "Ethernet only needs the NAS's network name among the FTP hosts, not only addresses"
        with mock.patch.object(nas_ftp, "reachable", side_effect=lambda: next(reach)), \
             mock.patch.object(nas_ftp, "link", return_value={}), \
             mock.patch.object(self.lane, "_wait",
                               side_effect=lambda s: notes.append(self.lane._note) or False):
            self.assertTrue(self.lane._offline(nas_ftp.NoLink(why, note=why)))
        self.assertEqual(notes, ["waiting: " + why] * 2)

    def test_status_says_how_old_the_link_is(self):
        import nas_link
        nas_link.remember({"iface": "en12", "bound": True, "wired": True, "priority": "ethernet"})
        with mock.patch.object(nas_link.time, "time", return_value=nas_link.last()["at"] + 300):
            self.assertEqual(self.lane.status()["link"]["age"], 300)


class Failures(_Lane):
    """Annihilation (2026-09-30): one SSH blip failed it with 43 GB converted and ready."""

    def test_a_failure_after_converting_keeps_the_file_and_tries_the_upload_again(self):
        self._converted()
        dvbook.update(NAME, phase="upload")
        self.lane._fail(self.e(), "NAS command failed (255)")
        out = os.path.join(dvlane.work_dir(HOST), "p81.mkv")
        self.assertTrue(os.path.exists(out))
        self.lane._sweep_orphans()                        # a relaunch must not delete it either
        self.assertTrue(os.path.exists(out))
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["tries"]), (dvbook.ACTIVE, "upload", 1))
        self.assertIsNone(self.lane._pick_ship())         # it waits out its pause first...
        self.assertTrue(self.lane.retry(NAME))            # ...unless asked to try now
        self.assertEqual(self.lane._pick_ship()["name"], NAME)

    def test_a_failure_before_converting_frees_the_disk_and_retry_starts_over(self):
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "source.mkv"), "wb") as fh:
            fh.write(b"s" * 50)
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="download")
        self.lane._fail(self.e(), "not on the NAS any more")
        self.assertFalse(os.path.exists(d))
        self.assertTrue(self.lane.retry(NAME))
        e = self.e()
        self.assertEqual((e["state"], e["phase"]), (dvbook.PENDING, None))

    def test_a_retry_whose_converted_file_is_gone_starts_over(self):
        dvbook.update(NAME, state=dvbook.FAILED, phase="upload", size_out=90)
        self.assertTrue(self.lane.retry(NAME))
        self.assertIsNone(self.e()["phase"])

    def test_a_staged_movie_resumes_at_the_swap_even_without_the_local_file(self):
        dvbook.update(NAME, state=dvbook.FAILED, phase="swap", size_out=90)
        self.assertTrue(self.lane.retry(NAME))
        self.assertEqual((self.e()["state"], self.e()["phase"]), (dvbook.ACTIVE, "swap"))


class FiveAttempts(_Lane):
    """User, 2026-09-30: "if something's downloaded and it fails, either delete it from this device
    or retry upload, try 5 times before deletion"."""

    def _fail_n(self, n, phase="upload"):
        for _ in range(n):
            dvbook.update(NAME, phase=phase)
            self.lane._fail(self.e(), "NAS command failed (255)")

    def test_the_pauses_between_attempts_grow(self):
        self._converted()
        waits = []
        with mock.patch.object(dvlane.time, "time", return_value=1000.0):
            for _ in range(dvlane.SHIP_TRIES - 1):
                dvbook.update(NAME, phase="upload")
                self.lane._fail(self.e(), "boom")
                waits.append(self.e()["retry_at"] - 1000)
        self.assertEqual(waits, list(dvlane.SHIP_RETRY_WAITS))

    def test_a_movie_is_picked_again_once_its_pause_is_over(self):
        self._converted()
        self._fail_n(1)
        self.assertIsNone(self.lane._pick_ship())
        with mock.patch.object(dvlane.time, "time", return_value=self.e()["retry_at"] + 1):
            self.assertEqual(self.lane._pick_ship()["name"], NAME)

    def test_the_fifth_failure_deletes_its_files_from_this_mac_and_the_nas_stage(self):
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage") as disc:
            self._fail_n(dvlane.SHIP_TRIES - 1)
            out = os.path.join(dvlane.work_dir(HOST), "p81.mkv")
            self.assertTrue(os.path.exists(out))          # four failures: still trying
            self.assertEqual(self.e()["state"], dvbook.ACTIVE)
            disc.assert_not_called()
            self._fail_n(1)
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["tries"]), (dvbook.FAILED, None, 5))
        self.assertIn("gave up after 5 attempts", e["error"])
        self.assertFalse(os.path.exists(dvlane.work_dir(HOST)))
        disc.assert_called_once_with(HOST)                # its staged copy on the NAS goes too
        self.assertIsNone(self.lane._pick_ship())

    def test_a_retry_after_giving_up_starts_over_with_fresh_attempts(self):
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage"):
            self._fail_n(dvlane.SHIP_TRIES)
        self.assertTrue(self.lane.retry(NAME))
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["tries"]), (dvbook.PENDING, None, 0))

    def test_a_swap_that_keeps_failing_is_given_up_on_too(self):
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(nas_ftp, "stat", side_effect=lambda p: (100, 7)
                               if p == HOST else (90, 8)):   # not swapped yet
            self._fail_n(dvlane.SHIP_TRIES, phase="swap")
        self.assertEqual(self.e()["state"], dvbook.FAILED)
        disc.assert_called_once_with(HOST)

    def test_a_swap_whose_rename_landed_is_finished_not_failed(self):
        # The rename ran, then the session dropped (it is never retried on the NAS side): on the
        # fifth attempt that must not delete anything or mark the movie failed (review 2026-09-30).
        self._converted()
        dvbook.update(NAME, tries=dvlane.SHIP_TRIES - 1, phase="swap")
        with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: (90, 9)
                               if p == HOST else None), \
             mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(self.lane, "_flush_plex"):
            self.lane._fail(self.e(), "NAS command failed (255)")
        e = self.e()
        self.assertEqual((e["state"], e.get("plex_pending"), e.get("tries")), (dvbook.DONE, True, None))
        disc.assert_not_called()

    def test_the_last_attempt_never_gives_up_on_a_swap_the_nas_cannot_check(self):
        # Its rename may have landed: deleting it and calling it failed could be wrong. It waits
        # and asks again, deleting nothing (review 2026-09-30).
        self._converted()
        dvbook.update(NAME, tries=dvlane.SHIP_TRIES - 1, phase="swap")
        with mock.patch.object(nas_ftp, "stat", side_effect=RuntimeError("ssh")), \
             mock.patch.object(nas_ftp, "discard_stage") as disc, \
             mock.patch.object(dvlane.time, "time", return_value=1000.0):
            self.lane._fail(self.e(), "NAS command failed (255)")
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["tries"]), (dvbook.ACTIVE, "swap", 4))
        self.assertEqual(e["retry_at"], 1000 + dvlane.SHIP_RETRY_WAITS[-1])
        self.assertTrue(os.path.exists(os.path.join(dvlane.work_dir(HOST), "p81.mkv")))
        disc.assert_not_called()

    def test_a_staged_copy_the_give_up_could_not_delete_is_deleted_later(self):
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage", side_effect=RuntimeError("ssh")):
            self._fail_n(dvlane.SHIP_TRIES)
        self.assertTrue(self.e()["stage_left"])
        with mock.patch.object(nas_ftp, "discard_stage") as disc:
            self.lane._resume_kept_failures()                # the lane's next start
        disc.assert_called_once_with(HOST)
        self.assertFalse(self.e().get("stage_left"))

    def test_remove_deletes_a_staged_copy_the_give_up_left(self):
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage", side_effect=RuntimeError("ssh")):
            self._fail_n(dvlane.SHIP_TRIES)
        with mock.patch.object(nas_ftp, "discard_stage") as disc:
            self.assertTrue(self.lane.remove(NAME))
        disc.assert_called_once_with(HOST)

    def test_an_unanswered_swap_check_is_not_taken_as_landed(self):
        self._converted()
        dvbook.update(NAME, phase="swap")
        with mock.patch.object(nas_ftp, "stat", side_effect=RuntimeError("ssh")):
            self.lane._fail(self.e(), "boom")
        self.assertEqual((self.e()["state"], self.e()["tries"]), (dvbook.ACTIVE, 1))

    def test_a_movie_queued_again_after_giving_up_runs_this_run(self):
        # Re-adding it from the picker must not leave it 'Queued' until the run restarts (review).
        self._converted()
        with mock.patch.object(nas_ftp, "discard_stage"):
            self._fail_n(dvlane.SHIP_TRIES)
        dvbook.add([{"name": NAME, "dir": "/Media/Movies", "title": "Temple", "bytes": 100}])
        with mock.patch.object(dvlane.shutil, "disk_usage", return_value=mock.Mock(free=10 ** 15)):
            self.assertEqual(self.lane._pick_fetch()["name"], NAME)

    def test_a_lost_converted_file_starts_over_with_fresh_attempts(self):
        self._converted()
        dvbook.update(NAME, phase="upload", tries=3)
        os.remove(os.path.join(dvlane.work_dir(HOST), "p81.mkv"))
        with mock.patch.object(nas_ftp, "discard_stage") as disc:
            self.lane._ship(self.e(), self.ev)
        e = self.e()
        self.assertEqual((e["phase"], e.get("tries"), e.get("retry_at")), (None, None, None))
        disc.assert_called_once_with(HOST)                 # the old file's staged copy is stale

    def test_an_attempt_clears_its_pause_and_a_success_clears_the_count(self):
        self._converted()
        self._fail_n(1)
        self.assertTrue(self.lane.retry(NAME))
        seen = []
        def ship(e, ev):
            seen.append(self.e().get("retry_at"))
            self.lane._finish(self.e())
            self.lane._abort.set()
        with mock.patch.object(self.lane, "_ship", side_effect=ship), \
             mock.patch.object(self.lane, "_flush_plex"), \
             mock.patch.object(nas_ftp, "link"):
            self.lane._ship_loop()
        self.assertEqual(seen, [None])
        e = self.e()
        self.assertEqual((e["state"], e.get("tries"), e.get("error")), (dvbook.DONE, None, None))

    def test_waiting_out_a_nas_outage_is_not_an_attempt(self):
        self._converted()
        def offline(_ex):
            self.lane._abort.set()
            return True
        with mock.patch.object(self.lane, "_ship", side_effect=RuntimeError("no route")), \
             mock.patch.object(self.lane, "_offline", side_effect=offline), \
             mock.patch.object(self.lane, "_flush_plex"):
            self.lane._ship_loop()
        self.assertFalse(self.e().get("tries"))

    def test_a_movie_waiting_to_try_again_holds_back_new_downloads(self):
        # Its new file is on this disk like a converted one's: while uploads fail, more conversions
        # would only pile up.
        self._converted()
        self._fail_n(1)
        host2 = "/volume1/Media/Movies/Other (2001).mkv"
        dvbook.seed([{"nas_path": host2, "size_bytes": 100, "enhancement_layer": "MEL"}])
        dvbook.add([{"name": os.path.basename(host2), "dir": "/Media/Movies", "bytes": 100}])
        host3 = "/volume1/Media/Movies/Third (2002).mkv"
        dvbook.seed([{"nas_path": host3, "size_bytes": 100, "enhancement_layer": "MEL"}])
        dvbook.add([{"name": os.path.basename(host3), "dir": "/Media/Movies", "bytes": 100}])
        dvbook.update(os.path.basename(host2), state=dvbook.ACTIVE, phase="converted")
        with mock.patch.object(dvlane.shutil, "disk_usage",
                               return_value=mock.Mock(free=10 ** 15)):
            self.assertIsNone(self.lane._pick_fetch())
        self.assertEqual(self.lane._note, "a movie that failed to upload is waiting to try again")

    def test_a_failure_before_a_verified_file_existed_is_deleted_at_once(self):
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "source.mkv"), "wb") as fh:
            fh.write(b"s" * 50)
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="convert")
        self.lane._fail(self.e(), "the RPU would not parse")
        self.assertEqual(self.e()["state"], dvbook.FAILED)
        self.assertFalse(os.path.exists(d))

    def test_a_kept_failure_from_before_gets_its_attempts(self):
        self._converted()
        dvbook.update(NAME, state=dvbook.FAILED, phase="upload", error="old")
        self.lane._resume_kept_failures()
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e["tries"]), (dvbook.ACTIVE, "upload", 1))
        self.assertEqual(self.lane._pick_ship()["name"], NAME)

    def test_the_lane_takes_up_kept_failures_when_it_starts(self):
        self._converted()
        dvbook.update(NAME, state=dvbook.FAILED, phase="upload", error="old")
        self.lane._guard("ship", lambda: None)            # what each thread runs first
        self.assertEqual(self.e()["state"], dvbook.ACTIVE)

    def test_a_kept_failure_whose_file_is_gone_starts_over_when_retried(self):
        dvbook.update(NAME, state=dvbook.FAILED, phase="upload", size_out=90)
        with mock.patch.object(nas_ftp, "discard_stage") as disc:
            self.lane._resume_kept_failures()
        e = self.e()
        self.assertEqual((e["state"], e["phase"]), (dvbook.FAILED, None))
        disc.assert_called_once_with(HOST)                 # its partial upload on the NAS goes too

    def test_the_panel_sees_the_attempts(self):
        self._converted()
        self._fail_n(2)
        st = self.lane.status()
        self.assertEqual(st["ship_tries"], 5)
        row = st["queue"][0]
        self.assertEqual(row["tries"], 2)
        self.assertGreater(row["retry_at"], 0)


class PlexGetsOneMovieAtATime(_Lane):
    """2026-10-02: told about every replaced movie at once, each with a forced re-analysis, Plex
    generated chapter thumbnails and loudness data for one 4K file after another and starved the
    NAS of memory until its services died."""

    IDLE = {"count": 0, "files": set()}

    def _done(self, name=NAME, rk="5", size_out=90):
        dvbook.update(name, state=dvbook.DONE, phase=None, plex_pending=True, size_out=size_out)

    def _flush(self, busy=(), size=None, watching=None):
        with mock.patch.object(dvlane.plex, "session_detail", return_value=watching or self.IDLE), \
             mock.patch.object(dvlane.plex, "heavy_activities",
                               return_value=(None if busy is None else list(busy))), \
             mock.patch.object(dvlane.plex, "part_size", return_value=size) as ps, \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=True) as rf, \
             mock.patch.object(dvlane.plex, "analyze", return_value=True) as an:
            self.lane._plex_busy_cache = (0.0, None)            # each call asks afresh
            self.lane._flush_plex()
        return rf, an, ps

    def _second(self):
        host2 = "/volume1/Media/Movies/Other (2001).mkv"
        dvbook.seed([{"nas_path": host2, "size_bytes": 100, "enhancement_layer": "MEL",
                      "plex_rating_key": "6", "plex_section": "2"}])
        dvbook.add([{"name": os.path.basename(host2), "dir": "/Media/Movies", "bytes": 100}])
        self._done(os.path.basename(host2))
        return os.path.basename(host2)

    def test_one_movie_at_a_time(self):
        self._done()
        other = self._second()
        rf, _an, _ps = self._flush()
        rf.assert_called_once()                                 # only the first
        self.assertTrue(self.e().get("plex_rescanned"))
        self.assertFalse(dvbook.entry(other).get("plex_rescanned"))

    def test_a_file_plex_already_took_in_settles_even_during_a_stream(self):
        # live 2026-10-02: Hugo's new file was in Plex, but the lane held every swap for an hour
        # because someone was watching Dahmer
        self._done()
        rf, an, _ps = self._flush(size=90, watching={"count": 1, "files": {"Dahmer S01E07.mkv"}})
        rf.assert_not_called()
        an.assert_not_called()
        self.assertFalse(self.e().get("plex_pending"))

    def test_a_movie_never_rescanned_stops_holding_the_swaps_after_its_deadline(self):
        self._done()
        dvbook.update(NAME, finished=int(time.time()) - dvlane.PLEX_UNSEEN_GIVE_UP_SECS - 1)
        self._flush(size=100, watching={"count": 1, "files": {"x.mkv"}})   # streams all evening
        self.assertFalse(self.e().get("plex_pending"))

    def test_nothing_while_plex_is_busy_or_unreachable(self):
        self._done()
        for busy in (["media.generate.loudness"], None):
            rf, an, _ps = self._flush(busy=busy)
            rf.assert_not_called()
            an.assert_not_called()

    def test_nothing_while_anyone_is_streaming(self):
        self._done()
        rf, _an, _ps = self._flush(watching={"count": 1, "files": {"x.mkv"}})
        rf.assert_not_called()

    def test_the_rescan_picking_up_the_new_file_needs_no_re_read(self):
        self._done()
        self._flush()                                           # rescan
        rf, an, ps = self._flush(size=90)                       # Plex has the new size
        rf.assert_not_called()
        an.assert_not_called()
        self.assertFalse(self.e().get("plex_pending"))

    def test_a_rescan_that_missed_it_gets_a_re_read_only_after_the_grace(self):
        self._done()
        self._flush()
        _rf, an, _ps = self._flush(size=100)                    # still the old file's size
        an.assert_not_called()
        self.assertTrue(self.e().get("plex_pending"))
        dvbook.update(NAME, plex_rescanned=int(time.time()) - dvlane.PLEX_RESCAN_GRACE_SECS - 1)
        _rf, an, _ps = self._flush(size=100)
        an.assert_called_once_with("5")
        self.assertFalse(self.e().get("plex_pending"))

    def test_the_next_movie_waits_for_the_first_to_be_settled(self):
        self._done()
        other = self._second()
        self._flush()                                           # first rescanned
        rf, _an, _ps = self._flush(size=100)                    # first not settled yet
        rf.assert_not_called()
        self._flush(size=90)                                    # first settled
        rf, _an, _ps = self._flush()                            # now the second
        rf.assert_called_once()
        self.assertTrue(dvbook.entry(other).get("plex_rescanned"))

    def test_transfers_ease_off_while_plex_is_busy_whatever_the_stream_setting(self):
        with mock.patch.object(self.lane, "_throttle_on", return_value=False), \
             mock.patch.object(dvlane.plex, "heavy_activities", return_value=["media.generate.chapter.thumbs"]):
            self.assertEqual(self.lane._limit(), dvlane.THROTTLE_BPS)
        self.lane._plex_busy_cache = (0.0, None)
        with mock.patch.object(self.lane, "_throttle_on", return_value=False), \
             mock.patch.object(dvlane.plex, "heavy_activities", return_value=[]):
            self.assertIsNone(self.lane._limit())


class SwapWaitsForPlex(_Lane):
    """Plex notices a changed file on its own, and the movies sit in a few flat folders: swaps that
    pile up while it is busy would all be found by its next scan at once (review 2026-10-02). One
    unsettled replaced movie at a time."""

    IDLE = {"count": 0, "files": set()}

    def _staged(self):
        self._converted()
        dvbook.update(NAME, phase="swap")

    def _other_pending(self):
        host2 = "/volume1/Media/Movies/Other (2001).mkv"
        dvbook.seed([{"nas_path": host2, "size_bytes": 100, "enhancement_layer": "MEL",
                      "plex_rating_key": "6", "plex_section": "2"}])
        dvbook.add([{"name": os.path.basename(host2), "dir": "/Media/Movies", "bytes": 100}])
        dvbook.update(os.path.basename(host2), state=dvbook.DONE, plex_pending=True, size_out=90,
                      plex_rescanned=int(time.time()))
        return os.path.basename(host2)

    def _ship_with(self, waits, busy_seq=None, settle_on=None, other=None):
        """Run _ship to the swap; `waits` = how many waits before stopping. Returns the notes."""
        notes, n = [], {"w": 0}
        def wait(_s):
            notes.append((self.lane._status.get("ship") or {}).get("note"))
            n["w"] += 1
            if settle_on is not None and n["w"] == settle_on and other:
                dvbook.update(other, plex_pending=False)
            return n["w"] > waits
        busy = iter(busy_seq) if busy_seq else None
        with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: (90, 7) if p.endswith(".part") else (100, 7)), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=self.IDLE), \
             mock.patch.object(dvlane.plex, "heavy_activities",
                               side_effect=(lambda: next(busy)) if busy else (lambda: [])), \
             mock.patch.object(dvlane.plex, "part_size", return_value=None), \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=True), \
             mock.patch.object(nas_ftp, "swap") as sw, \
             mock.patch.object(self.lane, "_wait", side_effect=wait), \
             mock.patch.object(self.lane, "_finish"):
            try:
                self.lane._ship(self.e(), self.ev)
            except nas_ftp.Stopped:
                pass
        return notes, sw

    def test_no_swap_while_another_replaced_movie_is_still_settling(self):
        self._staged()
        other = self._other_pending()
        notes, sw = self._ship_with(waits=2)
        sw.assert_not_called()
        self.assertTrue(notes and "still taking in Other" in notes[0])

    def test_the_swap_goes_once_the_other_has_settled(self):
        self._staged()
        other = self._other_pending()
        notes, sw = self._ship_with(waits=5, settle_on=1, other=other)
        sw.assert_called_once()

    def test_the_wait_itself_settles_the_other_movie_with_plex(self):
        # the ship thread is the only caller of _flush_plex: waiting without calling it would wait
        # forever for a movie only it can settle
        self._staged()
        other = self._other_pending()
        with mock.patch.object(dvlane.plex, "part_size",
                               side_effect=lambda rk, name: 90 if rk == "6" else None):
            n = {"w": 0}
            def wait(_s):
                n["w"] += 1
                return n["w"] > 3
            with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: (90, 7) if p.endswith(".part") else (100, 7)), \
                 mock.patch.object(dvlane.plex, "session_detail", return_value=self.IDLE), \
                 mock.patch.object(nas_ftp, "swap") as sw, \
                 mock.patch.object(self.lane, "_wait", side_effect=wait), \
                 mock.patch.object(self.lane, "_finish"):
                self.lane._ship(self.e(), self.ev)
        sw.assert_called_once()
        self.assertFalse(dvbook.entry(other).get("plex_pending"))

    def test_no_swap_while_plex_is_busy(self):
        self._staged()
        self.lane._plex_busy_cache = (0.0, None)
        with mock.patch.object(dvlane, "PLEX_CACHE_SECS", 0):
            notes, sw = self._ship_with(waits=2, busy_seq=[["media.generate.loudness"]] * 10)
        sw.assert_not_called()
        self.assertTrue(any(n and "busy analyzing" in n for n in notes))


class PlexGivesUp(_Lane):
    IDLE = {"count": 0, "files": set()}

    def _flush(self, refresh=True, analyze=False, size=None):
        with mock.patch.object(dvlane.plex, "session_detail", return_value=self.IDLE), \
             mock.patch.object(dvlane.plex, "part_size", return_value=size), \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=refresh), \
             mock.patch.object(dvlane.plex, "analyze", return_value=analyze):
            self.lane._plex_busy_cache = (0.0, None)
            self.lane._flush_plex()

    def test_a_rescan_that_keeps_failing_is_let_go(self):
        dvbook.update(NAME, state=dvbook.DONE, plex_pending=True, size_out=90)
        for _ in range(dvlane.PLEX_MAX_TRIES - 1):
            self._flush(refresh=False)
            self.assertTrue(self.e().get("plex_pending"))
        self._flush(refresh=False)
        self.assertFalse(self.e().get("plex_pending"))
        self.assertIn("could not tell Plex", dvlane.logbook.event.call_args.args[0])

    def test_an_item_plex_never_takes_is_let_go_after_the_deadline(self):
        dvbook.update(NAME, state=dvbook.DONE, plex_pending=True, size_out=90,
                      plex_rescanned=int(time.time()) - dvlane.PLEX_GIVE_UP_SECS - 1)
        self._flush(size=None)                                    # a stale rating key: no size
        self.assertFalse(self.e().get("plex_pending"))

    def test_the_status_says_why_transfers_are_capped(self):
        self.lane._plex_busy_cache = (0.0, ["media.generate.loudness"])
        self.assertEqual(self.lane._throttle_state(), {"throttled": True, "throttle_why": "plex-busy"})
        self.lane._plex_busy_cache = (0.0, [])
        with mock.patch.object(self.lane, "_throttle_on", return_value=True):
            self.lane._plex = (0.0, {"count": 1, "files": set()})
            self.assertEqual(self.lane._throttle_state(), {"throttled": True, "throttle_why": "watching"})
            self.lane._plex = (0.0, {"count": 0, "files": set()})
            self.assertEqual(self.lane._throttle_state(), {"throttled": False, "throttle_why": None})


class OneTransferAtATime(unittest.TestCase):
    """User-dictated 2026-09-30: "it should only upload/download one thing at a time"."""

    def test_the_gate_serves_in_the_order_asked(self):
        gate, order, started = dvlane.TransferGate(), [], []
        gate.acquire("first")
        def want(tag):
            started.append(tag)
            gate.acquire(tag, poll=0.01)
            order.append(tag)
            gate.release()
        threads = []
        for tag in ("second", "third", "fourth"):
            t = threading.Thread(target=want, args=(tag,))
            t.start()
            threads.append(t)
            while tag not in started:
                pass
            import time as _t
            _t.sleep(0.05)                     # each is in line before the next asks
        self.assertEqual(gate.holder, "first")
        gate.release()
        for t in threads:
            t.join(5)
        self.assertEqual(order, ["second", "third", "fourth"])
        self.assertIsNone(gate.holder)

    def test_a_stopped_waiter_leaves_the_line_without_holding_up_the_next(self):
        gate = dvlane.TransferGate()
        gate.acquire("upload of A")
        stop = threading.Event()
        def check():
            if stop.is_set():
                raise nas_ftp.Stopped("stopped")
        errs = []
        def waiter():
            try:
                gate.acquire("download of B", check, poll=0.01)
            except nas_ftp.Stopped as ex:
                errs.append(ex)
        t = threading.Thread(target=waiter)
        t.start()
        stop.set()
        t.join(5)
        self.assertEqual(len(errs), 1)
        gate.release()
        gate.acquire("download of C", poll=0.01)      # the line is not stuck behind B
        self.assertEqual(gate.holder, "download of C")


class LaneNeverOverlapsTransfers(_Lane):
    def test_a_download_and_an_upload_queued_together_run_one_after_the_other(self):
        """Movie A converted and uploading, movie B downloading — never both at once."""
        import time as _t
        other = "/volume1/Media/Movies/Other (1990) [2160p DV].mkv"
        dvbook.seed([{"nas_path": other, "size_bytes": 100, "enhancement_layer": "MEL"}])
        dvbook.add([{"name": os.path.basename(other), "title": "Other", "bytes": 100}])
        self._converted()                                     # NAME is ready to upload
        live, peak, log = [0], [0], []
        guard = threading.Lock()
        def busy(tag):
            with guard:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                log.append(("start", tag))
            _t.sleep(0.3)
            with guard:
                live[0] -= 1
                log.append(("end", tag))
        def fake_download(host, local, size, **kw):
            busy("download")
            with open(local, "wb") as fh:
                fh.write(b"s" * size)
        idle = {"count": 0, "files": set()}
        with mock.patch.object(nas_ftp, "stat", side_effect=lambda p: None if p.endswith(".part")
                               else (100, 7)), \
             mock.patch.object(nas_ftp, "download", side_effect=fake_download), \
             mock.patch.object(nas_ftp, "upload", side_effect=lambda *a, **k: busy("upload")), \
             mock.patch.object(nas_ftp, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ftp, "discard_stage"), \
             mock.patch.object(nas_ftp, "swap"), \
             mock.patch.object(dvp7, "convert", side_effect=self._fake_convert), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=idle), \
             mock.patch.object(dvlane.plex, "refresh_folder", return_value=True), \
             mock.patch.object(dvlane.plex, "analyze", return_value=True):
            b = dvbook.entry(os.path.basename(other))
            t1 = threading.Thread(target=self.lane._ship, args=(self.e(), threading.Event()))
            t2 = threading.Thread(target=self.lane._fetch, args=(b, threading.Event()))
            t1.start(); t2.start()
            t1.join(10); t2.join(10)
        self.assertEqual(peak[0], 1, log)
        self.assertEqual(sorted(x[1] for x in log if x[0] == "start"), ["download", "upload"])
        self.assertEqual(self.e()["state"], dvbook.DONE)
        self.assertEqual(dvbook.entry(os.path.basename(other))["phase"], "converted")

    def test_the_waiting_step_says_what_it_waits_behind(self):
        self.lane._gate.acquire("upload of Casino Royale (2006)")
        stop = threading.Event()
        seen = []
        def watch():
            while not stop.is_set():
                st = self.lane._status.get("fetch")
                if st and st.get("note"):
                    seen.append(st["note"])
                    stop.set()
        w = threading.Thread(target=watch)
        w.start()
        with self.assertRaises(nas_ftp.Stopped):
            with self.lane._transfer("fetch", self.e(), "download", stop):
                pass
        w.join(5)
        self.lane._gate.release()
        self.assertIn("waiting for the upload of Casino Royale (2006) to finish", seen[0])

    def test_the_waiting_note_is_cleared_the_moment_the_turn_comes(self):
        with self.lane._transfer("fetch", self.e(), "download", threading.Event()):
            st = self.lane._status["fetch"]
            self.assertIsNone(st["note"])
            self.assertEqual(st["phase"], "download")

    def test_a_stop_that_lands_as_the_turn_comes_starts_no_transfer(self):
        stop = threading.Event()
        stop.set()                       # already stopped when the gate is granted
        ran = []
        with self.assertRaises(nas_ftp.Stopped):
            with self.lane._transfer("fetch", self.e(), "download", stop):
                ran.append(1)
        self.assertEqual(ran, [])
        self.assertIsNone(self.lane._gate.holder)       # and the gate is free again

    def test_no_room_by_the_time_the_turn_comes_means_no_download(self):
        with mock.patch.object(nas_ftp, "stat", return_value=(100, 7)), \
             mock.patch.object(dvlane, "fits", return_value=False), \
             mock.patch.object(nas_ftp, "download", side_effect=AssertionError("no room")):
            with self.assertRaises(dvlane._NoRoom):
                self.lane._fetch(self.e(), self.ev)
        self.assertIsNone(self.lane._gate.holder)

    def test_after_the_upload_the_row_no_longer_reads_as_a_transfer(self):
        self._converted()
        seen = []
        def prof(stage):
            seen.append(dict(self.lane._status["ship"]))
            return 8
        with mock.patch.object(nas_ftp, "upload"), \
             mock.patch.object(nas_ftp, "remote_dv_profile", side_effect=prof), \
             mock.patch.object(nas_ftp, "swap"), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=None), \
             mock.patch.object(self.lane, "_wait", side_effect=lambda s: True):
            with self.assertRaises(nas_ftp.Stopped):
                self.lane._ship(self.e(), threading.Event())
        self.assertNotEqual(seen[0]["phase"], "upload")
        self.assertIsNone(seen[0].get("rate"))

    def test_a_waiting_note_does_not_poison_the_rate(self):
        self.lane._set("fetch", self.e(), "download", note="waiting")
        self.lane._set("fetch", self.e(), "download", 30_000_000_000, 40_000_000_000)
        self.assertEqual(self.lane._status["fetch"]["_b0"], 30_000_000_000)




class CachedDownloadsGiveWay(_Lane):
    """The DV 7 queue outranks the pipeline's cached downloads (user, 2026-09-30): a movie that fits
    once the prefetch buffer is cleared clears it, and the prefetcher leaves the lane its room."""

    def setUp(self):
        super().setUp()
        self.disk = {"free": 400 * GB + 100, "cache": 0}   # the movie needs 280: 180 short
        self.scratch = tempfile.mkdtemp()
        self.asks = []

        def evict(nbytes, dry_run=False):
            if dry_run:
                return self.disk["cache"]
            self.asks.append(nbytes)
            freed = min(self.disk["cache"], nbytes)
            self.disk["cache"] -= freed
            self.disk["free"] += freed
            return freed
        self.lane.use_cache(lambda: self.scratch, evict)
        for p in (mock.patch.object(dvlane.shutil, "disk_usage",
                                    side_effect=lambda _p: mock.Mock(free=self.disk["free"])),
                  mock.patch.object(dvlane, "_floor_bytes", return_value=400 * GB)):
            p.start()
            self.patches.append(p)

    def test_a_movie_that_fits_once_the_cache_is_cleared_clears_just_enough(self):
        self.disk["cache"] = 500
        e = self.lane._pick_fetch()
        self.assertEqual(e["name"], NAME)
        self.assertEqual(self.asks, [180])                # its shortfall, not the whole buffer
        self.assertIsNone(self.lane._note)
        self.assertIn("cleared", dvlane.logbook.event.call_args.args[0])

    def test_room_already_there_clears_nothing(self):
        self.disk.update(free=400 * GB + 1000, cache=500)
        self.assertEqual(self.lane._pick_fetch()["name"], NAME)
        self.assertEqual(self.asks, [])

    def test_a_cache_too_small_to_help_is_left_alone(self):
        self.disk["cache"] = 50                           # 100 free + 50 < 280
        self.assertIsNone(self.lane._pick_fetch())
        self.assertEqual(self.asks, [])                   # nothing thrown away for nothing
        self.assertIn("cached downloads it can clear", self.lane._note)

    def test_a_cache_on_another_disk_does_not_count(self):
        self.disk["cache"] = 500
        os.makedirs(dvlane.WORK_ROOT, exist_ok=True)
        real = os.stat
        def stat(p, *a, **k):
            st = real(p, *a, **k)
            return mock.Mock(st_dev=st.st_dev + (1 if p == self.scratch else 0))
        with mock.patch.object(dvlane.os, "stat", side_effect=stat):
            self.assertEqual(self.lane._cache_bytes(), 0)
        self.assertEqual(self.lane._cache_bytes(), 500)

    def test_the_turn_to_transfer_clears_cached_downloads_too(self):
        # Room is judged again when the transfer's turn comes; the cache must give way there as well,
        # not send the movie back to wait for disk.
        self.disk["cache"] = 500
        with mock.patch.object(nas_ftp, "stat", return_value=(100, 7)), \
             mock.patch.object(nas_ftp, "download", side_effect=self._fake_download), \
             mock.patch.object(nas_ftp, "discard_stage"), \
             mock.patch.object(dvp7, "convert", side_effect=self._fake_convert):
            self.lane._fetch(self.e(), self.ev)
        self.assertEqual(self.asks, [180])
        self.assertEqual(self.e()["phase"], "converted")

    def test_the_prefetcher_is_asked_for_nothing_while_the_lane_is_off(self):
        self.lane._active = self.e()
        self.assertEqual(self.lane.reserve_bytes(), 0)

    def test_the_movie_being_fetched_keeps_what_it_will_still_write(self):
        self.lane._active = self.e()
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "source.mkv"), "wb") as fh:
            fh.write(b"s" * 30)
        with mock.patch.object(self.lane, "running", return_value=True):
            self.assertEqual(self.lane.reserve_bytes(), 250)    # 280 - the 30 already written

    def test_a_movie_waiting_for_disk_holds_nothing(self):
        # The buffer it would keep empty is clearable the moment it fits anyway: holding it would
        # only switch the download-ahead off for as long as the wait lasts (review 2026-09-30).
        self.assertIsNone(self.lane._pick_fetch())
        with mock.patch.object(self.lane, "running", return_value=True):
            self.assertEqual(self.lane.reserve_bytes(), 0)

    def test_the_room_is_claimed_before_anything_is_cleared(self):
        # From the moment the clearing starts, the prefetcher must see the reserve, or it can start
        # a download into the very space being made (review 2026-09-30).
        self.disk["cache"] = 500
        seen = []
        evict = self.lane._cache[1]
        def watch(nbytes, dry_run=False):
            if not dry_run:
                seen.append(self.lane.reserve_bytes())
            return evict(nbytes, dry_run=dry_run)
        self.lane._cache = (self.lane._cache[0], watch)
        with mock.patch.object(self.lane, "running", return_value=True):
            self.assertEqual(self.lane._pick_fetch()["name"], NAME)
        self.assertEqual(seen, [280])

    def test_a_clearing_that_falls_short_hands_the_claim_back(self):
        self.disk["cache"] = 500
        self.lane._cache = (self.lane._cache[0], lambda n, dry_run=False: 500 if dry_run else 0)
        self.assertIsNone(self.lane._pick_fetch())
        self.assertIsNone(self.lane._active)

    def test_the_cache_is_measured_once_per_pick(self):
        # 183 open movies used to mean 183 scans of the pipeline's queue every 30 s (review).
        for i in range(3):
            host = f"/volume1/Media/Movies/Other {i} (2001).mkv"
            dvbook.seed([{"nas_path": host, "size_bytes": 100, "enhancement_layer": "MEL"}])
            dvbook.add([{"name": os.path.basename(host), "dir": "/Media/Movies", "bytes": 100}])
        self.disk["cache"] = 50
        calls = []
        evict = self.lane._cache[1]
        def count(nbytes, dry_run=False):
            calls.append(dry_run)
            return evict(nbytes, dry_run=dry_run)
        self.lane._cache = (self.lane._cache[0], count)
        self.assertIsNone(self.lane._pick_fetch())
        self.assertEqual(calls, [True])

    def test_waiting_out_a_nas_outage_holds_nothing(self):
        # An outage can last hours; the movie writes nothing meanwhile (review 2026-09-30).
        self.disk["free"] = 400 * GB + 1000
        seen = []
        def offline(_ex):
            seen.append(self.lane._active)
            self.lane._abort.set()
            return True
        with mock.patch.object(self.lane, "_fetch", side_effect=RuntimeError("ssh: no route")), \
             mock.patch.object(self.lane, "_offline", side_effect=offline):
            self.lane._fetch_loop()
        self.assertEqual(seen, [None])

    def test_the_fetch_loop_marks_the_movie_it_works_on(self):
        self.disk["free"] = 400 * GB + 1000
        seen = []
        def fetch(e, ev):
            seen.append(self.lane._active)
            self.lane._abort.set()
        with mock.patch.object(self.lane, "_fetch", side_effect=fetch):
            self.lane._fetch_loop()
        self.assertEqual(seen[0]["name"], NAME)           # held while it is being fetched...
        self.assertIsNone(self.lane._active)              # ...and let go after


if __name__ == "__main__":
    unittest.main()
