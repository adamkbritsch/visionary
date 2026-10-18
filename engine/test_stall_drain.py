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
import yield_lease
from types import SimpleNamespace as types_ns
from test_yield_lease import TRUST


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

    def _with_segdirs(self, n, extra=(), done=True):
        d = tempfile.mkdtemp()
        for i in range(n):
            sd = os.path.join(d, f"Show S01E{i:02d}_prob4_upscaled.segments")
            os.makedirs(sd)
            # A segdir is a BUFFER only once its topaz is FINISHED — a real segment alone
            # is not enough (see test_a_partly_upscaled_segdir_is_not_a_backlog).
            open(os.path.join(sd, "seg_0000.mov"), "w").close()
            if done:
                open(os.path.join(sd, "DONE"), "w").close()
        for name in extra:
            open(os.path.join(d, name), "w").close()
        return d

    def _count(self, d):
        # segments_complete() probes every chunk's real frame count, so it is stubbed on the
        # fixture's marker: what is under test here is WHICH dirs the orchestrator counts.
        done = lambda sd: os.path.exists(os.path.join(sd, "DONE"))
        with mock.patch.object(orch.scratch, "default_scratch", return_value=d), \
             mock.patch("topaz.segments_complete", side_effect=done):
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
        sd = os.path.join(d, "Borat_prob4_upscaled.segments")
        open(os.path.join(sd, "seg_0000.mov"), "w").close()
        self.assertEqual(self._count(d), 0)      # a segment alone is work in PROGRESS
        open(os.path.join(sd, "DONE"), "w").close()
        self.assertEqual(self._count(d), 1)      # finished topaz is what makes it a buffer

    def test_a_partly_upscaled_segdir_is_not_a_backlog(self):
        """The 2026-08-23 deadlock. A deploy killed topaz twice in three minutes, leaving two
        segdirs holding real seg_NNNN.mov files with the upscale unfinished. Counting those
        as a two-item backlog shut _dual_remux_pauses_topaz on every fresh item — "two
        remuxes running" with ZERO remuxes live — and nothing could clear it, because
        clearing it means resolving them and resolve needs the topaz they were blocked from
        finishing. Same shape as the plan-only deadlock above; the seg_ test closed that
        hole and left this one."""
        self.assertEqual(self._count(self._with_segdirs(2, done=False)), 0)
        # ...and the gate stays open for a fresh item while they sit there
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._drain_backlog = lambda: 0
        o.state = {"finishing": None, "finishing2": None}
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "topaz_is_noop", return_value=False):
            self.assertFalse(o._dual_remux_pauses_topaz(object()))

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

    def test_native_items_are_never_a_resolve_backlog(self):
        """dv_engine 'native': a finisher item keeps its chunks through its remux while the
        next one analyses beside it — the lane's steady state, two finished segdirs. They
        never wait on Resolve, so counting them read as a stall: pacing skipped, fresh Topaz
        held, the remux lanes standing down, the overlap gate dropped to 80 GB."""
        d = self._with_segdirs(3)
        names = sorted(n for n in os.listdir(d) if n.endswith(".segments"))
        for name, files in ((names[0], ("native.json",)),
                            (names[1], ("native.json",)),
                            (names[2], ("native.json", "fallback.json"))):
            os.makedirs(os.path.join(d, name, "native"))
            for f in files:
                open(os.path.join(d, name, "native", f), "w").close()
        # the fallen-back item IS Resolve's again; the two claimed ones are not counted
        self.assertEqual(self._count(d), 1)


