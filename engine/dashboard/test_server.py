import datetime
import unittest

import server
from power import PowerReading


def t(h, m=0):
    return datetime.time(h, m)


class InWindow(unittest.TestCase):
    def test_overnight_window_includes_late_night(self):
        self.assertTrue(server.in_window(t(22), "20:00", "09:00"))

    def test_overnight_window_includes_early_morning(self):
        self.assertTrue(server.in_window(t(7), "20:00", "09:00"))

    def test_overnight_window_excludes_daytime(self):
        self.assertFalse(server.in_window(t(15), "20:00", "09:00"))


SCRATCH = {"name": "2TB SSD", "connected": True, "path": "/Volumes/2TB SSD/topaz-scratch",
           "free_gb": 956, "source": "external"}


def state(power, manifest=None, automation_enabled=False, scratch=None, in_win=True, adapter_watts=65):
    return server.build_state(power=power, scratch=scratch or SCRATCH, adapter_watts=adapter_watts,
                              in_win=in_win, manifest=manifest,
                              automation_enabled=automation_enabled)


class BuildState(unittest.TestCase):
    def test_disabled_status_when_automation_off(self):
        st = state(PowerReading(True, False, 100, 0), automation_enabled=False)
        self.assertFalse(st["automation_enabled"])
        self.assertEqual(st["status"], "disabled")

    def test_power_adequate_when_not_draining(self):
        st = state(PowerReading(True, False, 100, 0), adapter_watts=140)   # adequacy = the 140 W brick
        self.assertTrue(st["power"]["adequate"])
        self.assertFalse(st["power"]["draining_on_ac"])

    def test_power_inadequate_when_draining_on_ac(self):
        st = state(PowerReading(True, False, 96, -1500))
        self.assertFalse(st["power"]["adequate"])
        self.assertTrue(st["power"]["draining_on_ac"])

    def test_no_job_when_no_manifest(self):
        st = state(PowerReading(True, False, 100, 0), manifest=None)
        self.assertIsNone(st["job"])

    def test_job_summary_from_manifest(self):
        st = state(PowerReading(True, False, 100, 0),
                   manifest={"show": "Brooklyn Nine-Nine", "total": 153, "located": 153, "missing": 0})
        self.assertEqual(st["job"]["show"], "Brooklyn Nine-Nine")
        self.assertEqual(st["job"]["total"], 153)


