"""Every supported display combination: a 16-inch MacBook Pro Retina panel and a 4K
display, in either role and any arrangement — plus a 4K on its own.

These are PURE: they drive the real arithmetic and the real eligibility rule against
synthesised display descriptors, so an arrangement nobody has physically built is still
covered. The one thing they cannot prove is that screencapture -R accepts a negative
origin; that was verified live (rc 0, correct 3840x2160 capture at -1920,-400).
"""
import unittest
from unittest import mock

import displays
import dv_shim
import preflight
import versions

MBP16 = dict(backing=(3456, 2234), size_pt=(1728, 1117), scale=2.0, builtin=True)
UHD4K = dict(backing=(3840, 2160), size_pt=(1920, 1080), scale=2.0, builtin=False)
# A 4K driven at 1x ("More Space"): the invariant refuses it, and must keep refusing it.
UHD_1X = dict(backing=(3840, 2160), size_pt=(3840, 2160), scale=1.0, builtin=False)


def disp(spec, key, origin, main=False):
    return {"key": key, "id": abs(hash(key)) % 1000, "uuid": key, "main": main,
            "mirror_slave": False, "origin": origin, "vendor": 1, "model": 1, "serial": 1,
            **spec}


class Eligibility(unittest.TestCase):
    def test_both_panels_qualify_in_either_role(self):
        for spec in (MBP16, UHD4K):
            got = preflight.match_display(spec["backing"][0], spec["backing"][1],
                                          spec["scale"], spec["builtin"])
            self.assertIsNotNone(got, spec)

    def test_a_4k_alone_on_a_mac_qualifies(self):
        # Desktop Mac, or a clamshell laptop: one display, and it is the 4K.
        got = preflight.match_display(3840, 2160, 2.0, False)
        self.assertIsNotNone(got)

    def test_a_4k_at_1x_is_refused_and_says_why(self):
        self.assertIsNone(preflight.match_display(3840, 2160, 1.0, False))
        with mock.patch.object(displays, "enumerate_displays",
                               return_value=[disp(UHD_1X, "uuid:4K", (0.0, 0.0), main=True)]):
            rows = preflight.eligible_displays()
        self.assertFalse(rows[0]["eligible"])
        self.assertIn("templates only match", rows[0]["why_not"])

    def test_the_scale_invariant_is_not_relaxed_for_a_second_display(self):
        # A non-main display gets exactly the same rule as main. No exceptions.
        with mock.patch.object(displays, "enumerate_displays", return_value=[
                disp(MBP16, "uuid:MBP", (0.0, 0.0), main=True),
                disp(UHD_1X, "uuid:4K", (1728.0, 0.0))]):
            rows = {r["key"]: r for r in preflight.eligible_displays()}
        self.assertTrue(rows["uuid:MBP"]["eligible"])
        self.assertFalse(rows["uuid:4K"]["eligible"])


class Arrangements(unittest.TestCase):
    """The 4K can sit right, left, above or below the built-in — and either can be main.
    Left/above give NEGATIVE origins, which is the case the plan flagged as unverified."""

    LAYOUTS = {
        "4K right of built-in": (1728.0, 0.0),
        "4K left of built-in": (-1920.0, 0.0),
        "4K above built-in": (0.0, -1080.0),
        "4K below built-in": (0.0, 1117.0),
        "4K diagonal, both axes negative": (-1920.0, -400.0),
    }

    def _host(self, origin):
        return disp(UHD4K, "uuid:4K", origin)

    def test_host_view_reports_each_origin_including_negative_ones(self):
        for name, origin in self.LAYOUTS.items():
            h = self._host(origin)
            with mock.patch.object(displays, "find", return_value=h):
                dv_shim.set_host(h)
                try:
                    ox, oy, scale, w, h_pt = dv_shim.host_view()
                finally:
                    dv_shim.set_host(None)
            self.assertEqual((ox, oy), origin, name)
            self.assertEqual((scale, w, h_pt), (2.0, 1920.0, 1080.0), name)

    @unittest.skipIf(dv_shim.cv2 is None, "cv2 not installed")
    def test_a_match_is_offset_by_the_origin_whatever_its_sign(self):
        """Drives the REAL matcher, not arithmetic restated in the test: the same capture
        matched with each origin must differ by exactly that origin."""
        import numpy as np, cv2, tempfile, os
        d = tempfile.mkdtemp()
        shot, tmpl = os.path.join(d, "s.png"), os.path.join(d, "t.png")
        rng = np.random.default_rng(11)
        img = rng.integers(0, 60, (400, 600, 3), dtype=np.uint8)
        img[120:160, 300:360] = rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        cv2.imwrite(shot, img)
        cv2.imwrite(tmpl, img[120:160, 300:360])
        base, score = dv_shim.match_template(shot, tmpl, scale=2.0, origin=(0.0, 0.0))
        self.assertGreater(score, 0.99)
        for name, (ox, oy) in self.LAYOUTS.items():
            got, _ = dv_shim.match_template(shot, tmpl, scale=2.0, origin=(ox, oy))
            self.assertAlmostEqual(got[0] - base[0], ox, places=6, msg=name)
            self.assertAlmostEqual(got[1] - base[1], oy, places=6, msg=name)

    def test_click_bounds_accept_the_host_and_reject_the_other_screen(self):
        for name, origin in self.LAYOUTS.items():
            ox, oy = origin
            view = (ox, oy, 2.0, 1920.0, 1080.0)
            inside = (ox + 960, oy + 540)
            # A point on the OTHER display must be refused even when the arithmetic would
            # otherwise produce it — that is the silent-wrong-screen failure.
            outside = (ox + 1920 + 500, oy + 540)
            with mock.patch.object(dv_shim, "host_view", return_value=view), \
                 mock.patch.object(dv_shim, "_diag", lambda *a, **k: ""), \
                 mock.patch.object(dv_shim.subprocess, "run", lambda *a, **k: None):
                dv_shim.click(*inside)                 # must not raise
            with mock.patch.object(dv_shim, "host_view", return_value=view), \
                 mock.patch.object(dv_shim, "_diag", lambda *a, **k: ""), \
                 mock.patch.object(dv_shim.subprocess, "run",
                                   mock.Mock(side_effect=AssertionError("must not click"))):
                with self.assertRaises(RuntimeError, msg=name):
                    dv_shim.click(*outside)

    def test_the_capture_rect_is_the_hosts_own_rect_in_global_points(self):
        for name, origin in self.LAYOUTS.items():
            ox, oy = origin
            seen = {}
            with mock.patch.object(dv_shim, "host_view",
                                   return_value=(ox, oy, 2.0, 1920.0, 1080.0)), \
                 mock.patch.object(dv_shim, "_HOST", self._host(origin)), \
                 mock.patch.object(dv_shim.subprocess, "run",
                                   lambda cmd, **kw: (seen.update(cmd=cmd),
                                                      mock.Mock(returncode=0, stderr="", stdout=""))[1]), \
                 mock.patch.object(dv_shim.os.path, "exists", return_value=True), \
                 mock.patch.object(dv_shim.os.path, "getsize", return_value=9):
                dv_shim.screenshot("/tmp/x.png")
            rect = seen["cmd"][seen["cmd"].index("-R") + 1]
            self.assertEqual(rect, "%d,%d,1920,1080" % (ox, oy), name)


