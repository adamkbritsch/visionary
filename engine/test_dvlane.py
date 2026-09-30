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


class _Lane(unittest.TestCase):
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

    def _converted(self):
        d = dvlane.work_dir(HOST)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "p81.mkv"), "wb") as fh:
            fh.write(b"n" * 90)
        dvbook.update(NAME, state=dvbook.ACTIVE, phase="converted", size_in=100, size_out=90,
                      expect_mtime=7)


class Steps(_Lane):
    def test_fetch_converts_and_hands_off_without_keeping_the_source(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(100, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "download", side_effect=self._fake_download), \
             mock.patch.object(nas_ssh, "discard_stage") as disc, \
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
             mock.patch.object(nas_ssh, "discard_stage"), \
             mock.patch.object(dvp7, "convert", side_effect=dvp7.NotP7("reads profile 5")):
            with self.assertRaisesRegex(RuntimeError, "profile 5"):
                self.lane._fetch(self.e(), self.ev)
        self.assertFalse(dvbook.is_p7(NAME))          # never offered again

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


class EthernetOnly(_Lane):
    def test_the_note_follows_the_live_reason_and_the_movie_never_fails(self):
        reach = iter([False, False, False, True])       # three looks unreachable, then back
        links = iter([{"unavailable": True}, {"unavailable": False}])
        notes = []
        with mock.patch.object(nas_ssh, "reachable", side_effect=lambda: next(reach)), \
             mock.patch.object(nas_ssh, "link", side_effect=lambda: next(links)), \
             mock.patch.object(self.lane, "_wait",
                               side_effect=lambda s: notes.append(self.lane._note) or False):
            self.assertTrue(self.lane._offline(nas_ssh.NoLink("no cable")))
        self.assertIn("waiting for an Ethernet link to the NAS", notes[0])     # no cable
        self.assertEqual(notes[1], "waiting for the NAS (it does not answer over SSH)")  # cable back
        self.assertEqual(self.e()["state"], dvbook.PENDING)          # not failed

    def test_status_says_how_old_the_link_is(self):
        import nas_link
        nas_link.remember({"iface": "en12", "bound": True, "wired": True, "priority": "ethernet"})
        with mock.patch.object(nas_link.time, "time", return_value=nas_link.last()["at"] + 300):
            self.assertEqual(self.lane.status()["link"]["age"], 300)


class Failures(_Lane):
    """Annihilation (2026-09-30): one SSH blip failed it with 43 GB converted and ready."""

    def test_a_failure_after_converting_keeps_the_file_and_retry_resumes_the_upload(self):
        self._converted()
        dvbook.update(NAME, phase="upload")
        self.lane._fail(self.e(), "NAS command failed (255)")
        out = os.path.join(dvlane.work_dir(HOST), "p81.mkv")
        self.assertTrue(os.path.exists(out))
        self.lane._sweep_orphans()                        # a relaunch must not delete it either
        self.assertTrue(os.path.exists(out))
        self.assertTrue(self.lane.retry(NAME))
        e = self.e()
        self.assertEqual((e["state"], e["phase"], e.get("error")), (dvbook.ACTIVE, "upload", None))
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
                raise nas_ssh.Stopped("stopped")
        errs = []
        def waiter():
            try:
                gate.acquire("download of B", check, poll=0.01)
            except nas_ssh.Stopped as ex:
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
        with mock.patch.object(nas_ssh, "stat", side_effect=lambda p: None if p.endswith(".part")
                               else (100, 7, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "download", side_effect=fake_download), \
             mock.patch.object(nas_ssh, "upload", side_effect=lambda *a, **k: busy("upload")), \
             mock.patch.object(nas_ssh, "remote_dv_profile", return_value=8), \
             mock.patch.object(nas_ssh, "discard_stage"), \
             mock.patch.object(nas_ssh, "swap"), \
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
        with self.assertRaises(nas_ssh.Stopped):
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
        with self.assertRaises(nas_ssh.Stopped):
            with self.lane._transfer("fetch", self.e(), "download", stop):
                ran.append(1)
        self.assertEqual(ran, [])
        self.assertIsNone(self.lane._gate.holder)       # and the gate is free again

    def test_no_room_by_the_time_the_turn_comes_means_no_download(self):
        with mock.patch.object(nas_ssh, "stat", return_value=(100, 7, 911, 10, "644")), \
             mock.patch.object(dvlane, "fits", return_value=False), \
             mock.patch.object(nas_ssh, "download", side_effect=AssertionError("no room")):
            with self.assertRaises(dvlane._NoRoom):
                self.lane._fetch(self.e(), self.ev)
        self.assertIsNone(self.lane._gate.holder)

    def test_after_the_upload_the_row_no_longer_reads_as_a_transfer(self):
        self._converted()
        seen = []
        def prof(stage):
            seen.append(dict(self.lane._status["ship"]))
            return 8
        with mock.patch.object(nas_ssh, "upload"), \
             mock.patch.object(nas_ssh, "remote_dv_profile", side_effect=prof), \
             mock.patch.object(nas_ssh, "swap"), \
             mock.patch.object(dvlane.plex, "session_detail", return_value=None), \
             mock.patch.object(self.lane, "_wait", side_effect=lambda s: True):
            with self.assertRaises(nas_ssh.Stopped):
                self.lane._ship(self.e(), threading.Event())
        self.assertNotEqual(seen[0]["phase"], "upload")
        self.assertIsNone(seen[0].get("rate"))

    def test_a_waiting_note_does_not_poison_the_rate(self):
        self.lane._set("fetch", self.e(), "download", note="waiting")
        self.lane._set("fetch", self.e(), "download", 30_000_000_000, 40_000_000_000)
        self.assertEqual(self.lane._status["fetch"]["_b0"], 30_000_000_000)


if __name__ == "__main__":
    unittest.main()
