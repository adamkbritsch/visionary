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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None):
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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None):
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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None):
            got["b"] = boundaries
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(stages, "_read_topaz_bounds", return_value=[]), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            stages.run_stage("remux", p, progress=lambda d: None)
        self.assertIsNone(got["b"])                                     # [] → None → ~SEG_SECONDS plan


class ReplaceSourcePolicy(unittest.TestCase):
    """The per-item `replace_source` setting decides the source's fate at upload: ON
    (default) → transfer.replace_original deletes the verified-superseded source; OFF →
    both files stay (Plex two-version). Keyed by p.series (show name / movie title)."""

    def _upload(self, p, *, per_item):
        import settings
        calls = {}
        def fake_replace(*a):
            calls["args"] = a
            return True, "replaced — deleted 1080p original"
        with mock.patch.object(stages.transfer, "upload",
                               return_value=(True, "/Media/x HDR10 DV.mp4", "uploaded 1 bytes")), \
             mock.patch.object(stages.transfer, "replace_original", side_effect=fake_replace), \
             mock.patch.object(settings, "get_show_replace_source", return_value=per_item) as g:
            ok, msg = stages.run_stage("upload", p)
        calls["keys"] = [c.args[0] for c in g.call_args_list]
        return ok, msg, calls

    def test_on_replaces_via_transfer(self):
        p = _paths(tempfile.mkdtemp())
        ok, msg, calls = self._upload(p, per_item=True)
        self.assertTrue(ok)
        self.assertIn("replaced", msg)
        self.assertEqual(calls["args"], ("/Media/x HDR10 DV.mp4", p.nas_source, p.final))
        self.assertEqual(calls["keys"], ["Show"])            # keyed by p.series

    def test_off_keeps_the_source(self):
        p = _paths(tempfile.mkdtemp())
        ok, msg, calls = self._upload(p, per_item=False)
        self.assertTrue(ok)
        self.assertIn("kept", msg)
        self.assertNotIn("args", calls)                      # replace_original never called

    def test_youtube_folder_split_never_consults_the_setting(self):
        import settings
        from orchestrator import youtube_paths
        p = youtube_paths("SomeChannel", "SomeChannel/vid [abcdefghijk]/vid [abcdefghijk].mp4",
                          scratch_dir=tempfile.mkdtemp())
        with mock.patch.object(stages.transfer, "publish_master",
                               return_value=(True, "/Media/YouTube/x.mp4", "published")), \
             mock.patch.object(stages.transfer, "replace_original",
                               side_effect=AssertionError("youtube must not replace")), \
             mock.patch.object(settings, "get_show_replace_source",
                               side_effect=AssertionError("youtube must not consult")):
            ok, _msg = stages.run_stage("upload", p)
        self.assertTrue(ok)


class NormalizeAudioGate(unittest.TestCase):
    """The per-item "Normalize audio" checkbox gates the SMART LOUDNESS BOOST at the remux
    stage: OFF → audio_target_lufs=None (remux's existing boost-off bit-exact copy path).
    The lookup key is p.series — the show name for TV, the movie TITLE for movies, the
    channel FOLDER for YouTube (the same key each item's preset uses)."""

    def _lufs_reaching_remux(self, p, *, per_item, target=-16):
        import types, remux, settings
        got = {}
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None):
            got["lufs"] = audio_target_lufs
            return types.SimpleNamespace(ok=True, reason="ok")
        # A YouTube item tries the SHIP path first; send it to the capped path so the
        # gate-under-test is exercised the same way for every kind.
        ship = types.SimpleNamespace(ok=False, reason="render-over-cap: test")
        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(remux, "remux_ship_render", return_value=ship), \
             mock.patch.object(settings, "get_show_normalize_audio", return_value=per_item) as g, \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": target}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(ok)
        got["key_lookups"] = [c.args[0] for c in g.call_args_list]
        return got

    def test_off_passes_none_for_a_tv_episode(self):
        got = self._lufs_reaching_remux(_paths(tempfile.mkdtemp()), per_item=False)
        self.assertIsNone(got["lufs"])
        self.assertEqual(got["key_lookups"], ["Show"])                  # keyed by the series name

    def test_on_passes_the_global_target_through(self):
        got = self._lufs_reaching_remux(_paths(tempfile.mkdtemp()), per_item=True)
        self.assertEqual(got["lufs"], -16)

    def test_off_gates_a_movie_via_its_title(self):
        p = _paths(tempfile.mkdtemp())
        p.movie, p.series, p.title = True, "Some Movie (2024)", "Some Movie (2024)"
        got = self._lufs_reaching_remux(p, per_item=False)
        self.assertIsNone(got["lufs"])
        self.assertEqual(got["key_lookups"], ["Some Movie (2024)"])     # keyed by the TITLE

    def test_off_gates_a_youtube_item_via_its_folder(self):
        p = _paths(tempfile.mkdtemp())
        p.youtube, p.series = True, "Channel Folder"
        got = self._lufs_reaching_remux(p, per_item=False)
        self.assertIsNone(got["lufs"])
        self.assertEqual(got["key_lookups"], ["Channel Folder"])        # keyed by the FOLDER

    def test_global_zero_stays_off_without_a_per_item_lookup(self):
        got = self._lufs_reaching_remux(_paths(tempfile.mkdtemp()), per_item=True, target=0)
        self.assertIsNone(got["lufs"])                                  # global off wins
        self.assertEqual(got["key_lookups"], [])                        # gate short-circuits

    def test_topaz_writes_boundaries_no_4k_multiplier(self):
        import types, plan, settings, topaz
        ends = [100, 220, 400]                          # topaz scene-cut ends
        for is_4k in (True, False):                     # SAME boundaries either way — no ×4 for 4K
            p = _paths(tempfile.mkdtemp())
            with open(p.source, "w") as fh:
                fh.write("x")
            pl = {"topaz": "clean" if is_4k else "upscale", "scale": 1, "res": "1080p",
                  "fit_height": None, "input": {"is_4k": is_4k}}
            def fake_upscale(cfr, *, segdir, profile, scale, fit_height, on_progress, abort, on_plan,
                             should_pause=None):
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


