import os
import tempfile
import unittest
from unittest import mock

import stages
from orchestrator import episode_paths


def _paths(scratch):
    return episode_paths("Show", "S01E01", "ep (Extended Cut).mp4",
                         scratch_dir=scratch, nas_tv_root="/Media/TV-Shows")


class Cleanup(unittest.TestCase):
    def test_deletes_all_working_files(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        for f in p.working_files():
            with open(f, "w") as fh:
                fh.write("x")
        ok, msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertFalse(any(os.path.exists(f) for f in p.working_files()))
        self.assertIn("5", msg)   # source + CFR + prores + dv_render + final removed


class DownloadReuse(unittest.TestCase):
    def test_no_redownload_when_source_and_cfr_present(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:        # source already on disk
            fh.write("x")
        # CFR already made → the stage reuses both and never re-pulls or re-encodes
        with mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch("topaz.is_cfr_ready", return_value=True):
            ok, msg = stages.run_stage("download", p)
        dl.assert_not_called()                 # reuse it — Topaz/Resolve/remux share this file
        self.assertTrue(ok)
        self.assertIn("reused", msg)

    def test_makes_cfr_when_source_present_but_cfr_missing(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:        # source on disk, but no CFR yet
            fh.write("x")
        import topaz
        with mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.to_cfr", return_value=topaz.CfrResult(
                 ok=True, frames=100, rate="24000/1001", error_tail="")) as cfr:
            ok, msg = stages.run_stage("download", p)
        dl.assert_not_called()                 # source reused...
        cfr.assert_called_once()               # ...but the CFR pass runs
        self.assertTrue(ok)
        self.assertIn("CFR", msg)


class RemuxProgress(unittest.TestCase):
    """The finishing lane shows the SAME notched segment bar as topaz — _remux surfaces
    notches (each segment end as a 0..1 fraction), seg_done (derived from cumulative frames),
    and seg_total from dvcap's on_plan + on_progress."""

    def test_emits_segment_notches_and_derived_seg_done(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan):
            on_plan([100, 200, 300], 300)      # 3 segments ending at 100/200/300 of 300 frames
            on_progress(0, 300)                # nothing done
            on_progress(150, 300)              # into segment 2 → 1 done
            on_progress(300, 300)              # all done
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertTrue(ok)
        self.assertEqual(emitted[0]["stage"], "remux")
        self.assertEqual(emitted[0]["notches"], [round(100/300, 4), round(200/300, 4), 1.0])
        self.assertEqual(emitted[0]["seg_total"], 3)
        self.assertEqual([e["seg_done"] for e in emitted], [0, 1, 3])   # frames ≥ each end
        self.assertEqual([e["pct"] for e in emitted], [0.0, 50.0, 100.0])


class TopazSegBounds(unittest.TestCase):
    """The remux re-encodes at the SAME scene-cut segment BOUNDARIES as this episode's topaz — the
    cumulative segment-end frames are stashed when topaz plans and read back when the remux runs
    (durable across the hand-off + a relaunch, since topaz's segdir is dropped). No 4K multiplier."""

    def test_bounds_roundtrip(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")):
            self.assertEqual(stages._read_topaz_bounds("ep.mp4"), [])   # absent → [] (→ SEG_SECONDS)
            stages._write_topaz_bounds("ep.mp4", [137, 402, 1000])
            self.assertEqual(stages._read_topaz_bounds("ep.mp4"), [137, 402, 1000])
            stages._write_topaz_bounds("ep.mp4", [])                    # empty never overwrites
            self.assertEqual(stages._read_topaz_bounds("ep.mp4"), [137, 402, 1000])

    def test_remux_passes_topaz_boundaries(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        got = {}
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan):
            got["b"] = boundaries
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(stages, "_read_topaz_bounds", return_value=[137, 402, 1000]), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(ok)
        self.assertEqual(got["b"], [137, 402, 1000])                    # topaz cuts → remux boundaries

    def test_remux_falls_back_when_no_bounds(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        got = {}
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan):
            got["b"] = boundaries
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(stages, "_read_topaz_bounds", return_value=[]), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            stages.run_stage("remux", p, progress=lambda d: None)
        self.assertIsNone(got["b"])                                     # [] → None → ~SEG_SECONDS plan

    def test_topaz_writes_boundaries_no_4k_multiplier(self):
        import types, plan, settings, topaz
        ends = [100, 220, 400]                          # topaz scene-cut ends
        for is_4k in (True, False):                     # SAME boundaries either way — no ×4 for 4K
            p = _paths(tempfile.mkdtemp())
            with open(p.source, "w") as fh:
                fh.write("x")
            pl = {"topaz": "clean" if is_4k else "upscale", "scale": 1, "res": "1080p",
                  "fit_height": None, "input": {"is_4k": is_4k}}
            def fake_upscale(cfr, *, segdir, profile, scale, fit_height, on_progress, abort, deadline, on_plan):
                on_plan(ends, 400)
                return types.SimpleNamespace(ok=True, error_tail="", frames=400)
            d = tempfile.mkdtemp()
            with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
                 mock.patch.object(plan, "plan_for", return_value=pl), \
                 mock.patch.object(settings, "show_topaz_params", return_value={}), \
                 mock.patch.object(settings, "show_preset_key", return_value="digital"), \
                 mock.patch.object(topaz, "total_frames", return_value=400), \
                 mock.patch.object(topaz, "upscale_resumable", side_effect=fake_upscale):
                ok, _ = stages.run_stage("topaz", p)
                self.assertTrue(ok)
                self.assertEqual(stages._read_topaz_bounds(p.source_basename), ends)   # exact cuts, ×1


if __name__ == "__main__":
    unittest.main()
