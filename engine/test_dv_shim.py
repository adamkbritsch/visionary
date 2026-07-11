import os
import tempfile
import unittest

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


class AnalysisDone(unittest.TestCase):
    """Completion is decided purely from a sequence of region hashes."""

    def test_not_done_with_too_few_samples(self):
        self.assertFalse(dv_shim.is_analysis_done(["a", "a"]))

    def test_not_done_while_unchanged(self):
        # stable but never changed = analysis hasn't started, NOT done
        self.assertFalse(dv_shim.is_analysis_done(["a", "a", "a", "a", "a"]))

    def test_not_done_while_still_moving(self):
        self.assertFalse(dv_shim.is_analysis_done(["a", "a", "b", "c"]))

    def test_done_after_change_then_settle(self):
        # 0.000 (a...) -> populated (b) -> settled (b,b,b)
        self.assertTrue(dv_shim.is_analysis_done(["a", "a", "b", "b", "b"]))


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
