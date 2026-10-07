import json
import os
import tempfile
import unittest
from unittest import mock

import series
from series import parse_episodes, build_queue

NAMES = [
    "The Office  S01e01 Pilot (Extended Cut).mp4",
    "The Office  S01e02 Diversity Day (Extended Cut).mp4",
    "The Office  S01e02 Diversity Day (Extended Cut) HDR10 DV.mp4",   # e02 done
    "The Office  S01e03 Health Care (Extended Cut).mp4",
    "The Office  S01e10 Finale (Extended Cut).mp4",                   # numeric order vs e03
    "The Office  S01e04 only-master (Extended Cut) HDR10 DV.mp4",     # done, no source
    "season-poster.jpg",                                             # ignored (not video)
]


class Parse(unittest.TestCase):
    def test_groups_and_flags_per_episode(self):
        eps = {e["ep"]: e for e in parse_episodes(NAMES)}
        self.assertTrue(eps["S01E01"]["has_source"] and not eps["S01E01"]["has_dv"])
        self.assertTrue(eps["S01E02"]["has_source"] and eps["S01E02"]["has_dv"])   # both
        self.assertTrue(eps["S01E04"]["has_dv"] and not eps["S01E04"]["has_source"])
        self.assertEqual(eps["S01E01"]["source_name"], NAMES[0])

    def test_ignores_non_video(self):
        self.assertFalse(any("poster" in str(e) for e in parse_episodes(NAMES)))


class Queue(unittest.TestCase):
    def test_next_is_first_unprocessed_source(self):
        self.assertEqual(build_queue(NAMES)["next"]["ep"], "S01E01")

    def test_remaining_skips_done_and_sourceless_in_order(self):
        q = build_queue(NAMES)
        self.assertEqual(q["remaining"], ["S01E01", "S01E03", "S01E10"])  # e02/e04 excluded
        self.assertEqual(q["remaining_count"], 3)

    def test_numeric_ordering_e10_after_e03(self):
        self.assertEqual(build_queue(NAMES)["remaining"][-1], "S01E10")

    def test_remaining_items_carry_titles_and_exclude_parked(self):
        q = build_queue(NAMES, skip={"S01E03"})           # park E03
        self.assertEqual([it["ep"] for it in q["remaining_items"]], ["S01E01", "S01E10"])
        self.assertEqual(q["remaining_items"][0]["source_name"], NAMES[0])   # for the title

    def test_done_count(self):
        self.assertEqual(build_queue(NAMES)["done_count"], 2)   # e02, e04

    def test_empty_when_all_done(self):
        q = build_queue(["X S01e01 (Extended Cut) HDR10 DV.mp4"])
        self.assertIsNone(q["next"])
        self.assertEqual(q["remaining"], [])


class MasterMarks(unittest.TestCase):
    """A shipped master MUST be recognised as finished. If it is not, it is classified as a
    source and fed back through the pipeline — and with replace_source on (the default) the real
    source is already deleted, so the run would upscale its own output forever, degrading each
    pass. That failure mode is why the SDR mark exists at all."""

    SRC = "Lost (2004) - S02E19 - S.O.S. (1080p BluRay x265 Silence).mkv"
    DV  = "Lost (2004) - S02E18 - Dave (2160p x265 HDR10 DV upscaled).mp4"
    SDR = "Lost (2004) - S02E17 - Lockdown (2160p x265 SDR upscaled).mp4"

    def _by_ep(self, names):
        return {e["ep"]: e for e in series.parse_episodes(names)}

    def test_a_dolby_vision_master_is_done(self):
        e = self._by_ep([self.DV])["S02E18"]
        self.assertTrue(e["has_dv"]); self.assertFalse(e["has_source"])

    def test_an_SDR_master_is_ALSO_done(self):
        # The regression: without the SDR mark this reads as a source and re-enters the queue.
        e = self._by_ep([self.SDR])["S02E17"]
        self.assertTrue(e["has_dv"]); self.assertFalse(e["has_source"])

    def test_a_real_source_is_still_a_source(self):
        # The mark must not be so loose that ordinary files match it.
        e = self._by_ep([self.SRC])["S02E19"]
        self.assertFalse(e["has_dv"]); self.assertTrue(e["has_source"])

    def test_neither_mark_matches_an_ordinary_name(self):
        self.assertFalse(series.is_master_name("Some.Show.S01E01.1080p.BluRay.x264.mkv"))
        self.assertFalse(series.is_master_name(""))
        self.assertFalse(series.is_master_name(None))

    def test_an_SDR_master_is_never_queued(self):
        q = series.build_queue([self.SDR, self.DV])
        self.assertIsNone(q["next"])
        self.assertEqual(q["done_count"], 2)


class Watched(unittest.TestCase):
    def test_unwatched_first_then_watched_each_numeric(self):
        # e01 watched, e03 + e10 unwatched -> unwatched group (numeric) then watched
        wm = {NAMES[0]: True, NAMES[3]: False, NAMES[4]: False}
        q = build_queue(NAMES, watched_map=wm)
        self.assertEqual(q["remaining"], ["S01E03", "S01E10", "S01E01"])
        self.assertEqual(q["next"]["ep"], "S01E03")     # first UNWATCHED, not first numeric
        self.assertEqual(q["unwatched_count"], 2)

    def test_no_watched_map_is_plain_numeric(self):
        self.assertEqual(build_queue(NAMES)["remaining"], ["S01E01", "S01E03", "S01E10"])

    def test_all_watched_keeps_numeric(self):
        wm = {n: True for n in NAMES}
        self.assertEqual(build_queue(NAMES, watched_map=wm)["remaining"],
                         ["S01E01", "S01E03", "S01E10"])

    def test_parse_flags_watched_on_source(self):
        eps = {e["ep"]: e for e in parse_episodes(NAMES, watched_map={NAMES[0]: True})}
        self.assertTrue(eps["S01E01"]["watched"])
        self.assertFalse(eps["S01E03"]["watched"])      # not in the map → unwatched


