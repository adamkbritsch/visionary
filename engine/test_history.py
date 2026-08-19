import os
import tempfile
import unittest
from unittest import mock

import history


class Book(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = mock.patch.object(history, "BOOK_FILE", os.path.join(d, "h.json"))
        p.start(); self.addCleanup(p.stop)
        history._revising.clear()

    def test_records_where_the_master_landed(self):
        # the log says an upload happened but not WHERE — this is the only record
        history.record(nas_path="/Media/TV/S04E10.mp4", kind="episode", title="S04E10",
                       series="Lost", ep="S04E10", gain=3.2)
        row = history.view()[0]
        self.assertEqual(row["nas_path"], "/Media/TV/S04E10.mp4")
        self.assertEqual((row["kind"], row["gain"]), ("episode", 3.2))

    def test_newest_first_and_no_duplicates_for_one_path(self):
        for t in ("a", "b"):
            history.record(nas_path="/m/%s.mp4" % t, kind="movie", title=t)
        history.record(nas_path="/m/a.mp4", kind="movie", title="a again")
        rows = history.view()
        self.assertEqual([r["title"] for r in rows], ["a again", "b"])   # re-record moves it up
        self.assertEqual(len(rows), 2)

    def test_the_book_is_capped(self):
        for i in range(history.MAX_ENTRIES + 25):
            history.record(nas_path="/m/%d.mp4" % i, kind="movie", title=str(i))
        self.assertLessEqual(len(history._read()), history.MAX_ENTRIES)

    def test_lossless_mkv_masters_are_refused(self):
        # MKV masters carry TrueHD/DTS-HD MA and the pipeline never transcodes those
        ok, why = history.can_revise({"nas_path": "/m/x.mkv"})
        self.assertFalse(ok)
        self.assertIn("lossless", why)
        self.assertTrue(history.can_revise({"nas_path": "/m/x.mp4"})[0])

    def test_view_exposes_whether_a_row_can_be_revised(self):
        history.record(nas_path="/m/x.mkv", kind="movie", title="x")
        self.assertFalse(history.view()[0]["can_revise"])


class Revise(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.d = d
        p = mock.patch.object(history, "BOOK_FILE", os.path.join(d, "h.json"))
        p.start(); self.addCleanup(p.stop)
        history._revising.clear()
        history.record(nas_path="/Media/TV/S04E10.mp4", kind="episode", title="S04E10")

    def _run(self, *, measured=-22.0, landed=-16.2, up=True, swap=True):
        import remux, transfer, settings
        local = os.path.join(self.d, "S04E10.mp4")
        open(local, "wb").write(b"x")
        seen = {}

        def fake_download(remote, ldir, **kw):
            return True, local, "ok"

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            open(cmd[-1], "wb").write(b"y")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(transfer, "download", side_effect=fake_download), \
             mock.patch.object(transfer, "upload",
                               return_value=(up, "/Media/TV/S04E10.revised.mp4", "ok")), \
             mock.patch.object(history, "_swap_in", return_value=(swap, "swapped in")) as sw, \
             mock.patch.object(remux, "measure_lufs", side_effect=[measured, landed]), \
             mock.patch.object(history.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(settings, "get_settings",
                               return_value={"audio_target_lufs": -16}):
            out = history.revise_audio("/Media/TV/S04E10.mp4", scratch_dir=self.d)
        seen["swap"] = sw
        return out, seen

    def test_it_reboosts_and_swaps_the_master_in(self):
        out, seen = self._run()
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["gain"], 6.0, places=1)          # -22 → -16
        cmd = seen["cmd"]
        # video + subs stream-copied, so Dolby Vision survives; only audio is touched
        self.assertIn("-c", cmd); self.assertIn("copy", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac_at")
        seen["swap"].assert_called_once()
        self.assertEqual(history.view()[0]["gain"], out["gain"])

    def test_an_already_loud_master_is_left_alone(self):
        out, _ = self._run(measured=-15.0)
        self.assertEqual(out["status"], "already-normalized")

    def test_a_bad_landing_is_never_shipped(self):
        out, seen = self._run(measured=-22.0, landed=-11.0)          # overshot
        self.assertEqual(out["status"], "landing-off")
        seen["swap"].assert_not_called()                             # the master is untouched

    def test_a_failed_swap_says_where_the_revised_file_is(self):
        out, _ = self._run(swap=False)
        self.assertEqual(out["status"], "swap-failed")
        self.assertIn("revised_at", out)

    def test_it_refuses_a_second_run_for_the_same_item(self):
        history._revising.add("/Media/TV/S04E10.mp4")
        self.assertEqual(history.revise_audio("/Media/TV/S04E10.mp4")["status"], "already-running")

    def test_unknown_items_and_mkv_are_refused(self):
        self.assertEqual(history.revise_audio("/nope.mp4")["status"], "unknown-item")
        history.record(nas_path="/m/x.mkv", kind="movie", title="x")
        self.assertEqual(history.revise_audio("/m/x.mkv")["status"], "refused")


if __name__ == "__main__":
    unittest.main()
