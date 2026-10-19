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

    def test_no_speedup_is_predicted_while_outpainting_has_the_machine(self):
        # AI outpainting PREEMPTS the lane (SIGSTOP), so its next regime is rate ZERO, not a
        # speed-up. Predicting one would be wrong in the dangerous direction.
        self.assertFalse(eta.correction_applies(run_stage="extend", run_active=True))
        self.assertTrue(eta.correction_applies(run_stage="topaz", run_active=True))
        self.assertTrue(eta.correction_applies(run_stage="dvrender", run_active=True))
        self.assertTrue(eta.correction_applies(run_stage="extend", run_active=False))


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


class NativeAnalysisRegime(unittest.TestCase):
    """The native DV analysis ('dvnative') runs on the CPU at nice 10 beside a remux's x265 —
    where Topaz sits — and then the run thread parks at the native gate until that remux ends."""

    def test_a_live_analysis_is_contention(self):
        self.assertEqual(eta.regime_of(run_stage="dvnative", run_active=True), eta.CONTENDED)

    def test_a_STALE_dvnative_stage_is_not(self):
        self.assertEqual(eta.regime_of(run_stage="dvnative", run_active=False), eta.SOLO)

    def test_it_never_preempts_the_lane_so_the_correction_applies(self):
        self.assertTrue(eta.correction_applies(run_stage="dvnative", run_active=True))

    def test_every_existing_stage_keeps_its_regime(self):
        # the stage list as it was before the native lane, run against the live function for
        # every input it can see.
        def before(run_stage, run_active, other_lane_live):
            if other_lane_live:
                return eta.CONTENDED
            if run_active and run_stage in ("download", "extend", "topaz"):
                return eta.CONTENDED
            return eta.SOLO
        for st in (None, "", "download", "extend", "topaz", "dv", "remux", "upload",
                   "cleanup", "idle"):
            for active in (True, False):
                for other in (True, False):
                    self.assertEqual(eta.regime_of(run_stage=st, run_active=active,
                                                   other_lane_live=other),
                                     before(st, active, other), (st, active, other))
                self.assertEqual(eta.correction_applies(run_stage=st, run_active=active),
                                 not (active and st == "extend"), (st, active))


class ChunkReplay(unittest.TestCase):
    """A resume re-fires one boundary per chunk already measured, about a second apart."""

    def test_a_replayed_chunk_is_recognised(self):
        self.assertTrue(eta.chunk_is_replay(1 / 6, 0.2))       # a sidecar reloaded
        self.assertTrue(eta.chunk_is_replay(0.01, 3.0))        # under CHUNK_REPLAY_SECS
        self.assertTrue(eta.chunk_is_replay(0.5, 20.0))        # the whole item in 40 s

    def test_real_analysis_is_not(self):
        self.assertFalse(eta.chunk_is_replay(1 / 6, 240.0))    # a 7,058-frame chunk in 4 min
        self.assertFalse(eta.chunk_is_replay(0.005, 10.0))     # a tiny last chunk, genuinely run

    def test_junk_is_never_banked(self):
        for w, t in ((0, 100), (-0.1, 100), (0.1, 0), (0.1, -5), (None, 100), ("x", 100),
                     (0.1, None)):
            self.assertTrue(eta.chunk_is_replay(w, t), (w, t))


