import json
import os
import unittest
from unittest import mock
import resolve
from resolve import render_preset, is_hdr10


class InheritColor(unittest.TestCase):
    """Color management is INHERITED — resolve.py must only READ it, never set it."""
    class _Proj:
        def __init__(self, out): self.out = out; self.sets = []
        def GetSetting(self, k): return self.out if k == "colorSpaceOutput" else "x"
        def SetSetting(self, k, v): self.sets.append((k, v)); return True

    def test_no_color_setters_exist(self):
        # the old SDR-bug-prone setters must be gone
        self.assertFalse(hasattr(resolve, "apply_color_management"))
        self.assertFalse(hasattr(resolve, "color_management"))

    def test_inspect_is_read_only(self):
        p = self._Proj("Rec.2100 ST2084")
        d = resolve.inspect_color_management(p)
        self.assertIn("colorSpaceOutput", d)
        self.assertEqual(p.sets, [])                 # never wrote a setting

    def test_is_hdr_project_guards_sdr(self):
        self.assertTrue(resolve.is_hdr_project(self._Proj("Rec.2100 ST2084")))
        self.assertFalse(resolve.is_hdr_project(self._Proj("Rec.709 Gamma 2.4")))


class RenderPreset(unittest.TestCase):
    def test_delivery_matches_screenshots(self):
        rp = render_preset()
        self.assertEqual(rp["format"], "mov")
        self.assertEqual(rp["codec"], "H265")
        self.assertEqual(rp["encoding_profile"], "Main10")
        self.assertEqual(rp["dolby_vision_profile"], "8.1")
        self.assertEqual(rp["render_mode"], "SingleClip")

    def test_audio_is_off_mute_render(self):
        self.assertFalse(render_preset()["export_audio"])


class HdrOutput(unittest.TestCase):
    SAMPLE = json.dumps({"streams": [{"codec_type": "video", "codec_name": "hevc",
             "profile": "Main 10", "width": 3840, "height": 2160,
             "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc"}]})

    def test_is_hdr10_false_for_rec709(self):
        self.assertFalse(is_hdr10({"profile": "Main 10", "width": 3840, "height": 2160,
                                   "transfer": "bt709", "primaries": "bt709"}))


if __name__ == "__main__":
    unittest.main()


class SuperScaleThreading(unittest.TestCase):
    """REGRESSION (live-caught 2026-08-06): the SuperScale block sat in setup_single,
    whose scope had no `superscale` — NameError killed the movie's whole Resolve pass.
    Pin the parameter chain: single() must PASS it into setup_single()."""

    def test_setup_single_accepts_superscale(self):
        import inspect, resolve_pipeline
        self.assertIn("superscale", inspect.signature(resolve_pipeline.setup_single).parameters)
        self.assertIn("superscale", inspect.signature(resolve_pipeline.single).parameters)

    def test_single_threads_it_through(self):
        import inspect, resolve_pipeline
        src = inspect.getsource(resolve_pipeline.single)
        self.assertIn("setup_single(video, mode, superscale", src)


class RefocusAfterPlacement(unittest.TestCase):
    def test_place_now_hands_the_main_display_back(self):
        # After Resolve moves to the PINNED display, the main display refocuses on the
        # app (user-dictated 2026-08-06) — and only in the pinned case: with Resolve on
        # the main display, raising Visionary would cover the automation's own window.
        import inspect, resolve_pipeline
        src = inspect.getsource(resolve_pipeline._place_now)
        self.assertIn('tell application "Visionary" to activate', src)
        self.assertIn("if not host:", src)          # the unpinned early-return guards it


class _FakeClip:
    def __init__(self, path): self._p = path
    def GetClipProperty(self, k): return self._p


class _FakeTL:
    def __init__(self, frames, items): self._f, self._i = frames, items
    def GetEndFrame(self): return self._f       # 1-based span: end - start + 1 == frames
    def GetStartFrame(self): return 1
    def GetItemListInTrack(self, kind, n): return ["shot"] * self._i


