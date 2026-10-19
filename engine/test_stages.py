import os
import tempfile
import types
import unittest
from unittest import mock

import audiogain
import history
import stages
import test_nativelane as _tn
from orchestrator import episode_paths
from orchestrator import youtube_paths as orch_youtube_paths


def _paths(scratch):
    return episode_paths("Show", "S01E01", "ep (Extended Cut).mp4",
                         scratch_dir=scratch, nas_tv_root="/Media/TV-Shows")


_BOOK_PATCH = None
_HIST_PATCH = None


def setUpModule():
    """_remux records the season's loudness decision. Redirect that book to a throwaway
    path for the whole module — a test run must never write into ~/.topaz-pipeline."""
    global _BOOK_PATCH
    import tempfile as _tf, os as _os
    d = _tf.mkdtemp()
    _BOOK_PATCH = mock.patch.object(audiogain, "BOOK_FILE",
                                    _os.path.join(d, "audio_gain.json"))
    _BOOK_PATCH.start()
    # _upload records into the history book as well — same rule, same throwaway dir. Test
    # fixtures ("S01E01", "ep (Extended Cut)") reached the REAL book before this existed.
    global _HIST_PATCH
    _HIST_PATCH = mock.patch.object(history, "BOOK_FILE", _os.path.join(d, "history.json"))
    _HIST_PATCH.start()


def tearDownModule():
    if _BOOK_PATCH is not None:
        _BOOK_PATCH.stop()
    if _HIST_PATCH is not None:
        _HIST_PATCH.stop()


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
        self.assertIn("6", msg)   # source + CFR + prores + dv_render + final + wide removed


class DownloadReuse(unittest.TestCase):
    def test_no_redownload_when_source_and_cfr_present(self):
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:        # source already on disk
            fh.write("x")
        # CFR already made → the stage reuses both and never re-pulls or re-encodes
        with mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch.object(stages, "_source_complete", return_value=True), \
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
             mock.patch.object(stages, "_source_complete", return_value=True), \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.to_cfr", return_value=topaz.CfrResult(
                 ok=True, frames=100, rate="24000/1001", error_tail="")) as cfr:
            ok, msg = stages.run_stage("download", p)
        dl.assert_not_called()                 # source reused...
        cfr.assert_called_once()               # ...but the CFR pass runs
        self.assertTrue(ok)
        self.assertIn("CFR", msg)

    def test_the_pull_and_the_cfr_are_stages_a_and_b(self):
        """The pull fills the first fifth of the step's bar and the CFR the rest, but each is
        its own lettered stage with its own 0..100 (the bar starts over for Stage B)."""
        p = _paths(tempfile.mkdtemp())
        seen = []
        import topaz
        def dl(remote, local_dir, on_progress=None, **kw):
            on_progress(50, 100)
            with open(p.source, "w") as fh:
                fh.write("x")
            return True, p.source, ""
        def cfr(src, dst, on_progress=None, **kw):
            on_progress(25)
            return topaz.CfrResult(ok=True, frames=100, rate="24000/1001", error_tail="")
        with mock.patch.object(stages.transfer, "download", side_effect=dl), \
             mock.patch("plan.probe_input", return_value={}), \
             mock.patch("topaz.is_cfr_ready", return_value=False), \
             mock.patch("topaz.total_frames", return_value=100), \
             mock.patch("topaz.to_cfr", side_effect=cfr):
            ok, _ = stages.run_stage("download", p, progress=seen.append)
        self.assertTrue(ok)
        pull, make = seen[0], seen[-1]
        self.assertEqual((pull["pct"], pull["stage_idx"], pull["stage_pct"]), (10, 0, 50.0))
        self.assertEqual((make["pct"], make["stage_idx"], make["stage_pct"]), (40, 1, 25.0))
        self.assertEqual(make["stages"], list(stages.DOWNLOAD_STAGES))


class RemuxProgress(unittest.TestCase):
    """The finishing lane shows the SAME notched segment bar as topaz — _remux surfaces
    notches (each segment end as a 0..1 fraction), seg_done (derived from cumulative frames),
    and seg_total from dvcap's on_plan + on_progress."""

    def test_emits_segment_notches_and_derived_seg_done(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None, encode_source=None, audio_gain_db=None):
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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None, encode_source=None, audio_gain_db=None):
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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None, encode_source=None, audio_gain_db=None):
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
             mock.patch.object(stages.transfer, "supersede_masters", return_value=([], "")), \
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
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort, on_progress, on_plan, should_pause=None, on_repair=None, encode_source=None, audio_gain_db=None):
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


class WrongClockInputIsRebuilt(unittest.TestCase):
    """Topaz refuses an input whose declared frame rate its frames do not run at (the
    Sharkboy release, 2026-09-06). The stage then drops that input and every segment
    planned on its clock, so the next pass rebuilds the CFR on the right one. Any other
    failure leaves the files alone."""

    def _run(self, error_tail):
        import types, plan, settings, topaz
        p = _paths(tempfile.mkdtemp())
        for f in (p.source, p.source_cfr):
            with open(f, "w") as fh:
                fh.write("x")
        os.makedirs(p.segdir, exist_ok=True)
        with open(os.path.join(p.segdir, "seg_0000.mov"), "w") as fh:
            fh.write("x")
        pl = {"topaz": "upscale", "scale": 2, "res": "1080p", "fit_height": None,
              "input": {"is_4k": False}}
        d = tempfile.mkdtemp()
        refused = types.SimpleNamespace(ok=False, error_tail=error_tail, frames=0)
        with mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(d, "sb.json")), \
             mock.patch.object(plan, "plan_for", return_value=pl), \
             mock.patch.object(settings, "show_topaz_params", return_value={}), \
             mock.patch.object(settings, "show_preset_key", return_value="digital"), \
             mock.patch.object(topaz, "total_frames", return_value=400), \
             mock.patch.object(topaz, "upscale_resumable", return_value=refused):
            ok, msg = stages.run_stage("topaz", p)
        return p, ok, msg

    def test_a_wrong_clock_input_is_discarded_for_a_rebuild(self):
        import topaz
        p, ok, msg = self._run(f"{topaz.RATE_MISMATCH} the input declares 500/21 fps "
                               f"but its frames run at 24000/1001")
        self.assertFalse(ok)
        self.assertIn("500/21", msg)
        self.assertIn("rebuilds", msg)
        self.assertFalse(os.path.exists(p.source_cfr))
        self.assertFalse(os.path.exists(p.segdir))
        self.assertTrue(os.path.exists(p.source))           # the download itself is kept

    def test_any_other_failure_keeps_the_input(self):
        p, ok, msg = self._run("segment 41 of 41 came out 11899 frames, expected 12826")
        self.assertFalse(ok)
        self.assertIn("11899", msg)
        self.assertTrue(os.path.exists(p.source_cfr))
        self.assertTrue(os.path.exists(os.path.join(p.segdir, "seg_0000.mov")))


class SlotNotes(unittest.TestCase):
    def test_the_stage_surface_says_it_is_waiting_and_then_stops(self):
        seen = []
        note = stages._slot_note(seen.append, "download", "S04E07")
        note("waiting for another app's NAS transfer to finish")
        note(None)
        self.assertEqual(seen[0]["waiting"], "waiting for another app's NAS transfer to finish")
        self.assertNotIn("waiting", seen[1])
        self.assertEqual((seen[1]["stage"], seen[1]["ep"]), ("download", "S04E07"))

    def test_no_surface_no_hook(self):
        self.assertIsNone(stages._slot_note(None, "upload", "S01E01"))

    def test_a_waiting_download_keeps_its_stage(self):
        seen = []
        stages._slot_note(seen.append, "download", "S04E07", {"stage_idx": 0})("waiting")
        self.assertEqual(seen[0]["stage_idx"], 0)

    def test_a_download_we_stopped_is_interrupted_not_a_failure(self):
        self.assertEqual(stages._stopped_is_interrupted("aborted mid-download"),
                         "interrupted: aborted mid-download")
        self.assertEqual(stages._stopped_is_interrupted("aborted: stopped while waiting for the NAS transfer slot"),
                         "interrupted: aborted: stopped while waiting for the NAS transfer slot")
        self.assertEqual(stages._stopped_is_interrupted("download failed: 550"), "download failed: 550")

    def test_the_download_ahead_is_background_and_the_run_is_foreground(self):
        import tempfile
        from orchestrator import episode_paths
        seen = []
        def dl(remote, local_dir, **kw):
            seen.append(kw.get("prio"))
            return False, None, "download failed: test"
        for low in (True, False):
            p = episode_paths("Show", "S01E01", "Show - S01E01.mkv", scratch_dir=tempfile.mkdtemp())
            with mock.patch.object(stages.transfer, "download", side_effect=dl):
                stages._download_body(p, None, None, low_prio=low)
        self.assertEqual(seen, [stages.transfer.BACKGROUND, stages.transfer.FOREGROUND])

if __name__ == "__main__":
    unittest.main()


class FastPathDispatch(unittest.TestCase):
    """HIGH-BITRATE 4K FAST PATH plumbing: topaz no-ops, resolve runs the `single` entry on
    the SOURCE, remux dispatches to the inject path (rpu-only) or the capped path otherwise."""

    RPU_PLAN = {"topaz": "rpu-only", "scale": 1, "res": None, "fit_height": None,
                "dv": "add_dv", "is_hdr": True, "reason": "4K HDR10 HEVC @ ~15 Mbps"}
    RES_PLAN = {"topaz": "dv-only", "scale": 1, "res": None, "fit_height": None,
                "dv": "add_hdr_dv", "is_hdr": False, "reason": "4K SDR HEVC @ ~15 Mbps"}

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
        # dv-only still runs topaz's PLANNING front half: scene detect + grouping →
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


    def test_remux_dispatches_to_inject_for_rpu_only(self):
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=15.0), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as inj, \
             mock.patch.object(remux, "remux", side_effect=AssertionError("cap path must not run")):
            ok, msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        args = inj.call_args[0]
        self.assertEqual(args, (p.dv_render, p.source_cfr, p.source, p.final))

    def test_remux_rpu_only_over_ceiling_revokes_the_stream_copy(self):
        # SHIELD DV ceiling (user-dictated): the whole stream must stay under ~80 Mbps with
        # TrueHD budgeted in, so source VIDEO peaks over the 72 Mbps gate may not ship as a
        # stream copy — the capped path runs instead, re-encoding the SOURCE (encode_source,
        # RPU still from the render), with scene-cut bounds planned lazily.
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=96.4), \
             mock.patch.object(stages, "_plan_fast_path_bounds", return_value=" planned") as pb, \
             mock.patch.object(stages, "_read_topaz_bounds", return_value=[100, 200]), \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("over-ceiling must NOT stream-copy")), \
             mock.patch.object(remux, "remux",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as rm:
            ok, _msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        pb.assert_called_once()
        self.assertEqual(rm.call_args.kwargs.get("encode_source"), p.source_cfr)
        self.assertEqual(rm.call_args.kwargs.get("boundaries"), [100, 200])

    def test_remux_rpu_only_gate_boundary_still_ships_the_copy(self):
        # exactly AT the gate = allowed (the gate already reserves the TrueHD headroom)
        import dvcap, plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(stages, "_source_peak_mbps",
                               return_value=float(dvcap.RPU_SHIP_VIDEO_GATE_MBPS)), \
             mock.patch.object(remux, "remux_inject",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 1, 1, "ok")) as inj, \
             mock.patch.object(remux, "remux", side_effect=AssertionError("cap path must not run")):
            ok, _msg = stages.run_stage("remux", p)
        self.assertTrue(ok)
        inj.assert_called_once()

    def test_remux_rpu_only_unmeasurable_peak_fails_the_attempt(self):
        # can't measure -> neither ship unverified nor burn an hour of x265 on a probe
        # hiccup — fail the attempt cleanly; the retry ladder re-measures.
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self.RPU_PLAN), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=None), \
             mock.patch.object(remux, "remux_inject",
                               side_effect=AssertionError("unverified must not ship")), \
             mock.patch.object(remux, "remux",
                               side_effect=AssertionError("unverified must not re-encode")):
            ok, msg = stages.run_stage("remux", p)
        self.assertFalse(ok)
        self.assertIn("could not measure", msg)

    def test_source_peak_is_measured_once_then_served_from_the_book(self):
        import dvcap
        d = tempfile.mkdtemp()
        p = _paths(d)
        with mock.patch.object(stages, "_PEAKGATE_FILE", os.path.join(d, "pk.json")):
            with mock.patch.object(dvcap, "video_peak_1s_mbps", return_value=88.4) as sweep:
                self.assertEqual(stages._source_peak_mbps(p), 88.4)
            sweep.assert_called_once_with(p.source_cfr)
            with mock.patch.object(dvcap, "video_peak_1s_mbps",
                                   side_effect=AssertionError("book hit must skip the sweep")):
                self.assertEqual(stages._source_peak_mbps(p), 88.4)

    def test_unmeasurable_peak_is_not_cached(self):
        import dvcap
        d = tempfile.mkdtemp()
        p = _paths(d)
        with mock.patch.object(stages, "_PEAKGATE_FILE", os.path.join(d, "pk.json")):
            with mock.patch.object(dvcap, "video_peak_1s_mbps", return_value=0.0):
                self.assertIsNone(stages._source_peak_mbps(p))
            with mock.patch.object(dvcap, "video_peak_1s_mbps", return_value=77.0):
                self.assertEqual(stages._source_peak_mbps(p), 77.0)   # sweep re-ran

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
        # ordinary capped runs encode the RENDER — encode_source is a fallback-only override
        self.assertIsNone(rm.call_args.kwargs.get("encode_source"))

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

    def test_cleanup_sweeps_the_mkv_mux_temps_and_the_old_builds(self):
        import orchestrator as O
        d = tempfile.mkdtemp()
        p = _paths(d)
        legacy = O.legacy_master_names(p)[0]
        made = [p.final + sfx for sfx in (".part", ".tracks.mka", ".fix.hevc", ".capped.hevc")]
        made += [legacy + sfx for sfx in (".tracks.mp4", ".dv.mp4", ".part")]   # pre-Matroska strays
        for f in made:
            open(f, "w").close()
        os.makedirs(legacy + ".remuxsegs")                  # an unmigrated resume dir
        ok, _msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertEqual([f for f in made if os.path.exists(f)], [])
        self.assertFalse(os.path.exists(legacy + ".remuxsegs"))

    def test_cleanup_removes_the_master_that_did_not_ship(self):
        import orchestrator as O
        d = tempfile.mkdtemp()
        p = _paths(d)
        legacy = O.legacy_master_names(p)[0]
        open(legacy, "w").close()                          # an unadopted pre-switch .mp4
        ok, _msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(legacy))
        # ...and the other way round: the adopted .mp4 shipped, an .mkv attempt was left over
        p2 = _paths(tempfile.mkdtemp())
        mkv_attempt = p2.final
        p2.final = O.legacy_master_names(p2)[0]
        for f in (mkv_attempt, mkv_attempt + ".part"):
            open(f, "w").close()
        os.makedirs(mkv_attempt + ".remuxsegs")
        ok, _msg = stages.run_stage("cleanup", p2)
        self.assertTrue(ok)
        self.assertEqual([f for f in (mkv_attempt, mkv_attempt + ".part", mkv_attempt + ".remuxsegs")
                          if os.path.exists(f)], [])


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


class ScopeHdr10IsNeverReEncoded(unittest.TestCase):
    """END TO END, through the REAL plan: a 2.39:1 HDR10 source must reach remux_inject.

    The tier used to require width==3840 AND height==2160 exactly, so a scope film (3840x1600)
    — i.e. most blockbusters — fell through to dv-only and had its HDR10 video re-encoded
    by the capped x265 path. Composing plan + stages here rather than mocking the plan is the
    point: the bug lived in the seam between them."""

    SCOPE_HDR10 = dict(is_4k=True, is_hdr=True, is_dv=False, is_cfr=True,
                       transfer="smpte2084", codec="hevc", pix_fmt="yuv420p10le",
                       width=3840, height=1600, video_kbps=68000)

    def _real_plan(self, **over):
        import plan
        return plan.choose_plan({**self.SCOPE_HDR10, **over})

    def test_the_real_plan_says_rpu_only(self):
        self.assertEqual(self._real_plan()["topaz"], "rpu-only")

    def test_and_the_remux_stage_injects_instead_of_re_encoding(self):
        import plan, remux
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for", return_value=self._real_plan()), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=68.0), \
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
             mock.patch.object(stages, "_source_peak_mbps", return_value=6.0), \
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
             mock.patch.object(stages, "_source_complete", return_value=True), \
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
             mock.patch.object(stages, "_source_complete", return_value=True), \
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
             mock.patch.object(stages, "_source_complete", return_value=True), \
             mock.patch("plan.probe_input", return_value={"is_dv": True}), \
             mock.patch.object(stages.logbook, "failure") as fail, \
             mock.patch.object(stages.logbook, "event") as ev:
            ok, _msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        fail.assert_not_called()
        ev.assert_called()