class FastPathDispatch(unittest.TestCase):
    """HIGH-BITRATE 4K FAST PATH plumbing: topaz no-ops, resolve runs the `single` entry on
    the SOURCE, remux dispatches to the inject path (rpu-only) or the capped path otherwise."""

    RPU_PLAN = {"topaz": "rpu-only", "scale": 1, "res": None, "fit_height": None,
                "resolve": "add_dv", "is_hdr": True, "reason": "4K HDR10 HEVC @ ~15 Mbps"}
    RES_PLAN = {"topaz": "resolve-only", "scale": 1, "res": None, "fit_height": None,
                "resolve": "add_hdr_dv", "is_hdr": False, "reason": "4K SDR HEVC @ ~15 Mbps"}

    def test_topaz_noops_successfully_for_both_modes(self):
        import plan, topaz
        p = _paths(tempfile.mkdtemp())
        for pl in (self.RPU_PLAN, self.RES_PLAN):
            with mock.patch.object(plan, "plan_for", return_value=pl), \
                 mock.patch.object(topaz, "upscale_resumable",
                                   side_effect=AssertionError("must not upscale")):
                ok, msg = stages.run_stage("topaz", p)
            self.assertTrue(ok)
            self.assertIn("skipping upscale", msg)

    def test_resolve_only_topaz_plans_scene_cut_bounds_for_the_remux(self):
        # resolve-only still runs topaz's PLANNING front half: scene detect + grouping →
        # bounds stashed so the capped remux segments at scene cuts (user-dictated).
        import plan, topaz
        d = tempfile.mkdtemp()
        p = _paths(d)
        # 300 frames @ 24 fps with target 90 s would swallow every cut — use the REAL
        # plan_segments with a fps/total shaped so both cuts survive the grouping.
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
             mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(topaz, "total_frames", return_value=30000), \
             mock.patch.object(topaz, "media_timing", return_value=(24.0, 1250.0)), \
             mock.patch.object(topaz, "_cached_scene_frames", return_value=[10000, 20000]):
            ok, msg = stages.run_stage("topaz", p)
            bounds = stages._read_topaz_bounds(p.source_basename)
        self.assertTrue(ok)
        self.assertIn("scene-cut segments planned", msg)
        self.assertEqual(bounds, [10000, 20000, 30000])

    def test_rpu_only_topaz_never_scans(self):
        import plan, topaz
        d = tempfile.mkdtemp()
        p = _paths(d)
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
             mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(topaz, "_cached_scene_frames",
                               side_effect=AssertionError("rpu-only must not scene-scan")):
            ok, msg = stages.run_stage("topaz", p)
            bounds = stages._read_topaz_bounds(p.source_basename)
        self.assertTrue(ok)
        self.assertEqual(bounds, [])

    def test_resolve_only_cached_bounds_skip_the_scan(self):
        import plan, topaz
        d = tempfile.mkdtemp()
        p = _paths(d)
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
             mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(topaz, "_cached_scene_frames",
                               side_effect=AssertionError("cached bounds must skip the scan")):
            stages._write_topaz_bounds(p.source_basename, [500, 900])   # a prior attempt planned
            ok, msg = stages.run_stage("topaz", p)
            bounds = stages._read_topaz_bounds(p.source_basename)
        self.assertTrue(ok)
        self.assertIn("cached", msg)
        self.assertEqual(bounds, [500, 900])

    def test_resolve_only_scan_failure_degrades_to_flat_segments(self):
        import plan, topaz
        d = tempfile.mkdtemp()
        p = _paths(d)
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
             mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(topaz, "total_frames", return_value=0):   # unprobeable
            ok, msg = stages.run_stage("topaz", p)
            bounds = stages._read_topaz_bounds(p.source_basename)
        self.assertTrue(ok)                                # NEVER a stage failure
        self.assertIn("flat remux segments", msg)
        self.assertEqual(bounds, [])

    def test_resolve_runs_single_on_the_source(self):
        import plan
        p = _paths(tempfile.mkdtemp())
        seen = {}
        def boom(cmd, **kw):
            seen["cmd"] = cmd
            raise RuntimeError("stop here")
        import preflight, settings
        # Deterministic pinning-off: on the maintainer's machine the LIVE settings pin a
        # display, which would trip the pre-spawn host guard before Popen is reached.
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(preflight, "chosen_host", return_value=(None, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=boom):
            ok, msg = stages.run_stage("resolve", p)
        self.assertFalse(ok)                              # launch failed on purpose — we only
        cmd = seen["cmd"]                                 # care what it TRIED to run
        self.assertIn("single", cmd)
        self.assertIn(p.source, cmd)                      # the ORIGINAL file, not the segdir
        self.assertNotIn(p.segdir, cmd)
        # Modes are named for the OUTPUT now: an HDR intake still masters to 2000-nit DV.
        self.assertEqual(cmd[cmd.index("single") + 3], "dv2000")
        # argv: [py, resolve_pipeline.py, phase, in, out, mode, bitrate, host]
        self.assertEqual(cmd[6], str(stages.EXPORT_BITRATE_FLOOR_KBPS))   # render video discarded
        self.assertEqual(cmd[7], "-", "unpinned must pass '-' = drive the main display")

    def test_resolve_only_uses_source_bitrate_floor_max(self):
        import plan
        p = _paths(tempfile.mkdtemp())
        seen = {}
        def boom(cmd, **kw):
            seen["cmd"] = cmd
            raise RuntimeError("stop here")
        import preflight, settings
        with mock.patch.object(preflight, "chosen_host", return_value=(None, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(stages, "_source_video_kbps", return_value=90000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=boom):
            stages.run_stage("resolve", p)
        cmd = seen["cmd"]
        self.assertIn("single", cmd)
        self.assertEqual(cmd[cmd.index("single") + 3], "dv1000")   # SDR intake -> 1000-nit DV
        self.assertEqual(cmd[6], "90000")                 # conversion IS the ship — match intake
        self.assertEqual(cmd[7], "-", "unpinned must pass '-' = drive the main display")

    def test_remux_dispatches_to_inject_for_rpu_only(self):
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as inj, \
             mock.patch.object(remux, "remux", side_effect=AssertionError("cap path must not run")):
            ok, msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        args = inj.call_args[0]
        self.assertEqual(args, (p.dv_render, p.source_cfr, p.source, p.final))

    def test_remux_resolve_only_still_uses_the_capped_path(self):
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(stages, "_read_topaz_bounds", return_value=[]), \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("inject must not run for SDR tier")), \
             mock.patch.object(remux, "remux",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as rm:
            ok, msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        rm.assert_called_once()

    def test_remux_threads_should_pause_through(self):
        # run_stage("remux", should_pause=...) must reach remux.remux — the Resolve
        # preemption's between-segment yield rides this.
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        flag = lambda: False
        with mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(stages, "_read_topaz_bounds", return_value=[]), \
             mock.patch.object(remux, "remux",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as rm:
            stages.run_stage("remux", p, should_pause=flag)
        self.assertIs(rm.call_args.kwargs.get("should_pause"), flag)

    def test_cleanup_sweeps_inject_transients(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        for suffix in (".src.hevc", ".inject.hevc"):
            with open(p.final + suffix, "w") as fh:
                fh.write("x")
        ok, _msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(p.final + ".src.hevc"))
        self.assertFalse(os.path.exists(p.final + ".inject.hevc"))


class _FakeResolveProc:
    """Scripted resolve_pipeline subprocess: yields its lines, then polls done."""
    def __init__(self, lines, rc):
        self.stdout = iter(lines)
        self.returncode = rc
    def poll(self): return self.returncode
    def kill(self): pass


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously on start() — the
    reader must have consumed the fake proc's stdout before the poll loop reads it."""
    def __init__(self, target=None, daemon=None, **kw): self._t = target
    def start(self): self._t()


class MezzanineFallback(unittest.TestCase):
    """FAST-PATH compat mezzanine: a Resolve INGEST failure (VP9/AV1 the pinned Resolve
    can't decode → 'IMPORT FAILED'/'FPS UNREADABLE') builds a lightweight HEVC Main10
    mezzanine and retries ONCE; render failures and the normal segment path never do."""

    RES_PLAN = FastPathDispatch.RES_PLAN

    def test_command_is_lightweight_hevc_main10_cfr(self):
        cmd = stages.build_mezzanine_command("/ff", "/in.webm", "/out_mezz.mp4",
                                             rate="24000/1001",
                                             color={"primaries": "bt709",
                                                    "transfer": "bt709", "space": "bt709"},
                                             kbps=123456)
        self.assertIn("hevc_videotoolbox", cmd)               # hardware, plain ffmpeg — no Topaz/ProRes
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "123456k")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "p010le")      # Main10
        self.assertEqual(cmd[cmd.index("-tag:v") + 1], "hvc1")
        self.assertEqual(cmd[cmd.index("-r") + 1], "24000/1001")        # uniform PTS out
        self.assertIn("cfr", cmd)
        self.assertIn("-an", cmd)                             # video only — audio ships from the CFR
        self.assertNotIn("-map 0:s", " ".join(cmd))
        self.assertIn("bt709", cmd)                           # color tags carried

    def _run_resolve(self, p, procs, mezz_ok=True, dv_ok_sequence=(False, True)):
        """Drive _resolve with scripted subprocess passes; returns (ok, msg, popen_calls, mezz_calls)."""
        import plan
        calls, mezz_calls = [], []
        mezz = stages.mezzanine_path(p.source)
        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return procs.pop(0)
        def fake_mezz(pp, abort, progress=None, src=None):
            mezz_calls.append(pp.source)
            if not mezz_ok:
                return False, "encode blew up"
            with open(mezz, "w") as fh:
                fh.write("m")
            return True, mezz
        import settings
        with mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=20000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.threading, "Thread", _InlineThread), \
             mock.patch.object(stages.time, "sleep"), \
             mock.patch.object(stages, "_vstream", return_value=None), \
             mock.patch.object(stages, "_is_dv81", side_effect=list(dv_ok_sequence)), \
             mock.patch.object(stages, "_build_mezzanine", side_effect=fake_mezz), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=fake_popen):
            ok, msg = stages.run_stage("resolve", p)
        return ok, msg, calls, mezz_calls

    def test_import_failure_builds_mezz_and_retries_once(self):
        p = _paths(tempfile.mkdtemp())
        ok, msg, calls, mezz_calls = self._run_resolve(
            p, [_FakeResolveProc(["IMPORT FAILED: 0/1 clips\n"], 1),
                _FakeResolveProc(["render ok\n"], 0)])
        self.assertTrue(ok)
        self.assertIn("compat mezzanine", msg)
        self.assertEqual(len(calls), 2)
        self.assertEqual(mezz_calls, [p.source])
        mezz = stages.mezzanine_path(p.source)
        self.assertIn(mezz, calls[1])                          # retry ingests the mezzanine
        self.assertNotIn(p.source, calls[1])
        self.assertEqual(calls[1][-1], calls[0][-1])           # export bitrate from the ORIGINAL
        self.assertFalse(os.path.exists(mezz))                 # big temp deleted after success

    def test_retry_failure_stops_after_second_run(self):
        p = _paths(tempfile.mkdtemp())
        ok, _msg, calls, mezz_calls = self._run_resolve(
            p, [_FakeResolveProc(["IMPORT FAILED: 0/1 clips\n"], 1),
                _FakeResolveProc(["FPS UNREADABLE from the imported clip\n"], 1)],
            dv_ok_sequence=(False, False))
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)                        # never a third attempt
        self.assertEqual(len(mezz_calls), 1)
        # KEPT on failure (changed 2026-08-06): the next attempt REUSES it instead of
        # re-encoding ~10 minutes; cleanup sweeps a stray. Success still deletes it.
        self.assertTrue(os.path.exists(stages.mezzanine_path(p.source)))

    def test_mezz_build_failure_fails_the_stage(self):
        p = _paths(tempfile.mkdtemp())
        ok, msg, calls, _ = self._run_resolve(
            p, [_FakeResolveProc(["IMPORT FAILED: 0/1 clips\n"], 1)], mezz_ok=False,
            dv_ok_sequence=(False,))
        self.assertFalse(ok)
        self.assertIn("compat mezzanine failed", msg)
        self.assertEqual(len(calls), 1)

    def test_unrelated_failure_never_builds_a_mezzanine(self):
        p = _paths(tempfile.mkdtemp())
        ok, _msg, calls, mezz_calls = self._run_resolve(
            p, [_FakeResolveProc(["render exploded at 42%\n"], 1)], dv_ok_sequence=(False,))
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(mezz_calls, [])

    def test_segment_path_never_builds_a_mezzanine(self):
        import plan
        p = _paths(tempfile.mkdtemp())
        normal = dict(self.RES_PLAN, topaz="upscale")          # the ordinary Topaz-segment path
        calls = []
        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return _FakeResolveProc(["IMPORT FAILED: 0/1 clips\n"], 1)
        import settings
        with mock.patch.object(plan, "plan_for", return_value=normal), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=20000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.threading, "Thread", _InlineThread), \
             mock.patch.object(stages.time, "sleep"), \
             mock.patch.object(stages, "_vstream", return_value=None), \
             mock.patch.object(stages, "_is_dv81", return_value=False), \
             mock.patch.object(stages, "_build_mezzanine",
                               side_effect=AssertionError("segment path must never mezzanine")), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=fake_popen):
            ok, _msg = stages.run_stage("resolve", p)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn(p.segdir, calls[0])                      # episode mode ingests the segdir

    def test_cleanup_sweeps_a_stranded_mezzanine(self):
        p = _paths(tempfile.mkdtemp())
        mezz = stages.mezzanine_path(p.source)
        with open(mezz, "w") as fh:
            fh.write("x")
        ok, _msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(mezz))


