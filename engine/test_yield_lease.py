"""The yield lease. Its TTL is a safety property, so the expiry behaviour is tested directly."""

import unittest

import yield_lease


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TakeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(now=self.clock)

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
        self.lease = yield_lease.YieldLease(now=self.clock)

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
        self.lease = yield_lease.YieldLease(now=Clock())
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
        lease = yield_lease.YieldLease(now=clock)
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
        lease = yield_lease.YieldLease(now=clock)
        lease.take("discretion", 60)
        clock.t += 61
        self.assertFalse(lease.state()["held"])
        self.assertIsNone(lease.state()["holder"])

    def test_no_lease_reports_as_free(self):
        self.assertFalse(yield_lease.YieldLease().state()["held"])


class LeaseIdentityTests(unittest.TestCase):
    """A holder cannot tell "my lease lapsed" from "I never had one" unless the lease has a name.

    The lease lives only in the server process, and the process is replaced by every deploy, quit
    or relaunch: twice on 2026-09-23 a sibling read back not-held within a minute of a successful
    take, and nothing on either side recorded which of those it was. An id makes the loss visible
    to the holder, whatever caused it: a restart, an expiry, a release, or a takeover."""

    def setUp(self):
        self.clock = Clock()
        self.lease = yield_lease.YieldLease(now=self.clock)

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
        a = yield_lease.YieldLease(now=Clock())
        b = yield_lease.YieldLease(now=Clock())
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
        self.lease = yield_lease.YieldLease(now=self.clock, on_change=self.seen.append)

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
            lease = yield_lease.YieldLease(now=Clock(), on_change=seen.append)
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
        lease = yield_lease.YieldLease(now=Clock(), on_change=boom)
        self.assertTrue(lease.take("discretion", 600)[0])
        self.assertTrue(lease.active())
        self.assertTrue(lease.release("discretion")[0])


if __name__ == "__main__":
    unittest.main()