class RepairProgressPlumbing(unittest.TestCase):
    """stages._remux forwards on_repair into remux() and publishes the repair keys on the
    same progress surface as the segment bar — full-bar frames, plus which segment is
    being re-capped and its live refill fraction."""

    def test_repair_ticks_carry_segment_and_frames(self):
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(dv, cfr, orig, out, *, cap_mbps, audio_target_lufs, boundaries, abort,
                       on_progress, on_plan, should_pause=None, on_repair=None,
                       encode_source=None, audio_gain_db=None):
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

    def _cfr_kwargs(self, plan_topaz, source_cfr=None):
        import plan, topaz
        p = _paths(tempfile.mkdtemp())
        with open(p.source, "w") as fh:
            fh.write("x")
        with mock.patch.object(stages.transfer, "download"), \
             mock.patch.object(stages, "_source_complete", return_value=True), \
             mock.patch.object(plan, "plan_for", return_value=dict(
                 {"topaz": plan_topaz}, **({} if source_cfr is None else {"source_cfr": source_cfr}))), \
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
        self.assertTrue(self._cfr_kwargs("dv-only")["copy_only"])

    def test_upscale_still_true_cfr(self):
        self.assertFalse(self._cfr_kwargs("upscale")["copy_only"])

    def test_a_variable_frame_rate_4k_source_is_really_re_timed(self):
        """No 4K source goes through Topaz (2026-09-30), a VFR one included — so the CFR pass
        must re-encode it to one rate: Resolve imports this file and its timeline runs at one rate."""
        self.assertFalse(self._cfr_kwargs("dv-only", source_cfr=False)["copy_only"])
        self.assertTrue(self._cfr_kwargs("dv-only", source_cfr=True)["copy_only"])


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


class FastRemuxStepProgress(unittest.TestCase):
    def test_steps_reach_the_lane_with_label_and_pct(self):
        # The inject/ship remuxes publish labeled steps — one bar at a time; a label-only
        # phase carries pct None (never a fake number).
        import types, plan, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_inject(dv, cfr, orig, out, *, audio_target_lufs, abort, on_step=None,
                        on_stages=None):
            on_stages(remux.inject_stages(True))
            on_step("extracting DV metadata", None)
            on_step("copying the original video", 41.5)
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "rpu-only"}), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=15.0), \
             mock.patch.object(remux, "remux_inject", side_effect=fake_inject), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertTrue(ok)
        # the peak gate announces itself first (label-only), then the inject steps follow
        self.assertEqual(emitted[0]["step"], "measuring source peaks")
        self.assertIsNone(emitted[0]["pct"])
        self.assertEqual(emitted[1]["step"], "extracting DV metadata")
        self.assertIsNone(emitted[1]["pct"])
        self.assertEqual(emitted[2]["step"], "copying the original video")
        self.assertEqual(emitted[2]["pct"], 41.5)
        # ...each a lettered stage of ONE plan: the peaks, then what remux_inject announced
        self.assertEqual(emitted[2]["stages"], ["measuring source peaks"] + remux.inject_stages(True))
        self.assertEqual([d["stage_idx"] for d in emitted], [0, 1, 2])
        self.assertEqual(emitted[2]["stage_pct"], 41.5)
        self.assertNotIn("stage_pct", emitted[1])          # label-only: no number, no bar

    def test_a_resumed_inject_letters_from_its_real_plan(self):
        # the RPU already extracted: remux_inject's plan has no extraction, so copying is B
        import types, plan, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_inject(dv, cfr, orig, out, *, audio_target_lufs, abort, on_step=None,
                        on_stages=None):
            on_stages(remux.inject_stages(False))
            on_step("copying the original video", 10.0)
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "rpu-only"}), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=15.0), \
             mock.patch.object(remux, "remux_inject", side_effect=fake_inject), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertEqual(emitted[0]["stages"][1], "extracting DV metadata")   # the provisional plan
        self.assertEqual(emitted[1]["stages"][:2], ["measuring source peaks",
                                                    "copying the original video"])
        self.assertEqual(emitted[1]["stage_idx"], 1)

    def test_a_revoked_fast_path_letters_the_capped_encode(self):
        # over the DV ship gate: peaks (A), planning (B), the capped re-encode (C) with its
        # segment ticks as Stage C's own
        import types, plan, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(*a, on_progress=None, on_plan=None, **k):
            on_plan([100, 200, 300], 300)
            on_progress(150, 300)
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "rpu-only"}), \
             mock.patch.object(stages, "_source_peak_mbps", return_value=95.0), \
             mock.patch.object(stages, "_plan_fast_path_bounds"), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertTrue(ok)
        last = emitted[-1]
        self.assertEqual(last["stages"], [stages.PEAKS_STAGE, stages.PLAN_STAGE,
                                          stages.CAPPED_STAGE])
        self.assertEqual(last["stage_idx"], 2)
        self.assertEqual(last["stage_pct"], 50.0)
        self.assertEqual(last["stage_notches"], [0.3333, 0.6667, 1.0])
        self.assertEqual(last["stage_seg_done"], 1)
        self.assertEqual(last["pct"], 50.0)                # the lane's own pct is unchanged

    def test_a_plain_capped_remux_has_no_stages(self):
        import types, plan, remux, settings
        p = _paths(tempfile.mkdtemp())
        emitted = []
        def fake_remux(*a, on_progress=None, on_plan=None, **k):
            on_plan([100, 200, 300], 300)
            on_progress(150, 300)
            return types.SimpleNamespace(ok=True, reason="ok")
        with mock.patch.object(plan, "plan_for", return_value={"topaz": "upscale"}), \
             mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            stages.run_stage("remux", p, progress=lambda d: emitted.append(d))
        self.assertTrue(emitted)
        self.assertTrue(all("stages" not in d for d in emitted))


class CombineStages(unittest.TestCase):
    """COMPANION COMBINE routing through the generic stages."""

    CV = {"verdict": {"video_from": "nas", "rpu_from": "remote", "rpu_profile": "7.x",
                      "rpu_inline": False, "audio_from": "remote"},
          "companion": {"path": "/seedbox/M/m.remux.mkv", "name": "m.remux.mkv",
                        "size": 500}}

    def _p(self, d, cv=None):
        import companion
        from orchestrator import movie_paths
        with mock.patch.object(companion, "confirmed_verdict",
                               return_value=(cv if cv is not None else dict(self.CV))):
            return movie_paths("Movie (2020) [2160p].mkv", "/Media/Movies/M",
                               "Movie (2020)", scratch_dir=d, combine=True)

    def test_topaz_noops_before_the_dv_refusal(self):
        import plan
        p = self._p(tempfile.mkdtemp())
        with mock.patch.object(plan, "plan_for",
                               side_effect=AssertionError("combine must not consult the plan")):
            ok, msg = stages.run_stage("topaz", p)
        self.assertTrue(ok)
        self.assertIn("combine", msg)


    def test_an_unverifiable_partial_nas_copy_is_kept_but_never_reused(self):
        import companion
        d = tempfile.mkdtemp()
        p = self._p(d)
        os.makedirs(os.path.dirname(p.source), exist_ok=True)
        open(p.source, "w").write("partial")
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_remote_size", return_value=None), \
             mock.patch.object(stages.transfer, "download",
                               side_effect=AssertionError("must not pull over an unverified copy")), \
             mock.patch.object(companion, "fetch_to_file",
                               side_effect=AssertionError("must not get that far")):
            ok, msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        self.assertIn("cannot verify the local NAS copy is complete", msg)
        self.assertTrue(os.path.exists(p.source))        # kept for the next attempt

    def test_an_unverifiable_copy_during_an_overload_says_overloaded(self):
        import companion
        d = tempfile.mkdtemp()
        p = self._p(d)
        os.makedirs(os.path.dirname(p.source), exist_ok=True)
        open(p.source, "w").write("partial")
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_remote_size", return_value=None), \
             mock.patch.object(stages.transfer, "nas_busy", return_value={"since": 1}):
            ok, msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("NAS overloaded — cannot verify"), msg)

    def test_download_pulls_both_copies(self):
        import companion
        d = tempfile.mkdtemp()
        p = self._p(d)
        def fake_dl(remote, local_dir, **kw):
            open(p.source, "w").write("xxx")
            return True, p.source, "downloaded 3 bytes"
        def fake_fetch(virtual, dest, size, **kw):
            open(dest, "w").write("x" * size)
            return True, f"fetched {size} bytes"
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_remote_size", return_value=3), \
             mock.patch.object(stages.transfer, "download", side_effect=fake_dl), \
             mock.patch.object(companion, "manifest",
                               return_value={"files": [{"rel": "m.remux.mkv", "size": 500}]}), \
             mock.patch.object(companion, "fetch_to_file", side_effect=fake_fetch), \
             mock.patch.object(stages, "_ensure_cfr",
                               side_effect=AssertionError("combine has no CFR pass")):
            ok, msg = stages.run_stage("download", p)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(p.companion_src))
        self.assertEqual(os.path.getsize(p.companion_src), 500)

    def test_download_vanished_companion_parks(self):
        import companion
        d = tempfile.mkdtemp()
        p = self._p(d)
        open(p.source, "w").write("xxx")                     # NAS copy already on disk
        with mock.patch.object(stages, "_remote_size", return_value=3), \
             mock.patch.object(companion, "manifest", return_value={"files": []}), \
             mock.patch.object(companion, "mark") as mk:
            ok, msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("permanent:"))
        mk.assert_called_once_with(p.source_basename, "vanished")

    def test_download_relay_down_is_retryable(self):
        import companion
        d = tempfile.mkdtemp()
        p = self._p(d)
        open(p.source, "w").write("xxx")
        with mock.patch.object(stages, "_remote_size", return_value=3), \
             mock.patch.object(companion, "manifest",
                               side_effect=companion.RelayDownError("relay unreachable")):
            ok, msg = stages.run_stage("download", p)
        self.assertFalse(ok)
        self.assertFalse(msg.startswith("permanent:"))
        self.assertIn("relay unreachable", msg)

    def test_remux_under_gate_streams_via_combine(self):
        import companion, remux
        d = tempfile.mkdtemp()
        p = self._p(d)
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_peak_of", return_value=40.0), \
             mock.patch.object(remux, "combine",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 2, 1,
                                                              "ok")) as cb, \
             mock.patch.object(remux, "remux", side_effect=AssertionError("classic path")), \
             mock.patch.object(remux, "remux_inject", side_effect=AssertionError("direct inject")):
            ok, _ = stages.run_stage("remux", p)
        self.assertTrue(ok)
        kw = cb.call_args.kwargs
        self.assertFalse(kw["capped"])
        self.assertEqual(kw["rpu_profile"], "7.x")
        args = cb.call_args.args
        self.assertEqual(args[0], p.source)              # winner = NAS copy
        self.assertEqual(args[1], p.companion_src)       # RPU donor = remote
        self.assertEqual(args[2], p.companion_src)       # audio donor = remote

    def test_remux_over_gate_takes_the_capped_combine(self):
        import companion, remux
        d = tempfile.mkdtemp()
        p = self._p(d)
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_peak_of", return_value=96.0), \
             mock.patch.object(stages, "_plan_fast_path_bounds", return_value=" planned") as pb, \
             mock.patch.object(stages, "_read_topaz_bounds", return_value=[100, 200]), \
             mock.patch.object(remux, "combine",
                               return_value=remux.RemuxResult(True, p.final, "8.1", 2, 1,
                                                              "ok")) as cb:
            ok, _ = stages.run_stage("remux", p)
        self.assertTrue(ok)
        pb.assert_called_once()
        kw = cb.call_args.kwargs
        self.assertTrue(kw["capped"])
        self.assertEqual(kw["boundaries"], [100, 200])

    def test_remux_cut_mismatch_with_a_real_donor_is_permanent(self):
        import companion, remux
        d = tempfile.mkdtemp()
        p = self._p(d)
        bad = remux.RemuxResult(False, p.final,
                                reason="RPU/source frame mismatch: rpu 9 != source 8 (shipped nothing)")
        with mock.patch.object(companion, "confirmed_verdict", return_value=dict(self.CV)), \
             mock.patch.object(stages, "_peak_of", return_value=40.0), \
             mock.patch.object(remux, "combine", return_value=bad):
            ok, msg = stages.run_stage("remux", p)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("permanent: companion is a different cut"))

    def test_remux_mismatch_against_the_resolve_render_is_retryable(self):
        import companion, remux
        d = tempfile.mkdtemp()
        cv = {"verdict": dict(self.CV["verdict"], rpu_from="resolve", rpu_profile="resolve"),
              "companion": self.CV["companion"]}
        p = self._p(d, cv=cv)
        bad = remux.RemuxResult(False, p.final,
                                reason="RPU/source frame mismatch: rpu 9 != source 8 (shipped nothing)")
        with mock.patch.object(companion, "confirmed_verdict", return_value=cv), \
             mock.patch.object(stages, "_peak_of", return_value=40.0), \
             mock.patch.object(remux, "combine", return_value=bad):
            ok, msg = stages.run_stage("remux", p)
        self.assertFalse(ok)
        self.assertFalse(msg.startswith("permanent:"))   # truncated render → retry resolves it


class AudioDonorMismatchMapping(unittest.TestCase):
    def test_audio_donor_cut_mismatch_is_permanent_even_with_resolve_rpu(self):
        import remux
        bad = remux.RemuxResult(False, "o.mkv",
                                reason="audio donor is a different cut: 99 != 100 frames "
                                       "— its audio would drift (shipped nothing)")
        ok, msg = stages._combine_result(bad, real_rpu_donor=False)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("permanent: companion is a different cut"))