class _FakeProj:
    def __init__(self, clips, timelines, tl):
        self._c, self._n, self._tl = clips, timelines, tl
        self.current = None
    def GetMediaPool(self):
        proj = self
        class MP:
            def GetRootFolder(self):
                class RF:
                    def GetClipList(self_rf): return proj._c
                return RF()
        return MP()
    def GetTimelineCount(self): return self._n
    def GetTimelineByIndex(self, i): return self._tl
    def SetCurrentTimeline(self, tl): self.current = tl


class ResumeValidation(unittest.TestCase):
    """A killed pass must not lose finished sub-steps (scene cuts, completed analysis) —
    but resume ONLY when every check proves the persistent project holds OUR state for
    THIS exact input. Anything off → the fresh path."""

    def setUp(self):
        import tempfile, resolve_pipeline as rp
        d = tempfile.mkdtemp()
        patcher = mock.patch.object(rp, "_RESUME_FILE", os.path.join(d, "r.json"))
        patcher.start(); self.addCleanup(patcher.stop)

    def _proj(self, *, clips=None, timelines=1, frames=1000, items=12):
        clips = clips if clips is not None else [_FakeClip("/scratch/movie.mkv")]
        return _FakeProj(clips, timelines, _FakeTL(frames, items))

    MARKER = {"ident": "x", "frames": 1000, "scenes": True}

    def test_valid_state_resumes(self):
        import resolve_pipeline as rp
        self.assertTrue(rp._validate_resume_single(self._proj(), "/scratch/movie.mkv", self.MARKER))

    def test_every_mismatch_falls_back_to_fresh(self):
        import resolve_pipeline as rp
        good = "/scratch/movie.mkv"
        self.assertFalse(rp._validate_resume_single(self._proj(), good, None))
        self.assertFalse(rp._validate_resume_single(self._proj(), good,
                                                    {**self.MARKER, "scenes": False}))
        self.assertFalse(rp._validate_resume_single(
            self._proj(clips=[_FakeClip("/scratch/OTHER.mkv")]), good, self.MARKER))
        self.assertFalse(rp._validate_resume_single(
            self._proj(clips=[_FakeClip(good), _FakeClip(good)]), good, self.MARKER))
        self.assertFalse(rp._validate_resume_single(self._proj(timelines=0), good, self.MARKER))
        self.assertFalse(rp._validate_resume_single(self._proj(frames=999), good, self.MARKER))
        self.assertFalse(rp._validate_resume_single(self._proj(items=1), good, self.MARKER))

    def test_episode_variant_checks_chunk_counts(self):
        import resolve_pipeline as rp
        marker = {"ident": "x", "frames": 1000, "scenes": True, "clips": 3}
        clips3 = [_FakeClip(f"/s/{i}.mov") for i in range(3)]
        self.assertTrue(rp._validate_resume_episode(
            _FakeProj(clips3, 1, _FakeTL(1000, 9)), marker))
        self.assertFalse(rp._validate_resume_episode(          # a chunk went missing
            _FakeProj(clips3[:2], 1, _FakeTL(1000, 9)), marker))
        self.assertFalse(rp._validate_resume_episode(          # items below chunk count
            _FakeProj(clips3, 1, _FakeTL(1000, 2)), marker))

    def test_book_roundtrip_reset_and_analyzed(self):
        import resolve_pipeline as rp
        rp._resume_put("/x/movie.mkv", "dv2000", ident="a", frames=10, scenes=True, analyzed=False)
        m = rp._resume_get("/x/movie.mkv", "dv2000")
        self.assertEqual((m["ident"], m["frames"], m["analyzed"]), ("a", 10, False))
        rp._mark_analyzed("/x/movie.mkv", "dv2000")
        self.assertTrue(rp._resume_get("/x/movie.mkv", "dv2000")["analyzed"])
        self.assertIsNone(rp._resume_get("/x/movie.mkv", "dv1000"))     # mode-scoped
        rp._resume_reset("/x/movie.mkv", "dv2000")
        self.assertIsNone(rp._resume_get("/x/movie.mkv", "dv2000"))

    def test_fresh_setup_resets_before_clearing(self):
        # Source-pin: the fresh path must reset the marker BEFORE _clear_project in BOTH
        # setups — a stale "analyzed" surviving into a cleared project would render an
        # unanalysed master, the exact regression the blind-poll guard exists to prevent.
        import inspect, resolve_pipeline as rp
        for fn in (rp.setup_single, rp.setup):
            src = inspect.getsource(fn)
            self.assertLess(src.index("_resume_reset"), src.index("_clear_project(proj)"), fn.__name__)
        self.assertIn("_mark_analyzed", inspect.getsource(rp.single))
        self.assertIn("_mark_analyzed", inspect.getsource(rp.episode))


