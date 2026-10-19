import os
import tempfile
import time
import unittest
from unittest import mock

import setup_jobs


def _wait(what, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = setup_jobs.status()["jobs"].get(what)
        if j and j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {what} never finished")


class Jobs(unittest.TestCase):
    def setUp(self):
        setup_jobs._JOBS.clear()
        setup_jobs._ACTIVE[0] = None

    def _with_cmd(self, cmd):
        return mock.patch.dict(setup_jobs.TARGETS,
                               {"python_deps": {"cmd": cmd}}, clear=False)

    def test_success_captures_tail_and_rc(self):
        with self._with_cmd("echo one; echo two"):
            out = setup_jobs.start("python_deps")
        self.assertEqual(out["state"], "running")
        j = _wait("python_deps")
        self.assertEqual(j["state"], "ok")
        self.assertEqual(j["rc"], 0)
        self.assertEqual(j["tail"], ["one", "two"])

    def test_failure_is_visible(self):
        with self._with_cmd("echo broken >&2; exit 3"):
            setup_jobs.start("python_deps")
        j = _wait("python_deps")
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["rc"], 3)
        self.assertIn("broken", " ".join(j["tail"]))      # stderr merged into the tail

    def test_tail_is_ring_bounded(self):
        with self._with_cmd("i=0; while [ $i -lt 300 ]; do echo line$i; i=$((i+1)); done"):
            setup_jobs.start("python_deps")
        j = _wait("python_deps")
        self.assertEqual(len(j["tail"]), setup_jobs.TAIL_LINES)
        self.assertEqual(j["tail"][-1], "line299")        # newest kept, oldest dropped

    def test_one_job_at_a_time(self):
        with self._with_cmd("sleep 2"):
            setup_jobs.start("python_deps")
            out = setup_jobs.start("python_deps")
        self.assertEqual(out["error"], "busy")
        self.assertEqual(out["active"], "python_deps")
        setup_jobs._ACTIVE[0] = None                       # don't hold the suite hostage

    def test_unknown_target_refused(self):
        self.assertEqual(setup_jobs.start("rm_rf_slash")["error"], "unknown")

    def test_brew_missing_is_a_copy_only_refusal(self):
        with mock.patch.object(setup_jobs, "BREW", "/no/such/brew"), \
             mock.patch.dict(setup_jobs.TARGETS,
                             {"brew_tools": {"cmd": "x", "needs_brew": True}}, clear=False):
            out = setup_jobs.start("brew_tools")
        self.assertEqual(out["error"], "homebrew-missing")
        self.assertIn("install.sh", out["hint"])           # the copyable command rides along


if __name__ == "__main__":
    unittest.main()


class BrewToolsJob(unittest.TestCase):
    def test_it_runs_brewdeps_and_needs_homebrew(self):
        t = setup_jobs.TARGETS["brew_tools"]
        self.assertIsNone(t["cmd"])              # argv computed by the server: engine/brewdeps.py
        self.assertTrue(t["needs_brew"])
        import brewdeps
        self.assertEqual(brewdeps.ALWAYS, ("x265", "mkvtoolnix"))
        self.assertNotIn("gpac", brewdeps.ALWAYS)
