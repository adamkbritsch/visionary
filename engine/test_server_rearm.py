"""APPLIANCE mode: should_rearm() — while ACTIVATED and not already running, the orchestrator
re-enables. A run ends only on a manual Deactivate; there is no auto-stop."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "dashboard"))
import server


class ShouldRearm(unittest.TestCase):
    def test_not_activated_never_rearms(self):
        self.assertFalse(server.should_rearm(activated=False, enabled=False))

    def test_already_enabled_never_rearms(self):
        self.assertFalse(server.should_rearm(activated=True, enabled=True))

    def test_activated_and_idle_rearms(self):
        # "runs whenever it can": activated + not running → re-arm immediately, any time of day.
        self.assertTrue(server.should_rearm(activated=True, enabled=False))


if __name__ == "__main__":
    unittest.main()