class OutputModeOverride(unittest.TestCase):
    """The per-item override picks the Resolve project. "auto" must keep the long-standing
    rule exactly; the three explicit values pin it regardless of what the source is."""

    RES_PLAN = dict(resolve="run", topaz="upscale", is_hdr=False)

    def _mode(self, override, is_hdr):
        """Drive the REAL resolve stage and read back the mode argv it hands
        resolve_pipeline.py — mirroring the branch here would test nothing."""
        import plan, settings
        pl = dict(self.RES_PLAN, is_hdr=is_hdr)
        calls = []
        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return _FakeResolveProc(["render exploded\n"], 1)   # fail fast; argv is the point
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(settings, "get_show_output_mode", return_value=override), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(plan, "plan_for", return_value=pl), \
             mock.patch.object(stages, "_source_video_kbps", return_value=20000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.threading, "Thread", _InlineThread), \
             mock.patch.object(stages.time, "sleep"), \
             mock.patch.object(stages, "_vstream", return_value=None), \
             mock.patch.object(stages, "_is_dv81", return_value=False), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=fake_popen):
            stages.run_stage("resolve", p)
        self.assertTrue(calls, "the resolve stage never spawned resolve_pipeline")
        # argv: [python, resolve_pipeline.py, episode|single, in, out, MODE, bitrate]
        return calls[0][5]

    def test_auto_is_unchanged_behaviour(self):
        self.assertEqual(self._mode("auto", is_hdr=False), "dv1000")
        self.assertEqual(self._mode("auto", is_hdr=True), "dv2000")

    def test_an_unset_override_is_also_auto(self):
        self.assertEqual(self._mode("", is_hdr=True), "dv2000")
        self.assertEqual(self._mode(None, is_hdr=False), "dv1000")

    def test_an_override_wins_over_the_source(self):
        # Deliberately contradictory: an SDR source pinned to 2000-nit, an HDR source to SDR.
        self.assertEqual(self._mode("dv2000", is_hdr=False), "dv2000")
        self.assertEqual(self._mode("sdr", is_hdr=True), "sdr")
        self.assertEqual(self._mode("dv1000", is_hdr=True), "dv1000")

    def test_the_automatic_rule_can_never_pick_sdr(self):
        """User-dictated invariant: automatically it is ALWAYS Dolby Vision — 1000-nit for
        an SDR intake, 2000-nit for an HDR one. A non-DV master is a manual choice only, so
        no source, and no junk left in the setting, may fall through to it."""
        for stored in ("auto", "", None, "SDR", "sdr ", "hdr", "dv3000", 0, True):
            for is_hdr in (False, True):
                got = self._mode(stored, is_hdr=is_hdr)
                self.assertNotEqual(got, "sdr", f"{stored!r}/is_hdr={is_hdr} fell through to SDR")
                self.assertEqual(got, "dv2000" if is_hdr else "dv1000")

    def test_the_sdr_mode_is_the_only_headless_one(self):
        import resolve_pipeline as RP
        self.assertFalse(RP.is_dv_mode("sdr"))       # no DV -> no Analyze All -> no screen
        self.assertTrue(RP.is_dv_mode("dv1000"))
        self.assertTrue(RP.is_dv_mode("dv2000"))

    def test_each_mode_resolves_to_a_distinct_project_list(self):
        import resolve_pipeline as RP
        lists = [RP.SDR_OUT_PROJECTS, RP.DV1000_PROJECTS, RP.DV2000_PROJECTS]
        firsts = [l[0] for l in lists]
        self.assertEqual(len(set(firsts)), 3)        # no two modes prefer the same project


