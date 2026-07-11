"""up_next() display: YouTube shows as 1 video per `youtube_every_tv_episodes` TV episodes,
counting from the live cadence position — matching orchestrator._next_episode."""
import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "dashboard"))
import server
import series
import movies
import youtube
import settings
import orchestrator


def _yt(i):
    return {"channel": "Chan", "source_name": f"v{i}.mp4", "title": f"V{i}"}


class UpNextCadence(unittest.TestCase):
    def _kinds(self, *, episodes, yt, every, tv_since, limit=12, current=None, parked=()):
        items = [{"ep": f"S01E{n:02d}", "source_name": f"e{n}.mkv"} for n in range(1, episodes + 1)]
        self._all_pending = mock.Mock(return_value=[_yt(i) for i in range(yt)])
        with contextlib.ExitStack() as s:
            s.enter_context(mock.patch.object(series, "get_active_series", return_value=["A"]))
            s.enter_context(mock.patch.object(series, "get_rotation", return_value=0))
            s.enter_context(mock.patch.object(series, "cached_queue",
                                              return_value={"remaining_items": items}))
            s.enter_context(mock.patch.object(movies, "get_selected", return_value=[]))
            s.enter_context(mock.patch.object(youtube, "all_pending", self._all_pending))
            s.enter_context(mock.patch.object(settings, "get_settings",
                                              return_value={"youtube_every_tv_episodes": every}))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_tv_since_yt", tv_since))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_parked", set(parked)))
            return [it["kind"] for it in server.up_next(limit=limit, current=current)]

    def test_one_video_every_two_episodes(self):
        kinds = self._kinds(episodes=6, yt=3, every=2, tv_since=0)
        self.assertEqual(kinds, ["episode", "episode", "youtube",
                                 "episode", "episode", "youtube",
                                 "episode", "episode", "youtube"])

    def test_live_position_offsets_the_first_video(self):
        # 1 episode already done since the last YouTube → the next video is only 1 episode away
        kinds = self._kinds(episodes=5, yt=2, every=2, tv_since=1)
        self.assertEqual(kinds[:2], ["episode", "youtube"])

    def test_higher_cadence_spaces_videos_out(self):
        kinds = self._kinds(episodes=6, yt=2, every=3, tv_since=0)
        self.assertEqual(kinds, ["episode", "episode", "episode", "youtube",
                                 "episode", "episode", "episode", "youtube"])

    def test_no_videos_is_pure_tv(self):
        self.assertEqual(self._kinds(episodes=3, yt=0, every=2, tv_since=0),
                         ["episode", "episode", "episode"])

    def test_leftover_videos_drain_after_tv(self):
        # 2 episodes, every=2, but 3 videos → 1 fires after the 2 eps, the other 2 drain at the end
        kinds = self._kinds(episodes=2, yt=3, every=2, tv_since=0)
        self.assertEqual(kinds, ["episode", "episode", "youtube", "youtube", "youtube"])

    def test_saturated_counter_leads_with_a_video(self):
        # counter already at the threshold → the orchestrator serves a video FIRST (gate before rotation);
        # the display must lead with it, not defer it behind an episode (the confirmed off-by-one bug).
        kinds = self._kinds(episodes=3, yt=1, every=2, tv_since=2)
        self.assertEqual(kinds, ["youtube", "episode", "episode", "episode"])

    def test_current_video_is_excluded_and_cadence_models_its_completion(self):
        # a video mid-pipeline: it is NOT in the queue (the header owns it), and the cadence
        # is modelled from AFTER it completes (counter resets → next video after N episodes),
        # even though the live counter is still saturated while it runs.
        kinds = self._kinds(episodes=4, yt=2, every=2, tv_since=2,
                            current={"kind": "youtube", "name": "v0.mp4"})
        self.assertEqual(kinds[:3], ["episode", "episode", "youtube"])   # v0 gone; v1 after 2 eps

    def test_current_episode_is_excluded_not_pinned(self):
        kinds = self._kinds(episodes=3, yt=0, every=2, tv_since=0,
                            current={"kind": "episode", "ep": "S01E01"})
        self.assertEqual(len(kinds), 2)                  # S01E01 dropped, 2 remain
        self.assertEqual(kinds, ["episode", "episode"])

    def test_parked_videos_are_excluded(self):
        # up_next must skip PARKED videos, like the orchestrator's next_due(skip=_parked) does.
        self._kinds(episodes=2, yt=1, every=2, tv_since=0, parked=("dead-stem",))
        _, kwargs = self._all_pending.call_args
        self.assertEqual(kwargs.get("skip"), {"dead-stem"})


if __name__ == "__main__":
    unittest.main()
