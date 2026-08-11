"""The Resolve-stall drain, and the disk gate that was blocking it.

Roughly weekly Resolve shows an "update available" dialog that blocks its screen
automation. The pipeline buffers Topaz output ahead while that lasts, then drains the
backlog with TWO remux lanes. Measured over the entire log, the second lane had never run
once — these tests pin down why, and that it can now.
"""
import os
import tempfile
import unittest
from unittest import mock

import orchestrator as orch


class DrainFloorInvariant(unittest.TestCase):
    def test_the_drain_gate_is_reachable_from_a_full_buffer(self):
        """THE bug, as an invariant. The stall deliberately fills the scratch TO
        STALL_FLOOR_GB, so any drain gate above it can never be satisfied by a full
        buffer — which is exactly how the old 400 GB gate blocked the drain it existed to
        allow. These two constants must never drift apart again."""
        self.assertLessEqual(orch.RESOLVE_DRAIN_MIN_GB, orch.STALL_FLOOR_GB)

    def test_the_drain_gate_still_fits_the_work_it_must_hold(self):
        # A DV render (~18 GiB for a 43-min episode) plus two remux lanes' transients
        # (~20 GiB each) plus margin. Below ~60 and it cannot hold the intended steady state.
        self.assertGreaterEqual(orch.RESOLVE_DRAIN_MIN_GB, 60)


class DrainBacklog(unittest.TestCase):
    """Derived from the DISK, because the in-memory version was destroyed by the very
    recovery gesture the user is told to perform (dismiss the prompt, press Start)."""

    def _with_segdirs(self, n, extra=()):
        d = tempfile.mkdtemp()
        for i in range(n):
            sd = os.path.join(d, f"Show S01E{i:02d}_prob4_upscaled.segments")
            os.makedirs(sd)
            # A segdir is a BUFFER only once it holds real upscaled work — see
            # test_plan_only_segdirs_never_count below.
            open(os.path.join(sd, "seg_0000.mov"), "w").close()
        for name in extra:
            open(os.path.join(d, name), "w").close()
        return d

    def _count(self, d):
        with mock.patch.object(orch.scratch, "default_scratch", return_value=d):
            return orch.Orchestrator.__new__(orch.Orchestrator)._drain_backlog()

    def test_one_segdir_is_the_normal_steady_state_not_a_backlog(self):
        # Outside a stall the pipeline guarantees at most one — dropped at every hand-off.
        self.assertEqual(self._count(self._with_segdirs(1)), 1)

    def test_two_or_more_segdirs_is_a_stall_buffer(self):
        self.assertEqual(self._count(self._with_segdirs(3)), 3)

    def test_loose_files_and_other_dirs_are_not_counted(self):
        d = self._with_segdirs(2, extra=("something.mov", "a_prob4_upscaled.segments.txt"))
        os.makedirs(os.path.join(d, "prefetch"))
        self.assertEqual(self._count(d), 2)

    def test_plan_only_segdirs_never_count(self):
        """The 2026-08-10 deadlock: _plan_fast_path_bounds creates a segdir holding ONLY
        scenes.json for an item that never upscales, and a topaz paused before its first
        segment leaves the same shape. Two of those read as a stall buffer, so the run
        thread held every fresh item under "two remuxes running" with no remux live — and
        nothing could clear it, since the blocked items owned the dirs."""
        d = tempfile.mkdtemp()
        for name in ("Borat_prob4_upscaled.segments", "Show S03E14_prob4_upscaled.segments"):
            sd = os.path.join(d, name)
            os.makedirs(sd)
            with open(os.path.join(sd, "scenes.json"), "w") as f:
                f.write("[]")
        self.assertEqual(self._count(d), 0)
        open(os.path.join(d, "Borat_prob4_upscaled.segments", "seg_0000.mov"), "w").close()
        self.assertEqual(self._count(d), 1)      # a real segment makes it a buffer

    def test_extend_chunk_dirs_never_count(self):
        """The extend stage's own resume dir also ends in .segments and shares the scratch,
        but holds wide_NNNN.mp4 — it is not topaz work and must not gate the run thread."""
        d = tempfile.mkdtemp()
        for i in range(3):
            sd = os.path.join(d, f"Show S01E{i:02d}_wide.mp4.segments")
            os.makedirs(sd)
            open(os.path.join(sd, f"wide_{i:04d}.mp4"), "w").close()
        self.assertEqual(self._count(d), 0)

    def test_a_missing_scratch_is_zero_not_a_crash(self):
        self.assertEqual(self._count("/nonexistent/scratch/path"), 0)

    def test_it_survives_a_restart(self):
        # The whole point: enable() used to clear the in-memory set, so a Stop/Start or a
        # deploy erased the backlog. A count read off the disk cannot be erased that way.
        d = self._with_segdirs(3)
        self.assertEqual(self._count(d), 3)
        self.assertEqual(self._count(d), 3)      # a "restart" changes nothing


