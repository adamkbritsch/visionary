import contextlib
import datetime
import unittest
from unittest import mock

import orchestrator as orch
from orchestrator import episode_paths, gate_state

SRC = "The Office  Superfan Episodes S02e10 Christmas Party (Extended Cut).mp4"

# The durable finisher work-list writes to ~/.topaz-pipeline by default; every hand-off/finish test
# would otherwise pollute the real file (and later constructions would load it). Redirect it to a
# throwaway path for the whole module so each Orchestrator() starts from an empty work-list.
_FINISHER_PATCH = None


def setUpModule():
    global _FINISHER_PATCH
    import os as _os
    import tempfile as _tf
    d = _tf.mkdtemp()
    _FINISHER_PATCH = mock.patch.object(orch, "FINISHER_FILE", _os.path.join(d, "finisher_queue.json"))
    _FINISHER_PATCH.start()


def tearDownModule():
    if _FINISHER_PATCH is not None:
        _FINISHER_PATCH.stop()


class Paths(unittest.TestCase):
    def setUp(self):
        self.p = episode_paths("The Office X", "S02E10", SRC,
                               scratch_dir="/scratch", nas_tv_root="/Media/TV-Shows")

    def test_one_source_reused_by_topaz_and_remux(self):
        # download writes p.source (the download-complete proof) then its CFR re-encode;
        # topaz/resolve/remux all read the ONE CFR file
        self.assertEqual(self.p.source, "/scratch/" + SRC)
        self.assertEqual(self.p.source_cfr, "/scratch/" + SRC[:-4] + "_cfr.mp4")
        self.assertNotIn("upscaled", self.p.source_cfr)   # intermediate, not a deliverable

    def test_stage_output_names(self):
        stem = SRC[:-4]
        self.assertEqual(self.p.prores, "/scratch/" + stem + "_prob4_upscaled.mov")
        self.assertEqual(self.p.dv_render, "/scratch/" + stem + " HDR10 DV upscaled.mov")
        self.assertEqual(self.p.final, "/scratch/" + stem + " HDR10 DV upscaled.mp4")
        for f in (self.p.prores, self.p.dv_render, self.p.final):   # every output is tagged
            self.assertIn("upscaled", f)

    def test_nas_season_dir_and_final_name(self):
        self.assertEqual(self.p.nas_dir, "/Media/TV-Shows/The Office X/S02")
        self.assertTrue(self.p.nas_final.endswith("(Extended Cut) HDR10 DV upscaled.mp4"))

    def test_working_files_are_the_locals(self):
        self.assertEqual(set(self.p.working_files()),
                         {self.p.source, self.p.source_cfr, self.p.prores,
                          self.p.dv_render, self.p.final})


class ApplyContainer(unittest.TestCase):
    """Container is locked from the LOCAL source: MKV when it needs it, else the .mp4 default."""
    def _paths(self):
        return episode_paths("Show", "S01E01", "Ep.mkv",
                             scratch_dir="/scratch", nas_tv_root="/Media/TV-Shows")

    def test_no_source_keeps_mp4_default(self):
        p = self._paths()
        with mock.patch("os.path.exists", return_value=False):
            orch.apply_container(p)
        self.assertTrue(p.final.endswith(".mp4"))
        self.assertTrue(p.source_cfr.endswith(".mp4"))

    def test_mkv_when_container_ext_says_mkv(self):
        p = self._paths()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("remux.container_ext", return_value=".mkv"):
            orch.apply_container(p)
        self.assertEqual(p.source_cfr, "/scratch/Ep_cfr.mkv")
        self.assertEqual(p.final, "/scratch/Ep HDR10 DV upscaled.mkv")
        self.assertEqual(p.nas_final, "/Media/TV-Shows/Show/S01/Ep HDR10 DV upscaled.mkv")

    def test_mp4_when_container_ext_says_mp4(self):
        p = self._paths()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("remux.container_ext", return_value=".mp4"):
            orch.apply_container(p)
        self.assertTrue(p.final.endswith(" HDR10 DV upscaled.mp4"))
        self.assertTrue(p.nas_final.endswith(" HDR10 DV upscaled.mp4"))


class YouTubePaths(unittest.TestCase):
    # FOLDER-SPLIT: source is in the STAGING lib (/Media/YouTube-raw); the master publishes to the
    # mirrored path in the Plex lib (/Media/YouTube), keeping youtarr's video-folder + stem.
    VP = "/Media/YouTube-raw/Chan/Chan - Title - abc123/Chan - Title [abc12345678].mp4"
    PLEX_DIR = "/Media/YouTube/Chan/Chan - Title - abc123"

    def test_folder_split_paths(self):
        p = orch.youtube_paths("Chan", self.VP, scratch_dir="/scratch")
        self.assertEqual(p.series, "Chan")
        self.assertTrue(p.youtube)
        self.assertEqual(p.nas_source, self.VP)                    # raw source stays in STAGING
        self.assertEqual(p.sidecar_dir, "/Media/YouTube-raw/Chan/Chan - Title - abc123")
        self.assertEqual(p.nas_dir, self.PLEX_DIR)                 # publish INTO the Plex lib
        self.assertEqual(p.nas_final, self.PLEX_DIR + "/Chan - Title [abc12345678].mp4")
        self.assertEqual(p.source, "/scratch/Chan - Title [abc12345678].mp4")

    def test_apply_container_keeps_stem_and_locks_ext(self):
        p = orch.youtube_paths("Chan", self.VP, scratch_dir="/scratch")
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("remux.container_ext", return_value=".mkv"):
            orch.apply_container(p)
        # master keeps youtarr's stem (so the copied .nfo matches) — NO "upscaled" suffix — ext locked
        self.assertEqual(p.nas_final, self.PLEX_DIR + "/Chan - Title [abc12345678].mkv")
        self.assertTrue(p.final.endswith(" HDR10 DV upscaled.mkv"))


class Gate(unittest.TestCase):
    """Power sufficiency = THE BRICK: >= min_watts adapter connected → adequate, full stop."""
    class R:
        def __init__(self, ext): self.external_connected = ext

    def test_the_140w_brick_is_sufficient(self):
        self.assertTrue(gate_state(self.R(True), watts=140)["runnable"])
        self.assertTrue(gate_state(self.R(True), watts=140)["adequate"])

    def test_a_lesser_brick_or_battery_is_not(self):
        self.assertFalse(gate_state(self.R(True), watts=96)["adequate"])    # monitor/hub PD
        self.assertFalse(gate_state(self.R(True), watts=None)["adequate"])  # unknown wattage
        self.assertFalse(gate_state(self.R(False), watts=140)["adequate"])  # battery (stale watts)

    def test_drain_is_irrelevant_on_the_big_brick(self):
        # "connected to a 140 W adapter at all → sufficient" — draining under load must NOT pause
        with mock.patch.object(orch.power, "is_draining_on_ac", return_value=True):
            self.assertTrue(gate_state(self.R(True), watts=140)["runnable"])

    def test_power_ok_uses_the_brick_rule(self):
        o = orch.Orchestrator()
        r = self.R(True)
        with mock.patch.object(orch.power, "read_power", return_value=r), \
             mock.patch.object(orch.power, "adapter_watts", return_value=140), \
             mock.patch.object(orch.settings, "get_settings", return_value={}):
            self.assertEqual(o._power_ok()[0], "run")
        with mock.patch.object(orch.power, "read_power", return_value=r), \
             mock.patch.object(orch.power, "adapter_watts", return_value=65), \
             mock.patch.object(orch.settings, "get_settings", return_value={}):
            st, msg = o._power_ok()
            self.assertEqual(st, "pause"); self.assertIn("65 W", msg)


class SkipCurrent(unittest.TestCase):
    def test_skip_aborts_only_the_matching_current_item(self):
        o = orch.Orchestrator()
        o.state["current"] = {"kind": "youtube", "name": "Chan - T [abc12345678].mp4"}
        self.assertFalse(o.skip_current("other.mp4"))
        self.assertFalse(o._abort.is_set())
        self.assertTrue(o.skip_current("Chan - T [abc12345678].mp4"))
        self.assertTrue(o._abort.is_set())


class Resume(unittest.TestCase):
    def _p(self):
        return episode_paths("S", "S01E01", "x (Extended Cut).mp4",
                             scratch_dir="/s", nas_tv_root="/Media/TV-Shows")

    def test_resume_point_is_first_incomplete_stage(self):
        with mock.patch.object(orch, "stage_done",
                               side_effect=lambda st, p, ftp=None: st in ("download", "topaz")):
            self.assertEqual(orch.first_incomplete_stage(self._p()), "resolve")

    def test_none_when_episode_fully_done(self):
        with mock.patch.object(orch, "stage_done", return_value=True):
            self.assertIsNone(orch.first_incomplete_stage(self._p()))


class ProgressETA(unittest.TestCase):
    """The ETA must stay sane across a STOP/RESUME — the bug was a from-zero
    `elapsed × remaining / pct`, which a resume's idle gap + chunk-replay leap wrecked."""
    def _feed(self, o, seq):
        last = {}
        for t, pct in seq:
            with mock.patch.object(orch.time, "time", return_value=float(t)):
                last = {"stage": "topaz", "ep": "S01E01", "pct": pct}
                o._set_progress(last)
        return last.get("eta_secs")

    def test_fresh_stage_eta_is_linear(self):
        o = orch.Orchestrator()                       # 0→50% over 1000s → 50% left ≈ 1000s
        eta = self._feed(o, [(t, t * 0.05) for t in range(0, 1001, 10)])
        self.assertAlmostEqual(eta, 1000, delta=60)

    def test_resume_after_gap_ignores_idle_and_replay_burst(self):
        o = orch.Orchestrator()
        self._feed(o, [(t, t * 0.05) for t in range(0, 1001, 10)])   # → 50% at t=1000
        # STOP 1200s; RESUME: a replay BURST (3→50% in 4s) then real encoding 50→56% over 600s.
        resume = [(2200, 3), (2201, 12), (2202, 25), (2203, 40), (2204, 50)]
        resume += [(2204 + 10 * i, 50 + 0.1 * i) for i in range(1, 61)]
        eta = self._feed(o, resume)
        self.assertAlmostEqual(eta, 4400, delta=700)  # 6% / 600s on 44% left — NOT idle, NOT tiny

    def test_relaunch_resume_not_wildly_underestimated(self):
        o = orch.Orchestrator()                       # fresh orchestrator (post-relaunch)
        resume = [(5000, 3), (5001, 25), (5002, 50)]  # first updates ARE the replay burst
        resume += [(5002 + 10 * i, 50 + 0.1 * i) for i in range(1, 31)]   # then 50→53% over 300s
        eta = self._feed(o, resume)
        self.assertGreater(eta, 1500)                 # old bug gave "a few seconds"