class ExtendStage(unittest.TestCase):
    """The AI border-extension stage: gate no-ops, environment failures, the chunked
    outpaint loop (mocked backend/client/ffmpeg), resume, and Topaz consuming the wide
    file. borders' own pure pieces (geometry/graph/argv) are covered in test_borders."""

    def setUp(self):
        import borders
        self.borders = borders
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.d, ignore_errors=True)
        self.p = _paths(self.d)
        for f in (self.p.source, self.p.source_cfr):
            with open(f, "w") as fh:
                fh.write("x")
        self.geom = borders.plan_geometry(640, 480)
        self.env = {"ok": True, "install_dir": self.d, "checkout": self.d,
                    "venv_python": "/x/python", "models_dir": self.d,
                    "port": 8189, "missing": [],
                    "desktop_version": "1.0", "comfy_version": "0.30.2"}

    def test_gate_not_needed_is_a_clean_no_op(self):
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": False,
                                             "reason": "extend borders is off for this show",
                                             "geom": None}):
            ok, msg = stages.run_stage("extend", self.p)
        self.assertTrue(ok)
        self.assertIn("no border extension", msg)

    def test_missing_comfy_fails_retryable(self):
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": True, "reason": "", "geom": self.geom}), \
             mock.patch.object(self.borders, "discover",
                               return_value={"ok": False, "missing": ["Comfy Desktop"],
                                             "port": 8189}):
            ok, msg = stages.run_stage("extend", self.p)
        self.assertFalse(ok)
        self.assertIn("Comfy Desktop", msg)

    def test_missing_models_fails_retryable(self):
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": True, "reason": "", "geom": self.geom}), \
             mock.patch.object(self.borders, "discover", return_value=self.env), \
             mock.patch.object(self.borders, "models_ready",
                               return_value=(False, ["WAN 2.1 VACE 1.3B"])):
            ok, msg = stages.run_stage("extend", self.p)
        self.assertFalse(ok)
        self.assertIn("WAN 2.1 VACE 1.3B", msg)

    def _run_mocked(self, total=170, emitted=None, cuts=None, show_prompt="",
                    dhash=None):
        """The full mocked chunk loop. total=170 -> chunks [(0,81),(81,89)] (tail folds);
        `cuts` feeds the scene-snap planner, `show_prompt` the per-show wing prompt,
        `dhash` a constant probe hash (None = unhashable -> reference-free, the default).
        The set book is always redirected to this test's tmp dir."""
        import re as _re
        import types
        import settings
        import topaz
        borders = self.borders
        p, geom = self.p, self.geom

        class FakeBackend:
            insts = []
            def __init__(self, env, in_dir, out_dir):
                self.in_dir, self.out_dir, self.base = in_dir, out_dir, "http://b"
                os.makedirs(in_dir, exist_ok=True)
                os.makedirs(out_dir, exist_ok=True)
                self.stopped = False
                FakeBackend.insts.append(self)
            def port_free(self): return True
            def start(self): pass
            def wait_ready(self, timeout=180): return True
            def alive(self): return True
            def stop(self): self.stopped = True
            tail = []

        class FakeClient:
            submits = []
            def __init__(self, base): self.base = base
            def submit(self, graph):
                FakeClient.submits.append(graph)
                return "pid"
            def wait(self, pid, backend=None, abort=None, timeout_s=3600): return {"outputs": {}}
            @staticmethod
            def output_file(hist, out_dir):
                path = os.path.join(out_dir, "gen.mp4")
                with open(path, "w") as fh:
                    fh.write("g")
                return path

        chunk_lens = [n for (_s, n) in borders.plan_chunks_snapped(total, cuts or [])]
        def fake_count(path, ffprobe=None):
            b = os.path.basename(path)
            if path == p.source_wide:
                return total
            if b.endswith("_cfr.mp4"):
                return total
            m = _re.search(r"wide_(\d+)", b)
            return chunk_lens[int(m.group(1))] if m else 0
        def fake_run(argv, **kw):
            with open(argv[-1], "w") as fh:     # every builder's dst is the last arg
                fh.write("out")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": True, "reason": "", "geom": geom}), \
             mock.patch.object(borders, "discover", return_value=self.env), \
             mock.patch.object(borders, "models_ready", return_value=(True, [])), \
             mock.patch.object(borders, "count_frames", side_effect=fake_count), \
             mock.patch.object(borders, "ComfyBackend", FakeBackend), \
             mock.patch.object(borders, "ComfyClient", FakeClient), \
             mock.patch.object(borders, "add_pace_sample", lambda s: None), \
             mock.patch.object(topaz, "media_timing", return_value=(23.976, 7.1)), \
             mock.patch.object(topaz, "_cached_scene_frames",
                               return_value=list(cuts or [])), \
             mock.patch.object(settings, "get_show_extend_prompt",
                               return_value=show_prompt), \
             mock.patch.object(borders, "SET_BOOK_ROOT",
                               os.path.join(self.d, "set_book")), \
             mock.patch.object(borders, "dhash_file", return_value=dhash), \
             mock.patch.object(stages.subprocess, "run", side_effect=fake_run):
            ok, msg = stages.run_stage("extend", self.p,
                                       progress=(emitted.append if emitted is not None
                                                 else None))
        return ok, msg, FakeBackend, FakeClient

    def test_chunked_loop_produces_wide_file(self):
        emitted = []
        ok, msg, FakeBackend, FakeClient = self._run_mocked(emitted=emitted)
        self.assertTrue(ok, msg)
        self.assertIn("2 chunks", msg)
        self.assertTrue(os.path.exists(self.p.source_wide))
        segdir = self.p.source_wide + ".segments"
        self.assertTrue(os.path.exists(os.path.join(segdir, "wide_0000.mp4")))
        self.assertTrue(os.path.exists(os.path.join(segdir, "wide_0001.mp4")))
        self.assertEqual(len(FakeClient.submits), 2)          # one graph per chunk
        self.assertTrue(FakeBackend.insts[-1].stopped)        # backend never left running
        # Notched progress, the Topaz shape.
        self.assertEqual(emitted[0]["stage"], "extend")
        self.assertEqual(emitted[0]["seg_total"], 2)
        self.assertEqual([e["seg_done"] for e in emitted], [1, 2])
        self.assertEqual(emitted[-1]["pct"], 100)
        self.assertEqual(emitted[0]["notches"], [round(81 / 170, 4), 1.0])
        # No scene cuts detected -> both chunks are scene 0 -> the SAME seed
        # (continuity: chunks within one scene share their noise).
        s0 = FakeClient.submits[0]["15"]["inputs"]["seed"]
        s1 = FakeClient.submits[1]["15"]["inputs"]["seed"]
        self.assertEqual(s1, s0)

    def test_scene_cuts_shape_chunks_and_seeds(self):
        """CONTINUITY tier 2 through the stage: boundaries snap to a cut, and the seed
        changes exactly at the scene change — not per chunk."""
        ok, msg, _B, FakeClient = self._run_mocked(cuts=[100])
        self.assertTrue(ok, msg)
        # 170 frames, cut at 100 -> (0,81) (cut beyond window), (81,19) (snap), (100,70)
        lens = [g["14"]["inputs"]["length"] for g in FakeClient.submits]
        self.assertEqual(lens, [81, 19, 70])
        seeds = [g["15"]["inputs"]["seed"] for g in FakeClient.submits]
        self.assertEqual(seeds[0], seeds[1])              # both start in scene 0
        self.assertEqual(seeds[2], seeds[0] + 1)          # the cut starts scene 1

    def test_set_references_chain_within_and_across_scenes(self):
        """CONTINUITY tier 3 through the stage: a NEW set's first chunk generates
        reference-free and registers its widened frame; the rest of its scene chains
        off it, and a later scene that MATCHES the set reuses it — one book entry."""
        ok, msg, _B, FakeClient = self._run_mocked(cuts=[100], dhash=0xBEEF)
        self.assertTrue(ok, msg)
        subs = FakeClient.submits                        # scenes: 0, 0, 1
        self.assertNotIn("19", subs[0])                  # unknown set -> reference-free
        self.assertEqual(subs[1]["14"]["inputs"]["reference_image"], ["19", 0])
        self.assertEqual(subs[1]["19"]["inputs"]["image"], "setref_0000.png")
        self.assertEqual(subs[2]["19"]["inputs"]["image"], "setref_0001.png")
        with mock.patch.object(self.borders, "SET_BOOK_ROOT",
                               os.path.join(self.d, "set_book")):
            self.assertEqual(self.borders.set_count(self.p.series), 1)  # matched, not re-added

    def test_unhashable_probes_stay_reference_free(self):
        # cv2 can't read the fake frames (dhash None): every chunk generates without a
        # reference and NOTHING lands in the book — fail open, never park.
        ok, _m, _B, FakeClient = self._run_mocked(cuts=[100])
        self.assertTrue(ok)
        self.assertTrue(all("19" not in g for g in FakeClient.submits))
        with mock.patch.object(self.borders, "SET_BOOK_ROOT",
                               os.path.join(self.d, "set_book")):
            self.assertEqual(self.borders.set_count(self.p.series), 0)

    def test_show_prompt_reaches_the_graph(self):
        """CONTINUITY tier 1: the per-show wing prompt conditions every chunk; empty
        falls back to the built-in default."""
        ok, _m, _B, FakeClient = self._run_mocked(show_prompt="dark wood bar, neon signs")
        self.assertTrue(ok)
        self.assertEqual(FakeClient.submits[0]["11"]["inputs"]["text"],
                         "dark wood bar, neon signs")
        ok, _m, _B, FakeClient = self._run_mocked(show_prompt="")
        self.assertTrue(ok)
        self.assertEqual(FakeClient.submits[-1]["11"]["inputs"]["text"],
                         self.borders.DEFAULT_PROMPT)

    def test_resume_skips_finished_chunks(self):
        ok, msg, _B, FakeClient = self._run_mocked()
        self.assertTrue(ok, msg)
        os.remove(self.p.source_wide)                         # lose the concat only
        FakeClient.submits.clear()
        ok, msg, _B, FakeClient = self._run_mocked()
        self.assertTrue(ok, msg)
        self.assertEqual(len(FakeClient.submits), 0)          # every chunk reused
        self.assertTrue(os.path.exists(self.p.source_wide))

    def test_stage_done_roundtrip(self):
        import orchestrator
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": False, "reason": "off", "geom": None}):
            self.assertTrue(orchestrator.stage_done("extend", self.p))   # no work -> done
        with mock.patch.object(self.borders, "extend_gate",
                               return_value={"needed": True, "reason": "", "geom": self.geom}):
            self.assertFalse(orchestrator.stage_done("extend", self.p))  # work, no wide yet
            with open(self.p.source_wide, "w") as fh:
                fh.write("w")
            with mock.patch.object(orchestrator, "_nb_frames", return_value=170):
                self.assertTrue(orchestrator.stage_done("extend", self.p))
            def mismatch(path):
                return 100 if path == self.p.source_wide else 170
            with mock.patch.object(orchestrator, "_nb_frames", side_effect=mismatch):
                self.assertFalse(orchestrator.stage_done("extend", self.p))  # short wide

    def test_topaz_consumes_the_wide_file(self):
        import types
        import plan
        import settings
        import topaz
        got = {}
        pl = {"topaz": "upscale", "scale": 2, "res": "480p", "fit_height": 2160,
              "input": {"is_4k": False}}
        def fake_upscale(cfr, *, segdir, profile, scale, fit_height, on_progress, abort,
                         on_plan, should_pause=None):
            got["input"] = cfr
            return types.SimpleNamespace(ok=True, error_tail="", frames=100)
        common = dict(plan_for=mock.patch.object(plan, "plan_for", return_value=pl),
                      params=mock.patch.object(settings, "show_topaz_params",
                                               return_value={}),
                      key=mock.patch.object(settings, "show_preset_key",
                                            return_value="digital"),
                      total=mock.patch.object(topaz, "total_frames", return_value=100),
                      up=mock.patch.object(topaz, "upscale_resumable",
                                           side_effect=fake_upscale))
        with common["plan_for"], common["params"], common["key"], common["total"], common["up"]:
            ok, _ = stages.run_stage("topaz", self.p)         # no wide file on disk
            self.assertTrue(ok)
            self.assertEqual(got["input"], self.p.source_cfr)
            with open(self.p.source_wide, "w") as fh:
                fh.write("w")
            ok, _ = stages.run_stage("topaz", self.p)         # wide file present
            self.assertTrue(ok)
            self.assertEqual(got["input"], self.p.source_wide)


class YoutubeShipsItsRender(unittest.TestCase):
    """YouTube's whole point is speed: Resolve exports at a cap-safe bitrate so the render
    IS the deliverable and the remux stream-COPIES its video (minutes) instead of running
    the hour-class capped x265 pass. That only pays off if the peak gate actually passes,
    and Resolve's VideoToolbox export has no peak control — hence a conservative target."""

    def test_render_target_leaves_headroom_under_the_cap(self):
        import dvcap
        cap = dvcap.DEFAULT_PEAK_MBPS
        target_mbps = stages.YOUTUBE_RENDER_KBPS / 1000.0
        # The gate compares a MEASURED 1-s peak against the cap; VideoToolbox bursts well
        # above its average, so the target must sit far enough below the cap that a normal
        # burst still lands inside it.
        self.assertLessEqual(target_mbps * 2.0, cap * dvcap.PEAK_TOLERANCE,
                             "a 2x burst over the YouTube render target must stay under cap")
        self.assertLess(target_mbps, stages.EXPORT_BITRATE_FLOOR_KBPS / 1000.0)

    def test_over_cap_render_still_falls_back_not_fails(self):
        # The safety net: an over-cap render must fall through to the capped re-encode,
        # never ship unverified and never fail the item outright.
        import remux, settings, types, plan
        p = _paths(tempfile.mkdtemp())
        p.youtube = True
        ship = types.SimpleNamespace(ok=False, reason="render-over-cap: 61.0 Mbps > 50 cap")
        capped = types.SimpleNamespace(ok=True, reason="DV 8.1 · 1 audio · 0 sub")
        def fake_remux(*a, **k):
            return capped
        with mock.patch.object(remux, "remux_ship_render", return_value=ship) as rs, \
             mock.patch.object(remux, "remux", side_effect=fake_remux) as rr, \
             mock.patch.object(plan, "plan_for",
                               return_value={"topaz": "upscale", "scale": 2, "res": "1080p",
                                             "fit_height": None, "input": {"is_4k": False}}), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, msg = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(rs.called)          # tried the fast path first
        self.assertTrue(rr.called)          # ...then fell back to the capped encode
        self.assertTrue(ok, msg)


class UnverifiableSourceIsNeverTrusted(unittest.TestCase):
    """A hard kill leaves a PARTIAL source on scratch. If the next arm can't reach the NAS
    (Tailscale still coming up, smbftpd at its connection cap), _source_complete returns
    None — and the stage used to promote that stub to a complete source. Nothing
    downstream could catch it: the CFR is built from the fragment, the render is judged
    against that same short CFR, the remux checks only internal consistency, upload
    verifies the master against itself, and replace_source (default ON) then deletes the
    real source. Truncated master + deleted original, every stage reporting success."""

    def _run(self, state):
        d = tempfile.mkdtemp()
        p = _paths(d)
        with open(p.source, "w") as fh:
            fh.write("partial")
        with mock.patch.object(stages, "_source_complete", return_value=state), \
             mock.patch.object(stages.transfer, "download") as dl, \
             mock.patch("plan.probe_input", return_value={"is_dv": False}), \
             mock.patch("topaz.is_cfr_ready", return_value=True):
            ok, msg = stages.run_stage("download", p)
        return ok, msg, dl, p

    def test_unverifiable_refuses_and_keeps_the_file(self):
        ok, msg, dl, p = self._run(None)
        self.assertFalse(ok)
        self.assertIn("cannot verify", msg)
        dl.assert_not_called()                       # no blind re-pull either
        self.assertTrue(os.path.exists(p.source))    # kept — it may well be complete

    def test_verified_complete_still_reuses(self):
        ok, _msg, dl, _p = self._run(True)
        self.assertTrue(ok)
        dl.assert_not_called()

    def test_verified_partial_is_deleted_and_repulled(self):
        ok, _msg, dl, _p = self._run(False)
        dl.assert_called_once()                      # verified short -> clean re-pull


class SeasonScopedLoudness(unittest.TestCase):
    """A season from one source is ONE mastering job: its FIRST episode measures the gain and
    every later episode from the same source reuses it, so the volume can't step mid-binge.
    A filename that clearly indicates a different master is measured on its own."""

    BLURAY = "Lost (2004) - S04E%02d - Ep (1080p BluRay x265 Silence).mkv"
    WEBDL = "Lost (2004) - S04E%02d - Ep (1080p WEB-DL DDP5.1 H.264-NTb).mkv"

    def setUp(self):
        import tempfile as _tf, os as _os
        p = mock.patch.object(audiogain, "BOOK_FILE",
                              _os.path.join(_tf.mkdtemp(), "b.json"))
        p.start(); self.addCleanup(p.stop)      # each test starts from an empty book

    def _run(self, ep, name, measured=-20.0, series="Lost (2004)"):
        import types, remux, settings
        p = episode_paths(series, ep, name.replace(".mkv", ".mp4"),
                          scratch_dir=tempfile.mkdtemp(), nas_tv_root="/Media/TV-Shows")
        seen = {}

        def fake_remux(dv, cfr, orig, out, *, audio_gain_db=None, **kw):
            seen["gain"] = audio_gain_db
            return types.SimpleNamespace(ok=True, reason="ok")

        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(remux, "measure_lufs", return_value=measured) as m, \
             mock.patch.object(settings, "get_show_normalize_audio", return_value=True), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: None)
        seen["ok"], seen["measured_times"] = ok, m.call_count
        return seen

    def test_a_matroska_master_gets_the_season_gain_too(self):
        # the gain used to be skipped for .mkv finals; every master is .mkv now (2026-10-18)
        p = episode_paths("Lost (2004)", "S04E01", "x.mp4", scratch_dir="/tmp",
                          nas_tv_root="/Media/TV-Shows")
        self.assertTrue(p.final.endswith(".mkv"))
        self._run("S04E01", self.BLURAY % 1, measured=-20.0)
        self.assertEqual(self._run("S04E02", self.BLURAY % 2, measured=-30.0)["gain"], 4.0)

    def test_first_episode_measures_and_the_next_reuses_it(self):
        a = self._run("S04E01", self.BLURAY % 1, measured=-20.0)
        self.assertTrue(a["ok"])
        self.assertEqual(a["gain"], 4.0)              # -16 target from -20 measured
        self.assertEqual(a["measured_times"], 1)
        # a LOUDER second episode must still ship the season's gain, not its own
        b = self._run("S04E02", self.BLURAY % 2, measured=-25.0)
        self.assertEqual(b["gain"], 4.0)              # NOT 9.0
        self.assertEqual(b["measured_times"], 0)      # and it didn't re-measure

    def test_a_different_master_in_the_season_is_measured_on_its_own(self):
        self._run("S04E01", self.BLURAY % 1, measured=-20.0)
        web = self._run("S04E03", self.WEBDL % 3, measured=-25.0)
        self.assertEqual(web["gain"], 9.0)            # its own measurement, not the BluRay 4.0
        self.assertEqual(web["measured_times"], 1)

    def test_the_next_season_decides_for_itself(self):
        self._run("S04E01", self.BLURAY % 1, measured=-20.0)
        s5 = self._run("S05E01", self.BLURAY % 1, measured=-25.0)
        self.assertEqual(s5["gain"], 9.0)
        self.assertEqual(s5["measured_times"], 1)

    def test_movies_and_youtube_still_measure_per_item(self):
        # no season -> no key -> stages passes None and remux measures for itself, as before
        import types, remux, settings
        p = _paths(tempfile.mkdtemp())
        p.movie, p.series = True, "Some Movie (2024)"
        seen = {}

        def fake_remux(dv, cfr, orig, out, *, audio_gain_db=None, **kw):
            seen["gain"] = audio_gain_db
            return types.SimpleNamespace(ok=True, reason="ok")

        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(remux, "measure_lufs", return_value=-20.0) as m, \
             mock.patch.object(settings, "get_show_normalize_audio", return_value=True), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(ok)
        self.assertIsNone(seen["gain"])               # remux decides, exactly as before
        self.assertEqual(m.call_count, 0)             # and stages never measured

    def test_normalize_off_skips_the_whole_thing(self):
        import types, remux, settings
        p = episode_paths("Lost (2004)", "S04E01", (self.BLURAY % 1).replace(".mkv", ".mp4"),
                          scratch_dir=tempfile.mkdtemp(), nas_tv_root="/Media/TV-Shows")
        seen = {}

        def fake_remux(dv, cfr, orig, out, *, audio_target_lufs, audio_gain_db=None, **kw):
            seen["lufs"], seen["gain"] = audio_target_lufs, audio_gain_db
            return types.SimpleNamespace(ok=True, reason="ok")

        with mock.patch.object(remux, "remux", side_effect=fake_remux), \
             mock.patch.object(remux, "measure_lufs", return_value=-20.0) as m, \
             mock.patch.object(settings, "get_show_normalize_audio", return_value=False), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": -16}):
            ok, _ = stages.run_stage("remux", p, progress=lambda d: None)
        self.assertTrue(ok)
        self.assertIsNone(seen["lufs"])               # the per-show gate still wins
        self.assertIsNone(seen["gain"])
        self.assertEqual(m.call_count, 0)             # nothing measured, nothing recorded
        self.assertEqual(audiogain.view(), [])