class DiskGateDuringDrain(unittest.TestCase):
    def _pause(self, *, phys, backlog, fin_movie=False, in_finisher=True):
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._finisher_lock = mock.MagicMock()
        o._finisher_lock.__enter__ = lambda *_a: None
        o._finisher_lock.__exit__ = lambda *_a: None
        o._in_finisher = {"k"} if in_finisher else set()
        o._in_finisher_movies = {"m"} if fin_movie else set()
        o._stall_active = False
        o._quiet_mode = lambda: False
        o._reclaim_for_pipeline = lambda **kw: None
        o._drain_backlog = lambda: backlog
        o._free_scratch_gb = lambda: 9999
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=phys), \
             mock.patch.object(orch, "_min_free_gb", return_value=400):
            return o._low_disk_pause()

    def test_the_old_400_gate_blocked_the_drain(self):
        # The reported symptom: buffer on disk, physical free below 400, run thread held —
        # so the items that would FREE the space could never be selected.
        self.assertIsNone(self._pause(phys=349, backlog=3),
                          "a drain backlog must no longer be blocked at 349 GB")

    def test_without_a_backlog_the_full_gate_still_applies(self):
        msg = self._pause(phys=349, backlog=1)
        self.assertIsNotNone(msg, "the ordinary overlap reserve must not be weakened")
        self.assertIn("low disk", msg)

    def test_a_finishing_movie_keeps_the_full_floor_even_while_draining(self):
        # No movie DV render has ever been measured; a movie ProRes reaches ~245 GiB.
        self.assertIsNotNone(self._pause(phys=349, backlog=3, fin_movie=True))

    def test_the_drain_gate_still_stops_a_genuinely_full_disk(self):
        self.assertIsNotNone(self._pause(phys=40, backlog=3))


class TruncatedRender(unittest.TestCase):
    """A render that died partway still carries a valid DOVI record, so it read as DONE —
    and that hand-off rmtree's the ~190 GiB ProRes, after which the upscale cannot be
    re-derived without a fresh 2-hour Topaz run."""

    class P:
        source_cfr = "/tmp/src.mp4"
        dv_render = "/tmp/out.mov"

    def _complete(self, want, got):
        def frames(path):
            return want if path == self.P.source_cfr else got
        with mock.patch.object(orch, "_nb_frames", side_effect=frames):
            return orch.render_is_complete(self.P)

    def test_a_full_length_render_is_accepted(self):
        self.assertTrue(self._complete(62304, 62304))

    def test_a_render_that_died_at_60_percent_is_rejected(self):
        self.assertFalse(self._complete(62304, 37000))

    def test_a_hair_short_render_is_still_accepted(self):
        self.assertTrue(self._complete(62304, 62200))     # within tolerance

    def test_an_unreadable_probe_never_fails_a_good_render(self):
        self.assertTrue(self._complete(None, 62304))
        self.assertTrue(self._complete(62304, None))

    def test_stage_done_resolve_rejects_a_truncated_render(self):
        p = self.P()
        with mock.patch.object(orch, "_vstream", return_value=[{"codec_type": "video"}]), \
             mock.patch.object(orch, "_is_dv81", return_value=True), \
             mock.patch.object(orch, "render_is_complete", return_value=False):
            self.assertFalse(orch.stage_done("resolve", p))
        with mock.patch.object(orch, "_vstream", return_value=[{"codec_type": "video"}]), \
             mock.patch.object(orch, "_is_dv81", return_value=True), \
             mock.patch.object(orch, "render_is_complete", return_value=True):
            self.assertTrue(orch.stage_done("resolve", p))

    def test_topaz_done_does_not_accept_a_truncated_render_as_proof(self):
        # stage_done("topaz") returns True when a valid render exists (the segments were
        # legitimately dropped). A SHORT render must not grant that, or a missing upscale
        # is masked forever.
        p = self.P()
        p.segdir = "/nonexistent"
        with mock.patch.object(orch, "_vstream", return_value=[{"codec_type": "video"}]), \
             mock.patch.object(orch, "_is_dv81", return_value=True), \
             mock.patch.object(orch, "render_is_complete", return_value=False):
            self.assertFalse(orch.stage_done("topaz", p))


