import math
import unittest

import eta


class AcceptSample(unittest.TestCase):
    """The outlier gate. Its job is to keep the resume REPLAY BURST out of the rate window."""

    def test_a_normal_encode_interval_is_accepted(self):
        self.assertTrue(eta.accept_sample(94, 30.0))       # ~3.1 fps, the measured real rate

    def test_the_replay_burst_is_rejected(self):
        # On restart, dvcap re-fires one callback per already-finished segment ~1 s apart, so
        # frames leap by thousands per second. Banking that produced an ETA ~45x too low.
        self.assertFalse(eta.accept_sample(3344, 1.0))     # ~3344 fps
        self.assertFalse(eta.accept_sample(60, 1.0))       # 60 fps — still impossible at 4K10

    def test_a_restart_going_backwards_is_rejected(self):
        self.assertFalse(eta.accept_sample(-500, 30.0))

    def test_a_stopped_or_backwards_clock_is_rejected(self):
        self.assertFalse(eta.accept_sample(10, 0.0))
        self.assertFalse(eta.accept_sample(10, -5.0))

    def test_a_short_idle_tick_counts_but_a_long_hold_does_not(self):
        self.assertTrue(eta.accept_sample(0, 5.0))         # briefly between frames
        self.assertFalse(eta.accept_sample(0, 600.0))      # a hold — not evidence of a slow encode


class RateEstimator(unittest.TestCase):
    def test_rate_is_total_over_total(self):
        b = eta.RateBook()
        for _ in range(20):
            b.tick(eta.CONTENDED, 90, 30.0)
        self.assertAlmostEqual(b.rate(eta.CONTENDED), 3.0, places=6)

    def test_no_rate_until_the_regime_has_been_seen_enough(self):
        b = eta.RateBook()
        b.tick(eta.CONTENDED, 90, 30.0)                    # one tick is not a measurement
        self.assertIsNone(b.rate(eta.CONTENDED))

    def test_regimes_never_blend(self):
        # The whole point of per-regime buckets: a solo burst must not inflate the contended rate.
        b = eta.RateBook()
        for _ in range(20):
            b.tick(eta.CONTENDED, 90, 30.0)                # 3 fps
            b.tick(eta.SOLO, 180, 30.0)                    # 6 fps
        self.assertAlmostEqual(b.rate(eta.CONTENDED), 3.0, places=6)
        self.assertAlmostEqual(b.rate(eta.SOLO), 6.0, places=6)
        self.assertAlmostEqual(b.k_observed(), 2.0, places=6)

    def test_JENSEN_the_estimator_must_not_be_optimistic(self):
        # THE regression that pins the estimator's form. Half the frames at 2 fps and half at
        # 6 fps: the honest answer for "how long will N frames take" is the HARMONIC mean, 3.0.
        # Averaging fps per-frame gives 4.0 -> a permanent 33% underestimate at every tick.
        b = eta.RateBook()
        # Equal FRAMES at each rate is what makes the harmonic mean the right answer. Interval
        # lengths kept under MAX_IDLE_TICK — an interval longer than that is treated as
        # containing a hold and is dropped, which is a different rule being tested elsewhere.
        frames = 200
        b.tick(eta.CONTENDED, frames, frames / 2.0)        # 200 frames at 2 fps = 100 s
        b.tick(eta.CONTENDED, frames, frames / 6.0)        # 200 frames at 6 fps = 33.3 s
        self.assertAlmostEqual(b.rate(eta.CONTENDED), 3.0, places=6)
        self.assertNotAlmostEqual(b.rate(eta.CONTENDED), 4.0, places=1)