class Prefetch(unittest.TestCase):
    """The background prefetcher stages the upcoming queue's download+CFR ahead of the GPU."""
    VP = "/Media/YouTube-raw/Chan/Chan - Title - abc/Chan - Title [abc12345678].mp4"

    def _candidates(self, o):
        with mock.patch.object(orch.youtube, "all_pending",
                               return_value=[{"channel": "Chan", "video_path": self.VP,
                                              "title": "T", "vid": "abc12345678"}]), \
             mock.patch.object(orch.movies, "get_selected",
                               return_value=[{"name": "Movie.mkv", "dir": "/Media/Movies/Movie",
                                              "title": "Movie", "pos": 0}]), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "cached_queue",
                               return_value={"remaining_items": [{"ep": "S02E10",
                                                                  "source_name": SRC}]}):
            return o._prefetch_candidates()

    def test_priority_order_is_youtube_then_movies_then_tv(self):
        cands = self._candidates(orch.Orchestrator())
        # newest YouTube first (front of the line), then the movie, then the TV episode
        self.assertTrue(cands[0].youtube)
        self.assertEqual(cands[1].source_basename, "Movie.mkv")
        self.assertEqual(cands[2].ep, "S02E10")
        self.assertEqual(len(cands), 3)

    def test_deduped_by_source_path(self):
        o = orch.Orchestrator()
        # same YouTube video listed twice → one candidate (a re-scan can surface a dup)
        with mock.patch.object(orch.youtube, "all_pending",
                               return_value=[{"channel": "Chan", "video_path": self.VP, "title": "T"},
                                             {"channel": "Chan", "video_path": self.VP, "title": "T"}]), \
             mock.patch.object(orch.movies, "get_selected", return_value=[]), \
             mock.patch.object(orch.series, "get_active_series", return_value=[]):
            self.assertEqual(len(o._prefetch_candidates()), 1)

    def test_parked_items_are_skipped(self):
        o = orch.Orchestrator()
        o._parked = {"S02E10"}                        # a parked TV ep must not be prefetched
        with mock.patch.object(orch.youtube, "all_pending", return_value=[]), \
             mock.patch.object(orch.movies, "get_selected", return_value=[]), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "cached_queue",
                               return_value={"remaining_items": [{"ep": "S02E10", "source_name": SRC}]}):
            self.assertEqual(o._prefetch_candidates(), [])

    def test_claim_promotes_prefetched_files_into_main_scratch(self):
        import tempfile, os as _os
        main, pf = tempfile.mkdtemp(), tempfile.mkdtemp()
        p = episode_paths("The Office", "S02E10", SRC)
        stem = _os.path.splitext(p.source_basename)[0]
        open(_os.path.join(pf, p.source_basename), "w").close()      # prefetched source
        open(_os.path.join(pf, stem + "_cfr.mp4"), "w").close()      # + its CFR
        open(_os.path.join(pf, "someone-elses.mp4"), "w").close()    # another item — must stay
        with mock.patch.object(orch.scratch, "default_scratch", return_value=main), \
             mock.patch.object(orch.scratch, "prefetch_dir", return_value=pf):
            orch.Orchestrator()._claim_prefetched(p)
        self.assertTrue(_os.path.exists(_os.path.join(main, p.source_basename)))   # promoted
        self.assertTrue(_os.path.exists(_os.path.join(main, stem + "_cfr.mp4")))
        self.assertFalse(_os.path.exists(_os.path.join(pf, p.source_basename)))    # left the buffer
        self.assertTrue(_os.path.exists(_os.path.join(pf, "someone-elses.mp4")))   # untouched

    def test_current_item_excluded_from_prefetch(self):
        # the run thread downloads its CURRENT item itself → the prefetcher must not also fetch it
        # (that collision is exactly what stalls the topaz/remux overlap)
        o = orch.Orchestrator()
        o._current_skip_key = "S02E10"
        tv = [c.ep for c in self._candidates(o) if not c.youtube and not c.movie]
        self.assertNotIn("S02E10", tv)            # excluded (it was the only queued TV episode)

    def test_claim_yields_when_prefetch_is_mid_fetching_the_same_item(self):
        # the overlap must NOT wait out a slow low-prio prefetch of its own current item: _claim_prefetched
        # tells the prefetcher to YIELD, drops the partial, and lets the download stage re-pull at normal prio
        import tempfile, os as _os, threading, time
        main, pf = tempfile.mkdtemp(), tempfile.mkdtemp()
        p = episode_paths("The Office", "S02E10", SRC)
        open(_os.path.join(pf, p.source_basename), "w").close()          # a PARTIAL, in-flight prefetch
        open(_os.path.join(pf, _os.path.splitext(p.source_basename)[0] + "_cfr.mp4"), "w").close()
        o = orch.Orchestrator()
        lock = o._item_lock(p); lock.acquire()                          # simulate the prefetcher holding it
        released = threading.Event()
        def prefetcher():
            while not o._prefetch_yield.is_set():
                time.sleep(0.005)
            lock.release(); released.set()                             # yields the moment it's asked
        threading.Thread(target=prefetcher, daemon=True).start()
        with mock.patch.object(orch.scratch, "default_scratch", return_value=main), \
             mock.patch.object(orch.scratch, "prefetch_dir", return_value=pf):
            o._claim_prefetched(p)
        self.assertTrue(released.wait(2))                              # the prefetch yielded + released
        self.assertFalse(o._prefetch_yield.is_set())                  # cleared after taking over
        self.assertFalse(_os.path.exists(_os.path.join(pf, p.source_basename)))    # partial dropped
        self.assertFalse(_os.path.exists(_os.path.join(main, p.source_basename)))  # NOT promoted (was partial)

    def test_purge_prefetch_orphans_keeps_only_pending(self):
        import tempfile, os as _os
        pf = tempfile.mkdtemp()
        keep = episode_paths("A", "S01E01", SRC)
        kstem = _os.path.splitext(keep.source_basename)[0]
        open(_os.path.join(pf, keep.source_basename), "w").close()   # a pending item — keep
        open(_os.path.join(pf, kstem + "_cfr.mp4"), "w").close()
        open(_os.path.join(pf, "orphan.mp4"), "w").close()           # not pending — purge
        open(_os.path.join(pf, "orphan_cfr.mp4"), "w").close()
        with mock.patch.object(orch.scratch, "prefetch_dir", return_value=pf):
            orch.Orchestrator()._purge_prefetch_orphans([keep])
        left = set(_os.listdir(pf))
        self.assertEqual(left, {keep.source_basename, kstem + "_cfr.mp4"})

    def test_buffer_names_are_exact_not_a_prefix(self):
        n = orch._buffer_names("Interview.mkv")
        self.assertEqual(n, {"Interview.mkv", "Interview_cfr.mp4", "Interview_cfr.mkv"})
        # a DIFFERENT item whose name merely starts with "<stem>_cfr" must NOT be captured (finding #6)
        self.assertNotIn("Interview_cfr breakdown [abcd1234].mkv", n)

    def test_purge_keeps_the_currently_claiming_item(self):
        import tempfile, os as _os
        pf = tempfile.mkdtemp()
        cur = episode_paths("A", "S01E01", SRC)
        cstem = _os.path.splitext(cur.source_basename)[0]
        open(_os.path.join(pf, cur.source_basename), "w").close()
        open(_os.path.join(pf, cstem + "_cfr.mp4"), "w").close()
        o = orch.Orchestrator()
        o.state["current"] = {"kind": "episode", "source_name": cur.source_basename}   # being claimed now
        with mock.patch.object(orch.scratch, "prefetch_dir", return_value=pf):
            o._purge_prefetch_orphans([])                 # empty cands — but current must survive the purge
        self.assertTrue(_os.path.exists(_os.path.join(pf, cur.source_basename)))
        self.assertTrue(_os.path.exists(_os.path.join(pf, cstem + "_cfr.mp4")))

    def test_download_once_is_a_noop_when_already_staged(self):
        o = orch.Orchestrator()
        p = episode_paths("The Office", "S02E10", SRC)
        with mock.patch.object(orch, "stage_done", return_value=True) as sd, \
             mock.patch("stages.run_stage") as rs:
            ok, msg = o._download_once(p)
        self.assertTrue(ok)
        rs.assert_not_called()                        # the loser of the race must NOT re-download

    def test_download_once_runs_the_stage_when_not_staged(self):
        o = orch.Orchestrator()
        p = episode_paths("The Office", "S02E10", SRC)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch("stages.run_stage", return_value=(True, "ok")) as rs:
            ok, _ = o._download_once(p)
        self.assertTrue(ok)
        rs.assert_called_once()
        self.assertEqual(rs.call_args.args[0], "download")   # only ever runs the download stage