if __name__ == "__main__":
    unittest.main()


class DrainConvertsEverythingFirst(unittest.TestCase):
    """User-dictated priority: with several Topaz exports waiting, push ALL of them through
    Resolve first, THEN let the two remux lanes run.

    The arithmetic backs it: Resolve is the serial bottleneck, and each conversion drops a
    ~190 GiB ProRes while adding a ~18 GiB render — about +172 GiB freed per item. Holding
    Resolve behind a remux keeps those intermediates on disk for a ~75-minute encode each.
    """

    def _orch(self, backlog, *, qsize=0, finishing=None, finishing2=None, lanes=2):
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._drain_backlog = lambda: backlog
        o.state = {"finishing": finishing, "finishing2": finishing2}
        o._finish_q = mock.Mock(qsize=lambda: qsize)
        o._in_finisher = set()
        o._finisher_lock = mock.MagicMock()
        o._finisher_lock.__enter__ = lambda *_a: None
        o._finisher_lock.__exit__ = lambda *_a: None
        return o

    def test_resolve_never_waits_while_a_backlog_exists(self):
        o = self._orch(3, finishing={"stage": "remux"}, finishing2={"stage": "remux"})
        with mock.patch.object(o, "_in_finisher_keys", return_value={"A", "B"}):
            self.assertFalse(o._resolve_should_hold(),
                             "both lanes full must NOT stop the conversions that free the disk")

    def test_the_run_thread_is_not_backpressured_while_draining(self):
        # The queued items are ~18 GiB each; the ones still to convert are ~190 GiB each.
        # Holding here would keep the big things to avoid accumulating the small ones.
        o = self._orch(3, qsize=5)
        self.assertFalse(o._finisher_backlogged())

    def test_normal_one_at_a_time_timing_is_untouched(self):
        # No backlog -> exactly the old behaviour, both gates intact.
        o = self._orch(1, finishing={"stage": "remux"})
        self.assertTrue(o._resolve_should_hold())
        o = self._orch(1, qsize=2)
        self.assertTrue(o._finisher_backlogged())
        o = self._orch(1, qsize=1)
        self.assertFalse(o._finisher_backlogged())

    def test_a_lone_leftover_item_is_not_treated_as_a_backlog(self):
        # One segdir is the ordinary steady state, not a stall buffer.
        o = self._orch(1, finishing={"stage": "remux"}, finishing2={"stage": "remux"})
        with mock.patch.object(o, "_in_finisher_keys", return_value={"A", "B"}):
            self.assertTrue(o._resolve_should_hold())