class TwoPhase(unittest.TestCase):
    def test_k_of_one_reduces_to_todays_formula(self):
        # The compatibility guarantee: with no measured speed-up, the estimate is unchanged.
        self.assertAlmostEqual(eta.two_phase_eta(17782, 5800, 1.0), 17782)

    def test_the_job_that_finishes_first_is_left_alone(self):
        # Its contended ETA is unbiased -- contention lasts its whole remaining life.
        self.assertAlmostEqual(eta.two_phase_eta(4000, 9000, 2.0), 4000)

    def test_the_longer_job_gets_its_excess_corrected(self):
        # Live numbers: E_r = 17782 s contended, topaz ends in 5800 s, k = 1.5
        # 5800 + (17782-5800)/1.5 = 13788 s (3h50m) vs a displayed 4h56m
        self.assertAlmostEqual(eta.two_phase_eta(17782, 5800, 1.5), 13788.0, delta=1.0)

    def test_it_is_continuous_across_the_crossing(self):
        # No visible jump as the other job's ETA passes ours.
        k, e = 2.0, 10_000.0
        a = eta.two_phase_eta(e, e - 1, k)
        b = eta.two_phase_eta(e, e + 1, k)
        self.assertLess(abs(a - b), 1.0)

    def test_with_no_other_job_it_is_unchanged(self):
        self.assertAlmostEqual(eta.two_phase_eta(9000, None, 2.0), 9000)
        self.assertAlmostEqual(eta.two_phase_eta(9000, 0, 2.0), 9000)

    def test_it_can_never_exceed_todays_estimate(self):
        # The "never worse" property, swept. k >= 1 is physically guaranteed (removing a load
        # cannot slow x265), so today's ETA is an upper bound on the truth.
        for e_self in (100, 5_000, 20_000):
            for e_other in (1, 500, 4_999, 20_001):
                for k in (1.0, 1.5, 2.0, 2.5):
                    self.assertLessEqual(eta.two_phase_eta(e_self, e_other, k), e_self + 1e-9)

    def test_it_is_monotone_in_our_own_remaining_work(self):
        # More work left can never predict an earlier finish -- so the race order can't flip.
        prev = -1.0
        for e_self in range(1000, 30_000, 500):
            v = eta.two_phase_eta(e_self, 5800, 1.8)
            self.assertGreater(v, prev)
            prev = v


class LearningK(unittest.TestCase):
    def test_cold_start_is_the_prior(self):
        self.assertAlmostEqual(eta.shrink_k([]), eta.K_PRIOR)

    def test_one_sample_moves_it_only_part_way(self):
        k = eta.shrink_k([2.0])
        self.assertGreater(k, eta.K_PRIOR)
        self.assertLess(k, 2.0)
        self.assertAlmostEqual(k, 1.61, delta=0.02)

    def test_many_samples_converge_on_the_truth(self):
        # Never all the way: at n=30 the prior still carries n0/(n+n0) = 3/33 ≈ 9% of the weight,
        # which lands on 1.948. That residual pull is the point of shrinkage, not a defect.
        self.assertAlmostEqual(eta.shrink_k([2.0] * 30), 1.948, delta=0.01)
        self.assertGreater(eta.shrink_k([2.0] * 30), eta.shrink_k([2.0] * 5))   # and it keeps closing

    def test_the_median_resists_one_bad_sample(self):
        # Content complexity can skew a single crossing badly; the median absorbs it.
        clean = eta.shrink_k([1.8] * 9)
        dirty = eta.shrink_k([1.8] * 9 + [9.0])
        self.assertLess(abs(clean - dirty), 0.15)

    def test_it_is_clamped_both_ways(self):
        self.assertEqual(eta.shrink_k([50.0] * 50), eta.K_MAX)
        self.assertEqual(eta.shrink_k([0.2] * 50), eta.K_MIN)

    def test_junk_never_escapes_the_clamp(self):
        self.assertEqual(eta.clamp_k("nonsense"), eta.K_PRIOR)
        self.assertEqual(eta.clamp_k(None), eta.K_PRIOR)
        self.assertEqual(eta.clamp_k(99), eta.K_MAX)


class Tail(unittest.TestCase):
    """After the last frame the remux still owes concat, mux, verify and a full peak measure —
    all invisible to progress, which is why the countdown used to reach 0 with work left."""

    def test_it_scales_with_the_output(self):
        self.assertAlmostEqual(eta.tail_estimate(67_000), 268.0, delta=1.0)   # ~4.5 min
        self.assertGreater(eta.tail_estimate(150_000), eta.tail_estimate(67_000))

    def test_it_never_reports_zero_while_work_remains(self):
        self.assertGreaterEqual(eta.tail_estimate(0), eta.TAIL_MIN_SECS)
        self.assertGreaterEqual(eta.tail_estimate(None), eta.TAIL_MIN_SECS)
        self.assertGreaterEqual(eta.tail_estimate("junk"), eta.TAIL_MIN_SECS)


