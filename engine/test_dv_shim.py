import os
import tempfile
import unittest
from unittest import mock

import dv_shim

cv2 = dv_shim.cv2


@unittest.skipIf(cv2 is None, "cv2 not installed")
class FindButton(unittest.TestCase):
    """The locator is what makes the shim robust to window size/position, so it
    is the part worth testing deterministically (no Resolve/permissions needed)."""

    def _fixture(self, d):
        import numpy as np
        canvas = np.full((300, 400, 3), 20, np.uint8)
        btn = np.full((20, 48, 3), (60, 170, 90), np.uint8)
        cv2.putText(btn, "All", (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        canvas[150:170, 100:148] = btn          # button placed at known location
        sp = os.path.join(d, "shot.png"); tp = os.path.join(d, "tmpl.png")
        cv2.imwrite(sp, canvas); cv2.imwrite(tp, btn)
        return sp, tp

    def test_locates_center_in_logical_points(self):
        with tempfile.TemporaryDirectory() as d:
            sp, tp = self._fixture(d)
            xy = dv_shim.find_button(sp, tp, threshold=0.9, scale=1.0)
            self.assertIsNotNone(xy)
            self.assertAlmostEqual(xy[0], 124, delta=2)   # 100 + 48/2
            self.assertAlmostEqual(xy[1], 160, delta=2)   # 150 + 20/2

    def test_halves_coordinates_for_retina(self):
        with tempfile.TemporaryDirectory() as d:
            sp, tp = self._fixture(d)
            xy = dv_shim.find_button(sp, tp, threshold=0.9, scale=2.0)
            self.assertAlmostEqual(xy[0], 62, delta=1)    # 124 / 2 (Retina)

    def test_none_when_button_absent(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            sp, _ = self._fixture(d)
            other = os.path.join(d, "other.png")
            # textured pattern that genuinely does not appear in the canvas
            rng = np.random.default_rng(0)
            cv2.imwrite(other, rng.integers(0, 256, (20, 48, 3), dtype=np.uint8))
            self.assertIsNone(dv_shim.find_button(sp, other, threshold=0.9, scale=1.0))


class DisplayGeometry(unittest.TestCase):
    """retina_scale() is READ from the main display (every supported config is 2.0), so a
    second supported display (the clamshell dummy) needs no recalibration."""

    def test_scale_derived_from_geometry(self):
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=(3840, 2160, 2.0, False)):
            self.assertEqual(dv_shim.retina_scale(), 2.0)
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=(3456, 2234, 2.0, True)):
            self.assertEqual(dv_shim.retina_scale(), 2.0)

    def test_scale_falls_back_when_unreadable(self):
        with mock.patch.object(dv_shim, "main_display_geometry", return_value=None):
            self.assertEqual(dv_shim.retina_scale(), 2.0)


class FullScreen(unittest.TestCase):
    """enter_fullscreen must target the window that OWNS AXFullScreen and VERIFY it.
    'window 1' can be the Project Manager dialog, where the attribute is settable:false
    and the set is silently dropped (osascript still exits 0) — that left Resolve
    windowed, and a windowed layout is where the DV palette click does not register
    (live-caught lid-closed, 2026-07-17)."""

    def test_returns_true_when_already_fullscreen(self):
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(1, True, "Main")):
            self.assertTrue(dv_shim.enter_fullscreen(settle=0))

    def test_sets_then_verifies_and_retries(self):
        # windowed -> set -> verified fullscreen on the readback
        states = [(2, False, "Main"), (2, True, "Main")]
        with mock.patch.object(dv_shim, "fullscreen_state", side_effect=states), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "", "")) as osa, \
             mock.patch.object(dv_shim, "activate"):
            self.assertTrue(dv_shim.enter_fullscreen(settle=0))
        # it must address the window index that owns the attribute — NOT a hardcoded 1
        self.assertIn("window 2", osa.call_args[0][0])

    def test_raises_when_no_window_accepts_fullscreen(self):
        # e.g. only the Project Manager dialog is open
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(None, None, None)), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "Project Manager", "")), \
             mock.patch.object(dv_shim, "activate"), \
             mock.patch.object(dv_shim, "_diag") as dg:
            with self.assertRaises(RuntimeError) as e:
                dv_shim.enter_fullscreen(attempts=2, settle=0)
        self.assertIn("Project Manager", str(e.exception))
        dg.assert_called_once()

    def test_raises_when_set_never_takes(self):
        with mock.patch.object(dv_shim, "fullscreen_state", return_value=(1, False, "Main")), \
             mock.patch.object(dv_shim, "_osa", return_value=(0, "", "")), \
             mock.patch.object(dv_shim, "activate"), \
             mock.patch.object(dv_shim, "_diag") as dg:
            with self.assertRaises(RuntimeError):
                dv_shim.enter_fullscreen(attempts=2, settle=0)
        dg.assert_called_once()

    def test_fullscreen_state_parses_the_owning_window(self):
        with mock.patch.object(dv_shim, "_osa", return_value=(0, "3|true|Overnight Upscaler SDR", "")):
            self.assertEqual(dv_shim.fullscreen_state(), (3, True, "Overnight Upscaler SDR"))
        with mock.patch.object(dv_shim, "_osa", return_value=(0, "none", "")):
            self.assertEqual(dv_shim.fullscreen_state(), (None, None, None))