class YouTubeCadence(unittest.TestCase):
    """YouTube is a COUNTER-GATED single-video insert: exactly 1 video per
    `youtube_every_tv_episodes` TV episodes (not a round-robin peer, not a batch)."""
    VP = "/Media/YouTube-raw/Chan/Chan - T - abc/Chan - T [abc12345678].mp4"

    def _decide(self, tv_since, *, yt=True, ep=True, active=("A",), every=2):
        o = orch.Orchestrator(); o._tv_since_yt = tv_since
        q = {"next": ({"ep": "S01E01", "source_name": SRC} if ep else None),
             "done_count": 0, "source_count": (5 if ep else 0)}
        v = {"channel": "Chan", "video_path": self.VP, "title": "T"} if yt else None
        with contextlib.ExitStack() as s:
            s.enter_context(mock.patch.object(orch.movies, "next_due", return_value=None))
            s.enter_context(mock.patch.object(orch.settings, "get_settings",
                                              return_value={"youtube_every_tv_episodes": every}))
            s.enter_context(mock.patch.object(orch.series, "get_active_series", return_value=list(active)))
            s.enter_context(mock.patch.object(orch.series, "series_root", return_value="/Media/TV"))
            s.enter_context(mock.patch.object(orch.series, "episode_queue", return_value=q))
            s.enter_context(mock.patch.object(orch.youtube, "next_due", return_value=v))
            return o._next_episode()

    def test_tv_runs_until_cadence_reached(self):
        p, why = self._decide(tv_since=1, every=2)         # only 1 ep since last YT → TV
        self.assertEqual(why, "ok"); self.assertFalse(p.youtube); self.assertEqual(p.ep, "S01E01")

    def test_youtube_fires_once_cadence_reached(self):
        p, why = self._decide(tv_since=2, every=2)         # 2 eps done → 1 YouTube video
        self.assertEqual(why, "ok"); self.assertTrue(p.youtube)

    def test_cadence_reached_but_no_video_stays_on_tv(self):
        p, why = self._decide(tv_since=9, yt=False, every=2)   # due, but nothing pending → TV
        self.assertFalse(p.youtube); self.assertEqual(p.ep, "S01E01")

    def test_drains_youtube_when_no_tv_ready_even_below_cadence(self):
        p, why = self._decide(tv_since=0, ep=False, every=2)   # counter not reached, but no TV → drain YT
        self.assertEqual(why, "ok"); self.assertTrue(p.youtube)

    def test_youtube_only_when_no_active_series(self):
        p, why = self._decide(tv_since=0, active=(), every=2)  # no series at all → YouTube drains
        self.assertEqual(why, "ok"); self.assertTrue(p.youtube)

    def test_nothing_ready_reports_no_series(self):
        p, why = self._decide(tv_since=0, yt=False, active=())
        self.assertIsNone(p); self.assertEqual(why, "no-series")

    def test_higher_cadence_holds_youtube_longer(self):
        self.assertFalse(self._decide(tv_since=3, every=5)[0].youtube)   # 3 < 5 → still TV
        self.assertTrue(self._decide(tv_since=5, every=5)[0].youtube)    # 5 == 5 → YouTube

    def test_participants_are_tv_series_only(self):
        with mock.patch.object(orch.series, "get_active_series", return_value=["A", "B"]):
            self.assertEqual(orch.Orchestrator()._participants(), ["A", "B"])

    def test_item_type_flags_drive_bookkeeping(self):
        # post-upload dispatch keys off the ITEM (p.youtube / p.movie), not queue membership — so a movie
        # removed mid-pipeline is still counted as a movie, never a TV episode (the confirmed miscount bug).
        self.assertTrue(orch.movie_paths("M.mkv", "/Media/Movies/M", "M").movie)
        self.assertFalse(orch.movie_paths("M.mkv", "/Media/Movies/M", "M").youtube)
        e = episode_paths("A", "S01E01", SRC)
        self.assertFalse(e.movie); self.assertFalse(e.youtube)          # TV ep is neither
        y = orch.youtube_paths("Chan", self.VP)
        self.assertFalse(y.movie); self.assertTrue(y.youtube)           # YouTube is not a movie

    def test_item_view_is_kind_correct_for_the_header(self):
        # the "now processing" header reads this — a YouTube video must render as channel+title,
        # NOT be mistaken for the next TV episode.
        y = orch.youtube_paths("Some Channel", self.VP, "My Great Video")
        self.assertEqual(y.item_view(), {"kind": "youtube", "channel": "Some Channel",
                                         "name": "Chan - T [abc12345678].mp4", "title": "My Great Video"})
        m = orch.movie_paths("Blade Runner.mkv", "/Media/Movies/BR", "Blade Runner")
        self.assertEqual(m.item_view()["kind"], "movie")
        self.assertEqual(m.item_view()["title"], "Blade Runner")
        e = episode_paths("The Office", "S02E10", SRC).item_view()
        self.assertEqual(e["kind"], "episode"); self.assertEqual(e["ep"], "S02E10")
        self.assertEqual(e["series"], "The Office")

    def test_yt_every_tv_clamps_and_defaults(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.settings, "get_settings", return_value={"youtube_every_tv_episodes": 0}):
            self.assertEqual(o._yt_every_tv(), 1)          # never < 1 (would divide-by-nothing the cadence)
        with mock.patch.object(orch.settings, "get_settings", return_value={}):
            self.assertEqual(o._yt_every_tv(), 2)          # default


class MovieTurns(unittest.TestCase):
    """A movie runs at most MOVIE_TURN_SECONDS per turn, then one other item, then resumes.
    CADENCE_FILE is patched per-test — the counters PERSIST for real, so an unpatched test
    would pollute the production file (and earlier tests would leak into later ones)."""

    def setUp(self):
        import tempfile, os as _os
        d = tempfile.mkdtemp()
        self._cf = mock.patch.object(orch, "CADENCE_FILE", _os.path.join(d, "cadence.json"))
        self._ef = mock.patch.object(orch, "ELAPSED_FILE", _os.path.join(d, "elapsed.json"))
        self._cf.start(); self._ef.start()

    def tearDown(self):
        self._cf.stop(); self._ef.stop()

    def _decide(self, o, *, movie=True, ep=True):
        q = {"next": ({"ep": "S01E01", "source_name": SRC} if ep else None),
             "done_count": 0, "source_count": (5 if ep else 0)}
        m = {"source_name": "Movie.mkv", "nas_dir": "/Media/Movies/M", "title": "M"} if movie else None
        with contextlib.ExitStack() as s:
            s.enter_context(mock.patch.object(orch.movies, "next_due", return_value=m))
            s.enter_context(mock.patch.object(orch.settings, "get_settings",
                                              return_value={"youtube_every_tv_episodes": 2}))
            s.enter_context(mock.patch.object(orch.series, "get_active_series",
                                              return_value=(["A"] if ep else [])))
            s.enter_context(mock.patch.object(orch.series, "series_root", return_value="/Media/TV"))
            s.enter_context(mock.patch.object(orch.series, "episode_queue", return_value=q))
            s.enter_context(mock.patch.object(orch.youtube, "next_due", return_value=None))
            return o._next_episode()

    def test_deferred_movie_yields_to_an_episode(self):
        o = orch.Orchestrator(); o._movie_wait = 1
        p, why = self._decide(o)
        self.assertEqual(why, "ok"); self.assertFalse(p.movie); self.assertEqual(p.ep, "S01E01")

    def test_deferred_movie_resumes_when_nothing_else_ready(self):
        o = orch.Orchestrator(); o._movie_wait = 1
        p, why = self._decide(o, ep=False)             # no TV, no YT → the deferral must not stall
        self.assertEqual(why, "ok"); self.assertTrue(p.movie)

    def test_due_movie_runs_when_not_deferred(self):
        o = orch.Orchestrator()
        p, why = self._decide(o)               # fresh episode (no topaz on disk) → movie interrupts
        self.assertTrue(p.movie)

    def test_midpipeline_episode_finishes_before_a_due_movie(self):
        # a part-processed episode (topaz segments already on disk) must NOT be preempted by a
        # due movie — else its ~140 GB intermediate sits idle through the movie's turn.
        import tempfile, os as _os
        o = orch.Orchestrator()
        scratchd = tempfile.mkdtemp()
        _os.makedirs(orch.episode_paths("A", "S01E01", SRC, scratch_dir=scratchd).segdir)
        with mock.patch.object(orch.scratch, "default_scratch", return_value=scratchd):
            p, why = self._decide(o)           # movie IS due + not deferred, but episode is mid-pipeline
        self.assertEqual(why, "ok")
        self.assertFalse(p.movie); self.assertEqual(p.ep, "S01E01")   # the episode wins

    def test_turn_budget_message_defers_without_fail_count(self):
        o = orch.Orchestrator(); o._enabled = True
        p = orch.movie_paths("Movie.mkv", "/Media/Movies/M", "M")
        with mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st == "download"), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch("stages.run_stage",
                        return_value=(False, "turn-budget: paused at a segment boundary")):
            o._process(p)
        self.assertEqual(o._movie_wait, 1)             # deferred behind one other item
        self.assertEqual(o._fail_counts, {})           # NOT a failure
        self.assertIn("90-minute turn", o.state["message"])

    def test_turn_budget_gates_resolve_start(self):
        # deadline already passed when the loop reaches 'resolve' → turn ends, no resolve run.
        o = orch.Orchestrator(); o._enabled = True
        p = orch.movie_paths("Movie.mkv", "/Media/Movies/M", "M")
        ran = []
        with mock.patch.object(orch, "stage_done",
                               side_effect=lambda st, _p: st in ("download", "topaz")), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(orch, "MOVIE_TURN_SECONDS", -1), \
             mock.patch("stages.run_stage",
                        side_effect=lambda st, *_a, **_k: ran.append(st) or (True, "ok")):
            o._process(p)
        self.assertEqual(ran, [])                      # resolve never started
        self.assertEqual(o._movie_wait, 1)

    def test_cadence_persists_across_orchestrators_and_enable(self):
        # The YouTube cadence + movie deferral are HISTORY — they must survive a relaunch
        # (new Orchestrator) and an enable() re-arm. Resetting them on every deploy/self-arm
        # is what starved YouTube (4 TV uploads, 0 videos).
        import tempfile, os as _os
        cf = _os.path.join(tempfile.mkdtemp(), "cadence.json")
        with mock.patch.object(orch, "CADENCE_FILE", cf):
            a = orch.Orchestrator()
            a._tv_since_yt = 2; a._movie_wait = 1; a._yt_wait = 1
            a._save_cadence()
            b = orch.Orchestrator()                        # relaunch
            self.assertEqual(b._tv_since_yt, 2)
            self.assertEqual(b._movie_wait, 1)
            self.assertEqual(b._yt_wait, 1)
            with mock.patch.object(orch.settings, "get_settings",
                                   return_value={}), \
                 mock.patch.object(orch.logbook, "event"), \
                 mock.patch.object(b, "_start_caffeinate"), \
                 mock.patch.object(b, "_ensure"):
                b.enable()                                 # re-arm must NOT reset either
            self.assertEqual(b._tv_since_yt, 2)
            self.assertEqual(b._movie_wait, 1)

    def test_non_movie_completion_decrements_the_deferral(self):
        o = orch.Orchestrator(); o._enabled = True; o._movie_wait = 1
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch("stages.run_stage", return_value=(True, "ok")), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.series, "get_active_series", return_value=["A"]), \
             mock.patch.object(orch.movies, "decrement_positions"), \
             mock.patch.object(orch.movies, "get_selected", return_value=[]):
            o._process(p)
            # upload bookkeeping moved to the FINISHER — drain the hand-off like the thread would
            o._finish_item(o._finish_q.get_nowait(), lambda st, *_a, **_k: (True, "ok"))
        self.assertEqual(o._movie_wait, 0)             # the movie is due again


if __name__ == "__main__":
    unittest.main()


class YouTubeTurns(MovieTurns):
    """A >90-min YouTube video postpones at a segment boundary, one episode runs, then it
    resumes — the same turn machinery as movies, tracked by _yt_wait. (Subclasses MovieTurns
    for the hermetic CADENCE_FILE setUp/tearDown + the _decide harness.)"""
    VP = "/Media/YouTube-raw/Chan/Chan - T - abc/Chan - T [abc12345678].mp4"

    def _decide_yt(self, o, *, ep=True):
        q = {"next": ({"ep": "S01E01", "source_name": SRC} if ep else None),
             "done_count": 0, "source_count": (5 if ep else 0)}
        v = {"channel": "Chan", "video_path": self.VP, "title": "T"}
        with contextlib.ExitStack() as s:
            s.enter_context(mock.patch.object(orch.movies, "next_due", return_value=None))
            s.enter_context(mock.patch.object(orch.settings, "get_settings",
                                              return_value={"youtube_every_tv_episodes": 2}))
            s.enter_context(mock.patch.object(orch.series, "get_active_series",
                                              return_value=(["A"] if ep else [])))
            s.enter_context(mock.patch.object(orch.series, "series_root", return_value="/Media/TV"))
            s.enter_context(mock.patch.object(orch.series, "episode_queue", return_value=q))
            s.enter_context(mock.patch.object(orch.youtube, "next_due", return_value=v))
            return o._next_episode()

    def test_deferred_video_yields_to_an_episode(self):
        o = orch.Orchestrator(); o._tv_since_yt = 5; o._yt_wait = 1
        p, why = self._decide_yt(o)                    # gate due, but deferred → episode first
        self.assertEqual(why, "ok"); self.assertFalse(p.youtube); self.assertEqual(p.ep, "S01E01")

    def test_deferred_video_drains_when_nothing_else(self):
        o = orch.Orchestrator(); o._tv_since_yt = 5; o._yt_wait = 1
        p, why = self._decide_yt(o, ep=False)          # no TV at all → drain (never stall)
        self.assertEqual(why, "ok"); self.assertTrue(p.youtube)

    def test_yt_turn_budget_sets_yt_wait_not_movie_wait(self):
        o = orch.Orchestrator(); o._enabled = True
        p = orch.youtube_paths("Chan", self.VP, "T")
        with mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st == "download"), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch("stages.run_stage",
                        return_value=(False, "turn-budget: paused at a segment boundary")):
            o._process(p)
        self.assertEqual(o._yt_wait, 1)                # the VIDEO is deferred
        self.assertEqual(o._movie_wait, 0)             # movies untouched
        self.assertEqual(o._fail_counts, {})           # not a failure

    def test_tv_completion_releases_a_deferred_video(self):
        o = orch.Orchestrator(); o._enabled = True; o._yt_wait = 1
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch("stages.run_stage", return_value=(True, "ok")), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.series, "get_active_series", return_value=["A"]), \
             mock.patch.object(orch.movies, "decrement_positions"), \
             mock.patch.object(orch.movies, "get_selected", return_value=[]):
            o._process(p)
            o._finish_item(o._finish_q.get_nowait(), lambda st, *_a, **_k: (True, "ok"))
        self.assertEqual(o._yt_wait, 0)


