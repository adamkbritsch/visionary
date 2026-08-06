import json
import os
import unittest
from unittest import mock
import resolve
from resolve import render_preset, hdr_summary, is_hdr10


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

    def test_summary_picks_video(self):
        s = hdr_summary(self.SAMPLE)
        self.assertEqual(s["profile"], "Main 10")
        self.assertEqual(s["transfer"], "smpte2084")

    def test_is_hdr10_true_for_4k_st2084(self):
        self.assertTrue(is_hdr10(hdr_summary(self.SAMPLE)))

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
        self.assertIn("setup_single(video, mode, superscale)", src)


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
