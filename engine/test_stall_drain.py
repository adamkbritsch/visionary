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
            os.makedirs(os.path.join(d, f"Show S01E{i:02d}_prob4_upscaled.segments"))
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
