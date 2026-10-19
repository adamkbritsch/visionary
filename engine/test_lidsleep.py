import os
import unittest
from unittest import mock

import lidsleep


class FakeMac:
    """pmset + sudo as a laptop answers them: a lid, a cable, the disablesleep switch, and
    whether the one-time rule lets `sudo -n pmset` run."""

    def __init__(self, lid=True, ac=True, rule=True, disabled=False):
        self.lid, self.ac, self.rule, self.disabled = lid, ac, rule, disabled
        self.sets = []

    def run(self, argv, timeout=10.0):
        if argv[:3] == [lidsleep.PMSET, "-g", "batt"]:
            src = "'AC Power'" if self.ac else "'Battery Power'"
            out = f"Now drawing from {src}\n"
            if self.lid:
                out += " -InternalBattery-0 (id=1)\t97%; charging; present: true\n"
            return 0, out
        if argv == [lidsleep.PMSET, "-g"]:
            return 0, "System-wide power settings:\n" + (" SleepDisabled\t\t1\n" if self.disabled else "")
        if argv[:2] == [lidsleep.SUDO, "-n"] and argv[2:5] == [lidsleep.PMSET, "-a", "disablesleep"]:
            if not self.rule:
                return 1, ""
            self.disabled = argv[5] == "1"
            self.sets.append(argv[5])
            return 0, ""
        raise AssertionError(f"unexpected command {argv}")


class LidSleep(unittest.TestCase):
    def setUp(self):
        self.mac = FakeMac()
        self.ps = [mock.patch.object(lidsleep, "_run", side_effect=self.mac.run),
                   mock.patch.object(lidsleep, "_spawn_guard")]
        for p in self.ps:
            p.start()
        lidsleep._allowed_at[:] = [0.0, None]
        self.addCleanup(self._clean)

    def _clean(self):
        for p in self.ps:
            p.stop()
        for f in (lidsleep.HOLD_FILE,):
            try:
                os.remove(f)
            except OSError:
                pass

    def test_activated_on_ac_turns_lid_sleep_off_and_back_on(self):
        self.assertEqual(lidsleep.hold(), (True, "held"))
        self.assertTrue(self.mac.disabled)
        lidsleep._spawn_guard.assert_called_once()
        self.assertEqual(lidsleep.hold(), (True, "held"))     # idempotent: no second set
        self.assertTrue(lidsleep.release())
        self.assertFalse(self.mac.disabled)
        self.assertFalse(os.path.exists(lidsleep.HOLD_FILE))

    def test_a_user_who_turned_sleep_off_keeps_it_off(self):
        self.mac.disabled = True
        lidsleep.hold()
        lidsleep.release()
        self.assertTrue(self.mac.disabled)

    def test_never_on_battery_never_without_a_lid_never_without_the_rule(self):
        self.mac.ac = False
        self.assertEqual(lidsleep.hold(), (False, "on battery"))
        self.mac.ac, self.mac.lid = True, False
        self.assertEqual(lidsleep.hold(), (False, "no lid"))
        self.mac.lid, self.mac.rule = True, False
        lidsleep._allowed_at[:] = [0.0, None]
        self.assertEqual(lidsleep.hold(), (False, "needs permission"))
        self.assertFalse(self.mac.disabled)

    def test_the_permission_test_changes_nothing(self):
        self.assertTrue(lidsleep.allowed(fresh=True))
        self.assertEqual(self.mac.sets, ["0"])                # set to what it already was
        self.assertFalse(self.mac.disabled)

    def test_the_guard_lets_go_when_the_engine_dies(self):
        lidsleep.hold(owner_pid=2 ** 22 + 12345)              # no such process
        self.assertEqual(lidsleep.guard(), 0)
        self.assertFalse(self.mac.disabled)
        self.assertIsNone(lidsleep._read_hold())

    def test_the_guard_lets_go_on_battery(self):
        lidsleep.hold()
        self.mac.ac = False
        self.assertEqual(lidsleep.guard(), 0)
        self.assertFalse(self.mac.disabled)

    def test_a_guard_never_releases_a_newer_engines_hold(self):
        lidsleep.hold(owner_pid=os.getpid())
        self.assertFalse(lidsleep.release(only_pid=1))        # an older engine's guard
        self.assertTrue(self.mac.disabled)

    def test_a_new_engine_releases_a_dead_engines_hold(self):
        lidsleep.hold(owner_pid=2 ** 22 + 12345)
        self.assertTrue(lidsleep.recover())
        self.assertFalse(self.mac.disabled)

    def test_reconcile_follows_the_run(self):
        self.assertEqual(lidsleep.reconcile(True), (True, "held"))
        self.assertEqual(lidsleep.reconcile(False), (False, "released"))
        self.assertEqual(lidsleep.reconcile(False), (False, "off"))

    def test_the_rule_is_exactly_the_two_commands(self):
        line = lidsleep.rule_line("someone")
        self.assertEqual(line, "someone ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, "
                               "/usr/bin/pmset -a disablesleep 0")
        cmd = lidsleep.install_command("someone")
        self.assertTrue(cmd.endswith("lidsleep.py --install"))
        self.assertNotIn("'", cmd)                            # nothing for a paste to mangle
        self.assertNotIn("(", cmd)


class TestsNeverTouchTheMac(unittest.TestCase):
    def test_unpatched_calls_run_nothing(self):
        self.assertEqual(lidsleep._run([lidsleep.PMSET, "-g"]), (None, ""))
        self.assertNotIn(os.path.expanduser("~/.topaz-pipeline"), lidsleep.HOLD_FILE)


if __name__ == "__main__":
    unittest.main()
