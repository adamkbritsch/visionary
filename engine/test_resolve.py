import json
import unittest
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
