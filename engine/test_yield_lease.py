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


if __name__ == "__main__":
    unittest.main()