class UpNext(unittest.TestCase):
    """up_next round-robins the active series into an episode stream + interleaves movies by slot."""
    def _run(self, movies_list, episodes, limit=10, current=None, inflight=None):   # single active series "show"
        import movies, series
        from unittest import mock
        with mock.patch.object(movies, "get_selected", return_value=movies_list), \
             mock.patch.object(series, "get_active_series", return_value=["show"]), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(series, "cached_queue", return_value={"remaining_items": episodes}):
            return [(o["kind"], o.get("name") or o.get("ep"))
                    for o in server.up_next(limit=limit, current=current, inflight=inflight)]

    def _rr(self, active, queues, rotation=0, limit=10):   # multi-series round-robin, no movies
        import movies, series
        from unittest import mock
        with mock.patch.object(movies, "get_selected", return_value=[]), \
             mock.patch.object(series, "get_active_series", return_value=active), \
             mock.patch.object(series, "get_rotation", return_value=rotation), \
             mock.patch.object(series, "cached_queue", side_effect=lambda nm: queues.get(nm)):
            return [(o.get("ep"), o.get("series")) for o in server.up_next(limit=limit)]

    def test_movies_interleave_at_their_slot(self):
        mvs = [{"name": "a", "title": "A", "pos": 0}, {"name": "b", "title": "B", "pos": 2}]
        eps = [{"ep": "E1", "source_name": "e1"}, {"ep": "E2", "source_name": "e2"},
               {"ep": "E3", "source_name": "e3"}]
        self.assertEqual(self._run(mvs, eps),
            [("movie", "a"), ("episode", "E1"), ("episode", "E2"), ("movie", "b"), ("episode", "E3")])

    def test_pos_zero_movie_is_first_pos_beyond_end_is_last(self):
        mvs = [{"name": "a", "title": "A", "pos": 0}, {"name": "z", "title": "Z", "pos": 9}]
        eps = [{"ep": "E1", "source_name": "e1"}, {"ep": "E2", "source_name": "e2"}]
        self.assertEqual(self._run(mvs, eps),
            [("movie", "a"), ("episode", "E1"), ("episode", "E2"), ("movie", "z")])

    def test_limit_caps_output(self):
        eps = [{"ep": f"E{i}", "source_name": f"e{i}"} for i in range(20)]
        self.assertEqual(len(self._run([], eps, limit=10)), 10)

    def test_current_episode_excluded_from_queue(self):
        eps = [{"ep": "E1"}, {"ep": "E2"}]                      # E1 is mid-pipeline (run-thread current)
        self.assertEqual(self._run([], eps, current={"kind": "episode", "ep": "E1"}),
            [("episode", "E2")])                               # E1 shows in the header, not the queue

    def test_finisher_item_also_excluded(self):
        # TWO things in the pipeline: E1 (run/current) + E2 (finisher/inflight) → only E3 is "next"
        eps = [{"ep": "E1"}, {"ep": "E2"}, {"ep": "E3"}]
        self.assertEqual(
            self._run([], eps, current={"kind": "episode", "ep": "E1"},
                      inflight=[{"kind": "episode", "ep": "E2"}]),
            [("episode", "E3")])

    def test_inflight_excluded_by_key_after_queue_resort(self):
        # user reorders the queue so the finisher item is no longer at the front — it must STILL be
        # excluded (key-based, not positional), so it can't float ahead as "next"
        eps = [{"ep": "E5"}, {"ep": "E2"}, {"ep": "E7"}]        # E2 (finisher) floated into the middle
        self.assertEqual(
            self._run([], eps, current={"kind": "episode", "ep": "E5"},
                      inflight=[{"kind": "episode", "ep": "E2"}]),
            [("episode", "E7")])                               # both in-flight gone, regardless of order

    def test_inflight_movie_excluded(self):
        mvs = [{"name": "a", "title": "A", "pos": 0}, {"name": "b", "title": "B", "pos": 0}]
        eps = [{"ep": "E1"}]
        self.assertEqual(
            self._run(mvs, eps, inflight=[{"kind": "movie", "name": "a"}]),
            [("movie", "b"), ("episode", "E1")])               # movie 'a' is in the finisher → gone

    def test_idle_pos_zero_movie_still_leads(self):            # no running ep → movie really is next
        mvs = [{"name": "m", "title": "M", "pos": 0}]
        eps = [{"ep": "E1"}, {"ep": "E2"}]
        self.assertEqual(self._run(mvs, eps),
            [("movie", "m"), ("episode", "E1"), ("episode", "E2")])

    def test_round_robin_one_each_then_loops_skipping_exhausted(self):
        q = {"A": {"remaining_items": [{"ep": "A1"}, {"ep": "A2"}]},
             "B": {"remaining_items": [{"ep": "B1"}, {"ep": "B2"}]},
             "C": {"remaining_items": [{"ep": "C1"}]}}              # C runs out first
        self.assertEqual(self._rr(["A", "B", "C"], q),
            [("A1", "A"), ("B1", "B"), ("C1", "C"), ("A2", "A"), ("B2", "B")])

    def test_round_robin_starts_at_rotation_pointer(self):
        q = {"A": {"remaining_items": [{"ep": "A1"}]}, "B": {"remaining_items": [{"ep": "B1"}]}}
        self.assertEqual(self._rr(["A", "B"], q, rotation=1), [("B1", "B"), ("A1", "A")])


if __name__ == "__main__":
    unittest.main()