class YouTubeRenderBitrate(unittest.TestCase):
    """The YouTube render target exists to land the render's PEAK under max_peak_mbps so the
    remux can stream-copy it — Resolve's VideoToolbox export has no peak control, so the
    target is the only lever. A NATIVE 4K source bursts much further above the target than a
    1080p one: live-measured, an 18-minute 4K video at 20 Mbps peaked at 59.9 against a 50
    cap, lost the gate, and took the hour-class capped re-encode the fast path exists to
    avoid."""

    def test_upscaled_1080p_sources_keep_the_tuned_target(self):
        for h in (0, 720, 1080, 1200, 1440):
            self.assertEqual(stages.youtube_render_kbps(h), stages.YOUTUBE_RENDER_KBPS, h)

    def test_native_4k_renders_lower(self):
        for h in (1700, 2160, 4320):
            self.assertEqual(stages.youtube_render_kbps(h), stages.YOUTUBE_RENDER_KBPS_4K, h)

    def test_the_4k_target_leaves_peak_headroom(self):
        # the observed burst ratio was ~3x (20 -> 59.9); the 4K target must project inside
        # the default 50 Mbps cap at that ratio, or it buys nothing
        self.assertLess(stages.YOUTUBE_RENDER_KBPS_4K * 3 / 1000.0, 50)
        self.assertLess(stages.YOUTUBE_RENDER_KBPS_4K, stages.YOUTUBE_RENDER_KBPS)

    def test_an_unknown_height_is_treated_as_the_common_case(self):
        self.assertEqual(stages.youtube_render_kbps(None), stages.YOUTUBE_RENDER_KBPS)


class ImportedVideosUseTheirBatchSettings(unittest.TestCase):
    """Stage-time settings for a YouTube video that arrived in an import batch come from the
    batch's key ("import:<id>"), not the staging channel folder — an imported playlist is
    its own configurable thing (user-asked 2026-08-28). Everything else keeps p.series."""

    def _yt(self, vid="aaaaaaaaaa1"):
        import tempfile
        return orch_youtube_paths("Chan", "/s/Chan/x/v [%s].mp4" % vid, "V",
                                  scratch_dir=tempfile.mkdtemp())

    def test_a_batch_video_scopes_to_the_batch(self):
        import youtube
        p = self._yt()
        with mock.patch.object(youtube, "settings_scope_for_vid",
                               return_value="import:imp9"):
            self.assertEqual(stages._settings_scope(p), "import:imp9")

    def test_a_channel_video_keeps_the_channel(self):
        import youtube
        p = self._yt()
        with mock.patch.object(youtube, "settings_scope_for_vid", return_value=None):
            self.assertEqual(stages._settings_scope(p), "Chan")

    def test_non_youtube_items_never_even_look(self):
        import tempfile, youtube
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(youtube, "settings_scope_for_vid",
                               side_effect=AssertionError("must not be consulted")):
            self.assertEqual(stages._settings_scope(p), "Show")

    def test_a_broken_lookup_falls_back_to_the_series(self):
        import youtube
        p = self._yt()
        with mock.patch.object(youtube, "settings_scope_for_vid",
                               side_effect=OSError("book unreadable")):
            self.assertEqual(stages._settings_scope(p), "Chan")


class AnAbortedCfrIsNotAFailure(unittest.TestCase):
    """One long video logged six red "CFR convert failed" lines in an evening and read like a
    broken file (user-caught 2026-09-04). Every one was OUR OWN kill — a stop/deploy, a Plex
    stream starting, or the run thread claiming an item the prefetcher was part-way through.
    to_cfr already distinguishes them (a negative rc is always our own signal); the stage was
    throwing that away and reporting a failure."""

    def test_an_abort_is_reported_as_interrupted(self):
        msg = stages.cfr_failure_message("aborted")
        self.assertTrue(msg.startswith("interrupted:"), msg)

    def test_a_real_failure_still_says_failed(self):
        msg = stages.cfr_failure_message("x264: invalid frame size")
        self.assertIn("CFR convert failed", msg)
        self.assertFalse(msg.startswith("interrupted:"))

    def test_it_is_never_the_yield_prefix(self):
        # "paused:" means a segment-boundary YIELD to the run loop, which does livelock
        # bookkeeping on it — an abort must not be mistaken for one
        for tail in ("aborted", "boom", None):
            self.assertFalse(stages.cfr_failure_message(tail).startswith("paused:"))

    def test_interrupted_logs_as_an_event_not_a_red_failure(self):
        with mock.patch.object(stages, "_download",
                               return_value=(False, "interrupted: CFR convert stopped")), \
             mock.patch.object(stages.logbook, "failure") as fail, \
             mock.patch.object(stages.logbook, "event") as event:
            stages.run_stage("download", mock.MagicMock(ep="X"))
        fail.assert_not_called()
        event.assert_called_once()

    def test_a_real_failure_still_logs_red(self):
        with mock.patch.object(stages, "_download",
                               return_value=(False, "CFR convert failed: boom")), \
             mock.patch.object(stages.logbook, "failure") as fail:
            stages.run_stage("download", mock.MagicMock(ep="X"))
        fail.assert_called_once()


class DownloadYieldsToASentVideo(unittest.TestCase):
    """A send-to-Visionary video runs after the current SEGMENT, not the current episode
    (user-dictated 2026-09-05). Topaz already yielded between segments; a YouTube item runs
    download -> CFR -> resolve and never touches Topaz, so sends waited out whole items.
    The download stage now yields too: neither the pull nor the CFR is segmented, so it
    stops now, drops the partial, reports a benign "paused:" and is resumed later."""

    def test_pause_abort_fires_on_the_predicate_and_remembers_it(self):
        pa = stages._PauseAbort(None, lambda: True)
        self.assertTrue(pa.is_set())
        self.assertTrue(pa.paused)

    def test_a_real_abort_wins_and_is_not_a_pause(self):
        import threading
        ev = threading.Event(); ev.set()
        pa = stages._PauseAbort(ev, lambda: False)
        self.assertTrue(pa.is_set())
        self.assertFalse(pa.paused)

    def test_the_predicate_is_throttled_not_polled_every_call(self):
        calls = []
        pa = stages._PauseAbort(None, lambda: calls.append(1) or False)
        for _ in range(50):
            pa.is_set()
        self.assertEqual(len(calls), 1)            # one look per POLL_SECONDS

    def test_a_predicate_that_throws_never_aborts_the_download(self):
        pa = stages._PauseAbort(None, lambda: 1 / 0)
        self.assertFalse(pa.is_set())
        self.assertFalse(pa.paused)

    def test_a_yielded_download_reports_paused_not_a_failure(self):
        def body(p, abort, progress, low_prio):
            abort.is_set()                          # the pull polls -> predicate fires
            return False, "CFR convert failed: aborted"
        with mock.patch.object(stages, "_download_body", side_effect=body):
            ok, msg = stages._download(object(), None, should_pause=lambda: True)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("paused:"), msg)

    def test_a_plain_abort_keeps_its_own_message(self):
        with mock.patch.object(stages, "_download_body",
                               return_value=(False, "interrupted: CFR convert stopped")):
            ok, msg = stages._download(object(), None, should_pause=lambda: False)
        self.assertEqual(msg, "interrupted: CFR convert stopped")

    def test_success_passes_straight_through(self):
        with mock.patch.object(stages, "_download_body", return_value=(True, "ok")):
            self.assertEqual(stages._download(object(), None, should_pause=lambda: True), (True, "ok"))

    def test_no_predicate_means_the_body_sees_the_raw_abort(self):
        import threading
        ev = threading.Event(); seen = {}
        with mock.patch.object(stages, "_download_body",
                               side_effect=lambda p, a, pr, lp: seen.update(abort=a) or (True, "ok")):
            stages._download(object(), ev)
        self.assertIs(seen["abort"], ev)

    def test_run_stage_forwards_should_pause_to_download(self):
        seen = {}
        def spy(p, abort, progress=None, low_prio=False, should_pause=None):
            seen["sp"] = should_pause; return True, "ok"
        sp = lambda: True
        with mock.patch.object(stages, "_download", side_effect=spy):
            stages.run_stage("download", mock.MagicMock(ep="X"), should_pause=sp)
        self.assertIs(seen["sp"], sp)


# ---- native DV (settings dv_engine 'native') ----------------------------------------------

class _NativeItem(_tn.Fixture):
    """test_nativelane's fixture — a real Topaz segdir (3 chunks + segments.json), a CFR and the
    native pins on a temp LUT/analyzer — re-homed on real EpisodePaths, so the stages see an
    ordinary TV episode."""

    def setUp(self):
        super().setUp()
        self.p = _paths(self.root)
        self.segdir = self.p.segdir
        self.make_segdir(self.segdir)
        self.cfr = self.p.source_cfr
        with open(self.cfr, "wb") as f:
            f.write(b"cfr" * 10)


class NativeAnalysisStage(_NativeItem):
    """The run-only 'dvnative' stage: run_stage dispatches it with should_pause (it is
    resumable per chunk), the marker moves analyzing -> ready, and the stage books failures so
    the orchestrator can fall back to Resolve."""

    def setUp(self):
        super().setUp()
        import nativelane
        self.lane = nativelane
        self.extra = [
            mock.patch.object(nativelane, "toolchain_ok", return_value=(True, "ok")),
            mock.patch.object(nativelane.topaz, "_frame_count", return_value=_tn.TOTAL),
            mock.patch.object(nativelane, "fps_fraction", return_value=_tn.FPS),
            mock.patch.object(nativelane.dvcap, "rpu_frame_count", return_value=_tn.TOTAL),
            mock.patch.object(nativelane.dvnative, "generate_rpu", side_effect=self._generate),
        ]
        for pt in self.extra:
            pt.start()

    def tearDown(self):
        for pt in self.extra:
            pt.stop()
        super().tearDown()

    @staticmethod
    def _generate(sc, rpu, **kw):
        with open(rpu, "wb") as f:
            f.write(b"generated-rpu")
        return {}

    def _run(self, analyze, *, should_pause=None, abort=None):
        sp = should_pause or (lambda: False)
        with mock.patch.object(self.lane.dvnative, "analyze_chunks", side_effect=analyze):
            return stages.run_stage("dvnative", self.p, abort=abort, progress=lambda d: None,
                                    should_pause=sp)

    def test_the_marker_moves_from_analyzing_to_ready(self):
        seen = {}
        sp = lambda: False

        def analyze(chunk_list, workdir, **kw):
            seen["state"] = self.lane.read_marker(self.p)["state"]
            seen["should_pause"] = kw["should_pause"]
            return _tn._sc()
        with mock.patch.object(self.lane.dvnative, "analyze_chunks", side_effect=analyze):
            ok, msg = stages.run_stage("dvnative", self.p, should_pause=sp)
        self.assertTrue(ok, msg)
        self.assertEqual(seen["state"], "analyzing")            # claimed before measuring
        self.assertIs(seen["should_pause"], sp)                  # the yield reaches the analysis
        self.assertEqual(self.lane.read_marker(self.p)["state"], "ready")
        self.assertTrue(self.lane.ready(self.p))
        self.assertEqual(self.lane.failure_count(self.p), 0)

    def test_a_toolchain_failure_never_leaves_the_item_native(self):
        # Whether the lane reports it as "native-fallback:" (fallback.json, sticky) or as a
        # plain reason on a first claim (no marker: the next decide() sends it to Resolve),
        # the item must not stay claimed, and Resolve's input must be untouched.
        with mock.patch.object(self.lane, "toolchain_ok", return_value=(False, "dovi_tool is 2.2.0")):
            ok, msg = self._run(lambda *a, **k: _tn._sc())
        self.assertFalse(ok)
        self.assertIn("dovi_tool is 2.2.0", msg)
        self.assertTrue(self.lane.has_fallback(self.p) or not self.lane.has_marker(self.p))
        self.assertTrue(os.path.exists(os.path.join(self.segdir, "seg_0000.mov")))   # Resolve's input

    def test_a_first_claim_toolchain_failure_is_this_time_only(self):
        # The lane's contract pinned at the STAGE: a broken toolchain on a first claim is not a
        # sticky fallback (no fallback.json, no "native-fallback:" prefix) and claims nothing —
        # the next decide() simply sends the item to Resolve while the tools are broken.
        with mock.patch.object(self.lane, "toolchain_ok", return_value=(False, "dovi_tool is 2.2.0")):
            ok, msg = self._run(lambda *a, **k: _tn._sc())
        self.assertFalse(ok)
        self.assertFalse(msg.startswith("native-fallback:"), msg)
        self.assertFalse(self.lane.has_fallback(self.p))
        self.assertFalse(self.lane.has_marker(self.p))

    def test_a_full_disk_while_booking_a_failure_is_not_a_crash(self):
        def broken(*a, **k):
            raise RuntimeError("chunk 1: analyzer failed: boom")
        with mock.patch.object(self.lane, "count_failure",
                               side_effect=OSError(28, "No space left on device")), \
             mock.patch.object(stages.logbook, "exception") as logged:
            ok, msg = self._run(broken)
        self.assertFalse(ok)
        self.assertIn("analyzer failed: boom", msg)
        self.assertIn("not booked", msg)
        self.assertNotIn("crashed", msg)
        logged.assert_called_once()

    def test_the_analysis_cpu_is_booked_per_chunk(self):
        def analyze(chunk_list, workdir, **kw):
            for k in range(3):
                kw["on_cpu"](k, 10.0 + k, 100)
            return _tn._sc()
        ok, msg = self._run(analyze)
        self.assertTrue(ok, msg)
        book = self.lane._read_json(self.lane.cpu_path(self.p))
        self.assertEqual(sorted(book["analysis"]), ["chunk_0000", "chunk_0001", "chunk_0002"])
        self.assertEqual(book["analysis"]["chunk_0002"], {"cpu_s": 12.0, "frames": 100})

    def test_the_second_failure_falls_back_the_first_retries(self):
        def broken(*a, **k):
            raise RuntimeError("chunk 1: analyzer failed: boom")
        ok, msg = self._run(broken)
        self.assertFalse(ok)
        self.assertIn("(native attempt 1 of 2)", msg)
        self.assertFalse(self.lane.has_fallback(self.p))
        self.assertEqual(self.lane.read_marker(self.p)["state"], "analyzing")   # resumes next pass
        ok, msg = self._run(broken)
        self.assertTrue(msg.startswith("native-fallback:"), msg)
        self.assertTrue(self.lane.has_fallback(self.p))
        self.assertFalse(self.lane.has_marker(self.p))
        self.assertTrue(all(os.path.exists(os.path.join(self.segdir, f"seg_{i:04d}.mov"))
                            for i in range(3)))                  # the chunks are untouched

    def test_a_pause_and_a_stop_are_never_counted(self):
        def pause(*a, **k):
            raise self.lane.dvnative.Paused("paused: low disk — under the floor")

        def stop(*a, **k):
            raise self.lane.dvnative.Aborted("aborted (run stopped) during chunk 2 analyzer")
        self.assertEqual(self._run(pause), (False, "paused: low disk — under the floor"))
        ok, msg = self._run(stop)
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("interrupted:"), msg)     # benign in the log, not counted
        self.assertEqual(self.lane.failure_count(self.p), 0)
        self.assertFalse(self.lane.has_fallback(self.p))

    def test_a_set_abort_is_an_interruption_whatever_the_lane_said(self):
        import threading
        ev = threading.Event()
        ev.set()

        def broken(*a, **k):
            raise RuntimeError("killed")
        ok, msg = self._run(broken, abort=ev)
        self.assertTrue(msg.startswith("interrupted:"), msg)
        self.assertEqual(self.lane.failure_count(self.p), 0)

    def test_a_lane_crash_costs_one_attempt_and_never_escapes(self):
        with mock.patch.object(self.lane, "run_analysis", side_effect=KeyError("state")):
            ok, msg = stages.run_stage("dvnative", self.p)
        self.assertFalse(ok)
        self.assertIn("native analysis crashed: KeyError", msg)
        self.assertEqual(self.lane.failure_count(self.p), 1)


