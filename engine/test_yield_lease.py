"""The yield lease. Its TTL is a safety property, so the expiry behaviour is tested directly."""

import unittest

import yield_lease


class TrustingGate(object):
    """For tests of the lease's TTL/identity mechanics, not of WHO may hold it (that is
    test_expurgate_gate / GatedLease). Production has no such gate: the default is the real one."""
    def verify_request(self, peer):
        return True, "", 4242, False
    def check_engine(self, owner):
        return True, "", False
    def identify(self, peer):
        return 4242, True


TRUST = TrustingGate()


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TakeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(gate=TRUST, now=self.clock)

    def test_a_lease_makes_stages_stand_down(self):
        self.assertFalse(self.lease.active())
        ok, _d = self.lease.take("discretion", 60, "Pass 1 on Arrival")
        self.assertTrue(ok)
        self.assertTrue(self.lease.active())

    def test_it_lapses_on_its_own_with_nobody_running_a_timer(self):
        """The whole point: a crashed or force-quit sibling must not wedge an overnight queue, and
        the worst case is one lease's worth of idling."""
        self.lease.take("discretion", 60)
        self.clock.t += 59
        self.assertTrue(self.lease.active())
        self.clock.t += 2
        self.assertFalse(self.lease.active())

    def test_the_same_holder_extends_rather_than_being_refused(self):
        self.lease.take("discretion", 60)
        self.clock.t += 30
        ok, detail = self.lease.take("discretion", 120)
        self.assertTrue(ok)
        self.assertIn("extended", detail)
        self.clock.t += 100
        self.assertTrue(self.lease.active())

    def test_a_different_holder_is_refused_while_one_is_live(self):
        """Two siblings both believing they have the machine is worse than one waiting and knowing."""
        self.lease.take("discretion", 60)
        ok, detail = self.lease.take("something-else", 60)
        self.assertFalse(ok)
        self.assertIn("another", detail)

    def test_a_different_holder_may_take_it_after_expiry(self):
        self.lease.take("discretion", 60)
        self.clock.t += 61
        self.assertTrue(self.lease.take("something-else", 60)[0])

    def test_an_over_long_lease_is_refused_with_the_ceiling_stated(self):
        ok, detail = self.lease.take("discretion", yield_lease.MAX_SECONDS + 1)
        self.assertFalse(ok)
        self.assertIn("at most", detail)

    def test_a_nonpositive_or_nonnumeric_lease_is_refused(self):
        self.assertFalse(self.lease.take("discretion", 0)[0])
        self.assertFalse(self.lease.take("discretion", -5)[0])
        self.assertFalse(self.lease.take("discretion", "soon")[0])

    def test_a_lease_needs_a_holder_so_an_expiry_can_be_attributed(self):
        self.assertFalse(self.lease.take("", 60)[0])
        self.assertFalse(self.lease.take(None, 60)[0])

    def test_the_ceiling_covers_a_real_render_but_not_a_week(self):
        self.assertGreaterEqual(yield_lease.MAX_SECONDS, 6 * 3600)
        self.assertLessEqual(yield_lease.MAX_SECONDS, 12 * 3600)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(gate=TRUST, now=self.clock)

    def test_release_frees_it_immediately(self):
        self.lease.take("discretion", 3600)
        ok, detail = self.lease.release("discretion")
        self.assertTrue(ok)
        self.assertIn("released", detail)
        self.assertFalse(self.lease.active())

    def test_releasing_nothing_is_a_clean_yes(self):
        self.assertEqual(self.lease.release("discretion"), (True, "no lease was held"))

    def test_a_mismatched_holder_cannot_release_someone_elses_lease(self):
        self.lease.take("discretion", 3600)
        ok, detail = self.lease.release("impostor")
        self.assertFalse(ok)
        self.assertIn("holds the lease", detail)
        self.assertTrue(self.lease.active())

    def test_an_anonymous_release_is_allowed_as_an_operator_escape(self):
        self.lease.take("discretion", 3600)
        self.assertTrue(self.lease.release()[0])
        self.assertFalse(self.lease.active())