class NativeRenderIsNeverAResolveBacklog(unittest.TestCase):
    """A native render ('dvrender', settings dv_routes) never waits on Resolve: its item keeps only
    the scene plan's segdir (scenes.json, dropped at hand-off like any Resolve item's) and its
    books beside the render (<dv_render>.dvrender/, analysis ranges and encodes). Neither may read
    as Topaz output buffered behind a stalled Resolve — that would hold fresh Topaz and stand the
    remux lanes down for a stall that does not exist."""

    _with_segdirs = DrainBacklog._with_segdirs
    _count = DrainBacklog._count

    def test_its_books_and_its_plan_only_segdir_never_count(self):
        import nativerender
        d = self._with_segdirs(1)
        stem = "Chan - Video [aaaaaaaaaa9]"
        os.makedirs(os.path.join(d, stem + "_prob4_upscaled.segments"))
        open(os.path.join(d, stem + "_prob4_upscaled.segments", "scenes.json"), "w").close()
        books = os.path.join(d, stem + " HDR10 DV upscaled.mov" + nativerender.BOOKS_SUFFIX)
        os.makedirs(os.path.join(books, nativerender.ANALYSIS_DIR))
        for name in ("range_0000_0_300.mkv", "seg_0000.mov"):     # even a seg_-named stray
            open(os.path.join(books, nativerender.ANALYSIS_DIR, name), "w").close()
        open(os.path.join(books, nativerender.ENCODE), "w").close()
        self.assertEqual(self._count(d), 1)                        # the Topaz item's alone


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
        o._display_held = set()                  # nothing waiting for the pinned display
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