class SegmentETA(unittest.TestCase):
    """Per-segment eta: the windowed rate applied to the current segment's remainder,
    plus the projected average seconds per segment (the UI's >10-min gate)."""
    def test_segment_eta_and_average(self):
        o = orch.Orchestrator()
        info = {}
        # steady 0.05%/s for 100s → rate window established
        for t in range(0, 101, 10):
            info = {"stage": "topaz", "ep": "E", "pct": t * 0.05,
                    "seg_rem_pct": 2.0, "seg_total": 25}
            with mock.patch.object(orch.time, "time", return_value=float(t)):
                o._set_progress(info)
        # rate = 0.05 %/s → 2.0% remaining in this segment ≈ 40s
        self.assertAlmostEqual(info["seg_eta_secs"], 40, delta=8)
        # average segment = (100%/25)/0.05%/s = 80s
        self.assertAlmostEqual(info["avg_seg_secs"], 80, delta=10)

    def test_no_segment_fields_no_eta(self):
        o = orch.Orchestrator()
        info = {}
        for t in range(0, 101, 10):
            info = {"stage": "topaz", "ep": "E", "pct": t * 0.05}
            with mock.patch.object(orch.time, "time", return_value=float(t)):
                o._set_progress(info)
        self.assertNotIn("seg_eta_secs", info)
        self.assertNotIn("avg_seg_secs", info)


class PlexFailsafe(unittest.TestCase):
    """Prefetcher backs off NAS pulls while Plex is streaming."""
    def test_any_event_ors(self):
        import threading
        a, b = threading.Event(), threading.Event()
        e = orch._AnyEvent(a, b)
        self.assertFalse(e.is_set())
        b.set(); self.assertTrue(e.is_set())     # either set → set
        b.clear(); a.set(); self.assertTrue(e.is_set())

    def test_prefetch_download_carries_the_plex_abort(self):
        # the PREFETCH path OR's _plex_abort onto the run abort; setting plex_abort makes the
        # download's abort fire even when the run abort is clear.
        o = orch.Orchestrator()
        captured = {}
        def fake_run_stage(stage, p, *, abort=None, **kw):
            captured["abort"] = abort
            return True, "ok"
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch("stages.run_stage", side_effect=fake_run_stage):
            o._download_once(p, low_prio=True, extra_abort=o._plex_abort)
        self.assertFalse(captured["abort"].is_set())
        o._plex_abort.set()
        self.assertTrue(captured["abort"].is_set())      # plex playing → prefetch pull aborts

    def test_foreground_download_ignores_plex(self):
        o = orch.Orchestrator(); o._plex_abort.set()     # Plex playing
        captured = {}
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch("stages.run_stage", side_effect=lambda s, p, *, abort=None, **k: captured.update(abort=abort) or (True, "ok")):
            o._download_once(p, on_progress=None)         # foreground path (no extra_abort)
        self.assertFalse(captured["abort"].is_set())      # the live download is NOT gated by Plex


class PipelineOverQueue(unittest.TestCase):
    """Pipeline > queue: when the active item's write needs disk, the prefetch buffer is sacrificed."""

    def test_purge_all_prefetch_removes_every_buffer_file(self):
        import tempfile, os as _os
        pf = tempfile.mkdtemp()
        for n in ("a.mp4", "b_cfr.mp4", "c.mkv"):
            open(_os.path.join(pf, n), "w").close()
        with mock.patch.object(orch.scratch, "prefetch_dir", return_value=pf):
            n = orch.Orchestrator()._purge_all_prefetch()
        self.assertEqual(n, 3)
        self.assertEqual(_os.listdir(pf), [])              # buffer emptied

    def test_purge_all_prefetch_no_dir_is_safe(self):
        with mock.patch.object(orch.scratch, "prefetch_dir", return_value="/no/such/prefetch"):
            self.assertEqual(orch.Orchestrator()._purge_all_prefetch(), 0)

    def test_reclaim_purges_when_raw_free_below_floor(self):
        o = orch.Orchestrator()
        # raw free 10 GB < 600 floor → purge the buffer, then re-read (now 700)
        with mock.patch.object(orch.scratch, "physical_free_gb", side_effect=[10, 700]), \
             mock.patch.object(o, "_purge_all_prefetch", return_value=4) as purge:
            free = o._reclaim_for_pipeline(need_gb=600)
        purge.assert_called_once()
        self.assertEqual(free, 700)

    def test_reclaim_skips_purge_when_free_ample(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=800), \
             mock.patch.object(o, "_purge_all_prefetch") as purge:
            free = o._reclaim_for_pipeline(need_gb=600)
        purge.assert_not_called()                          # nothing sacrificed when there's room
        self.assertEqual(free, 800)

    def test_reclaim_skips_purge_when_free_unreadable(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=None), \
             mock.patch.object(o, "_purge_all_prefetch") as purge:
            self.assertIsNone(o._reclaim_for_pipeline())
        purge.assert_not_called()                          # don't destroy the queue on a bad read


class ElapsedAccumulator(unittest.TestCase):
    """A stage's elapsed is per-(item,stage) and ACCUMULATES across pause/resume — never resets to 0."""
    def setUp(self):
        import tempfile, os as _os
        self.f = _os.path.join(tempfile.mkdtemp(), "elapsed.json")
        self.p = mock.patch.object(orch, "ELAPSED_FILE", self.f); self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_accumulates_across_pause_and_resume(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.time, "monotonic", side_effect=[100.0, 110.0]):
            o._elapsed_begin("X|topaz")                      # anchor @100 (base 0)
            self.assertAlmostEqual(o._elapsed_value(), 10.0) # @110 → 10s
        with mock.patch.object(orch.time, "monotonic", return_value=115.0):
            o._elapsed_pause()                               # fold: base = 15s, persisted
        self.assertAlmostEqual(orch._elapsed_map()["X|topaz"], 15.0)
        # RESUME (e.g. after a pause near the end): keeps counting from 15s, NOT from 0
        with mock.patch.object(orch.time, "monotonic", side_effect=[200.0, 205.0]):
            o._elapsed_begin("X|topaz")                      # loads base=15, anchor @200
            self.assertAlmostEqual(o._elapsed_value(), 20.0) # @205 → 15 + 5

    def test_done_clears_so_a_rerun_starts_fresh(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.time, "monotonic", return_value=100.0):
            o._elapsed_begin("Y|remux"); o._elapsed_pause()
        self.assertIn("Y|remux", orch._elapsed_map())
        o._elapsed_done("Y|remux")
        self.assertNotIn("Y|remux", orch._elapsed_map())

    def test_stages_are_independent_keys(self):
        o = orch.Orchestrator()
        with mock.patch.object(orch.time, "monotonic", side_effect=[10.0, 40.0]):
            o._elapsed_begin("Z|download"); o._elapsed_pause()   # 30s in download
        with mock.patch.object(orch.time, "monotonic", side_effect=[50.0, 55.0]):
            o._elapsed_begin("Z|topaz")                          # topaz starts FRESH at 0
            self.assertAlmostEqual(o._elapsed_value(), 5.0)
        self.assertAlmostEqual(orch._elapsed_map()["Z|download"], 30.0)  # download's total untouched


class QuietMode(unittest.TestCase):
    """QUIET MODE: keep download+topaz running but hold each item before the screen-invasive Resolve
    stage, process others, and resume when it's turned off. Movies key on basename, TV/YouTube on ep."""

    def test_skip_key_movie_uses_basename_tv_uses_ep(self):
        o = orch.Orchestrator()
        mv = orch.movie_paths("Movie.mkv", "/Media/Movies/M", "M")
        tv = episode_paths("The Office", "S02E10", SRC)
        self.assertEqual(o._skip_key(mv), "Movie.mkv")     # basename WITH extension (movies.next_due key)
        self.assertEqual(o._skip_key(tv), "S02E10")        # p.ep

    def test_process_defers_before_resolve_when_quiet(self):
        o = orch.Orchestrator(); o._enabled = True
        p = orch.movie_paths("Movie.mkv", "/Media/Movies/M", "M")
        ran = []
        with mock.patch.object(o, "_quiet_mode", return_value=True), \
             mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st in ("download", "topaz")), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch("stages.run_stage", side_effect=lambda st, *_a, **_k: ran.append(st) or (True, "ok")):
            o._process(p)
        self.assertEqual(ran, [])                          # Resolve (and remux/upload/cleanup) never ran
        self.assertIn("Movie.mkv", o._resolve_deferred)    # held for later
        self.assertIn("Screen Control", o.state["message"])

    def test_next_episode_skips_resolve_deferred(self):
        o = orch.Orchestrator(); o._resolve_deferred = {"S02E10"}
        def eq(ref, skip=()):
            nxt = None if "S02E10" in skip else {"ep": "S02E10", "source_name": SRC}
            return {"next": nxt, "done_count": 1, "source_count": 1}
        with mock.patch.object(orch.movies, "next_due", return_value=None), \
             mock.patch.object(orch.youtube, "next_due", return_value=None), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "episode_queue", side_effect=eq):
            p, why = o._next_episode()
        self.assertIsNone(p)                               # the only episode is Quiet-held → nothing to pick
        self.assertEqual(why, "complete")

    def test_resume_clears_deferred_when_quiet_off(self):
        o = orch.Orchestrator(); o._resolve_deferred = {"X"}
        with mock.patch.object(o, "_quiet_mode", return_value=False):
            o._maybe_resume_deferred()
        self.assertEqual(o._resolve_deferred, set())

    def test_resume_keeps_deferred_while_quiet_on(self):
        o = orch.Orchestrator(); o._resolve_deferred = {"X"}
        with mock.patch.object(o, "_quiet_mode", return_value=True):
            o._maybe_resume_deferred()
        self.assertEqual(o._resolve_deferred, {"X"})

    def test_low_disk_pause_gates_on_physical_free_in_quiet_mode(self):
        # available_gb ample (scratch full of topaz outputs) but raw physical free low → MUST pause.
        o = orch.Orchestrator()
        with mock.patch.object(o, "_quiet_mode", return_value=True), \
             mock.patch.object(orch.scratch, "physical_free_gb", return_value=10), \
             mock.patch.object(o, "_free_scratch_gb", return_value=900):
            msg = o._low_disk_pause()
        self.assertIsNotNone(msg); self.assertIn("Quiet Mode", msg)

    def test_low_disk_pause_normal_mode_ignores_physical(self):
        # same low physical free, but normal mode → scratch is reclaimable → do NOT pause.
        o = orch.Orchestrator()
        with mock.patch.object(o, "_quiet_mode", return_value=False), \
             mock.patch.object(orch.scratch, "physical_free_gb", return_value=10), \
             mock.patch.object(o, "_free_scratch_gb", return_value=900):
            self.assertIsNone(o._low_disk_pause())

    def test_reclaim_screen_aborts_only_mid_resolve(self):
        o = orch.Orchestrator(); o._stage_active = True; o.state["stage"] = "resolve"
        self.assertTrue(o.reclaim_screen()); self.assertTrue(o._abort.is_set())
        o2 = orch.Orchestrator(); o2._stage_active = True; o2.state["stage"] = "topaz"
        self.assertFalse(o2.reclaim_screen()); self.assertFalse(o2._abort.is_set())