class NativeRemuxBranch(_NativeItem):
    """stages._remux: an item with a ready native marker remuxes from its chunks
    (remux.remux(native=bundle)) and drops them once the master passes; every other item
    reaches remux.remux with exactly the pre-native arguments."""

    PRE_NATIVE_KWARGS = {"cap_mbps", "audio_target_lufs", "audio_gain_db", "boundaries", "abort",
                         "on_progress", "on_plan", "should_pause", "on_repair", "encode_source"}

    def _remux(self, ok=True, *, write_final=True, drive=None):
        import types, remux, settings
        got = {}

        def fake_remux(dv, cfr, orig, out, **kw):
            got.update(kw)
            got["_args"] = (dv, cfr, orig, out)
            if drive:
                drive(kw)
            if ok and write_final:
                with open(out, "wb") as f:
                    f.write(b"master" * 10)
            return types.SimpleNamespace(ok=ok, reason="shipped" if ok else "peak gate failed")
        with mock.patch.object(remux, "remux", side_effect=fake_remux) as rm, \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": 0}):
            res = stages.run_stage("remux", self.p, progress=lambda d: None)
        return res, got, rm

    def _chunk_files(self):
        return sorted(f for f in os.listdir(self.segdir) if f.startswith("seg_"))

    def test_a_ready_item_remuxes_from_its_chunks_and_frees_them_on_success(self):
        import nativelane
        m = self.make_ready(self.p)
        (ok, msg), got, _rm = self._remux(ok=True)
        self.assertTrue(ok, msg)
        native = got["native"]
        self.assertEqual(native["id"], m["id"])
        self.assertEqual(native["rpu"], nativelane.rpu_path(self.p))
        self.assertEqual(native["chunks"][-1], [_tn.TOTAL, None])
        self.assertEqual(native["total"], _tn.TOTAL)
        self.assertEqual(set(got) - {"_args", "native"}, self.PRE_NATIVE_KWARGS)
        self.assertEqual(self._chunk_files(), [])               # the ProRes is freed before the upload
        self.assertTrue(os.path.isdir(self.segdir))              # ...the proof stays until cleanup

    def test_the_pass_stamps_the_marker_with_the_master(self):
        import nativelane
        from orchestrator import NATIVE_ENCODED
        m = self.make_ready(self.p)
        (ok, msg), _got, _rm = self._remux(ok=True)
        self.assertTrue(ok, msg)
        stamp = nativelane.read_marker(self.p)
        st = os.stat(self.p.final)
        self.assertEqual(stamp["state"], NATIVE_ENCODED)
        self.assertEqual(stamp["final"], self.p.final)
        self.assertEqual((stamp["final_size"], stamp["final_mtime_ns"]), (st.st_size, st.st_mtime_ns))
        self.assertEqual(stamp["ready_id"], m["id"])
        self.assertFalse(nativelane.ready(self.p))               # never mistaken for a ready analysis
        self.assertTrue(nativelane.has_marker(self.p))
        self.assertEqual(nativelane.held_native_bytes(self.root), 0)
        self.assertTrue(os.path.exists(nativelane.rpu_path(self.p)))   # the small files stay
        self.assertTrue(os.path.exists(os.path.join(self.segdir, "segments.json")))

    def test_without_a_stamp_the_whole_segdir_goes_as_before(self):
        self.make_ready(self.p)
        with mock.patch.object(stages.logbook, "exception"):
            (ok, msg), _got, _rm = self._remux(ok=True, write_final=False)   # nothing to stat
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.segdir))

    def test_the_proof_and_the_ledger_are_cleanups_to_remove(self):
        import nativelane
        self.make_ready(self.p)
        self._remux(ok=True)
        with mock.patch.object(stages.transfer, "delete_tree", create=True):
            ok, msg = stages._cleanup(self.p, None)
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.segdir))
        self.assertFalse(nativelane.has_marker(self.p))

    def test_each_live_segment_is_timed_into_the_ledger(self):
        import nativelane
        self.make_ready(self.p)
        nativelane.record_cpu(self.p, "analysis", "chunk_0000", 42.0, 10)
        now = [100.0]

        def drive(kw):                       # the remux's own callbacks, as dvcap fires them
            kw["on_plan"]([10, 20, 30], 30)
            kw["on_progress"](10, 30)                    # segment 1 resumed: never timed
            now[0] = 160.0
            kw["on_progress"](15, 30)                    # segment 2 live...
            now[0] = 400.0
            kw["on_progress"](20, 30)                    # ...done: 300 s since segment 1 ended
            now[0] = 410.0
            kw["on_progress"](25, 30)
            now[0] = 500.0
            kw["on_progress"](30, 30)                    # segment 3: 100 s
            now[0] = 600.0
            kw["on_repair"](1, 1, 1, 0, 10)              # a peak repair of segment 2 (index 1)
            now[0] = 650.0
            kw["on_repair"](1, 1, 1, 7, 10)
            now[0] = 680.0
            kw["on_repair"](1, 1, 1, 10, 10)
        with mock.patch.object(stages.time, "monotonic", side_effect=lambda: now[0]):
            (ok, msg), got, _rm = self._remux(ok=True, drive=drive)
        self.assertTrue(ok, msg)
        book = nativelane._read_json(nativelane.cpu_path(self.p))
        self.assertEqual(book["remux"], {"seg_0001": {"wall_s": 300.0, "frames": 10},
                                         "seg_0002": {"wall_s": 100.0, "frames": 10}})
        self.assertEqual(book["remux_repair"], {"seg_0001": {"wall_s": 80.0, "frames": 10}})
        self.assertEqual(book["analysis"]["chunk_0000"]["cpu_s"], 42.0)   # survives the pass

    def test_the_resolve_call_gets_the_original_callbacks(self):
        (ok, _msg), got, _rm = self._remux(ok=True)
        self.assertTrue(ok)
        self.assertEqual((got["on_progress"].__name__, got["on_plan"].__name__,
                          got["on_repair"].__name__), ("_prog", "_on_plan", "_on_repair"))
        self.make_ready(self.p)
        (ok, _msg), got, _rm = self._remux(ok=True)
        self.assertEqual(got["on_progress"].__name__, "on_prog")     # only native is wrapped

    def test_a_failed_remux_keeps_the_chunks_for_the_retry(self):
        import nativelane
        self.make_ready(self.p)
        (ok, _msg), got, _rm = self._remux(ok=False)
        self.assertFalse(ok)
        self.assertIn("native", got)
        self.assertTrue(nativelane.ready(self.p))                # chunks, RPU and marker all kept

    def test_a_marker_that_no_longer_matches_ships_nothing(self):
        self.make_ready(self.p)
        _tn._append(os.path.join(self.segdir, "seg_0001.mov"), b"re-encoded")
        (ok, msg), _got, rm = self._remux()
        self.assertFalse(ok)
        self.assertIn("no longer matches", msg)
        self.assertIn("seg_0001.mov changed", msg)
        rm.assert_not_called()
        self.assertTrue(os.path.isdir(self.segdir))

    def test_a_resolve_item_gets_exactly_the_pre_native_call(self):
        # No marker (and a leftover segdir that must not be touched by the remux).
        (ok, _msg), got, _rm = self._remux(ok=True)
        self.assertTrue(ok)
        self.assertEqual(set(got) - {"_args"}, self.PRE_NATIVE_KWARGS)    # no `native` at all
        self.assertEqual(got["_args"], (self.p.dv_render, self.p.source_cfr, self.p.source,
                                        self.p.final))
        self.assertTrue(os.path.isdir(self.segdir))

    def test_a_fallen_back_item_is_a_resolve_item(self):
        import nativelane
        self.make_ready(self.p)
        nativelane.write_fallback(self.p, "test")
        (ok, _msg), got, _rm = self._remux(ok=True)
        self.assertTrue(ok)
        self.assertNotIn("native", got)
        self.assertTrue(os.path.isdir(self.segdir))


class NativeRemuxClock(unittest.TestCase):
    """The native remux's wall-time ledger (segdir/native/cpu.json): only LIVE segments, rows
    replaced on a retry, and booking never fails the remux."""

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        self.p = _paths(self.root)
        os.makedirs(self.p.segdir)
        self.now = [0.0]
        self.c = stages._NativeRemuxClock(self.p, clock=lambda: self.now[0])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _book(self):
        import nativelane
        return nativelane._read_json(nativelane.cpu_path(self.p)) or {}

    def _at(self, t, frames, **extra):
        self.now[0] = t
        self.c.progress(frames, 30, **extra)

    def test_a_resumed_attempt_times_only_what_it_encoded(self):
        self.c.plan([10, 20, 30], 30)
        self._at(1, 10)
        self._at(2, 20)                                  # two resumed segments: instant jumps
        self._at(50, 25)
        self._at(100, 30)
        self.assertEqual(self._book(), {"remux": {"seg_0002": {"wall_s": 98.0, "frames": 10}}})

    def test_a_retry_replaces_the_row(self):
        self.c.plan([10, 20], 20)
        self._at(5, 5)
        self._at(60, 10)
        self.c.plan([10, 20], 20)                        # a new attempt re-encodes segment 1
        self._at(70, 5)
        self._at(100, 10)
        self.assertEqual(self._book()["remux"]["seg_0000"], {"wall_s": 40.0, "frames": 10})

    def test_progress_before_the_plan_and_repair_ticks_are_ignored(self):
        self._at(5, 5)
        self.c.plan([10], 10)
        self._at(6, 10, repair_seg=1, repair_done=3)
        self.assertEqual(self._book(), {})

    def test_two_repair_rungs_of_one_segment_add_up(self):
        for t, done in ((0, 0), (30, 5), (50, 10), (60, 0), (70, 4), (90, 10)):
            self.now[0] = t
            self.c.repair(3, done, 10)
        self.c.finish()
        self.assertEqual(self._book()["remux_repair"], {"seg_0002": {"wall_s": 80.0, "frames": 10}})

    def test_an_unwritable_ledger_never_raises(self):
        import nativelane
        with open(nativelane.native_dir(self.p), "w") as f:      # a FILE where the dir should be
            f.write("x")
        self.c.plan([10], 10)
        self._at(5, 5)
        with mock.patch.object(stages.logbook, "exception") as logged:
            self._at(9, 10)
        logged.assert_called_once()

    def test_the_log_line_sums_the_ledger(self):
        self.c.plan([10, 20], 20)
        self._at(5, 5)
        self._at(10, 10)
        self._at(15, 15)
        self._at(30, 20)
        self.assertEqual(stages.native_wall_line(self.p),
                         "remux wall 30 s over 20 frames (2 segments, 1500.0 ms/frame)")


class _DvRenderItem(unittest.TestCase):
    """A native-4K YouTube item on disk (its CFR), a LUT, a bounds book and a settings file in a
    temp dir; the native toolchain check and the CFR probes faked as a 3840x2160 BT.709 source."""

    INFO = {"width": 3840, "height": 2160, "pix_fmt": "yuv420p", "r_frame_rate": "30000/1001",
            "color_primaries": "bt709", "color_space": "bt709", "color_transfer": "bt709",
            "color_range": "tv"}

    def setUp(self):
        import nativelane, settings
        self.d = tempfile.mkdtemp()
        self.p = orch_youtube_paths("Chan", "/s/Chan/x/v [aaaaaaaaaa7].mp4", "V", scratch_dir=self.d)
        for f in (self.p.source, self.p.source_cfr):
            with open(f, "wb") as fh:
                fh.write(b"x" * 64)
        self.cube = os.path.join(self.d, "rcm.cube")
        with open(self.cube, "w") as fh:
            fh.write("LUT_3D_SIZE 2\n")
        self.info = dict(self.INFO)
        self.probes = []                     # the keyword arguments of every probe_stream call
        self.cfr = {"path": self.p.source_cfr, "frames": 1000, "fps": "30000/1001"}
        self.toolchain = (True, "toolchain matches the pins")
        for cm in (mock.patch.multiple(settings, CONFIG_DIR=self.d,
                                       SETTINGS_FILE=os.path.join(self.d, "settings.json"),
                                       PROFILES_FILE=os.path.join(self.d, "show_profiles.json")),
                   mock.patch.object(stages, "_SEGBOUNDS_FILE", os.path.join(self.d, "bounds.json")),
                   mock.patch.object(nativelane, "toolchain_ok", side_effect=lambda **kw: self.toolchain),
                   mock.patch.object(nativelane, "probe_stream",
                                     side_effect=lambda _p, **kw: self.probes.append(kw) or dict(self.info)),
                   mock.patch.object(nativelane, "cfr_info", side_effect=lambda _p: dict(self.cfr)),
                   mock.patch.object(nativelane, "lut_path", side_effect=lambda: self.cube)):
            cm.start()
            self.addCleanup(cm.stop)


class NativeRenderSpec(_DvRenderItem):
    """dvrender_spec is Resolve's render settings for the route — or a refusal, and then Resolve
    renders the item as before."""

    def test_native_4k_youtube_gets_resolves_export_settings(self):
        stages._write_topaz_bounds(self.p.source_basename, [300, 700, 1000])
        spec, why = stages.dvrender_spec(self.p, "yt_native4k")
        self.assertEqual(why, "")
        self.assertEqual(spec.inputs, ((0, self.p.source_cfr), (1000, None)))   # resolve_input
        self.assertEqual((spec.fps, spec.lut, spec.pre_vf), ("30000/1001", self.cube, None))
        self.assertEqual(spec.kbps, stages.youtube_render_kbps(2160))
        self.assertEqual(spec.kbps, stages.YOUTUBE_RENDER_KBPS_4K)
        self.assertEqual(spec.cap_mbps, 50)                       # _resolve's cap_arg default
        self.assertEqual(spec.bounds, (300, 700, 1000))           # the scene plan's ranges
        self.assertEqual(spec.analysis_ranges(), [(0, 300), (300, 700), (700, 1000)])
        import settings
        settings.set_settings({"max_peak_mbps": 40})
        self.assertEqual(stages.dvrender_spec(self.p, "yt_native4k")[0].cap_mbps, 40)

    def test_4k_sdr_exports_at_the_intakes_bitrate_with_no_gate(self):
        with mock.patch.object(stages, "_source_video_kbps", return_value=80000) as kb:
            spec, why = stages.dvrender_spec(self.p, "sdr4k")
        kb.assert_called_once_with(self.p.source)                 # the ORIGINAL, as _resolve reads
        self.assertEqual((spec.kbps, spec.cap_mbps, spec.bounds), (80000, 0, None))
        with mock.patch.object(stages, "_source_video_kbps", return_value=20000):
            self.assertEqual(stages.dvrender_spec(self.p, "sdr4k")[0].kbps,
                             stages.EXPORT_BITRATE_FLOOR_KBPS)

    def test_every_refusal_says_why(self):
        def refused(route="yt_native4k"):
            spec, why = stages.dvrender_spec(self.p, route)
            self.assertIsNone(spec)
            return why
        for route in ("dvp7", "already_dv", "combine_rpu", "unknown", None):
            self.assertIn("has no native render yet", refused(route), route)
        self.toolchain = (False, "dovi_tool is 2.2.0, pinned 2.3.0")
        self.assertEqual(refused(), "the native toolchain is not ready: dovi_tool is 2.2.0, pinned 2.3.0")
        self.toolchain = (True, "ok")
        for key, value, words in (("color_space", "smpte170m", "the source matrix are smpte170m"),
                                  ("color_range", "pc", "the source is full range"),
                                  ("color_transfer", "smpte2084", "the source is already HDR")):
            self.info = dict(self.INFO, **{key: value})
            self.assertIn(words, refused())
        for w, h in ((4096, 2160), (3840, 1600), (2160, 3840)):   # not refused: fitted into 16:9
            self.info = dict(self.INFO, width=w, height=h)
            spec, why = stages.dvrender_spec(self.p, "sdr4k")
            self.assertEqual(why, "", (w, h))
            self.assertIn("pad=3840:2160:", spec.pre_vf)
        self.info = dict(self.INFO)
        self.cfr = dict(self.cfr, frames=None)
        self.assertIn("frame count or frame rate is unknown", refused())
        self.cfr = dict(self.cfr, frames=1000)
        os.remove(self.cube)
        self.assertIn("the LUT is missing", refused())
        os.remove(self.p.source_cfr)
        self.assertEqual(refused(), "its CFR source is not on disk")

    def test_it_writes_nothing(self):
        before = sorted(os.listdir(self.d))
        stages.dvrender_spec(self.p, "yt_native4k")
        self.assertEqual(sorted(os.listdir(self.d)), before)


