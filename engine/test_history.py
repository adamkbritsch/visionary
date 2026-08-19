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

    def test_lossless_masters_are_refused_by_codec_not_container(self):
        self.assertFalse(history.can_revise({"nas_path": "/m/x.mkv", "audio": "truehd"})[0])
        self.assertTrue(history.can_revise({"nas_path": "/m/x.mkv", "audio": "aac"})[0])
        self.assertTrue(history.can_revise({"nas_path": "/m/x.mp4"})[0])

    def test_view_exposes_whether_a_row_can_be_revised(self):
        history.record(nas_path="/m/x.mkv", kind="movie", title="x")
        history._mark("/m/x.mkv", audio="truehd")
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

    def test_an_unknown_item_is_refused(self):
        self.assertEqual(history.revise_audio("/nope.mp4")["status"], "unknown-item")

    def test_a_known_lossless_master_is_refused_before_any_download(self):
        history.record(nas_path="/m/x.mkv", kind="movie", title="x")
        history._mark("/m/x.mkv", audio="truehd")
        self.assertEqual(history.revise_audio("/m/x.mkv")["status"], "refused")

    def test_lossless_found_only_after_download_is_refused_there(self):
        # the authoritative check: the name said nothing, the file says TrueHD
        import transfer
        local = os.path.join(self.d, "S04E10.mp4")
        open(local, "wb").close()
        with mock.patch.object(transfer, "download", return_value=(True, local, "ok")), \
             mock.patch.object(history, "_probe_local_audio", return_value=("truehd", "")), \
             mock.patch.object(history, "_swap_in") as sw:
            out = history.revise_audio("/Media/TV/S04E10.mp4", scratch_dir=self.d)
        self.assertEqual(out["status"], "refused")
        self.assertIn("truehd", out["detail"])
        sw.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class LosslessRule(unittest.TestCase):
    """The pipeline never transcodes lossless audio, so a revision must not either. Judged by
    the actual CODEC, not the container — an MKV routinely carries lossy AAC, and refusing on
    the extension blocked exactly the 5.1-AAC master whose audio needed fixing (2026-08-18)."""

    def test_lossless_codecs_are_refused(self):
        for c, p in (("truehd", ""), ("mlp", ""), ("flac", ""), ("alac", ""),
                     ("pcm_s24le", ""), ("dts", "DTS-HD MA")):
            self.assertTrue(history.is_lossless(c, p), c)

    def test_lossy_codecs_are_allowed(self):
        for c, p in (("aac", "LC"), ("aac", "HE-AAC"), ("ac3", ""), ("eac3", ""),
                     ("opus", ""), ("dts", "DTS")):     # plain DTS core is lossy
            self.assertFalse(history.is_lossless(c, p), c)

    def test_an_mkv_with_lossy_audio_is_revisable(self):
        row = {"nas_path": "/m/Good Will Hunting HDR10 DV upscaled.mkv",
               "audio": "aac", "audio_profile": "LC"}
        self.assertTrue(history.can_revise(row)[0])

    def test_an_mkv_with_lossless_audio_is_not(self):
        row = {"nas_path": "/m/x.mkv", "audio": "truehd"}
        ok, why = history.can_revise(row)
        self.assertFalse(ok); self.assertIn("lossless", why)

    def test_unknown_audio_is_allowed_and_checked_later(self):
        # refusing on a guess is what went wrong; the revision re-checks after download
        self.assertTrue(history.can_revise({"nas_path": "/m/x.mkv"})[0])


class Adopt(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = mock.patch.object(history, "BOOK_FILE", os.path.join(d, "h.json"))
        p.start(); self.addCleanup(p.stop)

    def test_adopting_records_the_probed_codec(self):
        with mock.patch.object(history, "probe_audio", return_value=("aac", "LC")):
            out = history.adopt("/Media/Movies/X HDR10 DV upscaled.mkv")
        self.assertEqual(out["status"], "ok")
        row = history.view()[0]
        self.assertEqual(row["audio"], "aac")
        self.assertTrue(row["can_revise"])

    def test_kind_is_inferred_from_the_path(self):
        self.assertEqual(history._kind_of("/Media/TV-Shows/Lost/x.mkv"), "episode")
        self.assertEqual(history._kind_of("/Media/Movies/x.mkv"), "movie")
        self.assertEqual(history._kind_of("/Media/YouTube/Chan/x.mp4"), "youtube")


class OnlyOurOwnMasters(unittest.TestCase):
    """The scan must match the pipeline's OUTPUT TAG in full. series.MASTER_MARKS is the short
    form ("hdr10 dv") and is correct where it is used — inside one show's folder against that
    show's own files — but across whole libraries it matches every natively-Dolby-Vision
    release. Scanning with it adopted 198 untouched UHD remuxes, filled the book to its cap,
    and evicted the real master that had been asked for (live-hit 2026-08-18)."""

    OURS = [
        "Good Will Hunting (1997) [2160p BluRay HEVC 10bit AAC 5.1] HDR10 DV upscaled.mkv",
        "Lost (2004) - S04E10 [2160p] HDR10 DV upscaled.mp4",
        "Show - S01E01 SDR upscaled.mp4",                    # the pinned-SDR variant
    ]
    THEIRS = [
        "Zootopia (2016) [2160p UHD BluRay REMUX HDR10 DV HEVC 10bit TrueHD 7.1 Atmos]-FraMeSToR.mkv",
        "21 Jump Street (2012) [2160p BluRay HDR10 DV HEVC 10bit TrueHD 7.1].mkv",
        "Marvel One-Shot - Agent Carter (2013) [2160p BluRay HDR10 DV HEVC 10bit DTS 5.1].mkv",
        "Some Movie (2020) 1080p BluRay x264.mkv",
    ]

    def test_our_output_is_recognised(self):
        for n in self.OURS:
            self.assertTrue(history.is_our_master(n), n)

    def test_native_dolby_vision_releases_are_not(self):
        # these carry "HDR10 DV" but were never produced by the pipeline
        for n in self.THEIRS:
            self.assertFalse(history.is_our_master(n), n)

    def test_the_short_mark_alone_is_not_enough(self):
        self.assertFalse(history.is_our_master("Thing [HDR10 DV].mkv"))
        self.assertTrue(history.is_our_master("Thing HDR10 DV upscaled.mkv"))
