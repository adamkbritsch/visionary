import os
import subprocess
import sys
import unittest

import dvcap
import peakplan


def _gate(cap=50):
    return cap * dvcap.PEAK_TOLERANCE * peakplan.REEXPORT_MARGIN     # 51.75 for a 50 cap


class PeakPlanStandsAlone(unittest.TestCase):
    """peakplan.py is the YouTube-gamble arithmetic the native render uses: pure, importing
    nothing heavy."""


    def test_the_constants_kept_their_values(self):
        self.assertEqual((peakplan.REEXPORT_MARGIN, peakplan.REEXPORT_FLOOR_KBPS, peakplan.MAX_REEXPORTS),
                         (0.9, 8000, 2))


    def test_importing_it_loads_nothing_heavy(self):
        engine = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys, peakplan; peakplan.reexport_plan(20000, 60, 50); "
             "print(sorted(m for m in ('versions', 'orchestrator', 'stages') "
             "if m in sys.modules))"],
            cwd=engine, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class ReexportMath(unittest.TestCase):
    """The YouTube gamble's second throw (user-dictated 2026-09-14), as test_stages.YouTubeReexport
    pins it through resolve_pipeline — here against peakplan itself."""

    def test_the_math_scales_the_target_by_the_measured_burst_ratio(self):
        # 20 Mb/s peaked at 59.9 (live 2026-08-18) under a 50 cap
        self.assertEqual(peakplan.reexport_kbps(20000, 59.9, 50), int(20000 * _gate() / 59.9))
        self.assertIsNone(peakplan.reexport_kbps(20000, 40.0, 50), "not over — nothing to lower")
        self.assertIsNone(peakplan.reexport_kbps(20000, 300.0, 50), "under the floor — the remux caps it")
        self.assertIsNone(peakplan.reexport_kbps(0, 60, 50))
        self.assertIsNone(peakplan.reexport_kbps("x", 60, 50))

    def test_it_aims_at_the_gate_that_decides_not_at_the_bare_cap(self):
        """Live 2026-09-26: 14000 kb/s peaked at 79.7 against a 50 cap; the old aim asked for
        7905 (under the floor) and the item spent 4 h 36 m in x265. The gate-based aim: 9090."""
        want = peakplan.reexport_kbps(14000, 79.7, 50)
        self.assertEqual(want, int(14000 * _gate() / 79.7))
        self.assertEqual(want, 9090)
        self.assertGreaterEqual(want, peakplan.REEXPORT_FLOOR_KBPS)

    def test_two_measured_points_are_fitted_rather_than_assumed_proportional(self):
        fitted = peakplan.reexport_kbps(17000, 55.0, 50, prev=(20000, 60.0))
        self.assertLess(fitted, int(17000 * _gate() / 55.0))
        b = (60.0 - 55.0) / (20000 - 17000)
        self.assertEqual(fitted, int((_gate() - (55.0 - b * 17000)) / b))

    def test_a_fit_under_the_floor_or_a_line_that_never_reaches_the_gate_declines(self):
        self.assertIsNone(peakplan.reexport_kbps(8358, 61.7, 50, prev=(14000, 75.4)))
        self.assertIsNone(peakplan.reexport_kbps(15000, 68.0, 50, prev=(20000, 70.0)))

    def test_a_useless_fit_falls_back_to_the_ratio(self):
        self.assertEqual(peakplan.reexport_kbps(20000, 60.0, 50, prev=(20000, 59.0)), int(20000 * _gate() / 60.0))
        self.assertEqual(peakplan.reexport_kbps(17000, 62.0, 50, prev=(20000, 60.0)), int(17000 * _gate() / 62.0))

    def test_each_no_says_which_no_it_is(self):
        self.assertEqual(peakplan.reexport_plan(14000, 300.0, 50)[1], "under-floor")
        self.assertEqual(peakplan.reexport_plan(15000, 68.0, 50, prev=(20000, 70.0))[1], "content-floor")
        self.assertEqual(peakplan.reexport_plan(20000, 40.0, 50)[1], "not-over")
        self.assertEqual(peakplan.reexport_plan("x", 60, 50)[1], "unreadable")
        self.assertEqual(peakplan.reexport_plan(14000, 79.7, 50), (9090, ""))

    def test_kbps_is_the_target_half_of_plan(self):
        for args, kw in (((20000, 59.9, 50), {}), ((17000, 55.0, 50), {"prev": (20000, 60.0)}),
                         ((14000, 300.0, 50), {}), ((20000, 70.0, 50), {"margin": 0.8, "floor": 4000})):
            self.assertEqual(peakplan.reexport_kbps(*args, **kw), peakplan.reexport_plan(*args, **kw)[0])


if __name__ == "__main__":
    unittest.main()