class HostDisplayFailSafe(unittest.TestCase):
    """A pinned display that is unplugged, asleep or unmovable must DEFER the item —
    never quietly fall back to the main display, which is the exact screen the user
    pinned Resolve away from."""

    RES_PLAN = dict(resolve="run", topaz="upscale", is_hdr=False)

    def _resolve_with_output(self, out_lines, host_key="-"):
        import plan, preflight
        p = _paths(tempfile.mkdtemp())
        host = None if host_key == "-" else {"key": host_key, "name": "HDMI"}
        calls = []

        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return _FakeResolveProc(out_lines, 3)

        import settings
        with mock.patch.object(plan, "plan_for", return_value=dict(self.RES_PLAN)), \
             mock.patch.object(preflight, "chosen_host", return_value=(host, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=20000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.threading, "Thread", _InlineThread), \
             mock.patch.object(stages.time, "sleep"), \
             mock.patch.object(stages, "_vstream", return_value=None), \
             mock.patch.object(stages, "_is_dv81", return_value=False), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=fake_popen):
            ok, msg = stages.run_stage("resolve", p)
        return ok, msg, calls

    def test_an_unavailable_host_defers_rather_than_failing_the_episode(self):
        ok, msg, _ = self._resolve_with_output(
            ["HOST_UNAVAILABLE uuid:HDMI not attached\n"], host_key="uuid:HDMI")
        self.assertFalse(ok)
        self.assertIn("host-display", msg)
        self.assertIn("not attached", msg)

    def test_an_ordinary_render_failure_is_still_an_ordinary_failure(self):
        # The deferral must not swallow real failures — those still count toward the
        # give-up threshold.
        ok, msg, _ = self._resolve_with_output(["render exploded at 42%\n"])
        self.assertFalse(ok)
        self.assertNotIn("host-display", msg)

    def test_the_chosen_host_key_is_what_reaches_the_subprocess(self):
        _ok, _msg, calls = self._resolve_with_output(["boom\n"], host_key="uuid:HDMI")
        self.assertEqual(calls[0][7], "uuid:HDMI")

    def test_unpinned_passes_the_main_display_sentinel(self):
        _ok, _msg, calls = self._resolve_with_output(["boom\n"])
        self.assertEqual(calls[0][7], "-")