class Forensics(unittest.TestCase):
    """With the lid closed nobody can see the screen, so a failed match must leave
    evidence: the screenshot + a sidecar JSON, ring-buffered."""

    def test_diag_writes_screenshot_and_json(self):
        import json, tempfile, glob
        with tempfile.TemporaryDirectory() as d:
            shot = os.path.join(d, "shot.png")
            open(shot, "wb").write(b"notarealpng")
            with mock.patch.object(dv_shim, "DIAG_DIR", os.path.join(d, "diag")), \
                 mock.patch.object(dv_shim, "main_display_geometry", return_value=(3840, 2160, 2.0, False)), \
                 mock.patch.object(dv_shim, "screen_locked", return_value=False):
                out = dv_shim._diag("miss-analyze_all", shot, template="analyze_all.png", score=0.42)
            self.assertTrue(out.endswith(".png") and os.path.exists(out))
            js = glob.glob(os.path.join(d, "diag", "*.json"))[0]
            rec = json.load(open(js))
            self.assertEqual(rec["what"], "miss-analyze_all")
            self.assertEqual(rec["score"], 0.42)
            self.assertEqual(rec["display"], [3840, 2160, 2.0, False])

    def test_diag_ring_buffer_caps_growth(self):
        import tempfile, glob
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(dv_shim, "DIAG_DIR", d), \
                 mock.patch.object(dv_shim, "DIAG_KEEP", 3), \
                 mock.patch.object(dv_shim, "main_display_geometry", return_value=None), \
                 mock.patch.object(dv_shim, "screen_locked", return_value=None):
                for i in range(6):
                    dv_shim._diag(f"miss-{i}")
            self.assertLessEqual(len(glob.glob(os.path.join(d, "*.json"))), 3)

    def test_failed_match_leaves_evidence(self):
        import tempfile
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "s.png"); tp = os.path.join(d, "t.png")
            rng = np.random.default_rng(1)
            cv2.imwrite(sp, rng.integers(0, 256, (80, 120, 3), dtype=np.uint8))
            cv2.imwrite(tp, rng.integers(0, 256, (20, 40, 3), dtype=np.uint8))
            with mock.patch.object(dv_shim, "_diag") as dg:
                self.assertIsNone(dv_shim.find_button(sp, tp, threshold=0.99, scale=1.0))
            dg.assert_called_once()
            self.assertIn("score", dg.call_args.kwargs)


class RealTemplates(unittest.TestCase):
    """The captured templates must exist and be loadable — the shim is useless
    without them, and a missing/corrupt PNG should fail loudly in CI, not at 2am."""

    @unittest.skipIf(cv2 is None, "cv2 not installed")
    def test_three_templates_present_and_valid(self):
        for name in ("dolby_vision_palette.png", "analyze_all.png", "target_1000nit.png",
                     "analyze_modal.png"):
            p = os.path.join(dv_shim.TEMPLATES, name)
            self.assertTrue(os.path.exists(p), f"missing template {name}")
            self.assertIsNotNone(cv2.imread(p), f"unreadable template {name}")


class InheritNotSet(unittest.TestCase):
    """The shim VERIFIES the inherited target display; it must never SET color,
    DV profile, or target display (the user configures the project once)."""

    def test_no_setters_exist(self):
        for forbidden in ("set_target_display", "set_dv_profile", "apply_color_management"):
            self.assertFalse(hasattr(dv_shim, forbidden), f"{forbidden} must not exist")
        self.assertTrue(hasattr(dv_shim, "verify_target_display"))
        self.assertTrue(hasattr(dv_shim, "run_dv_ui"))


if __name__ == "__main__":
    unittest.main()