class TheTimelineMustBeThisSourcesLength(unittest.TestCase):
    """Don't Look Up rendered 206,200 frames for a 199,144-frame movie, five times over.
    Nothing between the import and the finished 56 GB file ever compared the timeline to the
    SOURCE: setup_single printed its frame count without checking it, and the resume
    validator compared the timeline to its own marker — a marker written from a wrong
    timeline blesses that same wrong timeline forever (live-caught 2026-08-21)."""

    def setUp(self):
        import tempfile, resolve_pipeline as rp
        d = tempfile.mkdtemp()
        p = mock.patch.object(rp, "_RESUME_FILE", os.path.join(d, "r.json"))
        p.start(); self.addCleanup(p.stop)

    def _proj(self, frames=1000):
        return _FakeProj([_FakeClip("/scratch/movie.mkv")], 1, _FakeTL(frames, 12))

    MARKER = {"ident": "x", "frames": 1000, "scenes": True}

    def _resume(self, *, timeline, source):
        import resolve_pipeline as rp
        with mock.patch.object(rp, "_probe_frames", return_value=source):
            return rp._validate_resume_single(self._proj(timeline),
                                              "/scratch/movie.mkv",
                                              {**self.MARKER, "frames": timeline})

    def test_a_timeline_longer_than_the_source_is_not_resumed(self):
        self.assertFalse(self._resume(timeline=206200, source=199144))

    def test_a_timeline_shorter_than_the_source_is_not_resumed(self):
        self.assertFalse(self._resume(timeline=120000, source=199144))

    def test_a_matching_timeline_resumes(self):
        self.assertTrue(self._resume(timeline=199144, source=199144))

    def test_an_edge_frame_of_rounding_is_tolerated(self):
        self.assertTrue(self._resume(timeline=199145, source=199144))

    def test_an_unreadable_source_never_breaks_a_good_resume(self):
        self.assertTrue(self._resume(timeline=199144, source=None))

    def test_setup_single_checks_the_built_timeline_against_the_source(self):
        # Source-pin, like the reset-before-clear one above: this check is the difference
        # between failing in seconds at setup and failing after a 2.5-hour render.
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.setup_single)
        self.assertIn("TIMELINE LENGTH MISMATCH", src)
        self.assertIn("_probe_frames(video)", src)


class ProbeFrames(unittest.TestCase):
    def _probe(self, st):
        import json as _json, resolve_pipeline as rp
        with mock.patch.object(rp.subprocess, "run",
                               return_value=mock.Mock(stdout=_json.dumps({"streams": [st]}))):
            return rp._probe_frames("/x.mp4")

    def test_a_published_frame_count_wins(self):
        self.assertEqual(self._probe({"nb_frames": "199144", "duration": "1.0",
                                      "r_frame_rate": "24/1"}), 199144)

    def test_duration_times_rate_when_the_container_publishes_no_count(self):
        self.assertEqual(self._probe({"nb_frames": "N/A", "duration": "8297.667",
                                      "r_frame_rate": "24/1"}), 199144)

    def test_neither_readable_is_None_not_a_guess(self):
        self.assertIsNone(self._probe({"nb_frames": "N/A", "duration": "N/A",
                                       "r_frame_rate": "0/0"}))

    def test_a_probe_that_blows_up_is_None(self):
        import resolve_pipeline as rp
        with mock.patch.object(rp.subprocess, "run", side_effect=OSError):
            self.assertIsNone(rp._probe_frames("/x.mp4"))