class ResolveStall(unittest.TestCase):
    """A stalled Resolve (its weekly update prompt blocks automation): HOLD topaz'd items before
    Resolve and keep upscaling the next ones into a buffer (down to STALL_FLOOR_GB), re-probing
    Resolve on a cadence, instead of parking each episode and piling ProRes intermediates."""

    def _stall_ctx(self, o, run):
        return [
            mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st in ("download", "topaz")),
            mock.patch.object(orch, "apply_container", side_effect=lambda x: x),
            mock.patch.object(o, "_claim_prefetched"),
            mock.patch.object(o, "_reclaim_for_pipeline"),
            mock.patch.object(o, "_sleep"),
            mock.patch.object(o, "_quiet_mode", return_value=False),
            mock.patch.object(o, "_hand_to_finisher"),
            mock.patch("stages.run_stage", side_effect=run),
        ]

    def _run_process(self, o, p, run):
        with contextlib.ExitStack() as es:
            for cm in self._stall_ctx(o, run):
                es.enter_context(cm)
            o._process(p)

    def test_a_single_resolve_failure_is_a_fluke_and_just_retries(self):
        o = orch.Orchestrator(); o._enabled = True
        p = episode_paths("The Office", "S02E10", SRC)
        run = lambda st, *_a, **_k: (False, "resolve failed (rc=1)") if st == "resolve" else (True, "ok")
        self._run_process(o, p, run)
        self.assertFalse(o._stall_active)                  # one failure is NOT a stall
        self.assertNotIn("S02E10", o._resolve_stall)       # not buffered — the same item retries
        self.assertEqual(o._resolve_fails.get("S02E10"), 1)
        self.assertEqual(o._parked, set())

    def test_buffering_starts_only_after_the_trigger_attempts(self):
        o = orch.Orchestrator(); o._enabled = True
        p = episode_paths("The Office", "S02E10", SRC)
        o._resolve_fails[p.ep] = orch.STALL_TRIGGER_ATTEMPTS - 1   # this failure is the Nth in a row
        run = lambda st, *_a, **_k: (False, "resolve failed (rc=1): update available") if st == "resolve" else (True, "ok")
        self._run_process(o, p, run)
        self.assertTrue(o._stall_active)                   # confirmed stall → buffer-ahead mode on
        self.assertIn("S02E10", o._resolve_stall)          # HELD before Resolve, not parked
        self.assertEqual(o._parked, set())
        self.assertEqual(o._fail_counts, {})               # a resolve fail doesn't use the generic streak
        self.assertEqual(o._finish_q.qsize(), 0)           # a failed resolve never hands off

    def test_next_episode_skips_resolve_stalled_items(self):
        o = orch.Orchestrator(); o._resolve_stall = {"S02E10"}
        def eq(ref, skip=()):
            nxt = None if "S02E10" in skip else {"ep": "S02E10", "source_name": SRC}
            return {"next": nxt, "done_count": 1, "source_count": 1}
        with mock.patch.object(orch.movies, "next_due", return_value=None), \
             mock.patch.object(orch.youtube, "next_due", return_value=None), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "episode_queue", side_effect=eq):
            p, why = o._next_episode()
        self.assertIsNone(p)                               # the only episode is held → nothing to pick

    def test_fresh_item_is_held_without_attempting_a_stalled_resolve(self):
        o = orch.Orchestrator(); o._enabled = True
        o._stall_active = True; o._resolve_stall = {"S02E09"}; o._stall_probe = None   # already stalled
        p = episode_paths("The Office", "S02E10", SRC)
        ran = []
        run = lambda st, *_a, **_k: ran.append(st) or (True, "ok")
        self._run_process(o, p, run)
        self.assertEqual(ran, [])                          # Resolve NOT attempted (would hang to timeout)
        self.assertIn("S02E10", o._resolve_stall)          # just buffered
        self.assertEqual(o._resolve_fails, {})             # it never failed → no count against it

    def test_probe_item_retries_resolve_and_recovers_on_success(self):
        o = orch.Orchestrator(); o._enabled = True
        o._stall_active = True; o._resolve_stall = {"S02E09"}; o._stall_probe = "S02E10"   # designated probe
        p = episode_paths("The Office", "S02E10", SRC)
        ran = []
        run = lambda st, *_a, **_k: ran.append(st) or (True, "ok")
        self._run_process(o, p, run)
        self.assertEqual(ran, ["resolve"])                 # the probe DID re-test Resolve
        self.assertIsNone(o._stall_probe)                  # token consumed
        self.assertFalse(o._stall_active)                  # success → stall cleared
        self.assertEqual(o._resolve_stall, set())          # whole buffer released to drain

    def test_resolve_recovered_releases_the_whole_buffer(self):
        o = orch.Orchestrator()
        o._stall_active = True; o._resolve_stall = {"A", "B", "C"}; o._stall_probe = "A"
        o._resolve_recovered()
        self.assertFalse(o._stall_active)
        self.assertEqual(o._resolve_stall, set())
        self.assertIsNone(o._stall_probe)

    def test_persistently_failing_item_parks_after_the_cap(self):
        o = orch.Orchestrator(); o._enabled = True; o._stall_active = True
        p = episode_paths("The Office", "S02E10", SRC)
        o._resolve_fails[p.ep] = orch.STALL_MAX_ITEM_RETRIES - 1   # one more fail → genuinely bad file
        o._on_resolve_failure(p, "S02E10", "resolve failed (rc=1)")
        self.assertIn(o._skip_key(p), o._parked)                  # parked, not held forever
        self.assertNotIn("S02E10", o._resolve_stall)
        self.assertNotIn(p.ep, o._resolve_fails)

    def test_maybe_retry_stall_releases_a_probe_after_the_interval(self):
        o = orch.Orchestrator(); o._stall_active = True; o._resolve_stall = {"S02E10"}; o._stall_retry_at = 0.0
        with mock.patch.object(orch.time, "monotonic", return_value=1000.0):
            o._maybe_retry_stall()
        self.assertEqual(o._stall_probe, "S02E10")                # released as the probe
        self.assertEqual(o._resolve_stall, set())                 # re-enters selection
        self.assertEqual(o._stall_retry_at, 1000.0 + orch.STALL_RETRY_SECONDS)

    def test_maybe_retry_stall_waits_for_the_cadence(self):
        o = orch.Orchestrator(); o._stall_active = True; o._resolve_stall = {"S02E10"}; o._stall_retry_at = 2000.0
        with mock.patch.object(orch.time, "monotonic", return_value=1000.0):
            o._maybe_retry_stall()
        self.assertIsNone(o._stall_probe)
        self.assertEqual(o._resolve_stall, {"S02E10"})

    def test_maybe_retry_stall_clears_probe_when_not_stalled(self):
        o = orch.Orchestrator(); o._stall_probe = "leftover"       # _stall_active False (default)
        o._maybe_retry_stall()
        self.assertIsNone(o._stall_probe)

    def test_reactivating_clears_the_stall_so_resolve_is_retried(self):
        o = orch.Orchestrator()
        o._stall_active = True; o._resolve_stall = {"S02E10", "S02E11"}
        o._resolve_fails = {"S02E10": 3}; o._stall_probe = "S02E10"
        with mock.patch.object(o, "_start_caffeinate"), \
             mock.patch.object(o, "_finisher_reconcile"), \
             mock.patch.object(o, "_ensure"):
            o.enable()                                       # deactivate→reactivate = "retry Resolve now"
        self.assertFalse(o._stall_active)                    # stall dropped → held items re-enter selection
        self.assertEqual(o._resolve_stall, set())
        self.assertEqual(o._resolve_fails, {})               # fresh count → another 5 attempts before re-stall
        self.assertIsNone(o._stall_probe)

    def test_low_disk_pause_uses_stall_floor_not_the_normal_floor(self):
        o = orch.Orchestrator(); o._stall_active = True; o._resolve_stall = {"S02E10"}
        # below the normal 400 GB floor but above the 100 GB stall floor → keep buffering (no pause)
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=250):
            self.assertIsNone(o._low_disk_pause())
        # at/below the stall floor → pause and ask to clear the prompt
        with mock.patch.object(orch.scratch, "physical_free_gb", return_value=80):
            msg = o._low_disk_pause()
        self.assertIsNotNone(msg); self.assertIn("update prompt", msg)