class NoRemuxingDuringResolve(unittest.TestCase):
    """User-dictated: nothing remuxes while Resolve is working. Gating on `_resolve_active`
    alone was not enough — it goes false in the gap between two back-to-back conversions, so
    a lane would start a fresh ~7-minute x265 segment into that gap and be encoding again the
    instant the next Resolve began."""

    def _orch(self, *, resolve_active, backlog, resolve_age=0.0):
        import threading, time
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._resolve_active = threading.Event()
        o._resolve_fast = False               # the whole-machine case these tests pin
        if resolve_active:
            o._resolve_active.set()
        o._drain_backlog = lambda: backlog
        o._last_resolve_at = time.time() - resolve_age
        return o

    def test_it_waits_while_resolve_runs(self):
        self.assertTrue(self._orch(resolve_active=True, backlog=1)._remux_must_wait())

    def test_it_keeps_waiting_in_the_gap_between_two_conversions(self):
        # THE case the old gate missed: Resolve is momentarily between items, but there is
        # still a backlog to convert. Starting a segment here defeats the whole point.
        self.assertTrue(self._orch(resolve_active=False, backlog=3)._remux_must_wait())

    def test_it_runs_once_the_backlog_is_converted(self):
        self.assertFalse(self._orch(resolve_active=False, backlog=1)._remux_must_wait())
        self.assertFalse(self._orch(resolve_active=False, backlog=0)._remux_must_wait())

    def test_the_ordinary_overlap_is_untouched(self):
        # No drain, no Resolve → remux runs alongside download/topaz exactly as before.
        self.assertFalse(self._orch(resolve_active=False, backlog=1)._remux_must_wait())

    def test_the_backlog_wait_is_BOUNDED(self):
        """Segdirs orphaned by a crash or an abandoned show satisfy the backlog test
        forever. Waiting on the count alone froze the finisher permanently with nothing
        actually being converted — it hung the test suite exactly that way."""
        stale = self._orch(resolve_active=False, backlog=5,
                           resolve_age=orch.BACKLOG_WAIT_GRACE_SECONDS + 60)
        self.assertFalse(stale._remux_must_wait(),
                         "a backlog nobody is converting must not hold the lanes forever")

    def test_it_still_covers_the_gap_between_two_conversions(self):
        # Resolve finished seconds ago and there is more to convert — keep standing down.
        fresh = self._orch(resolve_active=False, backlog=5, resolve_age=5)
        self.assertTrue(fresh._remux_must_wait())

    def test_the_same_predicate_gates_the_hold_AND_the_in_flight_yield(self):
        # Two call sites, one rule — if they drift, a lane can be held at the door while an
        # in-flight encode keeps running, or vice versa.
        import inspect
        src = inspect.getsource(orch.Orchestrator._finish_item)
        self.assertIn("self._remux_must_wait(lane)", src)      # the hold loop
        self.assertIn("should_pause=lambda: self._remux_must_wait(lane)", src)  # in-flight yield
        self.assertNotIn("should_pause=self._resolve_active.is_set", src)