class MatroskaMustNotBlindTheGuard(unittest.TestCase):
    """The timeline-length guard probes the file Resolve imports. Matroska publishes neither
    nb_frames nor a video duration, so both branches came back empty and the guard silently
    passed on every .mkv — including the one it exists for. The fallback is the last video
    PACKET, never the container: on that file the container is the very thing that lies
    (294s of audio past the last frame)."""

    def _probe(self, st, *, box=None, last=None, step=None):
        import json as _json, resolve_pipeline as rp

        def run(cmd, **kw):
            if "packet=pts_time,duration_time" in cmd:
                return mock.Mock(stdout="" if last is None else f"{last},{step}\n")
            if "format=duration" in cmd:
                return mock.Mock(stdout=("" if box is None else f"{box}\n"))
            return mock.Mock(stdout=_json.dumps({"streams": [st]}))
        with mock.patch.object(rp.subprocess, "run", side_effect=run):
            return rp._probe_frames("/x.mkv")

    MKV = {"nb_frames": "N/A", "duration": "N/A", "r_frame_rate": "24/1"}

    def test_the_picture_end_gives_the_frame_count(self):
        # last frame starts at 8297.625 and lasts 1/24 -> 8297.667 * 24 = 199144
        self.assertEqual(self._probe(self.MKV, box=8591.648, last=8297.625,
                                     step=0.0416667), 199144)

    def test_the_lying_container_is_NOT_used(self):
        # 8591.648 * 24 = 206200 — the wrong answer that has to stay wrong
        self.assertNotEqual(self._probe(self.MKV, box=8591.648, last=8297.625,
                                        step=0.0416667), 206200)

    def test_no_packets_at_all_is_None_not_a_guess(self):
        self.assertIsNone(self._probe(self.MKV, box=8591.648, last=None))

    def test_a_published_count_still_short_circuits(self):
        st = {"nb_frames": "199144", "duration": "N/A", "r_frame_rate": "24/1"}
        self.assertEqual(self._probe(st, box=8591.648, last=1.0, step=0.04), 199144)


class TheRenderRateFollowsTheTimeline(unittest.TestCase):
    """FrameRate was passed to SetRenderSettings as a STRING and the render came back at the
    preset's rate instead of the timeline's. Invisible for years — every episode and the
    preset are both 23.976 — and only exposed by a true-24 movie (2026-08-21)."""

    def test_the_rate_is_sent_as_a_number(self):
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.render)
        self.assertIn('"FrameRate": (_fps_from_rate(fps) or str(fps))', src)
        self.assertNotIn('"FrameRate": str(fps)', src)

    def test_both_rate_spellings_convert(self):
        import resolve_pipeline as rp
        self.assertEqual(rp._fps_from_rate("24"), 24.0)
        self.assertAlmostEqual(rp._fps_from_rate("23.976"), 23.976)
        self.assertAlmostEqual(rp._fps_from_rate("24000/1001"), 23.976, places=3)

    def test_an_unreadable_rate_degrades_to_todays_behaviour(self):
        import resolve_pipeline as rp
        self.assertIsNone(rp._fps_from_rate("weird"))     # -> falls back to str(fps)