class NativeRenderFitSpec(_DvRenderItem):
    """yt_plain and yt_1080 (2026-10-18): the fit front end before the LUT — BT.601 / full range to
    BT.709 limited when the tags (or, untagged, the frame size) say so, a Lanczos fit into
    3840x2160 and a black pad — at the bitrate and under the gate _resolve would give them."""

    BT601 = {"color_space": "smpte170m", "color_primaries": "smpte170m", "color_transfer": "bt709"}

    def _spec(self, route="yt_plain", **info):
        self.info = dict(self.INFO, **info)
        spec, why = stages.dvrender_spec(self.p, route)
        self.assertEqual(why, "")
        return spec

    def test_1080p_is_an_exact_2x_lanczos_with_no_pad(self):
        import dvnative
        stages._write_topaz_bounds(self.p.source_basename, [300, 700, 1000])
        spec = self._spec("yt_1080", width=1920, height=1080, sample_aspect_ratio="1:1")
        self.assertEqual(spec.pre_vf, "zscale=w=3840:h=2160:filter=lanczos:param_a=3:"
                                      "rangein=limited:range=limited,format=yuv444p16le,setsar=1")
        self.assertEqual(spec.pre_vf, dvnative.fit_front_end(self.info))
        self.assertEqual(self.probes, [{"extra": ("sample_aspect_ratio",)}])   # the SAR is asked for
        self.assertEqual((spec.lut, spec.fps, spec.bounds), (self.cube, "30000/1001", (300, 700, 1000)))
        self.assertEqual((spec.kbps, spec.cap_mbps), (stages.YOUTUBE_RENDER_KBPS, 50))
        # scale BEFORE the LUT, in the encode and in the analysis alike
        self.assertEqual(spec.encode_vf(), spec.pre_vf + "," + dvnative.lut_vf(self.cube))
        self.assertTrue(spec.analysis_vf().startswith(spec.pre_vf + ",scale=1920:1080:flags=area,"
                                                      "zscale=matrixin=709"))

    def test_480p_bt601_gets_the_601_front_end_then_a_pillarbox(self):
        spec = self._spec(width=640, height=480, **self.BT601)
        colour, fit = spec.pre_vf.split(",zscale=w=")
        self.assertEqual(colour, "zscale=matrixin=170m:rangein=limited:transferin=709:primariesin=170m:"
                                 "transfer=709:primaries=709:matrix=709:range=limited,format=yuv444p16le")
        self.assertEqual("zscale=w=" + fit,
                         "zscale=w=2880:h=2160:filter=lanczos:param_a=3:rangein=limited:range=limited,"
                         "format=yuv444p16le,setsar=1,setparams=range=pc,pad=3840:2160:480:0:color=black,"
                         "setparams=range=tv")
        self.assertEqual(spec.kbps, stages.YOUTUBE_RENDER_KBPS)

    def test_a_vertical_video_is_pillarboxed_at_resolves_14_mbps(self):
        spec = self._spec(width=1080, height=1920)
        self.assertIn("zscale=w=1216:h=2160:", spec.pre_vf)
        self.assertIn("pad=3840:2160:1312:0:", spec.pre_vf)
        self.assertNotIn("matrixin", spec.pre_vf)                 # BT.709 tags: no colour step
        self.assertEqual(spec.kbps, stages.youtube_render_kbps(1920))
        self.assertEqual(spec.kbps, stages.YOUTUBE_RENDER_KBPS_4K)


    def test_untagged_full_range_and_pal_sources(self):
        untagged = {"color_space": None, "color_primaries": None, "color_transfer": None,
                    "color_range": None}
        self.assertIn("matrixin=170m:rangein=limited:transferin=709:primariesin=170m:",
                      self._spec(width=640, height=480, **untagged).pre_vf)        # NTSC SD
        self.assertIn("matrixin=470bg:rangein=limited:transferin=709:primariesin=470bg:",
                      self._spec(width=720, height=576, **untagged).pre_vf)        # PAL SD
        self.assertNotIn("matrixin", self._spec(width=1280, height=720, **untagged).pre_vf)   # HD
        self.assertTrue(self._spec("yt_1080", width=1920, height=1080, color_range="pc").pre_vf
                        .startswith("zscale=matrixin=709:rangein=full:matrix=709:range=limited,"
                                    "format=yuv444p16le,zscale=w=3840:h=2160:"))

    def test_every_fit_refusal_says_why(self):
        def refused(**info):
            self.info = dict(dict(self.INFO, width=1920, height=1080), **info)
            spec, why = stages.dvrender_spec(self.p, "yt_1080")
            self.assertIsNone(spec)
            return why
        self.assertEqual(refused(color_transfer="smpte2084"), "the source is already HDR (smpte2084)")
        self.assertIn("transfer is iec61966-2-1", refused(color_transfer="iec61966-2-1"))
        self.assertIn("matrix are bt2020nc", refused(color_space="bt2020nc"))
        self.assertIn("primaries are bt2020", refused(color_primaries="bt2020"))
        self.assertIn("size is unknown", refused(width=0))
        self.info = {}
        self.assertEqual(stages.dvrender_spec(self.p, "yt_plain"), (None, "the source could not be probed"))

    def test_a_native_4k_frame_takes_no_front_end_and_any_other_is_fitted(self):
        spec, _why = stages.dvrender_spec(self.p, "yt_native4k")
        self.assertEqual(self.probes, [{"extra": ("sample_aspect_ratio",)}])
        self.assertIsNone(spec.pre_vf)                            # 3840x2160 BT.709: the LUT alone
        self.info = dict(self.INFO, width=4096, height=2160)      # DCI 4K: letterboxed into 16:9
        spec, why = stages.dvrender_spec(self.p, "yt_native4k")
        self.assertEqual(why, "")
        self.assertIn("pad=3840:2160:", spec.pre_vf)


class RouteRefusal(unittest.TestCase):
    """route_refusal: the refusals that do not depend on the chunks, asked before Topaz."""

    def _ask(self, route, *, base=None, youtube=False, combine=False, size=(3840, 2160)):
        import routes, plan
        p = types.SimpleNamespace(source="/x/src.mkv", youtube=youtube, combine=combine)
        def item_route(_p, mode=None):
            return base if mode == "dv1000" else route
        with mock.patch.object(routes, "item_route", side_effect=item_route), \
             mock.patch.object(plan, "probe_input", return_value={"width": size[0], "height": size[1]}):
            return stages.route_refusal(p)

    def test_makeable_and_unknown_yet_are_not_refused(self):
        for route in ("tv_sdr_topaz", "movie_sdr_topaz", "yt_plain", "rpu_only", "hdr_topaz", None,
                      "combine_rpu", "already_dv", "dvp7"):
            self.assertEqual(self._ask(route), "", route)

    def test_no_route_at_all(self):
        self.assertIn("has no native render", self._ask("unknown"))

    def test_hdr_under_an_sdr_pin_needs_a_tone_map(self):
        self.assertIn("tone map", self._ask("sdr_out", base="hdr_topaz"))
        self.assertEqual(self._ask("sdr_out", base="tv_sdr_topaz"), "")

    def test_an_hdr_youtube_video_of_another_size(self):
        self.assertIn("no HDR fit", self._ask("hdr_reencode", youtube=True, size=(1920, 1080)))
        self.assertEqual(self._ask("hdr_reencode", youtube=True), "")
        self.assertEqual(self._ask("rpu_only", youtube=True, size=(1920, 1080)), "")  # keeps its stream

    def test_it_never_raises(self):
        import routes
        with mock.patch.object(routes, "item_route", side_effect=OSError("probe")):
            self.assertEqual(stages.route_refusal(types.SimpleNamespace(source="/x")), "")


class NativeRouteDecision(_DvRenderItem):
    """native_route: the item's route and whether the native render can make it."""

    def test_a_route_gets_its_spec(self):
        import routes
        with mock.patch.object(routes, "item_route", return_value="yt_native4k"):
            route, spec, why = stages.native_route(self.p)
        self.assertEqual((route, why), ("yt_native4k", ""))
        self.assertEqual(spec.kbps, stages.YOUTUBE_RENDER_KBPS_4K)

    def test_a_route_the_stage_cannot_make_says_why(self):
        import routes
        with mock.patch.object(routes, "item_route", return_value="dvp7"):
            route, spec, why = stages.native_route(self.p)
        self.assertEqual((route, spec), ("dvp7", None))
        self.assertIn("has no native render yet", why)

    def test_a_fit_route_gets_its_spec(self):
        import routes
        self.info = dict(self.INFO, width=1920, height=1080)
        with mock.patch.object(routes, "item_route", return_value="yt_1080"):
            route, spec, why = stages.native_route(self.p)
        self.assertEqual((route, why), ("yt_1080", ""))
        self.assertTrue(spec.pre_vf.startswith("zscale=w=3840:h=2160:filter=lanczos"))

    def test_a_combine_whose_verdict_is_lost_is_refused_for_good(self):
        import companion, routes, types
        p = types.SimpleNamespace(**dict(vars(self.p), combine=True))
        with mock.patch.object(companion, "confirmed_verdict", return_value=None), \
             mock.patch.object(routes, "item_route", return_value=routes.UNKNOWN):
            route, spec, why = stages.native_route(p)
        self.assertIsNone(spec)
        self.assertTrue(why.startswith("permanent: combine verdict lost"), why)

class _FakeSpec:
    total = 1000


class NativeRenderStage(_DvRenderItem):
    """The run-only 'dvrender' stage: nativerender.render with the route's spec; should_pause
    counts only inside the analysis; a stop is 'interrupted:', a failure a plain reason."""

    def _run(self, render, *, should_pause=None, abort=None, route="yt_native4k", spec=_FakeSpec()):
        import nativerender, routes
        seen = []
        with mock.patch.object(routes, "item_route", return_value=route), \
             mock.patch.object(stages, "dvrender_spec", return_value=(spec, "") if spec else (None, "nope")), \
             mock.patch.object(nativerender, "render", side_effect=render) as r:
            out = stages.run_stage("dvrender", self.p, abort=abort, progress=seen.append,
                                   should_pause=should_pause)
        return out, seen, r

    def test_run_stage_dispatches_it_with_should_pause(self):
        sp = lambda: False
        with mock.patch.object(stages, "_dvrender", return_value=(True, "ok")) as fn:
            self.assertEqual(stages.run_stage("dvrender", self.p, should_pause=sp), (True, "ok"))
        self.assertIs(fn.call_args.args[3], sp)

    def test_it_renders_the_routes_spec_and_labels_its_progress(self):
        spec = _FakeSpec()

        def render(p, sp, abort=None, progress=None, should_pause=None):
            self.assertIs(sp, spec)
            progress({"stage": "dvrender", "ep": p.ep, "phase": "analysis", "pct": 12.5,
                      "seg_done": 1, "seg_total": 4})
            return True, "native DV render: 1000 frames"
        (ok, msg), seen, _r = self._run(render, spec=spec)
        self.assertEqual((ok, msg), (True, "native DV render: 1000 frames"))
        self.assertEqual(seen, [{"stage": "dvrender", "ep": self.p.ep, "phase": "analysis",
                                 "pct": 12.5, "seg_done": 1, "seg_total": 4, "frames": 1000,
                                 "route": "yt_native4k"}])

    def test_should_pause_counts_only_inside_the_analysis(self):
        asked = []

        def render(p, spec, abort=None, progress=None, should_pause=None):
            asked.append(should_pause())                           # before anything: setup
            for phase in ("analysis", "encode", "finish"):
                progress({"stage": "dvrender", "phase": phase, "pct": 1.0})
                asked.append(should_pause())
            return True, "ok"
        self._run(render, should_pause=lambda: True)
        self.assertEqual(asked, [True, True, False, False])

    def test_without_should_pause_render_gets_none(self):
        (ok, _m), _s, r = self._run(lambda *a, **k: (True, "ok"))
        self.assertIsNone(r.call_args.kwargs["should_pause"])

    def test_a_refused_spec_is_a_plain_failure_and_renders_nothing(self):
        (ok, msg), _s, r = self._run(lambda *a, **k: (True, "x"), spec=None)
        self.assertEqual((ok, msg), (False, "native render refused (route yt_native4k): nope"))
        r.assert_not_called()

    def test_a_stop_is_interrupted_a_pause_and_a_failure_pass_through(self):
        import threading
        with mock.patch.object(stages.logbook, "failure") as fail:
            (ok, msg), _s, _r = self._run(lambda *a, **k: (False, "aborted (run stopped)"))
            self.assertEqual((ok, msg), (False, "interrupted: native DV render stopped — the next "
                                                "pass resumes its analysis range by range"))
            ab = threading.Event(); ab.set()
            (ok, msg), _s, _r = self._run(lambda *a, **k: (False, "native render failed: x"), abort=ab)
            self.assertTrue(msg.startswith("interrupted:"))
            self.assertEqual(self._run(lambda *a, **k: (False, "paused: yielded before analysis range 2"))[0],
                             (False, "paused: yielded before analysis range 2"))
            fail.assert_not_called()                               # none of those is a red failure
            (ok, msg), _s, _r = self._run(lambda *a, **k: (False, "native render failed: rc=1"))
        self.assertEqual((ok, msg), (False, "native render failed: rc=1"))
        fail.assert_called_once()


class NativeRenderMessages(_DvRenderItem):
    """The skip messages name who makes the item's DV: the native render."""

    FAST = "4K SDR (av1) @ ~20 Mbps — no upscale, HDR + DV added"

    def _topaz(self, plan_topaz, native=None):
        import plan
        pl = {"topaz": plan_topaz, "reason": self.FAST}
        cms = [mock.patch.object(plan, "plan_for", return_value=pl),
               mock.patch.object(stages, "_plan_fast_path_bounds", return_value=" (scene plan cached)")]
        if native is not None:
            cms.append(mock.patch.object(stages, "native_route", return_value=native))
        with contextlib_exit(cms):
            return stages._topaz(self.p, None)

    def test_a_route_names_the_native_render(self):
        self.assertEqual(self._topaz("upscale", ("yt_native4k", _FakeSpec(), "")),
                         (True, "YouTube → the native DV render (yt_native4k) — skipping Topaz"
                                " (scene plan cached)"))
        self.assertEqual(self._topaz("dv-only", ("yt_native4k", _FakeSpec(), "")),
                         (True, self.FAST + " — skipping upscale (scene plan cached)"))

    def test_a_refused_or_broken_decision_never_costs_the_stage(self):
        self.assertEqual(self._topaz("upscale", ("yt_plain", None, "no native render yet")),
                         (True, "YouTube → the native DV render (its route) — skipping Topaz"
                                " (scene plan cached)"))
        with mock.patch.object(stages, "native_route", side_effect=RuntimeError("boom")):
            self.assertEqual(self._topaz("upscale")[0], True)


def contextlib_exit(cms):
    import contextlib
    es = contextlib.ExitStack()
    for cm in cms:
        es.enter_context(cm)
    return es


class NativeRenderCleanup(unittest.TestCase):
    """Cleanup deletes a native render's books and finish-step strays (never needed after the
    upload)."""

    def test_the_books_and_strays_go_and_the_message_is_the_same(self):
        import nativerender
        d = tempfile.mkdtemp()
        p = _paths(d)
        for f in p.working_files():
            with open(f, "w") as fh:
                fh.write("x")
        os.makedirs(os.path.join(nativerender.books_dir(p), nativerender.ANALYSIS_DIR))
        for f in (os.path.join(nativerender.books_dir(p), nativerender.RPU),
                  p.dv_render + nativerender.PART, p.dv_render + nativerender.ES_DV):
            with open(f, "w") as fh:
                fh.write("x")
        ok, msg = stages.run_stage("cleanup", p)
        self.assertTrue(ok)
        self.assertEqual(msg, f"permanently deleted 6 local working files + chunks for {p.ep}")
        self.assertEqual(os.listdir(d), [])