class NativeOverlapDisk(unittest.TestCase):
    """U2 ("Allow below 400 GB", user-decided 2026-10-15). While a FINISHER-OWNED native item
    still holds its Topaz chunks, the flat min(OVERLAP_MIN_PHYS_GB, floor) start gate gives way
    to the next item's OWN projected need (orchestrator._item_projection), never below 150 GiB
    — but only when that size is MEASURED; an unknown one keeps today's need. The
    pre-selection gate can only keep the minimum; the item's own need is asked once it is
    known. Quiet Mode and a pinned-display hold apply U2 too, when the native chunks are all
    that holds disk. The hard floors never move."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.held = os.path.join(self.td, "Show S01E01_prob4_upscaled.segments")
        os.makedirs(os.path.join(self.held, "native"))
        open(os.path.join(self.held, "native", "native.json"), "w").close()
        self.reclaims = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _orch(self, *, held=True, owned=True, stall=False):
        import threading
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._finisher_lock = threading.Lock()
        d = {"kind": "episode", "series": "Show", "ep": "S01E01",
             "source_basename": "Show S01E01.mkv"}
        key = orch.Orchestrator._desc_skip_key(d)
        o._finisher_persisted = {orch.Orchestrator._desc_cid(d): d} if owned else {}
        o._native_fin = {key: self.held} if held else {}
        o._in_finisher = {key}
        o._in_finisher_movies = set()
        o._stall_active = stall
        o._quiet_mode = lambda: False
        o._display_held = set()
        o._reclaim_for_pipeline = lambda **kw: self.reclaims.append(kw.get("need_gb"))
        o._drain_backlog = lambda: 0
        o._free_scratch_gb = lambda: 9999
        return o

    def _pause(self, o, phys):
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=phys), \
             mock.patch.object(orch, "_min_free_gb", return_value=400):
            return o._low_disk_pause()

    def _next(self, *, downloaded=True, ep="S01E02"):
        p = orch.episode_paths("Show", ep, f"Show {ep}.mkv", scratch_dir=self.td)
        if downloaded:
            open(p.source, "w").close()
        return p

    def test_the_next_item_may_start_below_400(self):
        self.assertIsNone(self._pause(self._orch(), 349))
        self.assertEqual(self.reclaims, [150])         # the prefetch buffer survives above that

    def test_its_hard_minimum_still_holds(self):
        msg = self._pause(self._orch(), 140)
        self.assertIn("low disk (140 GB)", msg)
        self.assertIn("native item(s) keep their Topaz chunks", msg)

    def test_without_native_chunks_the_flat_gate_is_untouched(self):
        msg = self._pause(self._orch(held=False), 349)
        self.assertEqual(msg, "paused — low disk (349 GB) while an item finishes in the "
                              "background (frees at its cleanup)")
        self.assertEqual(self.reclaims, [400])

    def test_an_item_that_left_the_finisher_stops_counting(self):
        o = self._orch(owned=False)                    # parked / abandoned: off the work-list
        self.assertIsNotNone(self._pause(o, 349))       # the flat gate again
        self.assertEqual(o._native_fin, {})              # ...and pruned

    def test_chunks_freed_by_the_remux_stop_counting(self):
        import shutil
        o = self._orch()
        shutil.rmtree(self.held)                        # stages._remux rmtree's them on success
        self.assertIsNotNone(self._pause(o, 349))

    def test_a_stall_no_longer_skips_the_overlap_need_while_native_chunks_are_held(self):
        self.assertIn("native item(s)", self._pause(self._orch(stall=True), 120))
        self.assertIsNone(self._pause(self._orch(stall=True, held=False), 120))   # as before
        self.assertIn("update prompt", self._pause(self._orch(stall=True), 90))  # floor first

    def test_the_need_is_the_next_items_projected_need(self):
        o = self._orch()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "_item_projection", return_value=(296, True)):
            self.assertEqual(o._native_overlap_need_gb(self._next()), 296)

    def test_prores_already_written_is_not_needed_twice(self):
        o = self._orch()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "_item_projection", return_value=(296, True)), \
             mock.patch.object(orch.Orchestrator, "_topaz_working_bytes",
                               return_value=100 * 1024 ** 3):
            self.assertEqual(o._native_overlap_need_gb(self._next()), 196)

    def test_never_below_the_hard_minimum(self):
        o = self._orch()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "_item_projection", return_value=(130, True)):
            self.assertEqual(o._native_overlap_need_gb(self._next()), 150)

    def test_topaz_done_needs_only_the_tail(self):
        o = self._orch()
        with mock.patch.object(orch, "_item_projection", return_value=(296, True)), \
             mock.patch.object(orch, "stage_done", return_value=True), \
             mock.patch.object(orch.Orchestrator, "_topaz_working_bytes",
                               side_effect=AssertionError("nothing left to write")):
            self.assertEqual(o._native_overlap_need_gb(self._next()), 150)

    def test_a_finishing_movie_keeps_its_full_floor_over_u2(self):
        # U2 relaxes the TV overlap need only: a movie remuxing in the finisher keeps today's
        # rule (need = min_free_gb), here 600 > 400 so the difference shows (verifier-caught).
        o = self._orch()
        o._in_finisher_movies = {"movie"}
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=450), \
             mock.patch.object(orch, "_min_free_gb", return_value=600):
            msg = o._low_disk_pause()
            self.assertIn("while an item finishes in the background", msg)
            with mock.patch.object(orch, "_item_projection", side_effect=AssertionError("not asked")):
                self.assertEqual(o._native_overlap_need_gb(self._next()), 600)

    def test_quiet_mode_counts_the_run_threads_own_native_item(self):
        # A native item still on the run thread (claimed, analysing) holds its chunks just as a
        # finisher one does: Quiet Mode must not pause the run on them (verifier-caught).
        import json, nativelane
        own = os.path.join(self.td, "Show S01E03_prob4_upscaled.segments")
        os.makedirs(os.path.join(own, "native"))
        with open(os.path.join(own, "native", "native.json"), "w") as f:
            json.dump({"state": "analyzing"}, f)
        o = self._orch(held=False)
        o._quiet_mode = lambda: True
        with mock.patch.object(nativelane, "engine_setting", return_value="native"), \
             mock.patch.object(orch.scratch, "default_scratch", return_value=self.td, create=True), \
             mock.patch.object(nativelane.scratch, "default_scratch", return_value=self.td):
            self.assertIsNone(self._pause(o, 349))                 # U2: above the 150 minimum
            self.assertIn("native item(s)", self._pause(o, 140))
        # dv_engine 'resolve' and nothing handed off native: no scan, today's message
        with mock.patch.object(nativelane, "engine_setting", return_value="resolve"), \
             mock.patch.object(nativelane, "held_native_segdirs",
                               side_effect=AssertionError("no scan in resolve mode")):
            self.assertIn("turn off Quiet Mode", self._pause(o, 349))

    def test_an_unknown_size_keeps_todays_need(self):
        # Not downloaded, unprobeable, or an item whose projection is a flat stand-in (YouTube,
        # combine, a fast-path movie): never gate on a guess -- min(OVERLAP_MIN_PHYS_GB, floor).
        o = self._orch()
        with mock.patch.object(orch, "stage_done", side_effect=AssertionError("not asked")):
            for floor in (400, 300, 600):
                with mock.patch.object(orch, "_min_free_gb", return_value=floor), \
                     mock.patch.object(orch, "_item_projection",
                                       return_value=(orch.ITEM_TAIL_GB, False)):
                    self.assertEqual(o._native_overlap_need_gb(self._next()),
                                     min(orch.OVERLAP_MIN_PHYS_GB, floor))

    def test_a_not_downloaded_item_is_unknown(self):
        o = self._orch()
        nxt = self._next(downloaded=False, ep="S01E03")
        with mock.patch.object(orch, "_min_free_gb", return_value=400), \
             mock.patch("plan.plan_for", return_value={"topaz": "upscale"}), \
             mock.patch("topaz.media_timing", return_value=(None, None)):
            self.assertEqual(orch._item_projection(nxt), (400, False))
            self.assertEqual(o._native_overlap_need_gb(nxt), 400)

    def test_the_hold_asks_raw_free_for_that_need(self):
        o = self._orch()
        nxt = self._next()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "_item_projection", return_value=(296, True)):
            with mock.patch.object(orch.scratch, "physical_free_gb", return_value=250):
                msg = o._native_overlap_hold(nxt)
            with mock.patch.object(orch.scratch, "physical_free_gb", return_value=300):
                self.assertIsNone(o._native_overlap_hold(nxt))
        self.assertIn("needs ~296 GB", msg)
        self.assertEqual(self.reclaims, [296, 296])

    def test_an_unknown_item_waits_for_todays_400(self):
        o = self._orch()
        nxt = self._next(downloaded=False, ep="S01E03")
        with mock.patch.object(orch, "_min_free_gb", return_value=400), \
             mock.patch.object(orch, "_item_projection", return_value=(400, False)):
            with mock.patch.object(orch.scratch, "physical_free_gb", return_value=399):
                self.assertIn("needs ~400 GB", o._native_overlap_hold(nxt))
            with mock.patch.object(orch.scratch, "physical_free_gb", return_value=400):
                self.assertIsNone(o._native_overlap_hold(nxt))

    def test_an_encoded_item_holds_no_chunks(self):
        # Its master passed: stages._remux deleted the chunks and stamped the marker. The
        # segdir stays (a few MB) until cleanup, but it no longer counts as held.
        import json
        with open(os.path.join(self.held, "native", "native.json"), "w") as f:
            json.dump({"state": orch.NATIVE_ENCODED}, f)
        o = self._orch()
        self.assertEqual(o._finisher_native_segdirs(), [])
        msg = self._pause(o, 349)
        self.assertEqual(msg, "paused — low disk (349 GB) while an item finishes in the "
                              "background (frees at its cleanup)")   # today's flat gate again

    # ---- U2 in the Quiet Mode and pinned-display branches -------------------------------

    def _quiet(self, o, *, quiet=True, display=False, backlog=0):
        o._quiet_mode = lambda: quiet
        o._display_held = {"Movie.mkv": 1} if display else set()
        o._drain_backlog = lambda: backlog
        return o

    def test_quiet_mode_with_only_native_chunks_held_applies_u2(self):
        o = self._quiet(self._orch())
        self.assertIsNone(self._pause(o, 335))                  # was: "turn off Quiet Mode..."
        self.assertEqual(self.reclaims, [150])
        msg = self._pause(o, 140)
        self.assertIn("native item(s) keep their Topaz chunks", msg)
        self.assertNotIn("Quiet Mode", msg)

    def test_quiet_mode_with_an_upscale_waiting_on_resolve_keeps_the_floor(self):
        o = self._quiet(self._orch(), backlog=1)
        self.assertEqual(self._pause(o, 335),
                         "paused — low disk (335 GB): turn off Quiet Mode to drain items "
                         "through Resolve")

    def test_quiet_mode_without_native_chunks_is_todays_rule(self):
        o = self._quiet(self._orch(held=False))
        self.assertEqual(self._pause(o, 335),
                         "paused — low disk (335 GB): turn off Quiet Mode to drain items "
                         "through Resolve")
        self.assertIsNone(self._pause(o, 400))                  # at the floor: no pause here

    def test_a_display_hold_with_only_native_chunks_held_applies_u2(self):
        o = self._quiet(self._orch(), quiet=False, display=True)
        self.assertIsNone(self._pause(o, 335))
        self.assertIn("native item(s)", self._pause(o, 140))

    def test_a_display_hold_with_an_upscale_waiting_keeps_the_floor(self):
        o = self._quiet(self._orch(), quiet=False, display=True, backlog=2)
        self.assertEqual(self._pause(o, 335),
                         "paused — low disk (335 GB): plug the pinned display back in to "
                         "drain 1 held item(s) through Resolve")
        o = self._quiet(self._orch(held=False), quiet=False, display=True)
        self.assertIn("plug the pinned display back in", self._pause(o, 335))

    def test_the_item_itself_is_never_its_own_obstacle(self):
        o = self._orch()
        me = types_ns(segdir=self.held)
        with mock.patch.object(orch.scratch, "physical_free_gb",
                               side_effect=AssertionError("no hold to compute")):
            self.assertIsNone(o._native_overlap_hold(me))


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
        # The gates read the queue through _queued_needing_remux now (an upload-only item no
        # longer counts). These tests model queued items as remux-needing work — which is
        # what qsize used to mean — so fake that seam directly.
        o._queued_needing_remux = lambda: qsize
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


class SiblingYieldLease(unittest.TestCase):
    """A sibling app (Discretion) can ask for the machine with a TTL'd lease. Same intent as the
    Resolve and outpainting exclusivity, from outside."""

    def _orch(self):
        import threading
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._resolve_active = threading.Event()
        o._extend_active = threading.Event()
        o._yield_lease = yield_lease.YieldLease(gate=TRUST)
        o._resolve_fast = False
        o._drain_backlog = lambda: 0
        o._last_resolve_at = 0.0
        return o

    def test_a_remux_stands_down_for_a_lease(self):
        o = self._orch()
        self.assertFalse(o._remux_must_wait(), "no lease, nothing to wait for")
        o._yield_lease.take("discretion", 600, "Pass 2 on Arrival")
        self.assertTrue(o._remux_must_wait())

    def test_it_resumes_by_itself_when_the_lease_lapses(self):
        """The safety property: a crashed sibling cannot wedge an overnight queue."""
        clock = [1000.0]
        o = self._orch()
        o._yield_lease = yield_lease.YieldLease(gate=TRUST, now=lambda: clock[0])
        o._yield_lease.take("discretion", 60)
        self.assertTrue(o._remux_must_wait())
        clock[0] += 61
        self.assertFalse(o._remux_must_wait())

    def test_releasing_frees_the_lane_immediately(self):
        o = self._orch()
        o._yield_lease.take("discretion", 3600)
        o._yield_lease.release("discretion")
        self.assertFalse(o._remux_must_wait())

    def test_resolve_and_upload_never_stand_down_for_a_lease(self):
        """Resolve holds the screen and cannot be paced; a half-written upload is worse than a slow
        one. Both are already uninterruptible in the deploy discipline, and a lease respects the
        same line."""
        o = self._orch()
        o._yield_lease.take("discretion", 3600)
        for stage in ("resolve", "upload", "extend"):
            self.assertFalse(o._yield_lease.blocks(stage), stage)


class ALeaseStopsWorkStarting(unittest.TestCase):
    """A lease has to be checked BEFORE a stage starts, not only inside it. The in-stage yield
    stops an encode already running at its next safe boundary — but a stage that STARTS and then
    yields is re-selected immediately, and on 2026-09-24 one lease produced 662 re-selections and
    1,324 log lines in 14 minutes with no work done in between."""

    def _orch(self):
        import threading
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._yield_lease = yield_lease.YieldLease(gate=TRUST)
        return o

    def test_no_lease_holds_nothing(self):
        self.assertIsNone(self._orch()._lease_hold("topaz"))

    def test_a_live_lease_names_the_holder_and_the_wait(self):
        o = self._orch()
        clock = [1000.0]
        o._yield_lease = yield_lease.YieldLease(gate=TRUST, now=lambda: clock[0])
        o._yield_lease.take("discretion", 600, "Pass 2 on Arrival")
        clock[0] += 60
        held = o._lease_hold("topaz")
        self.assertIn("discretion", held)
        self.assertIn("540", held)

    def test_the_uninterruptible_stages_are_never_held(self):
        o = self._orch()
        o._yield_lease.take("discretion", 600)
        for stage in ("resolve", "upload", "extend"):
            self.assertIsNone(o._lease_hold(stage), stage)

    def test_the_run_loop_asks_before_it_starts_the_stage(self):
        import inspect
        src = inspect.getsource(orch.Orchestrator._process)
        self.assertIn("_lease_hold", src)
        # run_st: the stage that runs in the slot ('dvrender' for a native route, else st)
        self.assertLess(src.index("_lease_hold"), src.index("run_stage(run_st, p"),
                        "checked after the stage starts = the stage starts and then yields")

    def test_a_lease_yield_does_not_feed_the_livelock_guard(self):
        """The guard exists for reasons this item could satisfy by taking the machine. A lease is
        not one: it is external and it clears on its own, so 'keeping the machine' is nonsense."""
        import inspect
        src = inspect.getsource(orch.Orchestrator._process)
        paused = src[src.index('startswith("paused:")'):]
        self.assertLess(paused.index("_lease_hold"), paused.index("_yield_streak"))


class NoRemuxingDuringResolve(unittest.TestCase):
    """User-dictated: nothing remuxes while Resolve is working. Gating on `_resolve_active`
    alone was not enough — it goes false in the gap between two back-to-back conversions, so
    a lane would start a fresh ~7-minute x265 segment into that gap and be encoding again the
    instant the next Resolve began."""

    def _orch(self, *, resolve_active, backlog, resolve_age=0.0):
        import threading, time
        o = orch.Orchestrator.__new__(orch.Orchestrator)
        o._resolve_active = threading.Event()
        o._extend_active = threading.Event()
        o._yield_lease = yield_lease.YieldLease(gate=TRUST)  # idle here — pinned in ExtendExclusivity
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

    def test_nothing_of_ours_ever_kills_expurgates_engine(self):
        # Expurgate runs its engine as .../Expurgate.app/.../engine/dashboard/server.py — the same
        # relative path. A bare `pkill -f "dashboard/server.py"` in the deploy script and in the
        # app's own launch killed it on every Visionary deploy (live 2026-10-06).
        import os, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(orch.__file__)))
        for rel in ("deploy-now.sh", "macapp/main.swift"):
            text = open(os.path.join(root, rel)).read()
            code = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith(("#", "//")))
            for m in re.finditer(r"p(?:kill|grep)[^\n]*", code):
                line = m.group(0)
                if "server" in line and ("$SERVER_PAT" not in line):
                    self.assertIn("Visionary[.]app/Contents/Resources/engine/dashboard/server", line,
                                  "%s: %s" % (rel, line))
            self.assertNotRegex(code, r'pkill[^\n]*"dashboard/server\.py"', rel)
        sh = open(os.path.join(root, "deploy-now.sh")).read()
        self.assertIn("SERVER_PAT='Visionary[.]app/Contents/Resources/engine/dashboard/server[.]py'", sh)

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
        o._extend_active = threading.Event()
        o._yield_lease = yield_lease.YieldLease(gate=TRUST)   # idle here — pinned in ExtendExclusivity
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


class DeployWaitsOutTheNativeRender(unittest.TestCase):
    """deploy-now.sh's patterns for the native render's children, matched against the argv the
    REAL command builders produce for a render in ~/topaz-scratch: RANGE_PAT is every analysis
    step (the range-boundary watch), RENDER_PAT every child at all (the wait-for-exit and the
    orphan sweep) — and never a remux, a Resolve pass or the native lane's chunk analysis."""

    def setUp(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(orch.__file__)))
        with open(os.path.join(root, "deploy-now.sh")) as f:
            self.sh = f.read()
        self.pat = {k: re.compile(re.search(r"^%s='([^']+)'" % k, self.sh, re.M).group(1))
                    for k in ("RANGE_PAT", "RENDER_PAT", "ANALYSIS_PAT")}

    def _argv(self):
        import dvcap, dvnative, nativerender, remux, sys
        scratch = "/Users/someone/topaz-scratch"
        p = orch.youtube_paths("A, B + C #1", "/s/x/Vid, a+b #2 [aaaaaaaaaa9].mp4", "V",
                               scratch_dir=scratch)
        books = nativerender.books_dir(p)
        mkv = os.path.join(books, nativerender.ANALYSIS_DIR, "range_0003_900_300.mkv")
        bin_ = mkv[:-4] + ".bin"
        spec = nativerender.RenderSpec(inputs=[[0, p.source_cfr], [1200, None]], fps="30000/1001",
                                       kbps=14000, lut="/x/rcm.cube")
        enc, es_dv = os.path.join(books, nativerender.ENCODE), p.dv_render + nativerender.ES_DV
        mine = {
            "intermediate": dvnative.build_analysis_intermediate_command(
                "ffmpeg", p.source_cfr, mkv, "/x/rcm.cube", start=900, frames=300, fps="30000/1001"),
            "analyzer": dvnative.build_analyzer_command("hdr_analyzer_mvp", mkv, bin_ + ".part"),
            "l4": dvnative.build_l4_command(sys.executable, mkv, bin_ + ".l4.json.part"),
            "encode": nativerender.build_encode_command("ffmpeg", spec, os.path.join(
                books, nativerender.ENCODE), 14000, progress=os.path.join(books, nativerender.PROGRESS)),
            "hdr10": nativerender.build_sei_command(sys.executable, enc, os.path.join(books, nativerender.SEI_TMP),
                                                    "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
                                                    "1000,400"),
            "inject": dvcap.build_inject_command("dovi_tool", enc, os.path.join(books, nativerender.RPU), es_dv),
            "mux": remux.build_capped_video_mux_command("MP4Box", es_dv, "30000/1001",
                                                        p.dv_render + nativerender.PART),
        }
        others = {
            "ship remux": remux.build_capped_video_mux_command("MP4Box", p.final + ".ship.hevc",
                                                               "30000/1001", p.final + ".dv.mp4"),
            "render read": ["ffmpeg", "-i", p.dv_render, "-c", "copy", p.final + ".ship.hevc"],
            "resolve": [sys.executable, "resolve_pipeline.py", "single", p.source_cfr, p.dv_render,
                        "dv1000", "14000"],
            "chunk analysis": dvnative.build_analysis_intermediate_command(
                "ffmpeg", p.segdir + "/seg_0001.mov", p.segdir + "/native/work/chunk_0001.mkv",
                "/x/rcm.cube"),
        }
        return mine, others

    def test_every_render_child_matches_and_nothing_else_does(self):
        mine, others = self._argv()
        for name, argv in mine.items():
            line = " ".join(argv)
            self.assertRegex(line, self.pat["RENDER_PAT"], name)
            if name in ("intermediate", "analyzer", "l4"):
                self.assertRegex(line, self.pat["RANGE_PAT"], name)
            else:
                self.assertNotRegex(line, self.pat["RANGE_PAT"], name)
            self.assertNotRegex(line, self.pat["ANALYSIS_PAT"], name)
        for name, argv in others.items():
            self.assertNotRegex(" ".join(argv), self.pat["RENDER_PAT"], name)

    def test_it_waits_for_them_sweeps_them_and_waits_out_the_encode(self):
        sh = self.sh
        self.assertIn('[ "$(pgrep -f "$RENDER_PAT" | wc -l | tr -d \' \')" = "0" ]', sh)
        self.assertIn('pkill -f "$RENDER_PAT"', sh)
        self.assertLess(sh.index("pkill -CONT -x x265"), sh.index('pkill -f "$RENDER_PAT"'))
        self.assertIn("dvrender:encode|dvrender:finish)", sh)          # the wait and the give-up
        self.assertEqual(sh.count("dvrender:encode|dvrender:finish)"), 2)
        self.assertIn("p.get('phase')", sh)