class ScopeHdr10IsNeverReEncoded(unittest.TestCase):
    """END TO END, through the REAL plan: a 2.39:1 HDR10 source must reach remux_inject.

    The tier used to require width==3840 AND height==2160 exactly, so a scope film (3840x1600)
    — i.e. most blockbusters — fell through to resolve-only and had its HDR10 video re-encoded
    by the capped x265 path. Composing plan + stages here rather than mocking the plan is the
    point: the bug lived in the seam between them."""

    SCOPE_HDR10 = dict(is_4k=True, is_hdr=True, is_dv=False, is_cfr=True,
                       transfer="smpte2084", codec="hevc", pix_fmt="yuv420p10le",
                       width=3840, height=1600, video_kbps=68000)

    def _real_plan(self, **over):
        import plan
        return plan.choose_plan({**self.SCOPE_HDR10, **over}, passthrough_min_kbps=12000)

    def test_the_real_plan_says_rpu_only(self):
        self.assertEqual(self._real_plan()["topaz"], "rpu-only")

    def test_and_the_remux_stage_injects_instead_of_re_encoding(self):
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self._real_plan()), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as inj, \
             mock.patch.object(remux, "remux",
                               side_effect=AssertionError("HDR10 must NOT be re-encoded")):
            ok, _msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        self.assertEqual(inj.call_args[0], (p.dv_render, p.source_cfr, p.source, p.final))

    def test_a_low_bitrate_scope_hdr10_also_injects(self):
        # The bitrate threshold must not drag HDR10 back onto the re-encode path.
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self._real_plan(video_kbps=4000)), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")), \
             mock.patch.object(remux, "remux",
                               side_effect=AssertionError("HDR10 must NOT be re-encoded")):
            ok, _msg = stages.run_stage("remux", p)
        self.assertTrue(ok)

    def test_topaz_is_skipped_and_never_scene_scans_it(self):
        import plan
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self._real_plan()), \
             mock.patch.object(stages, "_plan_fast_path_bounds",
                               side_effect=AssertionError("rpu-only copies the stream — no segments")):
            ok, _msg = stages.run_stage("topaz", p)
        self.assertTrue(ok)


