"""The machine goes to Expurgate only while it is open and activated — and to nothing else.

User rule, 2026-10-06: "expurgate shouldn't have the machine unless it's open and activated. Those
rules shouldn't be possible to bypass." An orphaned training script had held Visionary off all day
by putting "discretion" in a request body while Expurgate was not even running."""
import contextlib
import os
import unittest
from unittest import mock

import expurgate_gate as g

APP = g.EXPURGATE_APPS[0]
ENGINE, APP_PID, SCRIPT = 5001, 5000, 47649
CLIENT, LOCAL = ("127.0.0.1", 50123), ("127.0.0.1", 8765)


def world(listener=ENGINE, parent=APP_PID, app=APP, signed=True, state=True):
    """Patch the system calls to a described world."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(g, "listener_pid", return_value=listener))
    stack.enter_context(mock.patch.object(
        g, "parent_pid", side_effect=lambda pid: parent if pid == listener else None))
    stack.enter_context(mock.patch.object(
        g, "exe_path", side_effect=lambda pid: app if pid == parent else None))
    stack.enter_context(mock.patch.object(g, "signed_as_expurgate", return_value=signed))
    stack.enter_context(mock.patch.object(g, "automation_state", return_value=state))
    return stack


class Engine(unittest.TestCase):
    def test_open_activated_and_asking_itself(self):
        with world():
            self.assertEqual(g.check_engine(ENGINE), (True, "", False))

    def test_not_running_at_all_is_not_open(self):
        with world(listener=None):
            self.assertEqual(g.check_engine(SCRIPT), (False, "Expurgate is not open", True))

    def test_a_look_alike_bundle_anywhere_else_is_not_expurgate(self):
        # review 2026-10-06: a suffix match let /tmp/x/Expurgate.app/Contents/MacOS/Expurgate pass
        for path in ("/tmp/x/Expurgate.app/Contents/MacOS/Expurgate", "/usr/bin/python3",
                     "/Users/Owner/Desktop/Expurgate.app/Contents/MacOS/Expurgate"):
            with world(app=path), mock.patch.object(g, "signed_as_expurgate",
                                                     side_effect=AssertionError("not reached")):
                ok, why, hard = g.check_engine(ENGINE)
            self.assertEqual((ok, hard), (False, True), path)
            self.assertIn("not open", why)

    def test_the_right_place_with_the_wrong_signature_is_not_expurgate(self):
        with world(signed=False):
            ok, why, hard = g.check_engine(ENGINE)
        self.assertEqual((ok, hard), (False, True))
        self.assertIn("signature", why)

    def test_two_listeners_is_a_no_that_waits_one_check(self):
        # a fork mid-exec shares the socket for milliseconds; a real second listener stays and is
        # revoked on the next miss — either way never granted
        with world(listener=g.MANY):
            self.assertEqual(g.check_engine(ENGINE)[::2], (False, False))

    def test_a_rebuilt_expurgate_is_asked_to_relaunch_not_called_an_impostor(self):
        with world(signed=g.REBUILT):
            ok, why, hard = g.check_engine(ENGINE)
        self.assertEqual((ok, hard), (False, True))
        self.assertIn("relaunch Expurgate", why)

    def test_another_program_asking_while_expurgate_is_open_is_refused(self):
        with world():
            ok, why, hard = g.check_engine(SCRIPT)
        self.assertEqual((ok, hard), (False, True))
        self.assertIn("only Expurgate itself", why)

    def test_open_but_not_activated(self):
        with world(state=False):
            self.assertEqual(g.check_engine(ENGINE),
                             (False, "Expurgate is open but not activated", True))

    def test_someone_else_answering_on_its_port_is_refused(self):
        with world(state=g.MANY):
            self.assertEqual(g.check_engine(ENGINE)[::2], (False, True))

    def test_no_answer_is_soft_but_still_no(self):
        with world(state=None):
            self.assertEqual(g.check_engine(ENGINE)[::2], (False, False))

    def test_a_tool_that_fails_is_soft_never_a_verdict(self):
        # review 2026-10-06: a timed-out lsof read as "Expurgate is not open" and revoked at once
        for kw in ({"listener": g.TOOL_FAILED}, {"signed": g.TOOL_FAILED}):
            with world(**kw):
                ok, why, hard = g.check_engine(ENGINE)
            self.assertEqual((ok, hard), (False, False), kw)
        with world(), mock.patch.object(g, "parent_pid", return_value=g.TOOL_FAILED):
            self.assertEqual(g.check_engine(ENGINE)[::2], (False, False))

    def test_the_activation_state_is_never_asked_of_an_impostor(self):
        with world(signed=False), mock.patch.object(g, "automation_state",
                                                    side_effect=AssertionError("asked")):
            g.check_engine(ENGINE)


class Request(unittest.TestCase):
    def test_from_another_device_is_refused_before_anything_is_looked_up(self):
        with mock.patch.object(g, "owner_of", side_effect=AssertionError("no lookup")):
            for ip in ("192.168.1.50", "100.73.38.85", "fe80::1"):
                ok, _why, owner, hard = g.verify_request(((ip, 50000), LOCAL))
                self.assertEqual((ok, owner, hard), (False, None, True))
            for bad in (None, (), ("127.0.0.1", 1), "x"):
                self.assertFalse(g.verify_request(bad)[0])

    def test_the_requester_is_found_by_the_exact_address_pair(self):
        with mock.patch.object(g, "owner_of", return_value=ENGINE) as ow, world():
            self.assertEqual(g.verify_request((CLIENT, LOCAL)), (True, "", ENGINE, False))
        ow.assert_called_once_with(CLIENT, LOCAL)

    def test_an_unidentifiable_requester_is_refused(self):
        with mock.patch.object(g, "owner_of", return_value=None):
            ok, why, owner, hard = g.verify_request((CLIENT, LOCAL))
        self.assertEqual((ok, owner, hard), (False, None, True))
        for unsure in (g.TOOL_FAILED, g.MANY):
            with mock.patch.object(g, "owner_of", return_value=unsure):
                ok, why, owner, hard = g.verify_request((CLIENT, LOCAL))
            self.assertEqual((ok, owner, hard), (False, None, False))

    def test_the_orphaned_harvest_script_is_refused(self):
        with mock.patch.object(g, "owner_of", return_value=SCRIPT), world(listener=None):
            ok, why, owner, hard = g.verify_request((CLIENT, LOCAL))
        self.assertEqual((ok, why, hard), (False, "Expurgate is not open", True))

    def test_ipv6_and_mapped_loopback_count_as_this_mac(self):
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(g.is_loopback(ip), ip)
        for ip in ("0.0.0.0", "192.168.1.92", "garbage", ""):
            self.assertFalse(g.is_loopback(ip), ip)


class Owner(unittest.TestCase):
    LSOF = ("p99683\nf7\nn127.0.0.1:8765->127.0.0.1:50123\n"       # our server's end
            "p5001\nf12\nn127.0.0.1:50123->127.0.0.1:8765\n")      # the requester's end

    def parse_owner(self, text, name, own, ppid=None):
        return g.one_process(g.parse_owners(text, name, own), ppid=ppid or (lambda p: None))

    def test_the_client_end_is_the_requester(self):
        self.assertEqual(self.parse_owner(self.LSOF, "127.0.0.1:50123->127.0.0.1:8765", 99683), 5001)

    def test_never_our_own_end(self):
        only_ours = "p99683\nf7\nn127.0.0.1:8765->127.0.0.1:50123\n"
        self.assertIsNone(self.parse_owner(only_ours, "127.0.0.1:50123->127.0.0.1:8765", 99683))

    def test_a_forked_child_that_has_not_execd_is_its_parents(self):
        two = self.LSOF + "p6000\nf12\nn127.0.0.1:50123->127.0.0.1:8765\n"
        self.assertEqual(self.parse_owner(two, "127.0.0.1:50123->127.0.0.1:8765", 99683,
                                          ppid=lambda p: 5001 if p == 6000 else 1), 5001)

    def test_a_borrowed_port_identifies_the_borrower_not_the_engine(self):
        # review 2026-10-06: a script bound 127.0.0.1:8766 (Expurgate's port) next to the engine,
        # connected to 8765, asked, and half-closed so an ESTABLISHED-only lookup by port found
        # the engine's own socket instead. The exact pair, in any state, finds the script.
        lsof = ("p5001\nf3\nn*:8766\nf9\nn127.0.0.1:8766->127.0.0.1:61000\n"     # the engine
                "p12945\nf4\nn127.0.0.1:8766->127.0.0.1:8765\n"                  # the script (FIN_WAIT)
                "p99683\nf7\nn127.0.0.1:8765->127.0.0.1:8766\n")                 # us
        self.assertEqual(self.parse_owner(lsof, "127.0.0.1:8766->127.0.0.1:8765", 99683), 12945)

    def test_two_unrelated_owners_identify_nobody(self):
        two = self.LSOF + "p6000\nf3\nn127.0.0.1:50123->127.0.0.1:8765\n"
        self.assertIs(self.parse_owner(two, "127.0.0.1:50123->127.0.0.1:8765", 99683), g.MANY)

    def test_addresses_are_written_the_way_lsof_writes_them(self):
        self.assertEqual(g._addr("127.0.0.1", 8765), "127.0.0.1:8765")
        self.assertEqual(g._addr("::1", 8765), "[::1]:8765")

    def test_garbage_identifies_nobody(self):
        self.assertEqual(g.parse_owners("", "a->b", 1), set())
        self.assertEqual(g.parse_owners(None, "a->b", 1), set())


class VerifiedOnce(unittest.TestCase):
    def setUp(self):
        g._VERIFIED.clear()

    tearDown = setUp

    def test_a_running_expurgate_that_passed_stays_passed(self):
        runs = []
        def run(cmd, timeout=5):
            runs.append(cmd[0])
            return (0, "", "")
        with mock.patch.object(g, "proc_start", return_value="Mon Oct  6 21:41:27 2026"), \
             mock.patch.object(g, "_run", side_effect=run):
            self.assertTrue(g.signed_as_expurgate(APP_PID))
            self.assertTrue(g.signed_as_expurgate(APP_PID))   # e.g. after an in-place rebuild
        self.assertEqual(runs, [g.CODESIGN])

    def test_a_reused_pid_is_a_new_process(self):
        with mock.patch.object(g, "proc_start", side_effect=["t1", "t2"]), \
             mock.patch.object(g, "_run", side_effect=[(0, "", ""), (3, "", "fails")]):
            self.assertTrue(g.signed_as_expurgate(APP_PID))
            self.assertFalse(g.signed_as_expurgate(APP_PID))

    def test_rebuilt_before_it_was_ever_verified(self):
        err = "17629: the code on disk does not match what is running\n"
        with mock.patch.object(g, "proc_start", return_value="t"), \
             mock.patch.object(g, "_run", return_value=(1, "", err)):
            self.assertIs(g.signed_as_expurgate(APP_PID), g.REBUILT)
        with mock.patch.object(g, "proc_start", return_value="t"), \
             mock.patch.object(g, "_run", return_value=(3, "", "a sealed resource is missing or invalid")):
            self.assertIs(g.signed_as_expurgate(APP_PID), g.REBUILT)

    def test_a_wrong_signature_is_never_cached(self):
        with mock.patch.object(g, "proc_start", return_value="t"), \
             mock.patch.object(g, "_run", return_value=(3, "", "test-requirement: code failed")):
            self.assertFalse(g.signed_as_expurgate(APP_PID))
        self.assertEqual(g._VERIFIED, {})

    def test_a_ps_that_fails_is_soft_never_a_rebuild(self):
        # review 2026-10-06: a slow ps skipped the cache, codesign said REBUILT, and a hard revoke
        # followed mid-pass
        with mock.patch.object(g, "proc_start", return_value=None), \
             mock.patch.object(g, "_run", side_effect=AssertionError("no codesign without ps")):
            self.assertIs(g.signed_as_expurgate(APP_PID), g.TOOL_FAILED)

    def test_codesign_that_cannot_run_is_soft(self):
        with mock.patch.object(g, "proc_start", return_value="t"), \
             mock.patch.object(g, "_run", return_value=None):
            self.assertIs(g.signed_as_expurgate(APP_PID), g.TOOL_FAILED)


class Identify(unittest.TestCase):
    def test_who_sent_a_release(self):
        with mock.patch.object(g, "owner_of", return_value=ENGINE):
            self.assertEqual(g.identify((CLIENT, LOCAL)), (ENGINE, True))
        with mock.patch.object(g, "owner_of", return_value=g.TOOL_FAILED):
            self.assertEqual(g.identify((CLIENT, LOCAL)), (None, False))
        with mock.patch.object(g, "owner_of", return_value=g.MANY):
            self.assertEqual(g.identify((CLIENT, LOCAL)), (None, False))
        self.assertEqual(g.identify((("192.168.1.50", 1), LOCAL)), (None, True))
        self.assertEqual(g.identify(None), (None, False))


class Pins(unittest.TestCase):
    def test_the_requirement_pins_the_bundle_id_and_the_certificate(self):
        self.assertIn('identifier "com.adambritsch.discretion"', g.EXPURGATE_REQUIREMENT)
        self.assertIn("certificate leaf = H\"", g.EXPURGATE_REQUIREMENT)
        self.assertTrue(g.EXPURGATE_REQUIREMENT.startswith("="))

    def test_the_installed_places_are_exact_paths(self):
        for p in g.EXPURGATE_APPS:
            self.assertTrue(os.path.isabs(p) and p.endswith("/Expurgate.app/Contents/MacOS/Expurgate"))


class NeverFromATest(unittest.TestCase):
    def test_the_real_lookups_refuse_to_run_under_test(self):
        with self.assertRaises(RuntimeError):
            g._run(["/usr/sbin/lsof"])
        with self.assertRaises(RuntimeError):
            g.automation_state(1)
        self.assertIsNone(g.exe_path(1))

    def test_and_a_lease_whose_gate_cannot_run_is_refused(self):
        import yield_lease
        lease = yield_lease.YieldLease()                   # the REAL gate, which cannot run here
        ok, why = lease.take("discretion", 900, peer=(CLIENT, LOCAL))
        self.assertFalse(ok)
        self.assertFalse(lease.state()["held"])


if __name__ == "__main__":
    unittest.main()