class EitherPanelCanBeMain(unittest.TestCase):
    """Both orders must work: the 4K as main with Resolve on the built-in, and the
    built-in as main with Resolve on the 4K."""

    def _pinned(self, layout, pinned_key):
        smoke = {d["key"]: {"pass": True, "best": 1.0} for d in layout}
        with mock.patch.object(displays, "enumerate_displays", return_value=layout), \
             mock.patch.object(preflight, "load_display_smoke", return_value=smoke), \
             mock.patch("settings.get_settings",
                        return_value={"resolve_host_pinning": True}), \
             mock.patch("settings.get_display_priority", return_value=[pinned_key]):
            return preflight.chosen_host()

    def test_built_in_main_hosts_on_the_4k(self):
        layout = [disp(MBP16, "uuid:MBP", (0.0, 0.0), main=True),
                  disp(UHD4K, "uuid:4K", (1728.0, 0.0))]
        host, why = self._pinned(layout, "uuid:4K")
        self.assertIsNotNone(host, why)
        self.assertEqual(host["key"], "uuid:4K")

    def test_4k_main_hosts_on_the_built_in(self):
        layout = [disp(UHD4K, "uuid:4K", (0.0, 0.0), main=True),
                  disp(MBP16, "uuid:MBP", (1920.0, 0.0))]
        host, why = self._pinned(layout, "uuid:MBP")
        self.assertIsNotNone(host, why)
        self.assertEqual(host["key"], "uuid:MBP")

    def test_pinning_the_main_display_just_drives_main(self):
        layout = [disp(MBP16, "uuid:MBP", (0.0, 0.0), main=True),
                  disp(UHD4K, "uuid:4K", (1728.0, 0.0))]
        host, why = self._pinned(layout, "uuid:MBP")
        self.assertIsNone(host)
        self.assertIn("main", why)

    def test_a_single_4k_has_nothing_to_host_on_and_drives_main(self):
        layout = [disp(UHD4K, "uuid:4K", (0.0, 0.0), main=True)]
        host, why = self._pinned(layout, "uuid:4K")
        self.assertIsNone(host, "a lone display is main — there is nowhere else to go")
        self.assertIn("main", why)

    def test_an_unplugged_pinned_display_never_silently_becomes_main(self):
        layout = [disp(MBP16, "uuid:MBP", (0.0, 0.0), main=True)]
        host, why = self._pinned(layout, "uuid:4K")
        self.assertIsNone(host)
        self.assertIn("not attached", why)

    def test_an_unproven_display_is_not_chosen(self):
        layout = [disp(MBP16, "uuid:MBP", (0.0, 0.0), main=True),
                  disp(UHD4K, "uuid:4K", (1728.0, 0.0))]
        with mock.patch.object(displays, "enumerate_displays", return_value=layout), \
             mock.patch.object(preflight, "load_display_smoke", return_value={}), \
             mock.patch("settings.get_settings", return_value={"resolve_host_pinning": True}), \
             mock.patch("settings.get_display_priority", return_value=["uuid:4K"]):
            host, why = preflight.chosen_host()
        self.assertIsNone(host)
        self.assertIn("not proven", why)


if __name__ == "__main__":
    unittest.main()