class ProtectedStageTests(unittest.TestCase):
    def setUp(self):
        self.lease = yield_lease.YieldLease(gate=TRUST, now=Clock())
        self.lease.take("discretion", 3600)

    def test_resolve_upload_and_extend_never_stand_down(self):
        """Resolve holds the screen and cannot be paced; an upload half-written to the NAS is the one
        thing worse than a slow one."""
        for stage in ("resolve", "upload", "extend"):
            self.assertFalse(self.lease.blocks(stage), stage)

    def test_every_other_stage_does(self):
        for stage in ("topaz", "remux", "download", "cfr"):
            self.assertTrue(self.lease.blocks(stage), stage)

    def test_nothing_blocks_when_no_lease_is_held(self):
        self.lease.release()
        for stage in ("topaz", "remux", "download"):
            self.assertFalse(self.lease.blocks(stage))


class StateTests(unittest.TestCase):
    def test_state_says_who_has_the_machine_and_for_how_long(self):
        clock = Clock()
        lease = yield_lease.YieldLease(gate=TRUST, now=clock)
        lease.take("discretion", 600, "Pass 2 on Arrival")
        clock.t += 60
        s = lease.state()
        self.assertTrue(s["held"])
        self.assertEqual(s["holder"], "discretion")
        self.assertEqual(s["reason"], "Pass 2 on Arrival")
        self.assertEqual(s["seconds_left"], 540)
        self.assertEqual(s["held_for"], 60)

    def test_an_expired_lease_reports_as_free_without_needing_a_poll(self):
        clock = Clock()
        lease = yield_lease.YieldLease(gate=TRUST, now=clock)
        lease.take("discretion", 60)
        clock.t += 61
        self.assertFalse(lease.state()["held"])
        self.assertIsNone(lease.state()["holder"])

    def test_no_lease_reports_as_free(self):
        self.assertFalse(yield_lease.YieldLease(gate=TRUST).state()["held"])


class LeaseIdentityTests(unittest.TestCase):
    """A holder cannot tell "my lease lapsed" from "I never had one" unless the lease has a name.

    The lease lives only in the server process, and the process is replaced by every deploy, quit
    or relaunch: twice on 2026-09-23 a sibling read back not-held within a minute of a successful
    take, and nothing on either side recorded which of those it was. An id makes the loss visible
    to the holder, whatever caused it: a restart, an expiry, a release, or a takeover."""

    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(gate=TRUST, now=self.clock)

    def test_a_held_lease_has_an_id(self):
        self.lease.take("discretion", 600)
        self.assertTrue(self.lease.state()["id"])

    def test_extending_keeps_the_same_id(self):
        self.lease.take("discretion", 600)
        first = self.lease.state()["id"]
        self.clock.t += 60
        self.lease.take("discretion", 600)
        self.assertEqual(self.lease.state()["id"], first)   # one lease, renewed — not a new one

    def test_a_retake_after_a_lapse_gets_a_different_id(self):
        self.lease.take("discretion", 60)
        first = self.lease.state()["id"]
        self.clock.t += 61
        self.lease.take("discretion", 60)
        self.assertNotEqual(self.lease.state()["id"], first)

    def test_a_free_lease_has_no_id(self):
        self.assertIsNone(self.lease.state()["id"])
        self.lease.take("discretion", 60)
        self.clock.t += 61
        self.assertIsNone(self.lease.state()["id"])         # lapsed → the holder's id is stale

    def test_a_fresh_process_cannot_reissue_an_id(self):
        """A restart is exactly the case the id exists for, so two lives must not collide."""
        a = yield_lease.YieldLease(gate=TRUST, now=Clock())
        b = yield_lease.YieldLease(gate=TRUST, now=Clock())
        a.take("discretion", 600)
        b.take("discretion", 600)
        self.assertNotEqual(a.state()["id"], b.state()["id"])

    def test_a_stale_id_cannot_release_a_newer_lease(self):
        """A late release from a pass that predates a restart must not free the lease that replaced it."""
        self.lease.take("discretion", 60)
        stale = self.lease.state()["id"]
        self.clock.t += 61
        self.lease.take("discretion", 600)                  # a new lease, same holder
        ok, detail = self.lease.release("discretion", lease_id=stale)
        self.assertFalse(ok)
        self.assertIn("another lease", detail)
        self.assertTrue(self.lease.active())

    def test_the_matching_id_releases(self):
        self.lease.take("discretion", 600)
        ok, _d = self.lease.release("discretion", lease_id=self.lease.state()["id"])
        self.assertTrue(ok)
        self.assertFalse(self.lease.active())

    def test_a_release_without_an_id_still_works(self):
        """Discretion's current client sends no id, and an operator escape sends no holder either."""
        self.lease.take("discretion", 600)
        self.assertTrue(self.lease.release("discretion")[0])
        self.lease.take("discretion", 600)
        self.assertTrue(self.lease.release()[0])