class DownloadRefusesDolbyVision(unittest.TestCase):
    """An already-DV source that slipped past queue exclusion is refused the moment the
    file can be probed — BEFORE the expensive CFR re-encode — with a "permanent:" tag so
    the run loop parks it once instead of climbing the 60s retry ladder."""

    def test_dv_source_is_refused_before_the_cfr_pass(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:            # source already on disk (or just pulled)
            fh.write("x")
        with mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch("plan.probe_input", return_value={"is_dv": True}), \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.to_cfr") as cfr:
            ok, msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        self.assertTrue(str(msg).startswith("permanent:"))     # the run loop's park signal
        self.assertIn("already Dolby Vision", msg)
        cfr.assert_not_called()                    # refused BEFORE the libx264 CFR re-encode
        dl.assert_not_called()

    def test_non_dv_source_proceeds_to_the_cfr_pass(self):
        import topaz
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:
            fh.write("x")
        with mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch("plan.probe_input", return_value={"is_dv": False}), \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.to_cfr", return_value=topaz.CfrResult(
                 ok=True, frames=100, rate="24000/1001", error_tail="")) as cfr:
            ok, msg = stages.run_stage("download", p)
        dl.assert_not_called()
        cfr.assert_called_once()                   # the normal path is untouched
        self.assertTrue(ok)

    def test_permanent_refusal_logs_as_an_event_not_a_failure(self):
        # Expected behavior, not an error: it must never hit the red "Recent issues" banner
        # (the park adds its own single informational line).
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:
            fh.write("x")
        with mock.patch.object(stages.transfer, "download"), \
             mock.patch("plan.probe_input", return_value={"is_dv": True}), \
             mock.patch.object(stages.logbook, "failure") as fail, \
             mock.patch.object(stages.logbook, "event") as ev:
            ok, _msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        fail.assert_not_called()
        ev.assert_called()


class ResolveHostPreSpawnGuard(unittest.TestCase):
    """A PINNED display that is already gone must defer the stage BEFORE the subprocess
    spawns — never silently drive the main display (the documented fail-safe). The guard
    is also the consumer of resolve_host_fallback_main: True = proceed on main instead."""

    RES_PLAN = dict(resolve="run", topaz="upscale", is_hdr=False)

    def _run(self, *, pinning, fallback, why="not attached", priority=("uuid:X",)):
        import plan, preflight, settings
        p = _paths(tempfile.mkdtemp())
        s = dict(settings.DEFAULT_SETTINGS,
                 resolve_host_pinning=pinning, resolve_host_fallback_main=fallback)
        calls = []
        def fake_popen(cmd, **kw):
            calls.append(cmd)
            raise RuntimeError("stop here")     # argv is the point; fail fast after spawn
        with mock.patch.object(plan, "plan_for", return_value=self.RES_PLAN), \
             mock.patch.object(preflight, "chosen_host", return_value=(None, why)), \
             mock.patch.object(settings, "get_settings", return_value=s), \
             mock.patch.object(settings, "get_display_priority", return_value=list(priority)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=20000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=fake_popen):
            ok, msg = stages.run_stage("resolve", p)
        return ok, msg, calls

    def test_unplugged_pinned_display_defers_before_spawn(self):
        ok, msg, calls = self._run(pinning=True, fallback=False)
        self.assertFalse(ok)
        self.assertIn("host-display", msg)                # the retryable-hold reason string
        self.assertIn("not attached", msg)
        self.assertEqual(calls, [])                       # never spawned → main never driven

    def test_fallback_main_true_proceeds_on_main(self):
        _ok, _msg, calls = self._run(pinning=True, fallback=True)
        self.assertTrue(calls)                            # spawned...
        self.assertEqual(calls[0][7], "-")                # ...driving the main display

    def test_deliberate_main_choice_is_allowed_through(self):
        _ok, _msg, calls = self._run(pinning=True, fallback=False,
                                     why="chosen display is the main one")
        self.assertTrue(calls)                            # main-as-chosen is not a failure
        self.assertEqual(calls[0][7], "-")

    def test_pinning_off_never_defers(self):
        _ok, _msg, calls = self._run(pinning=False, fallback=False)
        self.assertTrue(calls)
        self.assertEqual(calls[0][7], "-")