class NativeChunkClock(unittest.TestCase):
    """The 'dvnative' ETA, from chunk boundaries — the only progress that stage publishes."""

    QUARTERS = [0.25, 0.5, 0.75, 1.0]

    def test_nothing_until_a_live_chunk_lands(self):
        c = eta.ChunkClock()
        self.assertFalse(c.observe(0, self.QUARTERS, 0.0))
        self.assertEqual(c.estimate(100.0), {})

    def test_eta_from_measured_chunks(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        self.assertTrue(c.observe(1, self.QUARTERS, 300.0))   # one chunk = 300 s
        e = c.estimate(300.0)
        self.assertAlmostEqual(e["eta_secs"], 900.0, delta=0.1)       # three chunks left
        self.assertAlmostEqual(e["seg_secs"], 300.0, delta=0.1)
        self.assertAlmostEqual(e["seg_eta_secs"], 300.0, delta=0.1)

    def test_it_counts_down_between_boundaries(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        e = c.estimate(400.0)
        self.assertAlmostEqual(e["eta_secs"], 800.0, delta=0.1)
        self.assertAlmostEqual(e["seg_eta_secs"], 200.0, delta=0.1)
        self.assertAlmostEqual(e["seg_secs"], 300.0, delta=0.1)        # the projection holds

    def test_the_rate_is_frames_weighted(self):
        # A 10% chunk in 100 s, then a 40% one in 200 s: 50% in 300 s, so the last 50% takes
        # 300 s. Averaging the two chunks' rates instead says 333 s.
        c = eta.ChunkClock()
        n = [0.1, 0.5, 1.0]
        c.observe(0, n, 0.0)
        c.observe(1, n, 100.0)
        c.observe(2, n, 300.0)
        self.assertAlmostEqual(c.estimate(300.0)["eta_secs"], 300.0, delta=0.1)

    def test_the_resume_replay_is_not_banked(self):
        # Three chunks were measured before a relaunch; the resume replays them in ~0.3 s.
        # Banking that would say the last three take ~0.3 s x 1 — the stage ETA would collapse.
        c = eta.ChunkClock()
        n = [i / 6 for i in range(1, 7)]
        c.observe(0, n, 0.0)
        for k, t in ((1, 0.1), (2, 0.2), (3, 0.3)):
            self.assertFalse(c.observe(k, n, t))
        self.assertEqual(c.estimate(0.3), {})                  # nothing measured yet
        self.assertTrue(c.observe(4, n, 240.3))                # the first live chunk: 240 s
        self.assertAlmostEqual(c.estimate(240.3)["eta_secs"], 480.0, delta=0.5)

    def test_a_new_attempt_keeps_what_was_measured(self):
        # A pause (or a relaunch) restarts the stage at seg_done 0; the chunks and the machine
        # are the same, so the rate is still the best evidence there is.
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        self.assertFalse(c.observe(0, self.QUARTERS, 5000.0))  # the next attempt starts
        self.assertFalse(c.observe(1, self.QUARTERS, 5000.2))  # chunk 1 replayed
        e = c.estimate(5000.2)
        self.assertAlmostEqual(e["eta_secs"], 900.0, delta=0.5)

    def test_a_different_item_starts_over(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        c.observe(0, [0.5, 1.0], 400.0)
        self.assertEqual(c.estimate(400.0), {})

    def test_a_republished_boundary_changes_nothing(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        self.assertFalse(c.observe(1, self.QUARTERS, 350.0))
        self.assertAlmostEqual(c.estimate(350.0)["seg_eta_secs"], 250.0, delta=0.1)

    def test_after_the_last_chunk_there_is_nothing_to_count(self):
        # Merging and the RPU take seconds; no countdown is better than a wrong one.
        c = eta.ChunkClock()
        n = [0.5, 1.0]
        c.observe(0, n, 0.0)
        c.observe(1, n, 300.0)
        c.observe(2, n, 600.0)
        self.assertEqual(c.estimate(600.0), {})

    def test_an_overrunning_chunk_never_reads_zero(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        e = c.estimate(5000.0)
        self.assertEqual(e["seg_eta_secs"], eta.TAIL_MIN_SECS)
        self.assertAlmostEqual(e["eta_secs"], eta.TAIL_MIN_SECS + 600.0, delta=0.1)

    def test_junk_progress_is_ignored(self):
        c = eta.ChunkClock()
        c.observe(0, self.QUARTERS, 0.0)
        c.observe(1, self.QUARTERS, 300.0)
        before = c.estimate(300.0)
        for args in ((None, self.QUARTERS, 400.0), ("x", self.QUARTERS, 400.0),
                     (2, None, 400.0), (2, [], 400.0), (5, self.QUARTERS, 400.0),
                     (-1, self.QUARTERS, 400.0), (2, [0.5, 0.25, 1.0], 400.0),
                     (2, self.QUARTERS, None)):
            self.assertFalse(c.observe(*args), args)
        self.assertEqual(c.estimate(300.0), before)

    def test_a_live_episode_end_to_end(self):
        # S06E05-shaped: six ~7,058-frame chunks at ~4 min each, a relaunch after chunk 3.
        n = [round(k * 7058 / 42348, 4) for k in range(1, 7)]
        c, t = eta.ChunkClock(), 0.0
        c.observe(0, n, t)
        for k in (1, 2, 3):
            t += 240.0
            c.observe(k, n, t)
            self.assertAlmostEqual(c.estimate(t)["eta_secs"], (6 - k) * 240.0, delta=2.0)
        t += 3600.0                                            # relaunched an hour later
        c.observe(0, n, t)
        for k in (1, 2, 3):
            t += 0.15
            c.observe(k, n, t)
        self.assertAlmostEqual(c.estimate(t)["eta_secs"], 3 * 240.0, delta=2.0)
        for k in (4, 5):
            t += 240.0
            c.observe(k, n, t)
            self.assertAlmostEqual(c.estimate(t)["eta_secs"], (6 - k) * 240.0, delta=2.0)


class NativeRenderRegime(unittest.TestCase):
    """The native render ('dvrender') runs its analysis and LUT encode at nice 10 beside a
    remux's x265 and never suspends it: ordinary contention, like 'dvnative'."""

    def test_a_live_render_is_contention_and_the_correction_applies(self):
        self.assertEqual(eta.regime_of(run_stage="dvrender", run_active=True), eta.CONTENDED)
        self.assertEqual(eta.regime_of(run_stage="dvrender", run_active=False), eta.SOLO)
        self.assertTrue(eta.correction_applies(run_stage="dvrender", run_active=True))

    def test_every_other_stage_keeps_its_regime(self):
        def before(run_stage, run_active, other_lane_live):
            if other_lane_live:
                return eta.CONTENDED
            if run_active and run_stage in ("download", "extend", "topaz", "dvnative"):
                return eta.CONTENDED
            return eta.SOLO
        for st in (None, "", "download", "extend", "topaz", "dvnative", "resolve", "remux",
                   "upload", "cleanup", "idle"):
            for active in (True, False):
                for other in (True, False):
                    self.assertEqual(eta.regime_of(run_stage=st, run_active=active,
                                                   other_lane_live=other),
                                     before(st, active, other), (st, active, other))


class NativeRenderClock(unittest.TestCase):
    """The 'dvrender' ETA: three phases on one bar, each timed on its own."""

    F = 6000                                    # the item's frames

    def _a(self, frac, done=None):
        return {"stage": "dvrender", "phase": "analysis", "pct": 50.0 * frac,
                "seg_done": done, "frames": self.F}

    def _e(self, frac, attempt=1):
        return {"stage": "dvrender", "phase": "encode", "pct": 50.0 + 45.0 * frac,
                "attempt": attempt, "frames": self.F}

    def test_the_spans_are_nativerenders(self):
        import nativerender
        self.assertEqual({k: tuple(map(float, v)) for k, v in nativerender.PHASE_SPAN.items()},
                         eta.RENDER_SPANS)

    def test_nothing_until_a_live_range_lands(self):
        c = eta.RenderClock()
        self.assertEqual(c.estimate(0.0), {})
        self.assertFalse(c.observe(self._a(0.0, 0), 0.0))
        self.assertEqual(c.estimate(100.0), {})

    def test_analysis_eta_adds_the_encode_prior_and_the_finish(self):
        c = eta.RenderClock()
        c.observe(self._a(0.0, 0), 0.0)
        self.assertTrue(c.observe(self._a(0.25, 1), 300.0))      # a quarter in 300 s
        enc = self.F / eta.RENDER_ENCODE_PRIOR_FPS                # 200 s at the prior
        fin = eta.render_finish_secs(self.F)
        self.assertAlmostEqual(c.estimate(300.0)["eta_secs"], 900.0 + enc + fin, delta=0.2)
        self.assertAlmostEqual(c.estimate(400.0)["eta_secs"], 800.0 + enc + fin, delta=0.2)

    def test_a_resume_replay_is_not_banked(self):
        c = eta.RenderClock()
        c.observe(self._a(0.0, 0), 0.0)
        c.observe(self._a(0.25, 1), 300.0)                       # measured: 0.25 / 300 s
        c.observe(self._a(0.0, 0), 5000.0)                       # a new attempt, then replays
        self.assertFalse(c.observe(self._a(0.25, 1), 5000.3))
        self.assertFalse(c.observe(self._a(0.5, 2), 5000.6))
        self.assertTrue(c.observe(self._a(0.75, 3), 5300.6))     # a live range at the same rate
        fin = eta.render_finish_secs(self.F)
        enc = self.F / eta.RENDER_ENCODE_PRIOR_FPS
        self.assertAlmostEqual(c.estimate(5300.6)["eta_secs"], 300.0 + enc + fin, delta=0.2)

    def test_the_encode_uses_its_own_rate_once_timed(self):
        c = eta.RenderClock()
        c.observe(self._e(0.0), 0.0)
        fin = eta.render_finish_secs(self.F)
        # under MIN_RENDER_ENCODE_SECONDS: still the prior (6000 frames at 30 fps = 200 s)
        c.observe(self._e(0.01), 2.0)
        self.assertAlmostEqual(c.estimate(2.0)["eta_secs"], 0.99 * 200.0 + fin, delta=0.2)
        c.observe(self._e(0.1), 20.0)                             # 10% in 20 s: 200 s per encode
        c.observe(self._e(0.5), 100.0)
        self.assertAlmostEqual(c.estimate(100.0)["eta_secs"], 100.0 + fin, delta=0.2)

    def test_a_re_encode_restarts_the_phase_but_keeps_the_rate(self):
        c = eta.RenderClock()
        c.observe(self._e(0.0), 0.0)
        c.observe(self._e(0.5), 100.0)                            # 200 s per encode
        c.observe(self._e(1.0), 200.0)
        self.assertFalse(c.observe(self._e(0.0, attempt=2), 260.0))   # the peak loop's 2nd throw
        fin = eta.render_finish_secs(self.F)
        self.assertAlmostEqual(c.estimate(260.0)["eta_secs"], 200.0 + fin, delta=0.2)

    def test_a_hold_is_not_encoding(self):
        c = eta.RenderClock()
        c.observe(self._e(0.0), 0.0)
        self.assertFalse(c.observe(self._e(0.1), eta.MAX_IDLE_TICK + 50.0))
        self.assertTrue(c.observe(self._e(0.2), eta.MAX_IDLE_TICK + 70.0))

    def test_the_finish_counts_down_to_the_floor(self):
        c = eta.RenderClock()
        c.observe({"phase": "finish", "pct": 95.0, "frames": 600000}, 0.0)
        self.assertAlmostEqual(c.estimate(0.0)["eta_secs"], eta.render_finish_secs(600000), delta=0.2)
        self.assertEqual(c.estimate(10 ** 6)["eta_secs"], eta.TAIL_MIN_SECS)

    def test_another_item_starts_over_and_junk_is_ignored(self):
        c = eta.RenderClock()
        c.observe(self._a(0.0, 0), 0.0)
        c.observe(self._a(0.25, 1), 300.0)
        other = dict(self._a(0.0, 0), frames=999)
        c.observe(other, 400.0)
        self.assertEqual(c.estimate(400.0), {})                   # nothing measured for it yet
        for junk in ({}, {"phase": "x", "pct": 1}, {"phase": "analysis", "pct": None},
                     {"phase": "analysis", "pct": "x"}, None):
            self.assertFalse(c.observe(junk, 500.0), repr(junk))