class ResolveGetsTheWholeMachine(unittest.TestCase):
    """Yielding at a segment boundary was not enough, and the gap is measurable: a segment
    is 5 minutes of VIDEO but ~7 minutes of wall clock, so an encode caught mid-segment kept
    running through most of Resolve's ~19-minute pass. Observed live — four minutes into
    Resolve, both lanes were still on the same seg_done and still burning CPU.

    SIGSTOP instead of killing: no work lost, no segment-granularity rounding."""

    def setUp(self):
        import dvcap
        dvcap._LIVE_ENCODERS.clear()
        dvcap._ENCODERS_SUSPENDED = False

    tearDown = setUp

    def _fake_pair(self):
        import dvcap
        sent = []
        procs = []
        for _ in range(2):
            p = mock.Mock()
            p.poll.return_value = None                 # still running
            p.send_signal = lambda sig, _p=None: sent.append(sig)
            procs.append(p)
        pair = tuple(procs)
        dvcap._LIVE_ENCODERS.add(pair)
        return pair, sent

    def test_suspend_stops_BOTH_ends_of_the_pipe(self):
        # Stopping only x265 would leave ffmpeg spinning until the pipe filled.
        import dvcap, signal
        _pair, sent = self._fake_pair()
        self.assertEqual(dvcap.suspend_encoders(), 1)
        self.assertEqual(sent, [signal.SIGSTOP, signal.SIGSTOP])

    def test_resume_restarts_them(self):
        import dvcap, signal
        _pair, sent = self._fake_pair()
        dvcap.suspend_encoders(); sent.clear()
        dvcap.resume_encoders()
        self.assertEqual(sent, [signal.SIGCONT, signal.SIGCONT])
        self.assertFalse(dvcap.encoders_suspended())

    def test_a_segment_spawned_DURING_a_suspension_starts_stopped(self):
        # Otherwise the next segment begins the moment the previous one ends and runs free
        # for the rest of Resolve's pass — the exact hole the boundary-yield left.
        import dvcap
        dvcap.suspend_encoders()
        self.assertTrue(dvcap.encoders_suspended())

    def test_a_finished_pair_is_never_left_frozen(self):
        # A SIGSTOPped process cannot exit; if _encode_pipe returned without resuming it,
        # its lane would stall forever and nothing else would ever send SIGCONT.
        import inspect, dvcap
        src = inspect.getsource(dvcap._encode_pipe)
        self.assertIn("finally:", src)
        self.assertIn("_LIVE_ENCODERS.discard(pair)", src)
        self.assertIn("SIGCONT", src)

    def test_the_orchestrator_suspends_and_ALWAYS_resumes(self):
        import inspect
        src = inspect.getsource(orch.Orchestrator._process)
        self.assertIn("self._suspend_remuxes()", src)
        # The resume must be in the finally, not the happy path — a failed or aborted
        # Resolve must not leave the encoders stopped.
        after = src[src.index("finally:"):]
        self.assertIn("self._resume_remuxes()", after)

    def test_stopping_the_run_also_unfreezes(self):
        for fn in (orch.Orchestrator.enable, orch.Orchestrator.disable):
            import inspect
            self.assertIn("_resume_remuxes", inspect.getsource(fn),
                          f"{fn.__name__} must not leave encoders frozen")

    def test_it_really_stops_a_real_process(self):
        """The load-bearing behaviour, against actual processes rather than mocks."""
        import dvcap, subprocess, sys, time
        procs = [subprocess.Popen([sys.executable, "-c",
                                   "import time\nwhile True: sum(range(20000))"])
                 for _ in range(2)]
        pair = tuple(procs)
        dvcap._LIVE_ENCODERS.add(pair)
        try:
            def state(p):
                return subprocess.run(["ps", "-o", "state=", "-p", str(p.pid)],
                                      capture_output=True, text=True).stdout.strip()
            time.sleep(0.3)
            dvcap.suspend_encoders()
            time.sleep(0.3)
            self.assertTrue(all(state(p).startswith("T") for p in procs),
                            "SIGSTOP did not actually stop the encoders")
            dvcap.resume_encoders()
            time.sleep(0.3)
            self.assertFalse(any(state(p).startswith("T") for p in procs))
        finally:
            for p in procs:
                p.kill()


class NeverLeaveEncodersFrozen(unittest.TestCase):
    """A SIGSTOPped x265 cannot exit and cannot be resumed by anything that does not know
    its pid. If one is left behind, its lane stalls forever and deploy-now.sh waits out its
    full timeout and then relaunches on top of it. Every exit path must unfreeze."""

    def test_resume_also_sweeps_orphans_from_a_previous_process(self):
        # dvcap's registry only knows encoders THIS run started.
        ran = []
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o.state = {}
        with mock.patch.object(orch.subprocess, "run",
                               lambda cmd, **kw: ran.append(cmd)):
            o._resume_remuxes()
        self.assertTrue(any("-CONT" in c and "x265" in c for c in ran),
                        "must SIGCONT any orphaned x265, not just its own")

    def test_the_deploy_script_unfreezes_before_waiting(self):
        # It waits for x265 to EXIT; a stopped one never will.
        import os
        sh = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(orch.__file__))), "deploy-now.sh")).read()
        cont = sh.index("pkill -CONT -x x265")
        wait = sh.index('pgrep -x x265 | wc -l | tr -d \' \'", "0"'.replace('", "0"', ''))
        self.assertLess(cont, sh.rindex("pgrep -x x265"),
                        "the unfreeze must come BEFORE the wait-for-exit loop")
        del wait