class Regimes(unittest.TestCase):
    def test_a_live_topaz_or_download_means_contended(self):
        self.assertEqual(eta.regime_of(run_stage="topaz", run_active=True), eta.CONTENDED)
        self.assertEqual(eta.regime_of(run_stage="download", run_active=True), eta.CONTENDED)

    def test_a_STALE_stage_does_not_count_as_contention(self):
        # `stage` is stale by design -- it still names topaz for the 60 s retry after a failure,
        # and a power hold keeps it set. Only stage_active is truthful.
        self.assertEqual(eta.regime_of(run_stage="topaz", run_active=False), eta.SOLO)

    def test_the_resolve_gate_parking_the_run_thread_is_solo(self):
        self.assertEqual(eta.regime_of(run_stage=None, run_active=False), eta.SOLO)

    def test_no_speedup_is_predicted_while_resolve_has_the_machine(self):
        # Resolve PREEMPTS the lane, so its next regime is rate ZERO, not a speed-up. Predicting
        # one would be wrong in the dangerous direction.
        self.assertFalse(eta.correction_applies(run_stage="resolve", run_active=True))
        self.assertTrue(eta.correction_applies(run_stage="topaz", run_active=True))
        self.assertTrue(eta.correction_applies(run_stage="resolve", run_active=False))


if __name__ == "__main__":
    unittest.main()


class SuspensionPoisoning(unittest.TestCase):
    """A lane SUSPENDED for Resolve stops reporting progress entirely, and the first callback
    after it resumes carries a few frames against the whole suspension — twenty minutes of
    wall clock for a dozen frames. That is a hold with an encoding sample stapled to the end,
    not slow encoding. One such tick measured a healthy 2.50 fps lane at 1.04 (2.4x ETA
    inflation); several Resolve passes compounded it to the 5x seen live."""

    def test_a_hold_spanning_interval_is_rejected_even_with_frames(self):
        self.assertFalse(eta.accept_sample(12, eta.MAX_IDLE_TICK + 1))
        self.assertFalse(eta.accept_sample(12, 1140.0))     # a 19-minute suspension

    def test_it_no_longer_drags_the_measured_rate_down(self):
        b = eta.RateBook()
        for _ in range(200):
            b.tick(eta.SOLO, 10, 4.0)                       # a healthy 2.5 fps
        before = b.rate(eta.SOLO)
        b.tick(eta.SOLO, 12, 1140.0)                        # resume after a suspension
        self.assertAlmostEqual(b.rate(eta.SOLO), before, places=6)

    def test_ordinary_ticks_and_short_idles_still_count(self):
        self.assertTrue(eta.accept_sample(10, 4.0))
        self.assertTrue(eta.accept_sample(0, 30.0))         # a brief gap is still evidence
        self.assertFalse(eta.accept_sample(5000, 1.0))      # the replay burst, still rejected


class TwoLaneContention(unittest.TestCase):
    """Two x265 encodes roughly halve each other. Calling that 'solo' banks the halved rate as
    the lane's BEST case and predicts no speed-up for when the other lane finishes, so the
    estimate stays pessimistic for the whole remainder."""

    def test_the_other_lane_is_contention(self):
        self.assertEqual(eta.regime_of(run_stage=None, run_active=False,
                                       other_lane_live=True), eta.CONTENDED)

    def test_a_lone_lane_on_an_idle_machine_is_solo(self):
        self.assertEqual(eta.regime_of(run_stage=None, run_active=False,
                                       other_lane_live=False), eta.SOLO)

    def test_topaz_still_counts_as_contention(self):
        self.assertEqual(eta.regime_of(run_stage="topaz", run_active=True), eta.CONTENDED)

    def test_the_default_keeps_every_existing_caller_unchanged(self):
        self.assertEqual(eta.regime_of(run_stage="resolve", run_active=True), eta.SOLO)