class TransitionLogTests(unittest.TestCase):
    """Every lease transition is reported, because the two sightings on 2026-09-23 could not be
    attributed afterwards: Visionary recorded nothing and the sibling's own events are in memory."""

    def setUp(self):
        self.clock = Clock()
        self.seen = []
        self.lease = yield_lease.YieldLease(gate=TRUST, now=self.clock, on_change=self.seen.append)

    def test_a_take_an_extension_and_a_release_are_each_reported(self):
        self.lease.take("discretion", 600, "Pass 2 on Arrival")
        self.lease.take("discretion", 600)
        self.lease.release("discretion")
        self.assertEqual(len(self.seen), 3)
        self.assertIn("discretion", self.seen[0])
        self.assertIn("Pass 2 on Arrival", self.seen[0])
        self.assertIn("extended", self.seen[1])
        self.assertIn("released", self.seen[2])

    def test_an_expiry_is_reported_once_however_often_it_is_read(self):
        self.lease.take("discretion", 60)
        self.clock.t += 61
        for _ in range(5):
            self.lease.active()
            self.lease.state()
            self.lease.blocks("topaz")
        self.assertEqual(len([m for m in self.seen if "expired" in m]), 1)

    def test_whichever_read_notices_the_expiry_first_reports_it(self):
        for read in (lambda l: l.active(), lambda l: l.state(), lambda l: l.blocks("remux")):
            seen = []
            lease = yield_lease.YieldLease(gate=TRUST, now=Clock(), on_change=seen.append)
            lease.take("discretion", 60)
            lease._now = Clock(1061.0)
            read(lease)
            self.assertEqual(len([m for m in seen if "expired" in m]), 1, read)

    def test_a_refusal_is_reported_so_a_fight_over_the_machine_is_visible(self):
        self.lease.take("discretion", 600)
        self.lease.take("someone-else", 600)
        self.assertTrue(any("refused" in m for m in self.seen))

    def test_a_reporting_callback_that_raises_never_breaks_the_lease(self):
        def boom(_m):
            raise RuntimeError("the log is not the point")
        lease = yield_lease.YieldLease(gate=TRUST, now=Clock(), on_change=boom)
        self.assertTrue(lease.take("discretion", 600)[0])
        self.assertTrue(lease.active())
        self.assertTrue(lease.release("discretion")[0])


class ARenewalNeverShortens(unittest.TestCase):
    """One holder, two tasks. Discretion runs everything as `discretion`, so a short take from a
    second task lands on the FIRST task's lease: live 2026-09-24, a 900 s "Pass 1 on a.mkv" cut a
    3600 s detection lease down to 900 s two minutes after it was granted. A renewal may only ever
    move the expiry later — the TTL is a ceiling on how long a crashed holder can wedge the queue,
    and nobody asking for the machine should be able to shorten somebody else's hold on it."""

    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(gate=TRUST, now=self.clock)

    def test_a_shorter_renewal_leaves_the_longer_expiry_alone(self):
        self.lease.take("discretion", 3600, "Pass 1 detection")
        self.clock.t += 120
        ok, _d = self.lease.take("discretion", 900, "Pass 1 on a.mkv")
        self.assertTrue(ok)
        self.assertEqual(self.lease.state()["seconds_left"], 3480)   # 3600 - 120, not 900

    def test_a_longer_renewal_still_extends(self):
        self.lease.take("discretion", 900)
        self.clock.t += 60
        self.lease.take("discretion", 3600)
        self.assertEqual(self.lease.state()["seconds_left"], 3600)

    def test_the_detail_reports_what_the_holder_actually_has(self):
        self.lease.take("discretion", 3600)
        self.clock.t += 120
        _ok, detail = self.lease.take("discretion", 900)
        self.assertIn("3480", detail)          # not "900" — that would be a lie

    def test_a_renewal_after_a_lapse_is_a_new_lease_at_its_own_length(self):
        self.lease.take("discretion", 3600)
        self.clock.t += 3601
        self.lease.take("discretion", 900)
        self.assertEqual(self.lease.state()["seconds_left"], 900)