class DoubleRemux(unittest.TestCase):
    """Backlog-scoped 2nd remux lane: while DRAINING a Resolve-stall buffer (>=2 finished-topaz items),
    let Resolve get 2 items ahead and run 2 remuxes at once; revert to 1-at-a-time below 2."""

    def test_recovery_moves_the_held_buffer_into_the_drain_backlog(self):
        o = orch.Orchestrator()
        o._stall_active = True; o._resolve_stall = {"A", "B", "C"}
        o._resolve_recovered()
        self.assertEqual(o._draining, {"A", "B", "C"})     # tracked as the backlog to double-remux
        self.assertEqual(o._resolve_stall, set())
        self.assertFalse(o._stall_active)

    def test_resolve_gate_keeps_single_timing_when_not_draining(self):
        o = orch.Orchestrator(); o._draining = set()
        o.state["finishing"] = {"stage": "remux"}
        self.assertTrue(o._resolve_should_hold())          # normal: hold while the one remux runs
        o.state["finishing"] = None
        self.assertFalse(o._resolve_should_hold())         # nothing remuxing, queue empty → proceed

    def test_resolve_gate_lets_two_ahead_while_draining(self):
        o = orch.Orchestrator(); o._draining = {"A", "B", "C"}
        with mock.patch.object(o, "_in_finisher_keys", return_value={"A"}):
            self.assertFalse(o._resolve_should_hold())     # only 1 in finisher → room for the 2nd remux
        with mock.patch.object(o, "_in_finisher_keys", return_value={"A", "B"}):
            self.assertTrue(o._resolve_should_hold())      # both lanes full → hold

    def test_lane2_only_helps_with_a_backlog_behind_a_busy_primary(self):
        o = orch.Orchestrator(); o._finish_q.put(object())   # an item is waiting
        o.state["finishing"] = {"stage": "remux"}            # primary lane busy
        o._draining = {"A"}
        self.assertFalse(o._lane2_should_help())             # backlog < 2 → don't split a lone item
        o._draining = {"A", "B"}
        self.assertTrue(o._lane2_should_help())              # backlog >= 2, primary busy, item waiting
        o.state["finishing"] = None
        self.assertFalse(o._lane2_should_help())             # primary idle → nothing to run alongside

    def test_lane2_finish_uses_its_own_slot_and_leaves_the_backlog(self):
        o = orch.Orchestrator(); o._enabled = True
        p = episode_paths("The Office", "S02E10", SRC)
        o._draining = {o._skip_key(p), "B"}
        seen = []
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.movies, "decrement_positions"), \
             mock.patch.object(o, "_finisher_persist_remove"), \
             mock.patch.object(o, "_save_cadence"):
            o._finish_item(p, lambda st, *a, **k: seen.append(st) or (True, "ok"), lane=2)
        self.assertEqual(seen, ["remux", "upload", "cleanup"])
        self.assertIsNone(o.state["finishing"])              # lane-1 slot untouched by lane 2
        self.assertNotIn(o._skip_key(p), o._draining)        # completed → left the backlog
        self.assertIn("B", o._draining)                      # the other item still pending

    def test_topaz_pauses_for_a_fresh_item_while_two_remuxes_drain(self):
        o = orch.Orchestrator()
        fresh = episode_paths("The Office", "S02E10", SRC)
        o._draining = {"A", "B"}
        with mock.patch.object(orch, "stage_done", return_value=False):     # fresh: not yet upscaled
            self.assertTrue(o._drain_pauses_topaz(fresh))                    # → hold Topaz
        with mock.patch.object(orch, "stage_done", return_value=True):      # already upscaled → resolve it
            self.assertFalse(o._drain_pauses_topaz(fresh))                   # (feeds the remux lanes)
        o._draining = {"A"}
        with mock.patch.object(orch, "stage_done", return_value=False):     # backlog < 2 → Topaz resumes
            self.assertFalse(o._drain_pauses_topaz(fresh))

    def test_second_lane_registered_on_enable(self):
        o = orch.Orchestrator()
        started = []
        with mock.patch.object(o, "_start_caffeinate"), \
             mock.patch.object(o, "_finisher_reconcile"), \
             mock.patch.object(o, "_ensure", side_effect=lambda name, t: started.append(name)):
            o.enable()
        self.assertIn("finisher2", started)                  # the 2nd remux lane thread is spawned