class RepairProgressPlumbing(unittest.TestCase):
    """stages._remux forwards on_repair into remux() and publishes the repair keys on the
    same progress surface as the segment bar — full-bar frames, plus which segment is
    being re-capped and its live refill fraction."""

    def test_repair_ticks_carry_segment_and_frames(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort,
                       on_progress, on_plan, should_pause=None, on_repair=None):
            on_plan([100, 200, 300], 300)
            on_progress(300, 300)                          # main encode done → bar at 100%
            on_repair(1, 2, 1, 0, 100)                     # re-capping segment index 1 (of 2 flagged)
            on_repair(1, 2, 1, 50, 100)                    # ...half way through its re-encode
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertTrue(ok)
        plain, start, mid = emitted[0], emitted[1], emitted[2]
        self.assertNotIn("repair_seg", plain)              # ordinary ticks carry no repair keys
        self.assertEqual(start["pct"], 100.0)              # the bar stays full during repair
        self.assertEqual(start["repair_seg"], 2)           # 1-based segment number
        self.assertEqual((start["repair_k"], start["repair_of"]), (1, 2))
        self.assertEqual((start["repair_done"], start["repair_total"]), (0, 100))
        self.assertEqual((mid["repair_done"], mid["repair_total"]), (50, 100))
        self.assertEqual(mid["seg_total"], 3)              # notch plan intact → span computable


class FastPathSkipsTheCfrReencode(unittest.TestCase):
    """The download stage's CFR pass runs copy-only for fast-path plans — hours of libx264
    on a 4K movie whose video bytes nothing reads (live-caught: a 60 GB HDR10 REMUX)."""

    def _cfr_kwargs(self, plan_topaz):
        import plan, topaz
        p = _paths(tempfile.mkdtemp())
        with open(p.source, "w") as fh:
            fh.write("x")
        with mock.patch.object(stages.transfer, "download"), \
             mock.patch.object(plan, "plan_for", return_value={"topaz": plan_topaz}), \
             mock.patch.object(plan, "probe_input", return_value={"is_dv": False}), \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.to_cfr", return_value=topaz.CfrResult(
                 ok=True, frames=100, rate="24000/1001", error_tail="")) as cfr:
            ok, _msg = stages.run_stage("download", p)
        self.assertTrue(ok)
        return cfr.call_args.kwargs

    def test_rpu_only_copies(self):
        self.assertTrue(self._cfr_kwargs("rpu-only")["copy_only"])

    def test_resolve_only_copies(self):
        self.assertTrue(self._cfr_kwargs("resolve-only")["copy_only"])

    def test_upscale_still_true_cfr(self):
        self.assertFalse(self._cfr_kwargs("upscale")["copy_only"])