class FakeGate(object):
    def __init__(self, verdict=(True, "", 4242, False), check=(True, "", False)):
        self.verdict, self.check, self.peers, self.owners = verdict, check, [], []
    def verify_request(self, peer):
        self.peers.append(peer)
        v = self.verdict
        if isinstance(v, Exception):
            raise v
        return v
    def check_engine(self, owner):
        self.owners.append(owner)
        c = self.check
        if isinstance(c, Exception):
            raise c
        return c

    sender = (4242, True)
    def identify(self, peer):
        return self.sender


class GatedLease(unittest.TestCase):
    """User rule 2026-10-06: Expurgate gets the machine only while it is open and activated, and the
    rule cannot be bypassed — an orphaned training script held Visionary off all day by saying
    "discretion" while Expurgate was not even running."""

    def setUp(self):
        self.clock = Clock()
        self.seen = []
        self.gate = FakeGate()
        self.lease = yield_lease.YieldLease(now=self.clock, gate=self.gate,
                                            on_change=self.seen.append)

    def test_the_production_default_is_the_real_gate(self):
        import expurgate_gate
        self.assertIs(yield_lease.YieldLease()._gate, expurgate_gate)
        import orchestrator
        self.assertIs(orchestrator.Orchestrator()._yield_lease._gate, expurgate_gate)

    def test_a_refused_request_gets_nothing_whatever_its_holder_says(self):
        self.gate.verdict = (False, "Expurgate is not open", None, True)
        ok, why = self.lease.take("discretion", 900, "Expurgate: overnight training harvest",
                                  peer=("127.0.0.1", 50123))
        self.assertFalse(ok)
        self.assertEqual(why, "Expurgate is not open")
        self.assertFalse(self.lease.state()["held"])
        self.assertEqual(self.gate.peers, [("127.0.0.1", 50123)])
        self.assertEqual(self.seen, ["discretion refused: Expurgate is not open"])

    def test_every_renewal_is_checked_too(self):
        self.assertTrue(self.lease.take("discretion", 900, peer=("127.0.0.1", 1))[0])
        self.gate.verdict = (False, "Expurgate is open but not activated", 4242, True)
        self.assertFalse(self.lease.take("discretion", 900, peer=("127.0.0.1", 2))[0])
        self.assertEqual(len(self.gate.peers), 2)

    def test_a_gate_that_errors_grants_nothing(self):
        self.gate.verdict = RuntimeError("lsof is gone")
        ok, why = self.lease.take("discretion", 900, peer=("127.0.0.1", 1))
        self.assertFalse(ok)
        self.assertIn("check failed", why)

    def test_a_repeated_refusal_is_logged_once_per_window(self):
        self.gate.verdict = (False, "Expurgate is not open", None, True)
        for _ in range(5):
            self.lease.take("discretion", 900)
        self.assertEqual(len(self.seen), 1)
        self.clock.t += yield_lease.REFUSAL_LOG_SECS + 1
        self.lease.take("discretion", 900)
        self.assertEqual(len(self.seen), 2)

    def test_a_held_lease_is_revoked_the_moment_expurgate_closes(self):
        self.lease.take("discretion", 3600)
        self.gate.check = (False, "Expurgate is not open", True)
        self.assertFalse(self.lease.guard_tick())
        self.assertFalse(self.lease.state()["held"])
        self.assertIn("revoked", self.seen[-1])
        self.assertIn("Expurgate is not open", self.seen[-1])
        self.assertEqual(self.gate.owners, [4242])        # the engine verified at take time

    def test_deactivating_expurgate_revokes_too(self):
        self.lease.take("discretion", 3600)
        self.gate.check = (False, "Expurgate is open but not activated", True)
        self.lease.guard_tick()
        self.assertFalse(self.lease.state()["held"])

    def test_one_unanswered_check_is_tolerated_two_are_not(self):
        self.lease.take("discretion", 3600)
        self.gate.check = (False, "Expurgate did not say whether it is activated", False)
        self.assertTrue(self.lease.guard_tick())
        self.assertTrue(self.lease.state()["held"])
        self.assertFalse(self.lease.guard_tick())
        self.assertFalse(self.lease.state()["held"])

    def test_an_answer_resets_the_miss_count(self):
        self.lease.take("discretion", 3600)
        self.gate.check = (False, "no answer", False)
        self.lease.guard_tick()
        self.gate.check = (True, "", False)
        self.lease.guard_tick()
        self.gate.check = (False, "no answer", False)
        self.assertTrue(self.lease.guard_tick())          # one miss again, not two

    def test_a_check_that_errors_counts_as_no_answer(self):
        self.lease.take("discretion", 3600)
        self.gate.check = RuntimeError("boom")
        self.assertTrue(self.lease.guard_tick())
        self.assertFalse(self.lease.guard_tick())

    def test_a_restarted_expurgate_gets_a_new_lease_not_the_old_one(self):
        self.lease.take("discretion", 3600)
        old = self.lease.state()["id"]
        self.gate.verdict = (True, "", 9999, False)       # a new engine pid
        self.lease.take("discretion", 900)
        self.assertNotEqual(self.lease.state()["id"], old)

    def test_the_guard_starts_once_and_stops_when_nothing_is_held(self):
        spawned, sleeps = [], []
        lease = yield_lease.YieldLease(now=self.clock, gate=self.gate,
                                       spawn=spawned.append, sleep=sleeps.append)
        lease.take("discretion", 3600)
        lease.take("discretion", 3600)                    # a renewal: no second guard
        self.assertEqual(len(spawned), 1)
        self.gate.check = (False, "Expurgate is not open", True)
        spawned[0]()                                      # run the loop: one check, revoke, exit
        self.assertEqual(sleeps, [yield_lease.GUARD_SECS])
        self.assertFalse(lease._guarding)
        lease.take("discretion", 3600)                    # a later lease gets a guard again
        self.assertEqual(len(spawned), 2)

    def test_a_renewal_survives_one_check_that_could_not_complete(self):
        self.lease.take("discretion", 900)
        self.gate.verdict = (False, "Expurgate did not say whether it is activated", 4242, False)
        self.clock.t += 400
        self.assertTrue(self.lease.take("discretion", 900)[0])        # the same engine, one miss
        self.assertFalse(self.lease.take("discretion", 900)[0])       # two in a row: no
        self.gate.verdict = (True, "", 4242, False)
        self.assertTrue(self.lease.take("discretion", 900)[0])        # an answer resets it

    def test_no_such_allowance_for_a_hard_no_or_an_unknown_program(self):
        self.lease.take("discretion", 900)
        self.gate.verdict = (False, "Expurgate is open but not activated", 4242, True)
        self.assertFalse(self.lease.take("discretion", 900)[0])
        self.gate.verdict = (False, "could not identify the program", None, False)
        self.assertFalse(self.lease.take("discretion", 900)[0])
        self.gate.verdict = (False, "no answer", 5555, False)         # a DIFFERENT engine
        self.assertFalse(self.lease.take("discretion", 900)[0])

    def test_nor_for_a_first_take(self):
        self.gate.verdict = (False, "Expurgate did not say whether it is activated", 4242, False)
        self.assertFalse(self.lease.take("discretion", 900)[0])

    def test_a_guard_that_cannot_start_never_fails_the_take_and_is_retried(self):
        calls = []
        def spawn(fn):
            calls.append(fn)
            if len(calls) == 1:
                raise RuntimeError("can't start new thread")
        lease = yield_lease.YieldLease(now=self.clock, gate=self.gate, spawn=spawn)
        self.assertTrue(lease.take("discretion", 900)[0])
        self.assertFalse(lease._guarding)
        lease.take("discretion", 900)
        self.assertEqual(len(calls), 2)

    def test_the_refusal_book_stays_small_whatever_callers_call_themselves(self):
        self.gate.verdict = (False, "Expurgate is not open", None, True)
        for i in range(500):
            self.lease.take("holder-%d" % i, 900)
            self.clock.t += yield_lease.REFUSAL_LOG_SECS / 50.0
        self.assertLessEqual(len(self.lease._refused_at), 64)

    def test_its_own_engine_releases_it(self):
        self.lease.take("discretion", 3600)
        self.gate.sender = (4242, True)
        self.assertTrue(self.lease.release("discretion", peer=("c", "l"))[0])
        self.assertFalse(self.lease.state()["held"])

    def test_another_program_cannot_release_expurgates_lease(self):
        # harvest.py "released discretion" after every scan: that must not end the real lease
        self.lease.take("discretion", 3600)
        self.gate.sender = (47649, True)
        ok, why = self.lease.release("discretion", peer=("c", "l"))
        self.assertFalse(ok)
        self.assertTrue(self.lease.state()["held"])
        self.assertIn("another program", self.seen[-1])
        self.gate.sender = (None, True)                   # from another device
        self.assertFalse(self.lease.release("discretion", peer=("c", "l"))[0])

    def test_an_unidentifiable_sender_errs_toward_releasing(self):
        self.lease.take("discretion", 3600)
        self.gate.sender = (None, False)
        self.assertTrue(self.lease.release("discretion", peer=("c", "l"))[0])

    def test_a_soft_renewal_never_revives_a_lease_revoked_in_the_meantime(self):
        self.lease.take("discretion", 900)
        lease, gate = self.lease, self.gate
        def soft_after_a_revoke(peer):
            gate.check = (False, "Expurgate is open but not activated", True)
            lease.guard_tick()                            # the guard revokes mid-request
            return False, "Expurgate did not say", 4242, False
        gate.verify_request = soft_after_a_revoke
        ok, _ = lease.take("discretion", 900)
        self.assertFalse(ok)
        self.assertFalse(lease.state()["held"])

    def test_a_leaving_guard_never_clears_a_newer_guards_flag(self):
        spawned = []
        lease = yield_lease.YieldLease(now=self.clock, gate=self.gate, spawn=spawned.append,
                                       sleep=lambda s: None)
        lease.take("discretion", 900)
        old = spawned[0]
        lease.release("discretion")                       # the old guard will find nothing...
        lease.take("discretion", 900)                     # ...but a new lease got its own guard?
        with lease._lock:
            lease._guarding = True                        # (simulate: the new take armed a guard)
            lease._guard_gen += 1
        lease.release("discretion")
        old()                                             # the OLD guard exits now
        self.assertTrue(lease._guarding)                  # the newer guard's flag stands

    def test_a_guard_that_decides_to_leave_never_strands_a_new_lease(self):
        spawned = []
        lease = yield_lease.YieldLease(now=self.clock, gate=self.gate, spawn=spawned.append,
                                       sleep=lambda s: None)
        lease.take("discretion", 900)
        lease.release("discretion")
        spawned[0]()                                      # the guard finds nothing and leaves...
        self.assertFalse(lease._guarding)                 # ...clearing its flag as it decides
        lease.take("discretion", 900)                     # so the next lease arms a guard
        self.assertEqual(len(spawned), 2)

    def test_a_release_without_a_connection_is_an_internal_one(self):
        self.lease.take("discretion", 3600)
        self.gate.sender = (47649, True)
        self.assertTrue(self.lease.release("discretion")[0])

if __name__ == "__main__":
    unittest.main()