class TheQueuedJobIsCheckedBeforeRendering(unittest.TestCase):
    """The rate we ask SetRenderSettings for and the rate the queued job actually carries
    were never compared. When they disagreed the only symptom was a finished render at the
    wrong rate 2.5 hours later — unusable, because an RPU aligns frame-by-frame."""

    def test_a_pinned_rate_fails_before_StartRendering(self):
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.render)
        # the readback must sit BETWEEN queuing the job and starting it
        self.assertLess(src.index("GetRenderJobList"), src.index("StartRendering"))
        self.assertIn("RENDER RATE PINNED", src)

    def test_the_message_names_the_one_time_fix(self):
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.render)
        self.assertIn("Save As New Render Preset", src)   # actionable, not just "mismatch"

    def test_an_unreadable_job_list_does_not_block_the_render(self):
        # Older Resolve builds may not expose FrameRate on the job — that must degrade to
        # today's behaviour (render, then let the post-render check judge), never to a stall.
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.render)
        self.assertIn("except Exception:\n        got_rate = None", src)
        self.assertIn("if want_rate and got_rate", src)


class YouTubeCleanupGrade(unittest.TestCase):
    """YouTube is the one path with nothing between the source and the deliverable — it skips
    Topaz by design, so the source's VP9/AV1 banding and block noise scale up 2x and freeze
    into the master. A grade saved once in Resolve (Deband + a small spatial NR) is applied to
    every shot. Inherited exactly like DV_PRESET: the pipeline never authors it, and its
    absence is the long-standing ungraded behaviour rather than a failure."""

    def _tl(self, ok=True, boom=False):
        calls = []

        class TL:
            def ApplyGradeFromDRX(self, path, mode, items):
                if boom:
                    raise RuntimeError("resolve said no")
                calls.append((path, mode, list(items)))
                return ok
        return TL(), calls

    def test_it_is_applied_to_every_shot_with_no_keyframes(self):
        import resolve_pipeline as rp
        tl, calls = self._tl()
        shots = ["a", "b", "c"]
        with mock.patch.object(rp.os.path, "exists", return_value=True):
            self.assertTrue(rp.apply_cleanup_grade(tl, shots, True))
        self.assertEqual(len(calls), 1)
        path, mode, items = calls[0]
        self.assertEqual(path, rp.CLEANUP_DRX)
        self.assertEqual(mode, 0)                 # "No keyframes" — a static correction
        self.assertEqual(items, shots)            # every shot, not just the first

    def test_a_non_youtube_item_is_never_graded(self):
        import resolve_pipeline as rp
        tl, calls = self._tl()
        with mock.patch.object(rp.os.path, "exists", return_value=True):
            self.assertFalse(rp.apply_cleanup_grade(tl, ["a"], False))
        self.assertEqual(calls, [])

    def test_no_saved_grade_means_no_call_and_no_failure(self):
        import resolve_pipeline as rp
        tl, calls = self._tl()
        with mock.patch.object(rp.os.path, "exists", return_value=False):
            self.assertFalse(rp.apply_cleanup_grade(tl, ["a"], True))
        self.assertEqual(calls, [])

    def test_resolve_refusing_it_never_raises(self):
        import resolve_pipeline as rp
        tl, _ = self._tl(boom=True)
        with mock.patch.object(rp.os.path, "exists", return_value=True):
            self.assertFalse(rp.apply_cleanup_grade(tl, ["a"], True))   # swallowed, not raised

    def test_an_empty_shot_list_is_a_no_op(self):
        import resolve_pipeline as rp
        tl, calls = self._tl()
        with mock.patch.object(rp.os.path, "exists", return_value=True):
            self.assertFalse(rp.apply_cleanup_grade(tl, [], True))
        self.assertEqual(calls, [])

    def test_it_runs_AFTER_scene_detection(self):
        # The cuts turn one clip into one item per shot; grading before them lands on an item
        # that no longer exists.
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp.setup_single)
        self.assertLess(src.index("DetectSceneCuts"), src.index("apply_cleanup_grade"))

    def test_the_flag_rides_the_argv_tail(self):
        # APPENDED, never inserted — an older resolve_pipeline.py must just ignore it.
        import inspect, resolve_pipeline as rp
        src = inspect.getsource(rp)
        self.assertIn('_cl = a[6] if len(a) > 6 else "-"', src)
        self.assertIn('_ss = a[5] if len(a) > 5 else "-"', src)   # superscale kept its index