class WaitForAnalysisFocus(unittest.TestCase):
    """The loop used to raise Resolve on every poll — up to 360 focus steals an episode.
    It now only does so when a capture cannot see Resolve at all. The safety property that
    makes that sound: a poll with no eyes on Resolve must advance NOTHING, or switching
    Spaces reads as "analysis finished" and ships an unanalysed master."""

    MODAL = "analyze_modal.png"

    def _run(self, frames, **kw):
        """`frames` is a list of what each successive screenshot 'contains':
        'modal' | 'resolve' | 'nothing'. Returns (result, activate_count, polls_used)."""
        seq = list(frames)
        calls = {"activate": 0, "shots": 0}

        def fake_screenshot(*a, **k):
            calls["shots"] += 1
            return seq[min(calls["shots"] - 1, len(seq) - 1)]

        def fake_found(shot, template, **k):
            name = os.path.basename(template)
            if name == self.MODAL:
                return shot == "modal"
            return shot in ("modal", "resolve")      # the on-screen witnesses

        def fake_activate():
            calls["activate"] += 1
            # Raising it works: every capture after this shows Resolve.
            for i in range(calls["shots"], len(seq)):
                if seq[i] == "nothing":
                    seq[i] = "resolve"

        with mock.patch.object(dv_shim, "screenshot", fake_screenshot), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate", fake_activate), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None):
            res = dv_shim.wait_for_analysis(poll=0, **kw)
        return res, calls["activate"], calls["shots"]

    def test_a_visible_resolve_is_never_raised(self):
        # modal appears, runs, then closes with Resolve still on screen -> done, no focus taken
        res, activates, _ = self._run(["modal", "modal", "resolve", "resolve"])
        self.assertTrue(res)
        self.assertEqual(activates, 0, "focus was taken while Resolve was plainly visible")

    def test_focus_is_taken_only_when_resolve_is_not_in_frame(self):
        res, activates, _ = self._run(["modal", "nothing", "resolve", "resolve"])
        self.assertTrue(res)
        self.assertEqual(activates, 1)

    def test_a_blind_poll_never_counts_as_completion(self):
        """THE regression this exists to prevent. The modal is out of frame only because
        Resolve is not being displayed. The OLD loop would count two such polls as
        'modal closed -> analysis complete' and return True, rendering an unanalysed
        master. The new one must keep waiting."""
        calls = {"activate": 0, "shots": 0}

        def fake_screenshot(*a, **k):
            calls["shots"] += 1
            return "modal" if calls["shots"] == 1 else "nothing"

        def fake_found(shot, template, **k):
            if os.path.basename(template) == self.MODAL:
                return shot == "modal"
            return shot in ("modal", "resolve")

        # activate() cannot help — Resolve stays out of frame no matter what.
        with mock.patch.object(dv_shim, "screenshot", fake_screenshot), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate",
                               lambda: calls.__setitem__("activate", calls["activate"] + 1)), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None), \
             mock.patch.object(dv_shim.time, "time",
                               mock.Mock(side_effect=[0] + [i * 10 for i in range(1, 60)])):
            res = dv_shim.wait_for_analysis(poll=0, appear_timeout=1e9, max_seconds=300)

        # It ran the clock out instead of returning early off the blind polls: the old code
        # would have returned after the 3rd screenshot.
        self.assertGreater(calls["shots"], 8,
                           "it returned early — blind polls were counted as completion")
        # saw_modal was true, so the TIMEOUT (not the blind polls) accepts the analysis.
        self.assertTrue(res)

    def test_it_falls_back_to_the_old_behaviour_when_resolve_stays_invisible(self):
        # Worst case must be "what it did before", not a new hang: after a few blind polls
        # it reverts to raising Resolve every single poll.
        calls = {"activate": 0, "shots": 0}

        def fake_found(shot, template, **k):
            return False                       # nothing is ever visible

        with mock.patch.object(dv_shim, "screenshot", lambda *a, **k: "nothing"), \
             mock.patch.object(dv_shim, "found", fake_found), \
             mock.patch.object(dv_shim, "activate", lambda: calls.__setitem__("activate", calls["activate"] + 1)), \
             mock.patch.object(dv_shim.time, "sleep", lambda *_a: None), \
             mock.patch.object(dv_shim.time, "time", mock.Mock(side_effect=[0] + [i * 10 for i in range(1, 40)])):
            dv_shim.wait_for_analysis(poll=0, appear_timeout=1e9, max_seconds=200)
        self.assertGreaterEqual(calls["activate"], 3,
                                "it must revert to raising Resolve when it stays invisible")

    def test_abort_still_wins_before_anything_is_touched(self):
        ab = mock.Mock()
        ab.is_set.return_value = True
        with mock.patch.object(dv_shim, "screenshot",
                               mock.Mock(side_effect=AssertionError("must not capture"))), \
             mock.patch.object(dv_shim, "activate",
                               mock.Mock(side_effect=AssertionError("must not steal focus"))):
            self.assertFalse(dv_shim.wait_for_analysis(abort=ab, poll=0))