class RouteE2ETool(unittest.TestCase):
    """tools/route_e2e.py builds a render for exactly the routes the stage can make, as the item
    kind each route is (a YouTube video, a movie), and refuses the live pipeline's directories."""

    def _tool(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(stages.__file__)), "..", "tools",
                            "route_e2e.py")
        spec = importlib.util.spec_from_file_location("route_e2e_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_it_covers_exactly_the_stages_routes(self):
        t = self._tool()
        # every route the stage renders from ONE file (a chunk route needs a Topaz run first,
        # a combine two copies and a confirmed verdict)
        self.assertEqual(sorted(t.KINDS), sorted(set(stages.DVRENDER_ROUTES)
                                                 - set(stages.DVRENDER_CHUNK_ROUTES)
                                                 - set(stages.DVRENDER_COMBINE_ROUTES)
                                                 - set(stages.DVRENDER_OUTPUT_ROUTES)))
        self.assertEqual(t.KINDS, {"yt_native4k": "youtube", "yt_plain": "youtube",
                                   "yt_1080": "youtube", "sdr4k": "movie", "rpu_only": "movie",
                                   "hdr_reencode": "movie", "hlg": "movie"})

    def test_square_pixels_means_1_1_or_none_stated(self):
        # 2026-10-18 review: the render / master geometry check also asks for square pixels
        t = self._tool()
        for sar in ("1:1", "0:1", "N/A", "", None):
            self.assertTrue(t.square_pixels({"sample_aspect_ratio": sar}), sar)
        self.assertTrue(t.square_pixels({}))
        for sar in ("1215:1216", "1281:1280", "8:9", "32:27"):
            self.assertFalse(t.square_pixels({"sample_aspect_ratio": sar}), sar)

    def test_it_refuses_the_live_directories(self):
        t = self._tool()
        src = tempfile.mkstemp()[1]
        for work in (os.path.expanduser("~/topaz-scratch/e2e"), os.path.expanduser("~/.topaz-pipeline/x"),
                     "/private/tmp/x/topaz-scratch"):
            with mock.patch("sys.stderr"):
                self.assertEqual(t.main(["--source", src, "--work", work, "--route", "sdr4k"]), 2, work)
        self.assertFalse(os.path.exists(os.path.expanduser("~/topaz-scratch/e2e")))


# ---- HDR intake on the native render (2026-10-18): rpu_only, hdr_reencode, hlg, hdr_topaz -------

HDR_HAMILTON = ("G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)", "0,0")
HDR_P3_4000 = ("G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)", "850,210")


class NativeRenderHdrSpec(_DvRenderItem):
    """dvrender_spec for the HDR intake routes: no LUT; PQ as it is, HLG through the BT.2100
    HLG -> PQ front end; the ORIGINAL's own mastering for PQ (its SEI as L6, as it stands); a
    keep_video render for rpu_only; Resolve's export bitrate and gate; Topaz's chunks for
    hdr_topaz. Every refusal names its reason, and Resolve then renders the item as before."""

    PQ = {"width": 3840, "height": 2160, "pix_fmt": "yuv420p10le", "r_frame_rate": "24000/1001",
          "codec_name": "hevc", "color_primaries": "bt2020", "color_space": "bt2020nc",
          "color_transfer": "smpte2084", "color_range": "tv"}

    def setUp(self):
        super().setUp()
        import orchestrator
        self.movie = orchestrator.movie_paths("Film (2020).mkv", "/Media/Movies/Film", scratch_dir=self.d)
        for f in (self.movie.source, self.movie.source_cfr):
            with open(f, "wb") as fh:
                fh.write(b"x" * 64)
        self.info = dict(self.PQ)
        self.cfr = {"path": self.movie.source_cfr, "frames": 1000, "fps": "24000/1001"}
        self.hdr10 = HDR_HAMILTON
        self.hdr10_asked = []
        for cm in (mock.patch.object(stages, "source_hdr10",
                                     side_effect=lambda path: self.hdr10_asked.append(path) or self.hdr10),
                   mock.patch.object(stages, "_source_video_kbps", return_value=16000)):
            cm.start()
            self.addCleanup(cm.stop)

    def spec(self, route, p=None, **info):
        self.info = dict(self.PQ, **info)
        spec, why = stages.dvrender_spec(p or self.movie, route)
        self.assertEqual(why, "")
        return spec

    def refused(self, route, p=None, **info):
        self.info = dict(self.PQ, **info)
        spec, why = stages.dvrender_spec(p or self.movie, route)
        self.assertIsNone(spec)
        return why

    def test_rpu_only_is_the_sources_own_stream_with_its_own_mastering(self):
        import dvnative
        spec = self.spec("rpu_only")
        self.assertEqual(spec.inputs, ((0, self.movie.source_cfr), (1000, None)))
        self.assertEqual((spec.lut, spec.pre_vf, spec.keep_video, spec.cap_mbps),
                         (None, None, True, 0))
        self.assertEqual(spec.kbps, stages.EXPORT_BITRATE_FLOOR_KBPS)      # _resolve's rpu-only bitrate
        # Hamilton's SEI says 0,0 ("unknown"): light None — measured, in L6 and the SEI (declared_light)
        self.assertEqual((spec.master.mdcv, spec.master.max_nits, spec.light), (HDR_HAMILTON[0], 1000, None))
        self.assertEqual(self.hdr10_asked, [self.movie.source])            # the ORIGINAL's SEI
        self.assertEqual(self.probes, [{"extra": ("codec_name",)}])
        self.assertEqual(spec.encode_vf(), "format=yuv420p10le")
        self.assertEqual(spec.analysis_vf(), dvnative.analysis_vf(None))  # measured as it is
        self.assertEqual(self.spec("rpu_only", p=self.p).keep_video, True)  # YouTube too (as _remux)

    def test_a_source_that_says_unknown_has_its_light_measured(self):
        self.assertIsNone(stages.declared_light((0, 0)))
        self.assertIsNone(stages.declared_light(None))
        self.assertEqual(stages.declared_light((850, 210)), (850, 210))
        self.assertEqual(stages.declared_light((620, 0)), (620, 0))       # a level, however odd

    def test_rpu_only_refusals(self):
        self.assertIn("not HEVC Main10", self.refused("rpu_only", codec_name="av1"))
        self.assertIn("not HEVC Main10", self.refused("rpu_only", pix_fmt="yuv420p"))
        self.assertIn("route rpu_only takes PQ", self.refused("rpu_only", color_transfer="arib-std-b67"))
        self.assertIn("full range", self.refused("rpu_only", color_range="pc"))
        self.hdr10 = None
        self.assertEqual(self.refused("rpu_only"), "the source's HDR10 metadata could not be read")
        self.hdr10 = (HDR_HAMILTON[0].replace("L(10000000,1)", "L(10005000,1)"), "0,0")
        self.assertIn("1000.5 nits", self.refused("rpu_only"))
        self.hdr10 = HDR_HAMILTON
        self.cfr = dict(self.cfr, frames=None)
        self.assertIn("frame count or frame rate is unknown", self.refused("rpu_only"))

    def test_hdr_reencode_keeps_its_frame_and_mastering_at_resolves_bitrate(self):
        self.hdr10 = HDR_P3_4000
        stages._write_topaz_bounds(self.movie.source_basename, [400, 1000])
        for w, h in ((3840, 2160), (3840, 1600), (4096, 2160)):
            spec = self.spec("hdr_reencode", width=w, height=h, codec_name="av1")
            self.assertEqual((spec.keep_video, spec.lut, spec.pre_vf, spec.cap_mbps), (False, None, None, 0))
            self.assertEqual(spec.kbps, stages.EXPORT_BITRATE_FLOOR_KBPS)  # max(16000, the floor)
            self.assertEqual((spec.master.max_nits, spec.master.l6_min, spec.light), (4000, 50, (850, 210)))
            self.assertEqual(spec.bounds, (400, 1000))
        with mock.patch.object(stages, "_source_video_kbps", return_value=90000):
            self.assertEqual(self.spec("hdr_reencode", codec_name="h264", pix_fmt="yuv420p").kbps, 90000)
        self.assertIn("larger than 4096x2176", self.refused("hdr_reencode", width=5120, height=2160))
        self.assertIn("route hdr_reencode takes PQ", self.refused("hdr_reencode", color_transfer="bt709"))
        self.assertIn("primaries are bt709", self.refused("hdr_reencode", color_primaries="bt709"))

    def test_a_youtube_hdr_video_renders_at_3840x2160_under_the_gate_or_stays_on_resolve(self):
        spec = self.spec("hdr_reencode", p=self.p, codec_name="vp9")
        self.assertEqual((spec.kbps, spec.cap_mbps), (stages.youtube_render_kbps(2160), 50))
        self.assertEqual(spec.inputs, ((0, self.p.source_cfr), (1000, None)))
        for w, h in ((1920, 1080), (3840, 1600)):
            self.assertIn("no HDR fit front end yet", self.refused("hdr_reencode", p=self.p, width=w, height=h))
            self.assertIn("no HDR fit front end yet",
                          self.refused("hlg", p=self.p, width=w, height=h, color_transfer="arib-std-b67"))

    def test_hlg_is_converted_to_pq_on_the_1000_nit_master(self):
        import dvnative
        spec = self.spec("hlg", color_transfer="arib-std-b67")
        self.assertEqual(spec.pre_vf, dvnative.hlg_to_pq_vf())
        self.assertEqual((spec.master, spec.light, spec.keep_video), (dvnative.DEFAULT_MASTER, None, False))
        self.assertEqual(self.hdr10_asked, [])                             # HLG has no mastering to keep
        self.assertEqual(spec.encode_vf(), dvnative.hlg_to_pq_vf() + ",format=yuv420p10le")
        self.assertTrue(spec.analysis_vf().startswith(dvnative.hlg_to_pq_vf() + ",scale=1920:1080"))
        self.assertIn("route hlg takes HLG", self.refused("hlg"))

    def test_hdr_topaz_renders_topazs_chunks(self):
        import dvnative, nativelane
        chunks = [[0, os.path.join(self.d, "c0.mov")], [600, os.path.join(self.d, "c1.mov")], [998, None]]
        with mock.patch.object(nativelane, "chunks", return_value=chunks) as ch:
            spec = self.spec("hdr_topaz")
            ch.assert_called_once_with(self.movie.segdir, expected=1000)
            self.assertEqual(spec.inputs, tuple(tuple(c) for c in chunks))
            self.assertEqual((spec.single, spec.bounds, spec.keep_video, spec.lut), (False, None, False, None))
            self.assertEqual((spec.light, spec.master.mdcv), (None, HDR_HAMILTON[0]))   # 0,0 -> measured
            self.assertEqual((spec.kbps, spec.cap_mbps), (stages.EXPORT_BITRATE_FLOOR_KBPS, 0))
            self.assertEqual(self.probes, [{"extra": ("codec_name",)}])
            hlg = self.spec("hdr_topaz", color_transfer="arib-std-b67")
            self.assertEqual((hlg.pre_vf, hlg.master), (dvnative.hlg_to_pq_vf(), dvnative.DEFAULT_MASTER))
            self.assertIn("chunk 0 runs at 25/1", self.refused("hdr_topaz", r_frame_rate="25/1"))
            self.assertIn("takes PQ or HLG", self.refused("hdr_topaz", color_transfer="bt709"))
        with mock.patch.object(nativelane, "chunks", side_effect=ValueError("chunk 1 (c1.mov) is missing")):
            self.assertEqual(self.refused("hdr_topaz"), "the Topaz chunks: chunk 1 (c1.mov) is missing")


    def test_it_probes_and_writes_nothing(self):
        before = sorted(os.listdir(self.d))
        for route in ("rpu_only", "hdr_reencode"):
            self.spec(route)
        self.assertEqual(sorted(os.listdir(self.d)), before)

    def test_the_route_gets_its_hdr_spec(self):
        import routes
        with mock.patch.object(routes, "item_route", return_value="rpu_only"):
            route, spec, why = stages.native_route(self.movie)
        self.assertEqual((route, why, spec.keep_video), ("rpu_only", "", True))


class SourceHdr10(unittest.TestCase):
    """source_hdr10 reads the HDR10 SEI off the FIRST frame only (no packet count)."""

    def _run(self, out, rc=0):
        r = mock.Mock(returncode=rc, stdout=out)
        with mock.patch.object(stages.subprocess, "run", return_value=r) as run:
            got = stages.source_hdr10("/x/src.mkv")
        self.assertIn("%+#1", run.call_args.args[0])
        self.assertNotIn("-count_packets", run.call_args.args[0])
        return got

    def test_reads_both_values_or_none(self):
        import json
        frame = {"frames": [{"side_data_list": [
            {"side_data_type": "Mastering display metadata", "red_x": "35400/50000", "red_y": "14600/50000",
             "green_x": "8500/50000", "green_y": "39850/50000", "blue_x": "6550/50000", "blue_y": "2300/50000",
             "white_point_x": "15635/50000", "white_point_y": "16450/50000",
             "min_luminance": "1/10000", "max_luminance": "10000000/10000"},
            {"side_data_type": "Content light level metadata", "max_content": 0, "max_average": 0}]}]}
        self.assertEqual(self._run(json.dumps(frame)), HDR_HAMILTON)
        self.assertEqual(self._run(json.dumps({"frames": [{}]})), (None, None))   # no SEI at all
        self.assertIsNone(self._run(json.dumps({"frames": []})))                   # unreadable
        self.assertIsNone(self._run("", rc=1))


class NativeRenderSdrChunkSpec(_DvRenderItem):
    """dvrender_spec for the SDR Topaz chunk routes (2026-10-18): movie_sdr_topaz, and
    sd_sdr_topaz when the chunk lane declined it. Topaz's chunks, the chunk front end (chunk 0's
    tags; untagged, the source's frame), the pinned LUT, Resolve's 'episode' export bitrate with no
    gate — and the chunks drop at hand-off as Resolve's did, so the movie disk model holds."""

    SUNNY = {"width": 2880, "height": 2160, "pix_fmt": "yuv422p10le", "r_frame_rate": "24000/1001",
             "sample_aspect_ratio": "1:1", "color_primaries": "smpte170m", "color_space": "smpte170m",
             "color_transfer": "smpte170m"}
    HD = {"width": 3840, "height": 2160, "pix_fmt": "yuv422p10le", "r_frame_rate": "24000/1001",
          "sample_aspect_ratio": "1:1", "color_primaries": "bt709", "color_space": "bt709",
          "color_transfer": "bt709"}

    def setUp(self):
        super().setUp()
        import nativelane, orchestrator
        self.movie = orchestrator.movie_paths("Film (1999).mkv", "/Media/Movies/Film", scratch_dir=self.d)
        self.ep = episode_paths("Show", "S02E06", "Show - 2x06.mkv", scratch_dir=self.d)
        for p in (self.movie, self.ep):
            for f in (p.source, p.source_cfr):
                with open(f, "wb") as fh:
                    fh.write(b"x" * 64)
        self.cfr = {"path": self.movie.source_cfr, "frames": 1000, "fps": "24000/1001"}
        self.chunk = dict(self.HD)
        self.source = {"width": 1920, "height": 1080}
        self.asked = []

        def probe(path, **kw):
            self.asked.append((os.path.basename(path), kw))
            return dict(self.source if path.endswith(("_cfr.mkv", "_cfr.mp4")) else self.chunk)
        self.chunks = [[0, os.path.join(self.d, "seg_0000.mov")],
                       [600, os.path.join(self.d, "seg_0001.mov")], [998, None]]
        for cm in (mock.patch.object(nativelane, "probe_stream", side_effect=probe),
                   mock.patch.object(nativelane, "chunks", side_effect=lambda *a, **k: [list(c) for c in self.chunks]),
                   mock.patch.object(stages, "_source_video_kbps", return_value=9000)):
            cm.start()
            self.addCleanup(cm.stop)

    def test_an_hd_movie_renders_its_chunks_through_the_lut_alone(self):
        spec, why = stages.dvrender_spec(self.movie, "movie_sdr_topaz")
        self.assertEqual(why, "")
        self.assertEqual(spec.inputs, tuple(tuple(c) for c in self.chunks))
        self.assertEqual((spec.single, spec.lut, spec.pre_vf, spec.bounds), (False, self.cube, None, None))
        self.assertEqual((spec.kbps, spec.cap_mbps), (stages.EXPORT_BITRATE_FLOOR_KBPS, 0))
        self.assertEqual((spec.master, spec.light, spec.keep_video), (stages_dvnative().DEFAULT_MASTER, None, False))
        self.assertEqual(self.asked[0], ("seg_0000.mov", {"extra": ("sample_aspect_ratio",)}))
        with mock.patch.object(stages, "_source_video_kbps", return_value=80000) as kb:
            self.assertEqual(stages.dvrender_spec(self.movie, "movie_sdr_topaz")[0].kbps, 80000)
        kb.assert_called_once_with(self.movie.source)              # the ORIGINAL, as _resolve reads

    def test_a_480p_dvd_film_gets_the_601_front_end(self):
        dvn = stages_dvnative()
        self.chunk, self.source = dict(self.SUNNY), {"width": 640, "height": 480}
        pillar = dvn.fit_pad_vf(2880, 2160, "1:1")              # 4:3 SD pillarboxed, as Resolve did
        spec, why = stages.dvrender_spec(self.movie, "movie_sdr_topaz")
        self.assertEqual(spec.pre_vf, dvn.colour_vf("170m", "170m") + "," + pillar)
        self.assertEqual(spec.encode_vf(),
                         dvn.colour_vf("170m", "170m") + "," + pillar + "," + dvn.lut_vf(self.cube))
        self.chunk = {k: v for k, v in self.SUNNY.items() if not k.startswith("color_")}   # untagged
        self.assertEqual(stages.dvrender_spec(self.movie, "movie_sdr_topaz")[0].pre_vf,
                         dvn.colour_vf("170m", "170m") + "," + pillar)   # judged by the source's frame
        self.chunk = dict(self.SUNNY, width=3240, sample_aspect_ratio="8:9")
        vf = stages.dvrender_spec(self.movie, "movie_sdr_topaz")[0].pre_vf
        self.assertTrue(vf.endswith("," + dvn.fit_pad_vf(3240, 2160, "8:9")), vf)   # squared, then the pillar
        self.assertIn(",setsar=1,", vf)

    def test_sd_tv_renders_only_what_the_chunk_lane_left_to_it(self):
        import nativelane
        self.chunk, self.source = dict(self.SUNNY), {"width": 640, "height": 480}
        spec, why = stages.dvrender_spec(self.ep, "sd_sdr_topaz")
        self.assertEqual(why, "")
        dvn = stages_dvnative()
        self.assertEqual(spec.pre_vf, dvn.colour_vf("170m", "170m") + "," + dvn.fit_pad_vf(2880, 2160, "1:1"))
        os.makedirs(nativelane.native_dir(self.ep), exist_ok=True)
        with open(nativelane.marker_path(self.ep), "w") as fh:
            fh.write("{}")
        for route in ("sd_sdr_topaz", "tv_sdr_topaz", "tv_disk_fallback"):     # the lane claimed it
            self.assertIn("claimed", stages.dvrender_spec(self.ep, route)[1], route)
        with open(nativelane.fallback_path(self.ep), "w") as fh:
            fh.write("{}")
        spec, why = stages.dvrender_spec(self.ep, "sd_sdr_topaz")     # ...and fell back: the render's
        self.assertEqual(why, "")
        self.assertIsNotNone(spec)

    def test_refusals_say_why(self):
        import nativelane

        def refused(**kw):
            spec, why = stages.dvrender_spec(self.movie, "movie_sdr_topaz")
            self.assertIsNone(spec)
            return why
        self.chunk = dict(self.HD, color_transfer="smpte2084")
        self.assertIn("Topaz's chunk 0: the source is already HDR", refused())
        self.chunk = dict(self.HD, r_frame_rate="24/1")
        self.assertIn("Topaz's chunk 0 runs at 24/1", refused())
        self.chunk = dict(self.HD)
        with mock.patch.object(nativelane, "chunks", side_effect=ValueError("chunk 1 is missing")):
            self.assertEqual(refused(), "the Topaz chunks: chunk 1 is missing")
        self.cfr = dict(self.cfr, fps=None)
        self.assertIn("frame count or frame rate is unknown", refused())
        self.cfr = dict(self.cfr, fps="24000/1001")
        self.toolchain = (False, "dovi_tool is 2.2.0")
        self.assertIn("toolchain is not ready", refused())

    def test_the_route_gets_its_chunk_spec(self):
        import routes
        with mock.patch.object(routes, "item_route", return_value="movie_sdr_topaz"):
            route, spec, why = stages.native_route(self.movie)
        self.assertEqual((route, why), ("movie_sdr_topaz", ""))
        self.assertIsNotNone(spec)


def stages_dvnative():
    import dvnative
    return dvnative


class NativeRenderCombineSpec(_DvRenderItem):
    """dvrender_spec for combine_nodv (2026-10-18): the verdict WINNER measured as rpu_only
    measures its source — no LUT, the winner's own mastering, its own coded video kept (HEVC
    Main10) or a VideoToolbox encode at the floor Resolve's combine export used. No CFR."""

    def setUp(self):
        super().setUp()
        import nativelane, orchestrator, topaz
        self.combine = orchestrator.movie_paths("Film (2020) REMUX.mkv", "/Media/Movies/Film",
                                                scratch_dir=self.d)
        self.combine.combine = True
        self.winner = os.path.join(self.d, "Film (2020) WEB.mkv")
        with open(self.winner, "wb") as fh:
            fh.write(b"w" * 64)
        self.info = dict(NativeRenderHdrSpec.PQ)
        self.frames, self.fps = 5000, "24000/1001"
        self.hdr10 = HDR_HAMILTON
        for cm in (mock.patch.object(orchestrator, "combine_winner_path", side_effect=lambda p: self.winner),
                   mock.patch.object(topaz, "_frame_count", side_effect=lambda path, **k: self.frames),
                   mock.patch.object(nativelane, "fps_fraction", side_effect=lambda path: self.fps),
                   mock.patch.object(stages, "source_hdr10", side_effect=lambda path: self.hdr10),
                   mock.patch.object(nativelane, "cfr_info", side_effect=AssertionError("a combine has no CFR"))):
            cm.start()
            self.addCleanup(cm.stop)

    def test_a_hevc_main10_winner_keeps_its_own_video(self):
        spec, why = stages.dvrender_spec(self.combine, "combine_nodv")
        self.assertEqual(why, "")
        self.assertEqual(spec.inputs, ((0, self.winner), (5000, None)))
        self.assertEqual((spec.keep_video, spec.lut, spec.pre_vf, spec.cap_mbps, spec.fps),
                         (True, None, None, 0, "24000/1001"))
        self.assertEqual(spec.kbps, stages.EXPORT_BITRATE_FLOOR_KBPS)
        self.assertEqual((spec.master.mdcv, spec.light), (HDR_HAMILTON[0], None))   # 0,0 -> measured
        self.assertFalse(os.path.exists(self.combine.source_cfr))           # never needed one

    def test_another_pq_codec_is_encoded_at_the_floor(self):
        self.info = dict(NativeRenderHdrSpec.PQ, codec_name="av1")
        self.hdr10 = HDR_P3_4000
        spec, why = stages.dvrender_spec(self.combine, "combine_nodv")
        self.assertEqual((spec.keep_video, spec.kbps, spec.light), (False, stages.EXPORT_BITRATE_FLOOR_KBPS, (850, 210)))
        self.assertEqual(spec.master.max_nits, 4000)

    def test_refusals_say_why(self):
        def refused():
            spec, why = stages.dvrender_spec(self.combine, "combine_nodv")
            self.assertIsNone(spec)
            return why
        self.info = dict(NativeRenderHdrSpec.PQ, color_transfer="bt709")
        self.assertIn("not PQ", refused())
        self.info = dict(NativeRenderHdrSpec.PQ, color_range="pc")
        self.assertIn("full range", refused())
        self.info = dict(NativeRenderHdrSpec.PQ, codec_name="av1", width=5120)
        self.assertIn("larger than 4096x2176", refused())
        self.info = dict(NativeRenderHdrSpec.PQ)
        self.frames = -1
        self.assertIn("frame count or frame rate is unknown", refused())
        self.frames = 5000
        self.hdr10 = None
        self.assertIn("HDR10 metadata could not be read", refused())
        self.hdr10 = HDR_HAMILTON
        self.winner = os.path.join(self.d, "gone.mkv")
        self.assertIn("winner is not on disk", refused())
        self.toolchain = (False, "dovi_tool is 2.2.0")
        self.assertIn("toolchain is not ready", refused())


class NativeRenderSdrOutSpec(_DvRenderItem):
    """dvrender_spec for sdr_out (2026-10-18): the item's unpinned route says what its picture is;
    it renders as that route's picture with NO LUT into the SDR render (sdr spec), at that route's
    export bitrate and gate. HDR and combines refuse."""

    def setUp(self):
        super().setUp()
        import orchestrator
        self.base = "yt_plain"
        self.asked = []
        self.movie = orchestrator.movie_paths("Film (1999).mkv", "/Media/Movies/Film", scratch_dir=self.d)
        for f in (self.movie.source, self.movie.source_cfr):
            with open(f, "wb") as fh:
                fh.write(b"x" * 64)
        import routes
        for cm in (mock.patch.object(routes, "item_route",
                                     side_effect=lambda p, mode=None: self.asked.append(mode) or self.base),
                   mock.patch.object(stages, "_source_video_kbps", return_value=9000)):
            cm.start()
            self.addCleanup(cm.stop)

    def test_a_youtube_video_takes_the_fit_front_end_and_its_gate(self):
        import dvnative
        self.info = dict(self.INFO, width=640, height=480, color_space="smpte170m",
                         color_primaries="smpte170m", color_transfer="smpte170m")
        spec, why = stages.dvrender_spec(self.p, "sdr_out")
        self.assertEqual(why, "")
        self.assertEqual(self.asked, ["dv1000"])                        # named as if unpinned
        self.assertEqual((spec.sdr, spec.lut, spec.keep_video), (True, None, False))
        self.assertEqual(spec.pre_vf, dvnative.fit_front_end(self.info, stages.DVRENDER_SIZE))
        self.assertEqual((spec.kbps, spec.cap_mbps), (stages.youtube_render_kbps(480), 50))
        self.assertTrue(spec.encode_vf().endswith(",format=yuv420p10le"))
        self.assertNotIn("lut3d", spec.encode_vf())

    def test_4k_sdr_keeps_its_frame_through_the_colour_step(self):
        self.base = "sdr4k"
        spec, why = stages.dvrender_spec(self.movie, "sdr_out")
        self.assertEqual((spec.pre_vf, spec.kbps, spec.cap_mbps, spec.sdr),
                         (None, stages.EXPORT_BITRATE_FLOOR_KBPS, 0, True))   # BT.709 already
        self.info = dict(self.INFO, color_range="pc")
        self.assertIn("rangein=full", stages.dvrender_spec(self.movie, "sdr_out")[0].pre_vf)

    def test_a_topaz_route_renders_its_chunks_without_the_lut(self):
        import nativelane
        self.base = "movie_sdr_topaz"
        chunks = [[0, os.path.join(self.d, "seg_0000.mov")], [500, os.path.join(self.d, "seg_0001.mov")],
                  [998, None]]
        self.cfr = dict(self.cfr, fps="30000/1001")
        with mock.patch.object(nativelane, "chunks", return_value=[list(c) for c in chunks]):
            spec, why = stages.dvrender_spec(self.movie, "sdr_out")
        self.assertEqual(why, "")
        self.assertEqual((spec.sdr, spec.lut, spec.single, spec.cap_mbps), (True, None, False, 0))
        self.assertEqual(spec.inputs, tuple(tuple(c) for c in chunks))

    def test_hdr_and_combines_refuse(self):
        for base, words in (("rpu_only", "tone map"), ("hdr_topaz", "tone map"),
                            ("combine_nodv", "combine ships Dolby Vision"), ("already_dv", "no SDR render"),
                            (None, "route unknown")):
            with self.subTest(base=base):
                self.base = base
                spec, why = stages.dvrender_spec(self.movie, "sdr_out")
                self.assertIsNone(spec)
                self.assertIn(words, why)


class SdrOutRemux(_DvRenderItem):
    """stages._remux: an SDR render the native render vouches for (nativerender.sdr_render_ok)
    becomes the SDR master (sdr=True on the remux call) while the item still ships as SDR
    (orchestrator.ships_as_sdr); without that proof every call is the DV one, with no `sdr`
    keyword at all; and a proofed SDR render of an item that now ships as DV is refused."""

    def _remux(self, proof, pinned=True):
        import types, remux, nativerender, settings
        got, results = {}, {}

        def fake(name):
            def f(*a, **kw):
                got[name] = kw
                return types.SimpleNamespace(ok=True, reason="shipped")
            return f
        import orchestrator
        for key in ("Chan", "Film (1999)"):  # pinned to SDR: the paths are built under SDR_TAG;
            settings.set_show_output_mode(key, "sdr" if pinned else "auto")      # else DV_TAG
        orchestrator.relabel_container(self.p, ".mp4")           # what a resume does
        movie = orchestrator.movie_paths("Film (1999).mkv", "/Media/Movies/Film", scratch_dir=self.d)
        for q in (self.p, movie):
            self.assertEqual(orchestrator.ships_as_sdr(q), pinned, q.final)
        for f in (movie.source, movie.source_cfr):
            with open(f, "wb") as fh:
                fh.write(b"x" * 64)
        for q in (self.p, movie):            # the proof file the remux looks for before anything
            os.makedirs(q.dv_render + ".dvrender", exist_ok=True)
            with open(q.dv_render + ".dvrender/sdr.json", "w") as fh:
                fh.write("{}")
        with mock.patch.object(nativerender, "sdr_render_ok", return_value=proof), \
             mock.patch.object(remux, "remux", side_effect=fake("remux")), \
             mock.patch.object(remux, "remux_ship_render", side_effect=fake("ship")), \
             mock.patch("plan.plan_for", return_value={"topaz": "upscale"}), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": 0}):
            results["youtube"] = stages.run_stage("remux", self.p, progress=lambda d: None)
            results["movie"] = stages.run_stage("remux", movie, progress=lambda d: None)
        self.results = results
        return got

    def test_the_proof_makes_the_sdr_master(self):
        got = self._remux(True)
        self.assertEqual(got["ship"].get("sdr"), True)
        self.assertEqual(got["remux"].get("sdr"), True)

    def test_without_it_nothing_changes(self):
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                got = self._remux(False, pinned)
                self.assertNotIn("sdr", got["ship"])
                self.assertNotIn("sdr", got["remux"])

    def test_a_proofed_sdr_render_of_an_item_that_now_ships_as_dv_is_refused(self):
        # 2026-10-18 review: pinned SDR, rendered SDR, then unpinned and resumed under the DV name
        # — the finisher's remux never asks stage_done("resolve"), so this is the last gate before
        # an SDR master named '... HDR10 DV upscaled' would have been made and uploaded
        got = self._remux(True, pinned=False)
        self.assertEqual(got, {})                                   # no remux call of any kind
        for kind in ("youtube", "movie"):
            ok, msg = self.results[kind]
            self.assertFalse(ok, kind)
            self.assertIn("now ships as Dolby Vision", msg)

    def test_no_proof_file_never_imports_or_asks(self):
        import types, remux, nativerender, settings
        got = {}
        with mock.patch.object(nativerender, "sdr_render_ok", side_effect=AssertionError("asked")), \
             mock.patch.object(remux, "remux_ship_render",
                               side_effect=lambda *a, **kw: got.update(kw) or types.SimpleNamespace(ok=True, reason="ok")), \
             mock.patch("plan.plan_for", return_value={"topaz": "upscale"}), \
             mock.patch.object(settings, "get_settings",
                               return_value={"max_peak_mbps": 50, "audio_target_lufs": 0}):
            self.assertEqual(stages.run_stage("remux", self.p, progress=lambda d: None), (True, "ok"))
        self.assertNotIn("sdr", got)



class SupersedeLegacyMasters(unittest.TestCase):
    """A Matroska re-run lands BESIDE an earlier .mp4 master (upload only replaces the same name);
    once the new master verifies, the old one goes, so Plex never shows two versions."""

    def test_tv_candidates_are_both_spellings_of_the_old_master(self):
        p = episode_paths("Show", "S01E01", "Show.S01E01.1080p.BluRay.x264-BiA.mkv",
                          scratch_dir="/tmp", nas_tv_root="/Vol/TV")
        c = stages.legacy_master_candidates(p, p.nas_final)
        self.assertEqual(c, ["/Vol/TV/Show/S01/Show.S01E01.2160p.BluRay.x265-BiA HDR10 DV upscaled.mp4",
                             "/Vol/TV/Show/S01/Show.S01E01.1080p.BluRay.x264-BiA HDR10 DV upscaled.mp4"])

    def test_never_the_source_never_a_non_master_never_after_an_mp4_upload(self):
        p = episode_paths("Show", "S01E01", "Ep.mkv", scratch_dir="/tmp", nas_tv_root="/Vol/TV")
        self.assertEqual(stages.legacy_master_candidates(p, p.nas_final[:-4] + ".mp4"), [])
        for c in stages.legacy_master_candidates(p, p.nas_final):
            self.assertNotEqual(c, p.nas_source)
            self.assertIn("HDR10 DV upscaled", c)

    def test_youtube_takes_the_old_mp4_in_the_plex_folder_only(self):
        from orchestrator import youtube_paths
        p = youtube_paths("Chan", "/Media/YouTube-raw/Chan/v/v [abcdefghijk].mp4", scratch_dir="/tmp")
        with mock.patch.object(stages.transfer, "NAS_FTP_YOUTUBE_ROOT", "/Media/YouTube"), \
             mock.patch.object(stages.transfer, "NAS_FTP_YOUTUBE_STAGING", "/Media/YouTube-raw"):
            self.assertEqual(stages.legacy_master_candidates(p, p.nas_final),
                             [p.nas_final[:-4] + ".mp4"])
        with mock.patch.object(stages.transfer, "NAS_FTP_YOUTUBE_ROOT", "/Media/YT/"), \
             mock.patch.object(stages.transfer, "NAS_FTP_YOUTUBE_STAGING", "/Media/YT"):
            # one folder for both: <stem>.mp4 could be the SOURCE itself
            self.assertEqual(stages.legacy_master_candidates(p, p.nas_final), [])

    def test_an_upload_supersedes_and_forgets_the_old_row(self):
        import settings, history
        p = _paths(tempfile.mkdtemp())
        old = p.nas_final[:-4] + ".mp4"
        with mock.patch.object(stages.transfer, "upload",
                               return_value=(True, p.nas_final, "uploaded 1 bytes")), \
             mock.patch.object(stages.transfer, "supersede_masters",
                               return_value=([old], "superseded 1 older master(s)")) as sup, \
             mock.patch.object(history, "forget") as forget, \
             mock.patch.object(settings, "get_show_replace_source", return_value=False):
            ok, msg = stages.run_stage("upload", p)
        self.assertTrue(ok)
        self.assertEqual(sup.call_args.args[:2], (p.nas_final, p.final))
        self.assertIn(old, sup.call_args.args[2])
        forget.assert_called_once_with(old)
        self.assertIn("superseded 1 older master", msg)

    def test_nothing_to_supersede_touches_nothing(self):
        import settings
        p = _paths(tempfile.mkdtemp())
        with mock.patch.object(stages.transfer, "upload",
                               return_value=(True, p.nas_final[:-4] + ".mp4", "uploaded")), \
             mock.patch.object(stages.transfer, "supersede_masters",
                               side_effect=AssertionError("no candidates, no FTP")), \
             mock.patch.object(settings, "get_show_replace_source", return_value=False):
            ok, _msg = stages.run_stage("upload", p)
        self.assertTrue(ok)