class Mode(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_defaults_to_tv(self):
        self.assertEqual(series.get_mode(), "tv")

    def test_set_and_persist_mode(self):
        self.assertEqual(series.set_mode("movie"), "movie")
        self.assertEqual(series.get_mode(), "movie")
        self.assertEqual(series.set_mode("bogus"), "tv")   # unknown → tv
        self.assertEqual(series.get_mode(), "tv")

    def test_mode_and_selection_coexist(self):
        series.set_selection("My Show")
        series.set_mode("movie")
        self.assertEqual(series.get_selection(), "My Show")   # selection survives a mode write
        self.assertEqual(series.get_mode(), "movie")


class ActiveSeries(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()
        # PIN THE CAP. `series.max_active()` reads the LIVE `max_active_shows` tunable out of
        # ~/.topaz-pipeline/settings.json, so these tests were silently measuring the developer's own
        # setting: with it at 1, "caps at three" failed with ['A'] != ['A','B','C'] and the suite had
        # six red tests that said nothing about the code (found 2026-09-23).
        self.cap = mock.patch.object(series, "max_active", lambda: 3)
        self.cap.start()

    def tearDown(self):
        self.cap.stop()
        self.p.stop()

    def test_default_empty_then_set_is_single_primary(self):
        self.assertEqual(series.get_active_series(), [])
        self.assertIsNone(series.get_selection())
        series.set_selection("A")
        self.assertEqual(series.get_active_series(), ["A"])
        self.assertEqual(series.get_selection(), "A")          # primary = active[0]

    def test_add_dedupes_and_caps_at_three(self):
        for n in ("A", "B", "C", "D"):
            series.add_series(n)
        series.add_series("A")                                 # dup ignored
        self.assertEqual(series.get_active_series(), ["A", "B", "C"])   # D rejected (cap 3)

    def test_remove_then_set_resets_to_single(self):
        for n in ("A", "B", "C"):
            series.add_series(n)
        series.remove_series("B")
        self.assertEqual(series.get_active_series(), ["A", "C"])
        series.set_selection("Z")                              # 'set' = sole series, resets extras
        self.assertEqual(series.get_active_series(), ["Z"])

    def test_rotation_advances_wraps_and_clamps_on_remove(self):
        for n in ("A", "B", "C"):
            series.add_series(n)
        self.assertEqual(series.get_rotation(), 0)
        self.assertEqual(series.advance_rotation("A"), 1)      # A done → B next
        self.assertEqual(series.advance_rotation("B"), 2)      # B done → C next
        self.assertEqual(series.advance_rotation("C"), 0)      # C done → wrap to A
        series.advance_rotation("A")                           # rotation = 1
        series.remove_series("C")                              # → [A, B]; rotation must stay valid
        self.assertLess(series.get_rotation(), 2)
        self.assertEqual(series.advance_rotation("ZZZ"), series.get_rotation())  # unknown → no-op

    def test_legacy_single_series_field_migrates(self):
        with open(series.SELECTION_FILE, "w") as f:
            json.dump({"series": "Old", "mode": "tv"}, f)     # pre-round-robin file shape
        self.assertEqual(series.get_active_series(), ["Old"])
        self.assertEqual(series.get_selection(), "Old")

    def test_set_series_at_replaces_appends_and_dedupes(self):
        series.add_series("A"); series.add_series("B")        # [A, B]
        series.set_series_at(1, "C")                          # replace slot 1
        self.assertEqual(series.get_active_series(), ["A", "C"])
        series.set_series_at(2, "D")                          # empty slot → append
        self.assertEqual(series.get_active_series(), ["A", "C", "D"])
        series.set_series_at(0, "C")                          # C already in slot 1 → dedup
        self.assertEqual(series.get_active_series(), ["C", "D"])


class MaxActiveSetting(unittest.TestCase):
    """'max_active_shows' governs ADDING a show, never reading the active list."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _at(self, n):
        return mock.patch.object(series, "max_active", return_value=n)

    def test_setting_caps_how_many_can_be_added(self):
        with self._at(1):
            series.add_series("A"); series.add_series("B")
            self.assertEqual(series.get_active_series(), ["A"])       # B rejected at a width of 1
        with self._at(4):
            for n in ("B", "C", "D"):
                series.add_series(n)
            self.assertEqual(series.get_active_series(), ["A", "B", "C", "D"])

    def test_lowering_it_never_drops_a_running_show(self):
        # THE reason reads truncate at the ceiling instead: three shows are mid-run and the
        # user drops the width to 1. Nothing may disappear — the extra slots drain naturally.
        with self._at(3):
            for n in ("A", "B", "C"):
                series.add_series(n)
        with self._at(1):
            self.assertEqual(series.get_active_series(), ["A", "B", "C"])
            series.advance_rotation("A")                              # a write path runs...
            self.assertEqual(series.get_active_series(), ["A", "B", "C"])   # ...still all three
            series.add_series("D")                                    # but no NEW show gets in
            self.assertEqual(series.get_active_series(), ["A", "B", "C"])
            series.remove_series("B")                                 # they drain by finishing
            self.assertEqual(series.get_active_series(), ["A", "C"])

    def test_reads_truncate_at_the_hard_ceiling(self):
        import settings
        with open(series.SELECTION_FILE, "w") as f:                   # hand-edited overfull file
            json.dump({"active": [f"S{i}" for i in range(10)]}, f)
        self.assertEqual(len(series.get_active_series()), settings.MAX_ACTIVE_CEILING)


if __name__ == "__main__":
    unittest.main()


class NxNNConvention(unittest.TestCase):
    """The '9x01' episode-naming convention must parse like SxxExx — live-hit: Season 9 of
    The Office ('The Office (US) - 9x01 - New Guys.mkv') was invisible to the queue."""

    def test_9x01_names_parse_and_zero_pad(self):
        eps = series.parse_episodes(["The Office (US) - 9x01 - New Guys.mkv",
                                     "The Office (US) - 9x02 - Roy's Wedding.mkv"])
        self.assertEqual([e["ep"] for e in eps], ["S09E01", "S09E02"])
        self.assertTrue(all(e["has_source"] for e in eps))

    def test_mixed_conventions_sort_together(self):
        eps = series.parse_episodes(["Show S08e24 Finale.mkv",
                                     "Show - 9x01 - Opener.mkv"])
        self.assertEqual([e["ep"] for e in eps], ["S08E24", "S09E01"])

    def test_sxxexx_wins_when_both_present(self):
        eps = series.parse_episodes(["Show S02E11 something 3x99 else.mkv"])
        self.assertEqual(eps[0]["ep"], "S02E11")

    def test_resolution_tokens_never_match(self):
        for n in ("Movie 1920x1080 BluRay.mkv", "Clip 3840x2160 HDR.mkv", "Thing 720x480.mkv"):
            self.assertEqual(series.parse_episodes([n]), [], n)


class NextUpSlot(unittest.TestCase):
    """Per-slot follow-up: a show queued to take the slot the moment its current show
    finishes (CLEAN HANDOFF — no interleaving). Armed at <10% remaining, which is when it
    locks in and becomes prefetch-eligible."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "SELECTION_FILE", os.path.join(self.d, "selection.json"))
        self.p.start()
        # Same pin as ActiveSeries: the cap comes from a live tunable, not from the code.
        self.cap = mock.patch.object(series, "max_active", lambda: 3)
        self.cap.start()
        series.set_selection("A")

    def tearDown(self):
        self.cap.stop()
        self.p.stop()

    def _queues(self, mapping):
        # mapping: show -> (remaining, done)
        return mock.patch.object(series, "cached_queue",
                                 side_effect=lambda s: {"remaining_count": mapping.get(s, (0, 0))[0],
                                                        "done_count": mapping.get(s, (0, 0))[1]})

    def test_set_get_and_clear(self):
        series.set_next_up("A", "B")
        self.assertEqual(series.get_next_up("A"), "B")
        series.set_next_up("A", "")                       # falsy clears
        self.assertIsNone(series.get_next_up("A") or None)

    def test_rejects_self_and_already_active(self):
        series.set_series_at(1, "B")                       # B is ACTIVE in another slot
        series.set_next_up("A", "A")                       # can't follow itself
        self.assertFalse(series.get_next_up("A"))
        series.set_next_up("A", "B")                       # would duplicate B on promotion
        self.assertFalse(series.get_next_up("A"))

    def test_near_done_is_the_single_threshold(self):
        # The UI only OFFERS a follow-up from >=90% done; the same predicate arms a queued
        # one, so the threshold has exactly one definition.
        with self._queues({"A": (5, 45)}):        # exactly 10% left -> not yet
            self.assertFalse(series.near_done("A"))
        with self._queues({"A": (4, 46)}):        # 8% left
            self.assertTrue(series.near_done("A"))
        with self._queues({"A": (0, 0)}):         # nothing known
            self.assertFalse(series.near_done("A"))
        with self._queues({"A": (84, 0)}):        # a show that just STARTED
            self.assertFalse(series.near_done("A"))

    def test_armed_only_under_ten_percent(self):
        series.set_next_up("A", "B")
        with self._queues({"A": (5, 45)}):                 # 10% left — not yet under
            self.assertFalse(series.next_up_armed("A"))
        with self._queues({"A": (4, 46)}):                 # 8% left
            self.assertTrue(series.next_up_armed("A"))
        with self._queues({"A": (0, 0)}):                  # nothing known -> never armed
            self.assertFalse(series.next_up_armed("A"))

    def test_no_follow_up_is_never_armed(self):
        with self._queues({"A": (1, 99)}):
            self.assertFalse(series.next_up_armed("A"))

    def test_promotes_in_place_only_when_finished(self):
        series.set_series_at(1, "C")
        series.set_next_up("A", "B")
        with self._queues({"A": (3, 47), "C": (10, 0)}):   # A still has episodes
            self.assertEqual(series.promote_finished_slots(), [])
            self.assertEqual(series.get_active_series(), ["A", "C"])
        with self._queues({"A": (0, 50), "C": (10, 0), "B": (20, 0)}):
            self.assertEqual(series.promote_finished_slots(), [("A", "B")])
        self.assertEqual(series.get_active_series(), ["B", "C"])   # slot ORDER preserved
        self.assertFalse(series.get_next_up("A"))                  # mapping consumed

    def test_a_count_only_forget_episode_made_never_promotes(self):
        series.set_next_up("A", "B")
        series._QUEUE_DERIVED.add("A")
        try:
            with self._queues({"A": (0, 50), "B": (20, 0)}):
                self.assertEqual(series.promote_finished_slots(), [])
        finally:
            series._QUEUE_DERIVED.discard("A")

    def test_unreachable_nas_never_promotes(self):
        series.set_next_up("A", "B")
        with self._queues({"A": (0, 0)}):        # empty listing = unknown, NOT finished
            self.assertEqual(series.promote_finished_slots(), [])
        self.assertEqual(series.get_active_series(), ["A"])

    def test_promote_is_a_noop_without_any_mapping(self):
        with self._queues({"A": (0, 10)}):
            self.assertEqual(series.promote_finished_slots(), [])
        self.assertEqual(series.get_active_series(), ["A"])

    def test_queued_show_settings_persist_before_it_is_active(self):
        # The point of configuring a follow-up EARLY: its settings key on the show NAME in
        # show_profiles.json, so they persist while it is only queued and are already in
        # force the moment it is promoted into the slot.
        import settings as st
        d2 = tempfile.mkdtemp()
        with mock.patch.object(st, "PROFILES_FILE", os.path.join(d2, "profiles.json")):
            series.set_next_up("A", "B")
            st.set_show_preset("B", "film")                 # configured while NOT active
            st.set_show_normalize_audio("B", False)
            st.set_show_replace_source("B", False)
            st.set_show_unwatched_first("B", False)
            with self._queues({"A": (0, 10), "B": (5, 0)}):
                self.assertEqual(series.promote_finished_slots(), [("A", "B")])
            self.assertEqual(series.get_active_series(), ["B"])
            self.assertEqual(st.get_show_preset("B"), "film")        # survives promotion
            self.assertFalse(st.get_show_normalize_audio("B"))
            self.assertFalse(st.get_show_replace_source("B"))
            self.assertFalse(st.get_show_unwatched_first("B"))   # queue ORDER too


class RealSeasonDirs(unittest.TestCase):
    """Season folders are NOT reliably named `S01` (real libraries carry `Season 1`,
    `Show Season 2 S02 1080p BluRay`, ...). The walk learns each file's REAL directory so
    the download/upload path isn't synthesized as <show>/S{NN} — which 550s on such shows."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(series, "EPISODE_DIRS_FILE", os.path.join(self.d, "ep_dirs.json"))
        self.p.start()
        series._EP_DIRS = None                      # force a reload from the patched path

    def tearDown(self):
        self.p.stop()
        series._EP_DIRS = None

    def test_remembers_and_persists_the_real_dir(self):
        series.remember_episode_dirs("Show", [("/Vol/TV/Show/Season 1", "Show.S01E01.mkv"),
                                              ("/Vol/TV/Show/Weird S02 Folder", "Show.S02E01.mkv")])
        self.assertEqual(series.episode_nas_dir("Show", "Show.S01E01.mkv"), "/Vol/TV/Show/Season 1")
        self.assertEqual(series.episode_nas_dir("Show", "Show.S02E01.mkv"), "/Vol/TV/Show/Weird S02 Folder")
        series._EP_DIRS = None                      # simulate a relaunch — must survive on disk
        self.assertEqual(series.episode_nas_dir("Show", "Show.S01E01.mkv"), "/Vol/TV/Show/Season 1")

    def test_unknown_file_returns_none_so_the_caller_falls_back(self):
        self.assertIsNone(series.episode_nas_dir("Show", "never-walked.mkv"))
        self.assertIsNone(series.episode_nas_dir("", ""))

    def test_episode_paths_uses_the_real_dir_and_falls_back(self):
        from orchestrator import episode_paths
        series.remember_episode_dirs("Show", [("/Vol/TV/Show/Season 1", "Show.S01E01.mkv")])
        p = episode_paths("Show", "S01E01", "Show.S01E01.mkv",
                          scratch_dir=self.d, nas_tv_root="/Vol/TV")
        self.assertEqual(p.nas_dir, "/Vol/TV/Show/Season 1")          # REAL dir, not /Show/S01
        self.assertTrue(p.nas_source.endswith("/Season 1/Show.S01E01.mkv"))
        q = episode_paths("Show", "S03E02", "unwalked.mkv",           # not walked -> convention
                          scratch_dir=self.d, nas_tv_root="/Vol/TV")
        self.assertEqual(q.nas_dir, "/Vol/TV/Show/S03")


class SeriesRootCache(unittest.TestCase):
    """The volume map is REBUILT on each successful listing. The old setdefault-forever
    behaviour meant a show that MOVED volumes (or was cached from a pass where its real
    volume failed to list) kept a wrong root permanently — the episode walk then found
    nothing and the run reported "NAS unreachable" until the app restarted."""

    def setUp(self):
        self._saved = dict(series._SERIES_ROOTS)
        series._SERIES_ROOTS = {}

    def tearDown(self):
        series._SERIES_ROOTS = self._saved

    def _listing(self, per_root, fail=()):
        def fake(ftp, root):
            if root in fail:
                raise series.ftplib.error_perm("550 nope")
            return per_root.get(root, [])
        return mock.patch.object(series, "ftp_listdir", side_effect=fake)

    def test_moved_show_follows_its_new_volume(self):
        V1, V3 = "/Media/TV-Shows", "/MediaVolume3/TV-Shows"
        with mock.patch.object(series, "ftp_connect", return_value=mock.MagicMock()):
            with self._listing({V1: ["Lost (2004)"], V3: []}):
                series.list_series()
            self.assertEqual(series.series_root("Lost (2004)"), V1)
            with self._listing({V1: [], V3: ["Lost (2004)"]}):    # moved to vol3
                series.list_series()
        self.assertEqual(series.series_root("Lost (2004)"), V3)   # follows it (was stuck on V1)

    def test_vol1_still_wins_a_real_name_collision(self):
        V1, V3 = "/Media/TV-Shows", "/MediaVolume3/TV-Shows"
        with mock.patch.object(series, "ftp_connect", return_value=mock.MagicMock()), \
             self._listing({V1: ["Dup Show"], V3: ["Dup Show"]}):
            series.list_series()
        self.assertEqual(series.series_root("Dup Show"), V1)

    def test_a_failed_volume_does_not_forget_its_shows(self):
        V1, V3 = "/Media/TV-Shows", "/MediaVolume3/TV-Shows"
        with mock.patch.object(series, "ftp_connect", return_value=mock.MagicMock()):
            with self._listing({V1: ["A"], V3: ["B"]}):
                series.list_series()
            with self._listing({V1: ["A"], V3: []}, fail=(V3,)):   # vol3 unreadable this pass
                names = series.list_series()
        self.assertIn("B", names)                                  # kept, not dropped
        self.assertEqual(series.series_root("B"), V3)


class Featurettes(unittest.TestCase):
    """Season 00 = specials/featurettes. They are real SxxExx files and "S00" sorts before
    "S01", so untouched they would be upscaled BEFORE the show itself.

    Last place is now UNCONDITIONAL and the toggle asks whether to upscale them at all
    (user-dictated 2026-09-05): the ordering was never a real choice, the Topaz hours are."""

    NAMES = ["Lost - S00E17 - Missing Pieces.mkv", "Lost - S00E18 - More Pieces.mkv",
             "Lost - S01E01 - Pilot.mkv", "Lost - S01E02 - Tabula Rasa.mkv"]

    def test_specials_go_last_by_default(self):
        q = build_queue(self.NAMES)
        self.assertEqual(q["remaining"], ["S01E01", "S01E02", "S00E17", "S00E18"])
        self.assertEqual(q["next"]["ep"], "S01E01")     # a REAL episode, not a mobisode
        self.assertEqual(q["featurette_count"], 2)

    def test_specials_go_last_even_when_that_is_not_asked_for(self):
        # there is no longer any input that puts a season-00 ahead of the show
        q = build_queue(self.NAMES, do_featurettes=True)
        self.assertEqual(q["remaining"][:2], ["S01E01", "S01E02"])

    def test_off_drops_them_from_the_queue_entirely(self):
        q = build_queue(self.NAMES, do_featurettes=False)
        self.assertEqual(q["remaining"], ["S01E01", "S01E02"])
        self.assertEqual(q["next"]["ep"], "S01E01")
        self.assertEqual(q["remaining_count"], 2)
        self.assertEqual([i["ep"] for i in q["remaining_items"]], ["S01E01", "S01E02"])

    def test_off_still_reports_the_count_so_the_toggle_stays_visible(self):
        # the UI shows the row only when featurette_count > 0 — turning them off must not
        # hide the control that turns them back on
        self.assertEqual(build_queue(self.NAMES, do_featurettes=False)["featurette_count"], 2)

    def test_off_on_a_show_that_is_ONLY_specials_leaves_nothing_to_do(self):
        q = build_queue(self.NAMES[:2], do_featurettes=False)
        self.assertEqual(q["remaining"], [])
        self.assertIsNone(q["next"])

    def test_zero_count_when_a_show_has_no_specials(self):
        self.assertEqual(build_queue(self.NAMES[2:])["featurette_count"], 0)

    def test_unwatched_first_still_applies_within_each_group(self):
        watched = {"Lost - S01E01 - Pilot.mkv": True}          # S01E01 already seen
        q = build_queue(self.NAMES, watched_map=watched)
        self.assertEqual(q["remaining"], ["S01E02", "S01E01", "S00E17", "S00E18"])
        self.assertTrue(all(series.is_featurette(e) for e in q["remaining"][-2:]))


class UnreachableIsNotEmpty(unittest.TestCase):
    """An unreadable NAS must read as UNKNOWN, never as "this show has no episodes".
    The live symptom (2026-08-17): every deploy relaunches the server at exactly the
    moment FTP/Tailscale is least likely to answer; the failed listing returned [],
    build_queue read that as a finished show, cached_queue CACHED it, and the up-next
    list — having no episodes to interleave against — rendered EVERY queued movie at
    the top, persisting until some other call happened to refresh the queue."""

    def test_failed_listing_is_none_not_empty(self):
        import ftplib
        with mock.patch.object(series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(series, "ftp_connect",
                               side_effect=ftplib.error_temp("421 timeout")):
            self.assertIsNone(series.list_episode_files("Lost (2004)"))

    def test_genuinely_empty_show_still_reads_empty(self):
        with mock.patch.object(series, "series_root", return_value="/Media/TV"), \
             mock.patch.object(series, "ftp_connect", return_value=mock.MagicMock()), \
             mock.patch.object(series, "ftp_walk_files", return_value=[]), \
             mock.patch.object(series, "remember_episode_dirs"):
            self.assertEqual(series.list_episode_files("Empty Show"), [])

    def test_unreachable_never_overwrites_or_poisons_the_cache(self):
        good = {"next": {"ep": "S01E01", "source_name": "x.mkv"},
                "remaining_items": [{"ep": "S01E01", "source_name": "x.mkv"}],
                "remaining": ["S01E01"], "remaining_count": 1, "done_count": 0,
                "unwatched_count": 1, "featurette_count": 0, "source_count": 1}
        import time as _t

        def _settled():
            # a miss WARMS IN THE BACKGROUND now — wait for the warmer to finish before
            # judging what it did or didn't cache
            for _ in range(50):
                with series._QUEUE_WARM_LOCK:
                    if "Show" not in series._QUEUE_WARMING:
                        return
                _t.sleep(0.02)

        _reset_queue_cache()
        with mock.patch.object(series, "episode_queue", return_value=None):
            self.assertIsNone(series.cached_queue("Show"))          # the poll never blocks
            _settled()
            self.assertNotIn("Show", series._QUEUE_CACHE)           # ...and a failure retries later
        with mock.patch.object(series, "episode_queue", return_value=good), \
             mock.patch.object(series, "QUEUE_RETRY", 0):         # (the retry throttle, elsewhere)
            series.cached_queue("Show")                             # kicks the warm
            _settled()
            self.assertEqual(series.cached_queue("Show"), good)
        with mock.patch.object(series, "episode_queue", return_value=None):
            self.assertEqual(series.refresh_queue("Show"), good)    # keeps the good one
            self.assertEqual(series.cached_queue("Show"), good)
            _settled()
        _reset_queue_cache()


class StatePollingNeverWalksTheNAS(unittest.TestCase):
    """cached_queue's docstring always promised "no NAS I/O", but its miss path computed the
    queue LIVE over FTP. A freshly relaunched app during a NAS outage therefore hung every
    /api/state poll inside socket.create_connection — blank app for the whole outage
    (live-caught 2026-08-25). Only a COLD cache walks the NAS, which is what hid it."""

    def setUp(self):
        _reset_queue_cache()

    def test_a_miss_returns_immediately_and_warms_behind(self):
        import threading, time
        ev = threading.Event()
        def slow(name):
            ev.wait(0.5)
            return {"remaining_items": [1]}
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            t0 = time.time()
            self.assertIsNone(series.cached_queue("Show"))
            self.assertLess(time.time() - t0, 0.05)     # the poll never blocks
            ev.set(); time.sleep(0.3)
            self.assertIsNotNone(series.cached_queue("Show"))

    def test_polls_during_the_warm_do_not_stack_warmers(self):
        import threading, time
        ev, calls = threading.Event(), []
        def slow(name):
            calls.append(name); ev.wait(0.4)
            return {}
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            for _ in range(5):
                series.cached_queue("Show")
            ev.set(); time.sleep(0.2)
        self.assertEqual(len(calls), 1)

    def test_a_failed_warm_is_not_cached_so_the_next_miss_retries(self):
        import time
        with mock.patch.object(series, "episode_queue", return_value=None):
            series.cached_queue("Show"); time.sleep(0.1)
        self.assertNotIn("Show", series._QUEUE_CACHE)
        with series._QUEUE_WARM_LOCK:
            self.assertNotIn("Show", series._QUEUE_WARMING)   # the slot was released

    def test_a_warm_cache_is_served_synchronously_as_before(self):
        import time
        series._QUEUE_CACHE["Show"] = {"remaining_items": [1, 2]}
        series._QUEUE_AT["Show"] = time.monotonic()                # computed just now: fresh
        with mock.patch.object(series, "episode_queue",
                               side_effect=AssertionError("must not compute")):
            self.assertEqual(series.cached_queue("Show"), {"remaining_items": [1, 2]})


def _reset_queue_cache():
    with series._QUEUE_WARM_LOCK:
        for d in (series._QUEUE_CACHE, series._QUEUE_AT, series._QUEUE_STORED,
                  series._QUEUE_TRIED, series._QUEUE_WARMING, series._QUEUE_DERIVED):
            d.clear()


def _settle(name="Show"):
    """Wait for the background warmer (if any) to finish — inside the caller's patches."""
    import time
    for _ in range(100):
        with series._QUEUE_WARM_LOCK:
            if name not in series._QUEUE_WARMING:
                return
        time.sleep(0.01)
    raise AssertionError("the queue warmer never finished")


def _q(*eps, done=0):
    items = [{"ep": e, "source_name": f"Show - {e}.mkv"} for e in eps]
    return {"next": dict(items[0], has_source=True, has_dv=False, watched=False) if items else None,
            "remaining": list(eps), "remaining_items": items, "remaining_count": len(eps),
            "unwatched_count": len(eps), "done_count": done, "featurette_count": 0,
            "source_count": len(eps) + done}


class QueueCacheNeverGoesStaleForever(unittest.TestCase):
    """Live 2026-10-06: Community S04E06 was replaced at 08:37 but the post-upload re-list hit
    a thrashing NAS, and the cache had no age — so it listed the deleted 1080p source as up
    next all day, and the prefetcher tried to download it 15 times (550 each time)."""

    def setUp(self):
        _reset_queue_cache()

    def tearDown(self):
        _reset_queue_cache()

    def _fresh(self, q):
        import time
        series._QUEUE_CACHE["Show"] = q
        series._QUEUE_AT["Show"] = time.monotonic()

    def test_an_old_queue_is_served_at_once_and_refreshed_behind(self):
        import time
        self._fresh(_q("S01E01"))
        series._QUEUE_AT["Show"] = time.monotonic() - series.QUEUE_TTL - 1
        with mock.patch.object(series, "episode_queue", return_value=_q("S01E02", done=1)):
            self.assertEqual(series.cached_queue("Show")["remaining"], ["S01E01"])   # no waiting
            _settle()
        self.assertEqual(series.cached_queue("Show")["remaining"], ["S01E02"])

    def test_a_failed_refresh_leaves_it_stale_and_retries_without_hammering(self):
        self._fresh(_q("S01E01"))
        calls = []
        def down(name):
            calls.append(name)
            return None
        with mock.patch.object(series, "episode_queue", side_effect=down):
            self.assertEqual(series.refresh_queue("Show")["remaining"], ["S01E01"])  # kept
            self.assertEqual(series.cached_queue("Show")["remaining"], ["S01E01"])
            _settle()
            self.assertEqual(len(calls), 1)            # inside QUEUE_RETRY: no second walk yet
            with mock.patch.object(series, "QUEUE_RETRY", 0):
                series.cached_queue("Show")            # ...but it IS stale, so it retries
                _settle()
            self.assertEqual(len(calls), 2)
        with mock.patch.object(series, "episode_queue", return_value=_q("S01E02", done=1)), \
             mock.patch.object(series, "QUEUE_RETRY", 0):
            series.cached_queue("Show"); _settle()
        self.assertEqual(series.cached_queue("Show")["remaining"], ["S01E02"])

    def test_stale_means_stale_even_right_after_boot(self):
        # monotonic() counts from boot: a "0 = stale" marker would read as fresh for the
        # first QUEUE_TTL seconds of uptime
        self._fresh(_q("S01E01"))
        series.mark_stale("Show")
        clock = mock.Mock(); clock.monotonic.return_value = 5.0
        with mock.patch.object(series, "time", clock), \
             mock.patch.object(series, "episode_queue", return_value=_q("S01E02")) as eq:
            series.cached_queue("Show"); _settle()
        eq.assert_called_once()

    def test_forget_episode_drops_it_at_once_without_the_nas(self):
        self._fresh(_q("S04E06", "S04E07", "S04E08", done=5))
        with mock.patch.object(series, "episode_queue", side_effect=AssertionError("no NAS I/O")):
            series.forget_episode("Show", "S04E06")
        q = series._QUEUE_CACHE["Show"]
        self.assertEqual(q["remaining"], ["S04E07", "S04E08"])
        self.assertEqual([it["ep"] for it in q["remaining_items"]], ["S04E07", "S04E08"])
        self.assertEqual(q["next"]["ep"], "S04E07")
        self.assertEqual(q["next"]["source_name"], "Show - S04E07.mkv")
        self.assertEqual((q["remaining_count"], q["done_count"], q["unwatched_count"]), (2, 6, 2))
        self.assertTrue(series._queue_stale("Show"))   # ...and the next read re-lists for real

    def test_forgetting_the_last_episode_leaves_no_next(self):
        self._fresh(_q("S01E09", done=8))
        series.forget_episode("Show", "S01E09")
        q = series._QUEUE_CACHE["Show"]
        self.assertIsNone(q["next"])
        self.assertEqual((q["remaining_count"], q["done_count"]), (0, 9))

    def test_forget_never_touches_the_original_dict(self):
        orig = _q("S01E01", "S01E02")
        self._fresh(orig)
        series.forget_episode("Show", "S01E01")
        self.assertEqual(orig["remaining"], ["S01E01", "S01E02"])   # a reader may still hold it

    def test_forgetting_an_unlisted_episode_changes_nothing_but_staleness(self):
        self._fresh(_q("S01E02"))
        series.forget_episode("Show", "S01E01")
        self.assertEqual(series._QUEUE_CACHE["Show"]["remaining"], ["S01E02"])
        self.assertEqual(series._QUEUE_CACHE["Show"]["done_count"], 0)

    def test_a_listing_taken_before_the_upload_never_lands_after_it(self):
        import threading
        self._fresh(_q("S01E01", "S01E02"))
        series.mark_stale("Show")
        started, release = threading.Event(), threading.Event()
        def slow(name):                                 # listed BEFORE the upload landed
            started.set(); release.wait(2)
            return _q("S01E01", "S01E02")
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            series.cached_queue("Show")
            self.assertTrue(started.wait(2))
            series.forget_episode("Show", "S01E01")    # the upload finishes mid-listing
            release.set(); _settle()
        self.assertEqual(series._QUEUE_CACHE["Show"]["remaining"], ["S01E02"])

    def test_an_older_warm_never_overwrites_a_refresh(self):
        import threading
        self._fresh(_q("S01E01"))
        series.mark_stale("Show")
        started, release = threading.Event(), threading.Event()
        def slow(name):
            started.set(); release.wait(2)
            return _q("S01E01")                         # the old listing
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            series.cached_queue("Show")
            self.assertTrue(started.wait(2))
        with mock.patch.object(series, "episode_queue", return_value=_q("S01E05", done=4)):
            self.assertEqual(series.refresh_queue("Show")["remaining"], ["S01E05"])
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            release.set(); _settle()
        self.assertEqual(series._QUEUE_CACHE["Show"]["remaining"], ["S01E05"])

    def test_mark_stale_makes_the_next_read_relist(self):
        self._fresh(_q("S04E06"))
        series.mark_stale("Show")
        with mock.patch.object(series, "episode_queue", return_value=_q("S04E07")) as eq:
            self.assertEqual(series.cached_queue("Show")["remaining"], ["S04E06"])  # served at once
            _settle()
        eq.assert_called_once()
        self.assertEqual(series.cached_queue("Show")["remaining"], ["S04E07"])

    def test_a_later_failed_refresh_never_discards_an_earlier_one_that_worked(self):
        import threading
        self._fresh(_q("S01E01"))
        started, release = threading.Event(), threading.Event()
        def slow_good(name):
            started.set(); release.wait(2)
            return _q("S01E03", done=2)
        out = {}
        with mock.patch.object(series, "episode_queue", side_effect=slow_good):
            t = threading.Thread(target=lambda: out.setdefault("r", series.refresh_queue("Show")))
            t.start()
            self.assertTrue(started.wait(2))
        with mock.patch.object(series, "episode_queue", return_value=None):
            series.refresh_queue("Show")               # starts later, fails at once
        release.set(); t.join(2)
        self.assertEqual(series._QUEUE_CACHE["Show"]["remaining"], ["S01E03"])
        self.assertFalse(series._queue_stale("Show"))  # the earlier one landed, and is current

    def test_a_miss_is_throttled_too_after_a_failure(self):
        calls = []
        def down(name):
            calls.append(name)
            return None
        with mock.patch.object(series, "episode_queue", side_effect=down):
            for _ in range(5):                          # five polls, one listing
                self.assertIsNone(series.cached_queue("Show"))
                _settle()
        self.assertEqual(len(calls), 1)

    def test_an_overloaded_nas_serves_what_is_cached_without_relisting(self):
        self._fresh(_q("S01E01"))
        series.mark_stale("Show")
        import transfer
        with mock.patch.object(transfer, "nas_busy", return_value={"since": 1}), \
             mock.patch.object(series, "episode_queue", side_effect=AssertionError("no listing")):
            self.assertEqual(series.cached_queue("Show")["remaining"], ["S01E01"])   # served
        self.assertNotIn("Show", series._QUEUE_WARMING)

    def test_an_overloaded_nas_still_fills_a_show_that_has_nothing(self):
        import transfer
        with mock.patch.object(transfer, "nas_busy", return_value={"since": 1}), \
             mock.patch.object(series, "episode_queue", return_value=_q("S01E01")) as eq:
            self.assertIsNone(series.cached_queue("Show"))
            _settle()
        eq.assert_called_once()
        self.assertEqual(series._QUEUE_CACHE["Show"]["remaining"], ["S01E01"])

    def test_an_overloaded_nas_skips_the_post_upload_relist(self):
        self._fresh(_q("S01E01", "S01E02"))
        series.forget_episode("Show", "S01E01")
        import transfer
        with mock.patch.object(transfer, "nas_busy", return_value={"since": 1}), \
             mock.patch.object(series, "episode_queue", side_effect=AssertionError("no listing")):
            self.assertEqual(series.refresh_queue("Show")["remaining"], ["S01E02"])
        self.assertTrue(series._queue_stale("Show"))

    def test_forget_alone_never_promotes_a_slot(self):
        self._fresh(_q("S01E09", done=8))
        series.forget_episode("Show", "S01E09")          # "nothing left" — by our own arithmetic
        self.assertFalse(series.queue_confirmed("Show"))
        series._store_queue("Show", _q(done=9), next(series._QUEUE_SEQ))   # a real listing agrees
        self.assertTrue(series.queue_confirmed("Show"))

    def test_mark_stale_keeps_the_retry_throttle(self):
        self._fresh(_q("S01E01"))
        calls = []
        def down(name):
            calls.append(name)
            return None
        with mock.patch.object(series, "episode_queue", side_effect=down):
            series.refresh_queue("Show")                # a failed listing, just now
            for _ in range(5):                          # five failed downloads → five mark_stales
                series.mark_stale("Show")
                series.cached_queue("Show"); _settle()
        self.assertEqual(len(calls), 1)                 # still one listing per QUEUE_RETRY

    def test_a_refresh_counts_as_the_shows_warmer(self):
        import threading
        self._fresh(_q("S01E01"))
        series.mark_stale("Show")
        started, release = threading.Event(), threading.Event()
        calls = []
        def slow(name):
            calls.append(name); started.set(); release.wait(2)
            return _q("S01E02")
        with mock.patch.object(series, "episode_queue", side_effect=slow):
            t = threading.Thread(target=series.refresh_queue, args=("Show",))
            t.start()
            self.assertTrue(started.wait(2))
            series.cached_queue("Show")                 # stale, but a listing is already running
            release.set(); t.join(2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("Show", series._QUEUE_WARMING)

    def test_mark_stale_and_forget_ignore_unknown_shows(self):
        series.mark_stale("Nope"); series.mark_stale("")
        series.forget_episode("Nope", "S01E01"); series.forget_episode("", "S01E01")
        self.assertNotIn("Nope", series._QUEUE_CACHE)