class RemuxRunsDuringAnUpload(unittest.TestCase):
    """User-dictated and already true, pinned so it stays true: an upload on one lane must
    never stop the other lane remuxing.

    Uploads are I/O to the NAS and remuxes are CPU, so serialising them wastes the machine.
    Only uploads serialise against EACH OTHER, via _upload_lock — one NAS push at a time.
    """

    def _orch(self, lane1_stage, queued=2, backlog=1):
        import threading
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o.state = {"finishing": {"ep": "A", "stage": lane1_stage, "pct": 60},
                   "finishing2": None}
        o._finish_q = mock.Mock(qsize=lambda: queued)
        o._resolve_active = threading.Event()
        o._resolve_fast = False
        o._drain_backlog = lambda: backlog
        o._last_resolve_at = 0.0
        return o

    def test_lane2_may_take_work_while_lane1_uploads(self):
        o = self._orch("upload")
        with mock.patch.object(orch, "_finisher_lanes", return_value=2):
            self.assertTrue(o._lane2_should_help())

    def test_an_upload_does_not_make_a_remux_stand_down(self):
        # _remux_must_wait exists for RESOLVE, which needs the whole machine. An upload does
        # not — it is network I/O.
        self.assertFalse(self._orch("upload")._remux_must_wait())

    def test_resolve_still_does_make_it_stand_down(self):
        o = self._orch("upload")
        o._resolve_active.set()
        self.assertTrue(o._remux_must_wait())

    def test_uploads_still_serialise_against_each_other(self):
        # The one thing that must NOT overlap: two NAS pushes.
        import inspect
        src = inspect.getsource(orch.Orchestrator._finish_item)
        self.assertIn("_upload_lock", src)
        i = src.index('st == "upload"')
        self.assertIn("_upload_lock", src[i:i + 400],
                      "the upload branch must still take the single-push lock")


class FastPathResolveStartsBesideARemux(unittest.TestCase):
    """The OTHER half of the sharing rule (user-caught live: a movie sat at
    "resolve-gate" behind one remux): a fast-path item's Resolve may START while up to
    `share` lanes are mid-remux — it holds only when live remuxes exceed the share.
    Non-fast incoming items keep the old 1-at-a-time gate."""

    def test_fast_incoming_starts_beside_one_remux(self):
        self.assertFalse(orch.resolve_must_wait({"stage": "remux"}, 0, None,
                                                incoming_fast=True, share=1))

    def test_fast_incoming_holds_beyond_the_share(self):
        self.assertTrue(orch.resolve_must_wait({"stage": "remux"}, 0, {"stage": "remux"},
                                               incoming_fast=True, share=1))
        self.assertFalse(orch.resolve_must_wait({"stage": "remux"}, 0, {"stage": "remux"},
                                                incoming_fast=True, share=2))

    def test_fast_incoming_ignores_the_finisher_queue(self):
        # Queued finisher items occupy no machine — the share counts RUNNING remuxes.
        self.assertFalse(orch.resolve_must_wait({"stage": "remux"}, 2, None,
                                                incoming_fast=True, share=1))

    def test_share_zero_keeps_the_old_gate_for_fast(self):
        self.assertTrue(orch.resolve_must_wait({"stage": "remux"}, 0, None,
                                               incoming_fast=True, share=0))

    def test_non_fast_incoming_unchanged(self):
        self.assertTrue(orch.resolve_must_wait({"stage": "remux"}, 0, None))
        self.assertFalse(orch.resolve_must_wait({"stage": "upload"}, 0, None))