class HandoffMovieGauge(unittest.TestCase):
    """The finisher's movie-sized flag is gauged from the topaz working set, which must be measured
    BEFORE _drop_topaz_intermediates: the old gauge ran AFTER the drop and probed only p.prores —
    the legacy single-file name the no-concat design never writes — so it always raised OSError and
    a feature-length YouTube item never entered _in_finisher_movies (user-caught)."""

    def _yt(self, tmp):
        return orch.youtube_paths("Some Channel", "/staging/Some Channel/Feature Documentary.mp4",
                                  "Feature Documentary", scratch_dir=tmp)

    def test_working_bytes_sums_the_segment_chunks(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._yt(tmp)
            os.makedirs(p.segdir)
            with open(os.path.join(p.segdir, "seg000.mov"), "wb") as f:
                f.write(b"x" * 1000)
            with open(os.path.join(p.segdir, "seg001.mov"), "wb") as f:
                f.write(b"y" * 500)
            self.assertEqual(orch.Orchestrator._topaz_working_bytes(p), 1500)

    def test_working_bytes_zero_when_nothing_on_disk(self):
        p = self._yt("/nonexistent-scratch")
        self.assertEqual(orch.Orchestrator._topaz_working_bytes(p), 0)

    def test_feature_youtube_counts_movie_sized_and_is_measured_before_the_drop(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = self._yt(tmp)
            os.makedirs(p.segdir)
            with open(os.path.join(p.segdir, "seg000.mov"), "wb") as f:
                f.write(b"x" * 4096)
            o = orch.Orchestrator(); o._enabled = True
            with mock.patch.object(orch, "MOVIE_SIZED_BYTES", 1024), \
                 mock.patch.object(o, "_advance_cadence_at_handoff"):
                o._hand_to_finisher(p)                       # REAL drop — measurement must beat it
            self.assertIn(o._skip_key(p), o._in_finisher_movies)   # gauged movie-sized...
            self.assertFalse(os.path.isdir(p.segdir))              # ...even though the drop ran

    def test_small_episode_stays_non_movie(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = episode_paths("The Office X", "S02E10", SRC,
                              scratch_dir=tmp, nas_tv_root="/Media/TV-Shows")
            os.makedirs(p.segdir)
            with open(os.path.join(p.segdir, "seg000.mov"), "wb") as f:
                f.write(b"x" * 10)
            o = orch.Orchestrator(); o._enabled = True
            with mock.patch.object(o, "_advance_cadence_at_handoff"):
                o._hand_to_finisher(p)
            self.assertIn(o._skip_key(p), o._in_finisher_keys())
            self.assertNotIn(o._skip_key(p), o._in_finisher_movies)

    def test_movie_flag_short_circuits_without_measuring(self):
        o = orch.Orchestrator(); o._enabled = True
        p = orch.movie_paths("Movie.mkv", "/Media/Movies/M", "M", scratch_dir="/scratch")
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"), \
             mock.patch.object(orch.Orchestrator, "_topaz_working_bytes",
                               side_effect=AssertionError("a movie must not be measured")):
            o._hand_to_finisher(p)
        self.assertIn(o._skip_key(p), o._in_finisher_movies)


class FinisherOverlap(unittest.TestCase):
    """TOPAZ/REMUX OVERLAP: the run thread owns download/topaz/resolve, then hands the item to
    the FINISHER thread (remux/upload/cleanup) so the ~75-min x265 peak-cap re-encode runs while
    the next item downloads/upscales. In-finisher items are invisible to selection; disk gating
    switches to RAW physical free while a finisher item's working set is still resident."""

    def _ep(self):
        return episode_paths("The Office X", "S02E10", SRC,
                             scratch_dir="/scratch", nas_tv_root="/Media/TV-Shows")

    def test_stage_split_covers_all_stages_no_overlap(self):
        self.assertEqual(orch.RUN_STAGES + orch.FINISH_STAGES, orch.STAGES)

    def test_one_queued_waiter_does_not_freeze_the_run_thread(self):
        # A re-picked item with all GPU stages already done fast-paths past the resolve gate and
        # queues behind a live remux. The old qsize>0 freeze then serialized the pipeline for the
        # whole remux (user-caught) — one waiter must still overlap; the resolve GATE (not the run
        # freeze) is what holds the NEXT item at the resolve doorstep while anything is queued.
        o = orch.Orchestrator()
        self.assertFalse(o._finisher_backlogged())            # empty → run freely
        o._finish_q.put(object())                             # ONE waiter (movie fast-path scenario)
        self.assertFalse(o._finisher_backlogged())            # download/topaz still overlap...
        self.assertTrue(orch.resolve_must_wait(None, o._finish_q.qsize()))   # ...resolve holds instead
        o._finish_q.put(object())                             # TWO waiters = genuine backlog
        self.assertTrue(o._finisher_backlogged())

    def test_process_runs_gpu_stages_then_hands_off(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        ran = []
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(o, "_claim_prefetched"), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(o, "_download_once", side_effect=lambda _p, on_progress=None: ran.append("download") or (True, "ok")), \
             mock.patch.object(o, "_save_cadence"), \
             mock.patch.object(orch.series, "get_active_series", return_value=[]), \
             mock.patch("stages.run_stage", side_effect=lambda st, *_a, **_k: ran.append(st) or (True, "ok")):
            o._process(p)
        self.assertEqual(ran, ["download", "topaz", "resolve"])   # remux/upload/cleanup NOT here
        self.assertIn(o._skip_key(p), o._in_finisher_keys())      # owned by the finisher now
        self.assertEqual(o._finish_q.qsize(), 1)
        self.assertIn("finisher", o.state["message"])

    def test_no_handoff_after_abort(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        def fail_resolve(st, *_a, **_k):
            if st == "resolve":
                return (False, "boom")
            return (True, "ok")
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(o, "_claim_prefetched"), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(o, "_sleep"), \
             mock.patch.object(o, "_download_once", return_value=(True, "ok")), \
             mock.patch("stages.run_stage", side_effect=fail_resolve):
            o._process(p)
        self.assertEqual(o._finish_q.qsize(), 0)                  # a failed resolve never hands off
        self.assertEqual(o._in_finisher_keys(), set())

    def test_next_episode_skips_in_finisher_items(self):
        o = orch.Orchestrator()
        with o._finisher_lock:
            o._in_finisher.add("S02E10")
        def eq(ref, skip=()):
            nxt = None if "S02E10" in skip else {"ep": "S02E10", "source_name": SRC}
            return {"next": nxt, "done_count": 1, "source_count": 1}
        with mock.patch.object(orch.movies, "next_due", return_value=None), \
             mock.patch.object(orch.youtube, "next_due", return_value=None), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office X"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "episode_queue", side_effect=eq):
            p, why = o._next_episode()
        self.assertIsNone(p)                                      # the finisher owns it → not re-picked

    def test_finisher_runs_finish_stages_in_order(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        ran = []
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.movies, "decrement_positions"), \
             mock.patch.object(o, "_participants", return_value=[]):
            o._finish_item(p, lambda st, *_a, **_k: ran.append(st) or (True, "ok"))
        self.assertEqual(ran, ["remux", "upload", "cleanup"])

    def test_finisher_failure_counts_and_parks(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        o._fail_counts[p.ep] = orch.MAX_EPISODE_FAILS - 1
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"):
            o._finish_item(p, lambda st, *_a, **_k: (False, "x265 exploded"))
        self.assertIn(o._skip_key(p), o._parked)                  # threshold reached → parked
        self.assertNotIn(p.ep, o._fail_counts)

    def test_finisher_abort_is_not_a_failure(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        o._finish_abort.set()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"):
            o._finish_item(p, lambda st, *_a, **_k: (False, "aborted"))
        self.assertEqual(o._fail_counts, {})                      # stop/pause abort → no fail count
        self.assertEqual(o._parked, set())

    def test_disable_aborts_the_finisher_too(self):
        o = orch.Orchestrator(); o._enabled = True
        with mock.patch.object(orch, "topaz", create=True):
            o.disable("test")
        self.assertTrue(o._finish_abort.is_set())

    def test_low_disk_gates_on_physical_free_while_finishing(self):
        o = orch.Orchestrator()
        with o._finisher_lock:
            o._in_finisher.add("S02E10")
        with mock.patch.object(o, "_quiet_mode", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.scratch, "physical_free_gb", return_value=100):
            msg = o._low_disk_pause()
        self.assertIn("finishes in the background", msg)
        with mock.patch.object(o, "_quiet_mode", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.scratch, "physical_free_gb",
                               return_value=orch.OVERLAP_MIN_PHYS_GB + 10):
            self.assertIsNone(o._low_disk_pause())                # physical headroom → proceed

    def test_no_encode_priority_throttling(self):
        # GOVERNOR + topaz nice REMOVED (2026-07-07): topaz and the finisher x265 both run at
        # NORMAL priority and freely contend — throttling the bottleneck (topaz) cost throughput.
        import dvcap, topaz
        self.assertFalse(hasattr(dvcap, "NICE"))                  # remux x265: normal priority
        self.assertFalse(hasattr(topaz, "TVAI_NICE"))             # topaz: normal priority (nice gone)
        self.assertFalse(hasattr(topaz, "HOLD"))                  # no between-segment gate
        self.assertEqual(topaz.build_command("/ff", "/i", "/o", "vf")[0], "/ff")   # no nice prefix


class FinisherResume(unittest.TestCase):
    """RESUME BOTH (user-caught): deactivating right at the start of a remux — before a segment save
    state existed — used to DROP the item. The finisher's queue is in-memory, so only the run thread
    came back on re-arm and it grabbed the NEXT topaz item, forgetting the aborted remux (only a
    manual file-delete + restart recovered it). Now the finisher work-list is DURABLE: a re-arm/
    relaunch reconciles it and resumes the remux on the finisher thread, in parallel with the run
    thread's next topaz."""

    def _ep(self, ep="S02E10", src=SRC):
        return episode_paths("The Office X", ep, src,
                             scratch_dir="/scratch", nas_tv_root="/Media/TV-Shows")

    def test_handoff_records_durable_ownership(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        self.assertIn(o._skip_key(p), o._finisher_persisted_keys())   # durable now
        import os, json
        with open(orch.FINISHER_FILE) as f:                           # and actually on disk
            self.assertEqual(json.load(f)[0]["ep"], "S02E10")

    def test_relaunch_loads_and_reconciles_the_remux(self):
        # Orchestrator A hands off, then "dies" mid-remux (no upload). A fresh Orchestrator B
        # (relaunch) must re-queue that item onto the finisher — WITHOUT the run thread's help.
        a = orch.Orchestrator(); a._enabled = True
        p = self._ep()
        with mock.patch.object(a, "_drop_topaz_intermediates"), \
             mock.patch.object(a, "_advance_cadence_at_handoff"):
            a._hand_to_finisher(p)
        b = orch.Orchestrator()                                       # relaunch: reads the file
        self.assertIn(o_key := b._skip_key(p), b._finisher_persisted_keys())
        self.assertEqual(b._finish_q.qsize(), 0)                      # not queued until reconcile
        b._finisher_reconcile()
        self.assertIn(o_key, b._in_finisher_keys())                   # finisher now owns it
        self.assertEqual(b._finish_q.qsize(), 1)                      # remux is queued to resume

    def test_reconcile_is_idempotent(self):
        a = orch.Orchestrator(); a._enabled = True
        with mock.patch.object(a, "_drop_topaz_intermediates"), \
             mock.patch.object(a, "_advance_cadence_at_handoff"):
            a._hand_to_finisher(self._ep())
        b = orch.Orchestrator()
        b._finisher_reconcile(); b._finisher_reconcile(); b._finisher_reconcile()
        self.assertEqual(b._finish_q.qsize(), 1)                      # never double-queued

    def test_upload_success_clears_the_durable_worklist(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.movies, "decrement_positions"):
            o._finish_item(p, lambda st, *_a, **_k: (True, "ok"))     # remux→upload→cleanup all OK
        self.assertNotIn(o._skip_key(p), o._finisher_persisted_keys())  # terminal → dropped
        b = orch.Orchestrator()                                       # a relaunch would NOT resume it
        b._finisher_reconcile()
        self.assertEqual(b._finish_q.qsize(), 0)

    def test_abort_keeps_item_for_resume(self):
        # A stop/pause abort must NOT drop the item — that's exactly what has to survive to resume.
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        o._finish_abort.set()
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"):
            o._finish_item(p, lambda st, *_a, **_k: (False, "aborted"))
        self.assertIn(o._skip_key(p), o._finisher_persisted_keys())   # still durable → will resume

    def test_park_clears_the_durable_worklist(self):
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        o._fail_counts[p.ep] = orch.MAX_EPISODE_FAILS - 1
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(o, "_save_cadence"):
            o._finish_item(p, lambda st, *_a, **_k: (False, "x265 exploded"))
        self.assertIn(o._skip_key(p), o._parked)
        self.assertNotIn(o._skip_key(p), o._finisher_persisted_keys())  # parked → stop resuming it

    def test_next_episode_skips_a_durably_persisted_item(self):
        # The disable→enable discard race: the item is momentarily out of _in_finisher but still on
        # the durable work-list. Selection must exclude it (the reconcile owns its resume).
        o = orch.Orchestrator()
        p = self._ep()
        with o._finisher_lock:                                        # persisted but NOT in_finisher
            d = o._finisher_descriptor(p)
            o._finisher_persisted[o._desc_cid(d)] = d
        def eq(ref, skip=()):
            nxt = None if "S02E10" in skip else {"ep": "S02E10", "source_name": SRC}
            return {"next": nxt, "done_count": 1, "source_count": 1}
        with mock.patch.object(orch.movies, "next_due", return_value=None), \
             mock.patch.object(orch.youtube, "next_due", return_value=None), \
             mock.patch.object(orch.series, "get_active_series", return_value=["The Office X"]), \
             mock.patch.object(orch.series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(orch.series, "episode_queue", side_effect=eq):
            picked, why = o._next_episode()
        self.assertIsNone(picked)                                     # not re-picked by the run thread

    def test_descriptor_roundtrip_episode_movie_youtube(self):
        from orchestrator import movie_paths, youtube_paths
        ep = self._ep()
        mv = movie_paths("Big Movie (2020).mkv", "/Media/Movies/Big Movie (2020)", "Big Movie",
                         scratch_dir="/scratch")
        yt = youtube_paths("SomeChannel", "/Media/YouTube-raw/SomeChannel/vid/clip.mp4", "Clip",
                           scratch_dir="/scratch")
        o = orch.Orchestrator()
        with mock.patch.object(orch.scratch, "default_scratch", return_value="/scratch"):
            for original in (ep, mv, yt):     # same scratch both sides (as in production)
                d = o._finisher_descriptor(original)
                back = o._finisher_reconstruct(d)
                self.assertEqual((back.series, back.ep, back.nas_final, back.movie, back.youtube,
                                  back.source_cfr, back.final),
                                 (original.series, original.ep, original.nas_final,
                                  original.movie, original.youtube, original.source_cfr, original.final),
                                 msg=f"round-trip broke for {d['kind']}")

    def test_mkv_container_is_preserved_across_resume(self):
        # review-caught HIGH: an item whose source needs MKV (lossless audio / bitmap subs) had
        # apply_container() rewrite final/source_cfr/nas_final to .mkv on the run thread. The resume
        # path must NOT rebuild it as the .mp4 default (that silently corrupts / orphans the master).
        p = self._ep()
        orch.relabel_container(p, ".mkv")                        # simulate apply_container's MKV pick
        self.assertTrue(p.final.endswith(".mkv"))
        o = orch.Orchestrator()
        d = o._finisher_descriptor(p)
        self.assertEqual(d["container_ext"], ".mkv")             # captured at hand-off
        with mock.patch.object(orch.scratch, "default_scratch", return_value="/scratch"):
            back = o._finisher_reconstruct(d)
        self.assertTrue(back.final.endswith(".mkv"), "resumed remux would write the wrong container")
        self.assertTrue(back.source_cfr.endswith(".mkv"))
        self.assertTrue(back.nas_final.endswith(".mkv"))
        self.assertEqual(back.final, p.final)                   # identical target the run thread chose

    def test_cleanup_is_durably_protected_after_upload(self):
        # review-caught MEDIUM: dropping the item at the upload boundary left a window where a
        # deactivate between upload and cleanup lost the ~250-350 GB working set (cleanup never
        # re-ran). The item must stay on the durable work-list until cleanup ALSO completes.
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        def rs(st, *_a, **_k):
            if st == "upload":
                o._finish_abort.set()          # user deactivates the instant upload lands
            return (True, "ok")
        with mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.movies, "decrement_positions"):
            o._finish_item(p, rs)              # remux ok, upload ok, then abort BEFORE cleanup
        self.assertIn(o._skip_key(p), o._finisher_persisted_keys())   # still durable → cleanup resumes
        # and on resume, with cleanup now completing, it finally drops:
        o._finish_abort.clear()
        with mock.patch.object(orch, "stage_done",
                               side_effect=lambda st, _p, **_k: st in ("remux", "upload")), \
             mock.patch.object(o, "_reclaim_for_pipeline"):
            o._finish_item(p, lambda st, *_a, **_k: (True, "ok"))     # cleanup runs → terminal
        self.assertNotIn(o._skip_key(p), o._finisher_persisted_keys())

    def test_finisher_views_expose_the_exclusion_key(self):
        # up_next uses these to keep a finisher-owned item OUT of the queue (its remux/upload hasn't
        # put a master on the NAS, so it still looks 'remaining') — must carry the kind + skip key.
        o = orch.Orchestrator(); o._enabled = True
        p = self._ep()
        with mock.patch.object(o, "_drop_topaz_intermediates"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"):
            o._hand_to_finisher(p)
        views = o.finisher_views()
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["kind"], "episode")
        self.assertEqual(views[0]["ep"], "S02E10")             # the key up_next filters on

    def test_corrupt_worklist_file_is_ignored(self):
        import os
        os.makedirs(os.path.dirname(orch.FINISHER_FILE), exist_ok=True)
        with open(orch.FINISHER_FILE, "w") as f:
            f.write("{ this is not valid json ]")
        o = orch.Orchestrator()                                       # must not raise
        self.assertEqual(o._finisher_persisted, {})
        os.remove(orch.FINISHER_FILE)


class ContentionETA(unittest.TestCase):
    """Overlap changes the shared-power throughput: the remux runs slow while topaz shares the
    machine and speeds up the instant topaz ends (and vice versa). Each stage's ETA window must
    RE-ANCHOR on that transition so it tracks the new rate instead of blending both regimes."""

    def test_remux_eta_reanchors_when_topaz_ends(self):
        o = orch.Orchestrator()
        o.state["stage"] = "topaz"                         # topaz running → remux is contended
        o._set_finishing_progress({"stage": "remux", "pct": 10, "frames": 100, "total": 1000})
        self.assertTrue(o._fin_contended)
        a1 = o._fin_eta_anchor
        o._set_finishing_progress({"stage": "remux", "pct": 12, "frames": 120, "total": 1000})
        self.assertEqual(o._fin_eta_anchor[1], a1[1])      # topaz still on → same window, no re-anchor
        o.state["stage"] = None                            # topaz ENDS → remux now solo
        o._set_finishing_progress({"stage": "remux", "pct": 13, "frames": 130, "total": 1000})
        self.assertFalse(o._fin_contended)
        self.assertEqual(o._fin_eta_anchor[2], 130)        # re-anchored at the current frame (fresh rate)

    def test_topaz_eta_reanchors_when_remux_ends(self):
        o = orch.Orchestrator()
        o.state["finishing"] = {"stage": "remux"}          # remux running → topaz is contended
        o._set_progress({"stage": "topaz", "ep": "S06E11", "pct": 10})
        self.assertTrue(o._run_contended)
        o.state["finishing"] = None                        # remux ENDS → topaz now solo
        o._set_progress({"stage": "topaz", "ep": "S06E11", "pct": 12})
        self.assertFalse(o._run_contended)
        self.assertEqual(o._progress_start_pct, 12)        # window re-anchored at the current %

    def test_no_reanchor_without_a_contention_change(self):
        o = orch.Orchestrator()
        o.state["stage"] = None                            # no topaz → remux uncontended throughout
        o._set_finishing_progress({"stage": "remux", "pct": 10, "frames": 100, "total": 1000})
        a1 = o._fin_eta_anchor
        o._set_finishing_progress({"stage": "remux", "pct": 20, "frames": 200, "total": 1000})
        self.assertEqual(o._fin_eta_anchor[1], a1[1])      # steady state → anchor holds (real ETA accrues)


class CadenceOncePerItem(unittest.TestCase):
    """Review-caught HIGH: a finisher retry / power requeue / disable-drop / restart re-hands-off
    the same item — its scheduling turn must count exactly ONCE until it fully completes."""

    def setUp(self):
        self._cm = mock.patch.object(orch, "CADENCE_FILE",
                                     "/tmp/_test_cadence_once.json")
        self._cm.start()
        import os
        try: os.remove("/tmp/_test_cadence_once.json")
        except OSError: pass

    def tearDown(self):
        self._cm.stop()
        import os
        try: os.remove("/tmp/_test_cadence_once.json")
        except OSError: pass

    def test_second_handoff_does_not_double_count(self):
        o = orch.Orchestrator(); o._enabled = True
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(o, "_participants", return_value=["A"]):
            o._advance_cadence_at_handoff(p)
            self.assertEqual(o._tv_since_yt, 1)
            o._advance_cadence_at_handoff(p)          # finisher retry re-hand-off
            o._advance_cadence_at_handoff(p)          # and again after a restart
        self.assertEqual(o._tv_since_yt, 1)           # counted ONCE

    def test_guard_survives_restart(self):
        a = orch.Orchestrator(); a._enabled = True
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(a, "_participants", return_value=["A"]):
            a._advance_cadence_at_handoff(p)
        b = orch.Orchestrator()                       # relaunch mid-finisher
        with mock.patch.object(b, "_participants", return_value=["A"]):
            b._advance_cadence_at_handoff(p)          # re-pick + re-hand-off after restart
        self.assertEqual(b._tv_since_yt, 1)           # still counted once

    def test_completion_frees_the_key_for_a_future_redo(self):
        o = orch.Orchestrator(); o._enabled = True
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(o, "_participants", return_value=["A"]), \
             mock.patch.object(orch, "stage_done", return_value=False), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.series, "refresh_queue"), \
             mock.patch.object(orch.movies, "decrement_positions"):
            o._advance_cadence_at_handoff(p)
            o._finish_item(p, lambda st, *_a, **_k: (True, "ok"))   # completes (upload ok)
            self.assertNotIn(p.source_basename, o._cadence_advanced)
            o._advance_cadence_at_handoff(p)          # a FULL redo later is a genuine new turn
        self.assertEqual(o._tv_since_yt, 2)


class DropTopazAtHandoff(unittest.TestCase):
    """The finisher never reads the ProRes/segments — they're deleted AT HAND-OFF, not held
    ~hours until cleanup (the overlap made that ~300 GB of dead scratch weight)."""

    def test_handoff_deletes_prores_and_segments(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as td:
            o = orch.Orchestrator(); o._enabled = True
            p = episode_paths("A", "S01E01", SRC, scratch_dir=td)
            open(p.prores, "w").write("x")
            os.makedirs(p.segdir, exist_ok=True)
            open(os.path.join(p.segdir, "seg_0000.mov"), "w").write("x")
            open(p.dv_render, "w").write("x")     # the file the finisher DOES need
            with mock.patch.object(o, "_advance_cadence_at_handoff"):
                o._hand_to_finisher(p)
            self.assertFalse(os.path.exists(p.prores))
            self.assertFalse(os.path.exists(p.segdir))
            self.assertTrue(os.path.exists(p.dv_render))   # untouched
            self.assertEqual(o._finish_q.qsize(), 1)


class ResolveGate(unittest.TestCase):
    """The next item may not START Resolve while the previous item's remux is in flight or
    queued — Resolve can't be paced, and its load would crater the un-resumable x265."""

    def test_pure_gate(self):
        self.assertTrue(orch.resolve_must_wait({"stage": "remux"}, 0))
        self.assertTrue(orch.resolve_must_wait(None, 1))               # queued, not yet started
        self.assertFalse(orch.resolve_must_wait({"stage": "upload"}, 0))   # light tail — no wait
        self.assertFalse(orch.resolve_must_wait(None, 0))

    def test_process_holds_resolve_until_remux_clears(self):
        o = orch.Orchestrator(); o._enabled = True
        o.state["finishing"] = {"stage": "remux", "ep": "S06E03"}
        p = episode_paths("A", "S01E01", SRC)
        ran = []
        def fake_sleep(_s):                       # remux "finishes" after the first hold tick
            o.state["finishing"] = None
        with mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st in ("download", "topaz")), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(o, "_claim_prefetched"), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(o, "_advance_cadence_at_handoff"), \
             mock.patch.object(orch.time, "sleep", side_effect=fake_sleep), \
             mock.patch("stages.run_stage", side_effect=lambda st, *_a, **_k: ran.append(st) or (True, "ok")):
            o._process(p)
        self.assertEqual(ran, ["resolve"])        # held once, then proceeded
        self.assertIn(o._skip_key(p), o._in_finisher_keys())

    def test_screen_control_off_during_gate_hold_defers(self):
        # DUAL-SYSTEM BUG: disabling Screen Control (quiet mode) WHILE an item holds in the resolve
        # gate must DEFER it — not launch Resolve when the previous remux clears. The gate hold can
        # span the whole previous remux, so the check must live INSIDE the hold loop, not only before.
        o = orch.Orchestrator(); o._enabled = True
        o.state["finishing"] = {"stage": "remux", "ep": "S06E03"}   # remux in flight → gate closed
        p = episode_paths("A", "S01E01", SRC)
        ran = []
        def flip_off_mid_hold(_s):
            o._quiet_flag = True                  # user turns Screen Control OFF (remux keeps running)
        with mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st in ("download", "topaz")), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(o, "_claim_prefetched"), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(o, "_quiet_mode", side_effect=lambda: getattr(o, "_quiet_flag", False)), \
             mock.patch.object(orch.time, "sleep", side_effect=flip_off_mid_hold), \
             mock.patch("stages.run_stage", side_effect=lambda st, *_a, **_k: ran.append(st) or (True, "ok")):
            o._process(p)
        self.assertEqual(ran, [])                             # Resolve NEVER launched
        self.assertIn(o._skip_key(p), o._resolve_deferred)    # deferred instead

    def test_stop_while_holding_returns_cleanly(self):
        o = orch.Orchestrator(); o._enabled = True
        o.state["finishing"] = {"stage": "remux", "ep": "S06E03"}
        p = episode_paths("A", "S01E01", SRC)
        def stop(_s):
            o._enabled = False
        with mock.patch.object(orch, "stage_done", side_effect=lambda st, _p: st in ("download", "topaz")), \
             mock.patch.object(orch, "apply_container", side_effect=lambda x: x), \
             mock.patch.object(o, "_claim_prefetched"), \
             mock.patch.object(o, "_reclaim_for_pipeline"), \
             mock.patch.object(orch.time, "sleep", side_effect=stop), \
             mock.patch("stages.run_stage", side_effect=lambda st, *_a, **_k: (True, "ok")):
            o._process(p)
        self.assertEqual(o._finish_q.qsize(), 0)  # never handed off — clean stop mid-hold


class TopazSkipForward(unittest.TestCase):
    def test_valid_dv_render_counts_topaz_done(self):
        # segments dropped at hand-off + remux aborted → resume must go straight to remux
        p = episode_paths("A", "S01E01", SRC)
        good = [{"codec_type": "video", "side_data_list": [
            {"side_data_type": "DOVI configuration record", "dv_profile": 8,
             "dv_bl_signal_compatibility_id": 1}]}]
        with mock.patch.object(orch, "_vstream", return_value=good):
            self.assertTrue(orch.stage_done("topaz", p))

    def test_no_dv_render_still_requires_segments(self):
        p = episode_paths("A", "S01E01", SRC)
        with mock.patch.object(orch, "_vstream", return_value=None), \
             mock.patch("topaz.segments_complete", return_value=False):
            self.assertFalse(orch.stage_done("topaz", p))


class FinisherEta(unittest.TestCase):
    """Lane ETA comes from THIS attempt's live rate — the accumulated elapsed spans killed
    attempts and read ~38 h after a remux restart (live-hit S06E03)."""

    def test_eta_uses_attempt_rate_not_accumulated_elapsed(self):
        o = orch.Orchestrator()
        o._fin_el_base = 7000.0                     # ~2 h of prior-attempt elapsed on the books
        clk = {"t": 100.0}                          # deterministic clock, any call count
        with mock.patch.object(orch.time, "monotonic", side_effect=lambda: clk["t"]):
            o._set_finishing_progress({"stage": "remux", "frames": 0, "total": 41071, "pct": 0})
            clk["t"] = 200.0                        # +100 s, +1000 frames → 10 fps live
            o._set_finishing_progress({"stage": "remux", "frames": 1000, "total": 41071, "pct": 2.4})
        eta = (o.state["finishing"] or {}).get("eta_secs")
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta, (41071 - 1000) * 100 / 1000, delta=1)   # ~4007 s, NOT 38 h

    def test_no_eta_until_warmup(self):
        o = orch.Orchestrator()
        clk = {"t": 100.0}
        with mock.patch.object(orch.time, "monotonic", side_effect=lambda: clk["t"]):
            o._set_finishing_progress({"stage": "remux", "frames": 0, "total": 41071, "pct": 0})
            clk["t"] = 105.0                        # only 5 s / 10 frames — below warmup
            o._set_finishing_progress({"stage": "remux", "frames": 10, "total": 41071, "pct": 0})
        self.assertNotIn("eta_secs", o.state["finishing"] or {})