class YouTubeSkipsTopaz(unittest.TestCase):
    """YouTube items skip Topaz entirely (user-dictated 2026-08-06): Resolve scales them —
    SuperScale 2x for ~1080p sources (a scripting-API clip property, argv 7), the 4K
    timeline's plain scaling otherwise. Single mode ingests the TRUE-CFR file (web sources
    are routinely VFR, and the render gate counts frames against source_cfr)."""

    def _yt(self):
        from orchestrator import youtube_paths
        return youtube_paths("Chan", "YouTube-raw/Chan/vid/vid.mp4", "T",
                             scratch_dir=tempfile.mkdtemp())

    def test_topaz_noops_and_plans_scene_cuts(self):
        import plan
        p = self._yt()
        with mock.patch.object(plan, "plan_for",
                               return_value={"topaz": "upscale", "resolve": "run"}), \
             mock.patch.object(stages, "_plan_fast_path_bounds",
                               return_value=" — 3 scene-cut segments planned for the remux") as b:
            ok, msg = stages.run_stage("topaz", p)
        self.assertTrue(ok)
        self.assertIn("skipping Topaz", msg)
        b.assert_called_once()                    # the capped remux still segments at cuts

    def _resolve_cmd(self, height):
        import plan, preflight, settings
        p = self._yt()
        seen = {}
        def boom(cmd, **kw):
            seen["cmd"] = cmd
            raise RuntimeError("stop here")
        with mock.patch.object(plan, "plan_for",
                               return_value={"resolve": "run", "topaz": "upscale",
                                             "is_hdr": False, "input": {"height": height}}), \
             mock.patch.object(preflight, "chosen_host", return_value=(None, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=8000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=boom):
            stages.run_stage("resolve", p)
        return seen["cmd"], p

    def test_1080p_youtube_gets_superscale_2x(self):
        cmd, p = self._resolve_cmd(1080)
        self.assertIn("single", cmd)              # no Topaz segdir — single-clip mode
        self.assertIn(p.source_cfr, cmd)          # the TRUE-CFR file, not the raw source
        self.assertNotIn(p.segdir, cmd)
        self.assertEqual(cmd[-1], "2")            # SuperScale 2x

    def test_720p_youtube_scales_plainly(self):
        cmd, _p = self._resolve_cmd(720)
        self.assertIn("single", cmd)
        self.assertEqual(cmd[-1], "-")            # no SuperScale below ~1080p

    def test_4k_youtube_no_superscale(self):
        cmd, _p = self._resolve_cmd(2160)
        self.assertEqual(cmd[-1], "-")

    def test_tv_episode_keeps_the_topaz_path(self):
        import plan, preflight, settings
        p = _paths(tempfile.mkdtemp())
        seen = {}
        def boom(cmd, **kw):
            seen["cmd"] = cmd
            raise RuntimeError("stop here")
        with mock.patch.object(plan, "plan_for",
                               return_value={"resolve": "run", "topaz": "upscale",
                                             "is_hdr": False, "input": {"height": 1080}}), \
             mock.patch.object(preflight, "chosen_host", return_value=(None, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=8000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=boom):
            stages.run_stage("resolve", p)
        cmd = seen["cmd"]
        self.assertIn("episode", cmd)             # Topaz-fed segdir mode, unchanged
        self.assertIn(p.segdir, cmd)
        self.assertEqual(cmd[-1], "-")            # lockstep argv: ss present, none


class YouTubeFastRemux(unittest.TestCase):
    """The remux stage ships the render for YouTube items when the peak gate agrees;
    ONLY "render-over-cap" falls back to the normal capped re-encode. Non-YouTube items
    never touch the ship path."""

    def _yt(self):
        from orchestrator import youtube_paths
        return youtube_paths("Chan", "YouTube-raw/Chan/vid/vid.mp4", "T",
                             scratch_dir=tempfile.mkdtemp())

    def _run(self, p, ship_result, expect_fallback):
        import types, plan, remux, settings
        calls = {"ship": 0, "cap": 0}
        def fake_ship(*a, **k):
            calls["ship"] += 1
            return ship_result
        def fake_remux(*a, **k):
            calls["cap"] += 1
            return types.SimpleNamespace(ok=True, reason="capped ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "upscale"}), \
             mock.patch.object(remux, "remux_ship_render", side_effect=fake_ship), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, msg = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertEqual(calls["cap"], 1 if expect_fallback else 0)
        return ok, msg, calls

    def test_ship_success_never_reencodes(self):
        import types
        ok, msg, calls = self._run(self._yt(),
                                   types.SimpleNamespace(ok=True, reason="shipped as-is"),
                                   expect_fallback=False)
        self.assertTrue(ok)
        self.assertEqual(calls["ship"], 1)
        self.assertIn("shipped", msg)

    def test_over_cap_falls_back_to_the_capped_path(self):
        import types
        ok, msg, _ = self._run(self._yt(),
                               types.SimpleNamespace(ok=False, reason="render-over-cap: 81 > 50"),
                               expect_fallback=True)
        self.assertTrue(ok)
        self.assertIn("capped ok", msg)

    def test_other_ship_failures_do_not_silently_fall_back(self):
        import types
        ok, msg, _ = self._run(self._yt(),
                               types.SimpleNamespace(ok=False, reason="mux failed: boom"),
                               expect_fallback=False)
        self.assertFalse(ok)                        # a genuine failure retries the SHIP path

    def test_tv_episode_never_ships(self):
        import types, plan, remux, settings
        p = _paths(tempfile.mkdtemp())
        def fake_remux(*a, **k):
            return types.SimpleNamespace(ok=True, reason="capped ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "upscale"}), \
             mock.patch.object(remux, "remux_ship_render",
                               side_effect=AssertionError("TV must use the capped path")), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _msg = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(ok)

    def test_youtube_render_targets_the_ship_safe_bitrate(self):
        import plan, preflight, settings
        p = self._yt()
        seen = {}
        def boom(cmd, **kw):
            seen["cmd"] = cmd
            raise RuntimeError("stop here")
        with mock.patch.object(plan, "plan_for",
                               return_value={"resolve": "run", "topaz": "upscale",
                                             "is_hdr": False, "input": {"height": 1080}}), \
             mock.patch.object(preflight, "chosen_host", return_value=(None, "test")), \
             mock.patch.object(settings, "get_settings",
                               return_value=dict(settings.DEFAULT_SETTINGS)), \
             mock.patch.object(stages, "_source_video_kbps", return_value=8000), \
             mock.patch.object(stages, "_quit_resolve_focus_app"), \
             mock.patch.object(stages.subprocess, "Popen", side_effect=boom):
            stages.run_stage("resolve", p)
        self.assertEqual(seen["cmd"][6], str(stages.YOUTUBE_RENDER_KBPS))   # 35000, not the 60000 floor


class MezzanineReuse(unittest.TestCase):
    """A lid-close/transient that kills a Resolve pass used to cost a full ~10-minute
    mezzanine REBUILD on every retry. A complete mezzanine (frame-count-verified against
    its input) is now reused; a partial can never match."""

    def test_complete_mezz_is_reused_without_reencoding(self):
        p = _paths(tempfile.mkdtemp())
        mezz = stages.mezzanine_path(p.source)
        with open(mezz, "w") as fh:
            fh.write("m")
        import topaz
        with mock.patch.object(topaz, "total_frames", return_value=1000), \
             mock.patch.object(topaz, "_run_ffmpeg",
                               side_effect=AssertionError("a complete mezz must be reused")):
            ok, path = stages._build_mezzanine(p, None)
        self.assertTrue(ok)
        self.assertEqual(path, mezz)

    def test_frame_mismatch_rebuilds(self):
        p = _paths(tempfile.mkdtemp())
        mezz = stages.mezzanine_path(p.source)
        with open(mezz, "w") as fh:
            fh.write("partial")
        import topaz
        counts = {p.source: 1000, mezz: 400}                  # a cut-short partial
        with mock.patch.object(topaz, "total_frames", side_effect=lambda f: counts.get(f, 0)), \
             mock.patch.object(topaz, "_fps_fraction", return_value="24000/1001"), \
             mock.patch.object(topaz, "source_color", return_value=None), \
             mock.patch.object(topaz, "_run_ffmpeg", return_value=(1, 0, False, "boom")) as rf:
            ok, _ = stages._build_mezzanine(p, None)
        rf.assert_called_once()                               # rebuild attempted, no silent reuse
        self.assertFalse(ok)
