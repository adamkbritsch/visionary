import unittest
from unittest import mock

import brewdeps


class Plan(unittest.TestCase):
    """What the Setup job asks Homebrew for, per state: only what is missing, and ffmpeg 8 at
    /opt/homebrew/bin/ffmpeg (an installed 8 is kept; else ffmpeg@8, linked)."""

    def test_a_ready_mac_does_nothing(self):
        self.assertEqual(brewdeps.plan({"installed": {"x265": True, "mkvtoolnix": True},
                                        "ffmpeg_major": 8, "ffmpeg_linked": True}), [])

    def test_a_fresh_mac(self):
        self.assertEqual(brewdeps.plan({"installed": {}, "ffmpeg_major": None, "ffmpeg_linked": False}),
                         [["install", "x265"], ["install", "mkvtoolnix"], ["install", "ffmpeg@8"],
                          ["link", "--force", "ffmpeg@8"]])

    def test_a_newer_ffmpeg_is_unlinked_not_uninstalled(self):
        steps = brewdeps.plan({"installed": {"x265": True, "mkvtoolnix": True, "ffmpeg@8": True},
                               "ffmpeg_major": 9, "ffmpeg_linked": True})
        self.assertEqual(steps, [["unlink", "ffmpeg"], ["link", "--force", "ffmpeg@8"]])
        self.assertNotIn("uninstall", [s[0] for s in steps])

    def test_dovi_tool_never_comes_from_homebrew(self):
        steps = brewdeps.plan({"installed": {}, "ffmpeg_major": None, "ffmpeg_linked": False})
        self.assertFalse([s for s in steps if "dovi_tool" in s])


class Main(unittest.TestCase):
    def test_runs_the_plan_in_order_and_checks_ffmpeg_at_the_end(self):
        ran = []
        with mock.patch.object(brewdeps.os.path, "exists", return_value=True), \
             mock.patch.object(brewdeps, "current_state",
                               return_value={"installed": {}, "ffmpeg_major": None,
                                             "ffmpeg_linked": False}), \
             mock.patch.object(brewdeps, "ffmpeg_major", return_value=8), \
             mock.patch.object(brewdeps.subprocess, "run",
                               side_effect=lambda c, **k: ran.append(c) or mock.Mock(returncode=0)):
            self.assertEqual(brewdeps.main(), 0)
        self.assertEqual([c[1:] for c in ran],
                         [["install", "x265"], ["install", "mkvtoolnix"], ["install", "ffmpeg@8"],
                          ["link", "--force", "ffmpeg@8"]])
        self.assertTrue(all(c[0] == brewdeps.BREW for c in ran))

    def test_a_failed_step_stops_and_fails(self):
        ran = []
        with mock.patch.object(brewdeps.os.path, "exists", return_value=True), \
             mock.patch.object(brewdeps, "current_state",
                               return_value={"installed": {}, "ffmpeg_major": None,
                                             "ffmpeg_linked": False}), \
             mock.patch.object(brewdeps.subprocess, "run",
                               side_effect=lambda c, **k: ran.append(c) or mock.Mock(returncode=1)):
            self.assertEqual(brewdeps.main(), 1)
        self.assertEqual(len(ran), 1)

    def test_still_not_8_at_the_end_fails(self):
        with mock.patch.object(brewdeps.os.path, "exists", return_value=True), \
             mock.patch.object(brewdeps, "current_state",
                               return_value={"installed": {"x265": True, "mkvtoolnix": True},
                                             "ffmpeg_major": 8, "ffmpeg_linked": True}), \
             mock.patch.object(brewdeps, "ffmpeg_major", return_value=9):
            self.assertEqual(brewdeps.main(), 1)

    def test_no_homebrew(self):
        with mock.patch.object(brewdeps.os.path, "exists", return_value=False):
            self.assertEqual(brewdeps.main(), 1)


class Probes(unittest.TestCase):
    def test_ffmpeg_major(self):
        with mock.patch.object(brewdeps.subprocess, "run", return_value=mock.Mock(
                returncode=0, stdout="ffmpeg version 8.1.3 Copyright (c) 2000-2026")):
            self.assertEqual(brewdeps.ffmpeg_major(), 8)
        with mock.patch.object(brewdeps.subprocess, "run", side_effect=OSError("missing")):
            self.assertIsNone(brewdeps.ffmpeg_major())

    def test_which_formula_links_ffmpeg(self):
        with mock.patch.object(brewdeps.os.path, "realpath",
                               return_value="/opt/homebrew/Cellar/ffmpeg/9.0.2/bin/ffmpeg"):
            self.assertTrue(brewdeps.links_ffmpeg())
        with mock.patch.object(brewdeps.os.path, "realpath",
                               return_value="/opt/homebrew/Cellar/ffmpeg@8/8.1.3/bin/ffmpeg"):
            self.assertFalse(brewdeps.links_ffmpeg())


if __name__ == "__main__":
    unittest.main()
