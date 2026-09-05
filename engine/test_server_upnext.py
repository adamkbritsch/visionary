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
            # up_next now reads the burst position too — pin it, or these read whatever the
            # LIVE pipeline happens to be part-way through
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_yt_in_burst", 0))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_parked", set(parked)))
            # up_next LEADS with the priority book, so an unpinned book means these cadence
            # tests read whatever is genuinely queued on this machine — the same live-state
            # leak the _yt_in_burst pin above exists for. Book behaviour has its own tests.
            s.enter_context(mock.patch.object(youtube, "_priority", return_value=[]))
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


class SentVideosLeadTheQueue(unittest.TestCase):
    """Sent-to-Visionary videos were effectively invisible in the up-next list (user-caught
    2026-09-04: of 8 sends exactly 1 appeared anywhere, one at position 62 of 80) while being
    the very next thing the pipeline would run. The list was built only from the per-channel
    staging cache, round-robined; the book that actually decides serving order was read only
    to decorate rows that happened to already be there."""

    def _rows(self, book, pending=(), current=None, items=None, done=()):
        items = items if items is not None else [{"ep": "S01E01", "source_name": "a.mkv"}]
        with contextlib.ExitStack() as s:
            s.enter_context(mock.patch.object(series, "get_active_series", return_value=["A"]))
            s.enter_context(mock.patch.object(series, "get_rotation", return_value=0))
            s.enter_context(mock.patch.object(series, "cached_queue",
                                              return_value={"remaining_items": items}))
            s.enter_context(mock.patch.object(movies, "get_selected", return_value=[]))
            s.enter_context(mock.patch.object(youtube, "all_pending",
                                              return_value=list(pending)))
            s.enter_context(mock.patch.object(youtube, "_priority", return_value=list(book)))
            s.enter_context(mock.patch.object(youtube, "get_done", return_value=set(done)))
            s.enter_context(mock.patch.object(settings, "get_settings",
                                              return_value={"youtube_every_tv_episodes": 99}))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_tv_since_yt", 0))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_yt_in_burst", 0))
            s.enter_context(mock.patch.object(orchestrator.ORCH, "_parked", set()))
            return server.up_next(limit=10, current=current)

    def _sent(self, vid, name, title="Sent One"):
        return {"vid": vid, "title": title, "channel": "Chan", "sent_at": 1,
                "path": "/Media/YouTube-raw/Chan/x/" + name}

    def test_a_send_leads_the_list(self):
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4")])
        self.assertEqual(rows[0]["kind"], "youtube")
        self.assertEqual(rows[0]["name"], "v1.mp4")
        self.assertTrue(rows[0]["priority"])

    def test_book_order_is_preserved(self):
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4"),
                           self._sent("aaaaaaaaaa2", "v2.mp4")])
        self.assertEqual([r["name"] for r in rows[:2]], ["v1.mp4", "v2.mp4"])

    def test_a_send_buried_in_its_channel_column_moves_up_and_is_not_duplicated(self):
        # THE BUG: it was at position 62 of 80 while being next.
        deep = [{"channel": "Chan", "source_name": "old%d.mp4" % i, "title": "old",
                 "vid": "z" * 10 + str(i % 10)} for i in range(30)]
        deep.append({"channel": "Chan", "source_name": "v1.mp4", "title": "Sent One",
                     "vid": "aaaaaaaaaa1"})
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4")], pending=deep, items=[])
        self.assertEqual(rows[0]["name"], "v1.mp4")
        self.assertEqual(sum(1 for r in rows if r.get("name") == "v1.mp4"), 1)

    def test_the_running_video_is_not_listed_as_next(self):
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4")],
                          current={"kind": "youtube", "name": "v1.mp4"})
        self.assertFalse([r for r in rows if r.get("name") == "v1.mp4"])

    def test_a_send_still_downloading_holds_its_place_and_says_so(self):
        # no `path` yet = youtarr is still fetching. Hiding it made the send look inert —
        # the press is what the user is waiting to see acknowledged.
        e = self._sent("aaaaaaaaaa1", "v1.mp4"); e["path"] = None
        rows = self._rows([e])
        self.assertEqual(rows[0]["kind"], "youtube")
        self.assertTrue(rows[0]["priority"])
        self.assertTrue(rows[0]["awaiting_download"])       # "fetching", not "running next"

    def test_a_located_send_is_not_marked_as_fetching(self):
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4")])
        self.assertFalse(rows[0]["awaiting_download"])

    def test_an_entry_with_neither_path_nor_vid_is_skipped(self):
        e = self._sent("aaaaaaaaaa1", "v1.mp4"); e["path"] = None; e["vid"] = None
        self.assertFalse([r for r in self._rows([e]) if r.get("kind") == "youtube"])

    def test_a_finished_send_drops_out(self):
        rows = self._rows([self._sent("aaaaaaaaaa1", "v1.mp4")], done={"aaaaaaaaaa1"})
        self.assertFalse([r for r in rows if r.get("kind") == "youtube"])

    def test_imports_do_not_jump(self):
        # jump=False = a pasted-link import: it joins the cadence, it does not preempt
        e = self._sent("aaaaaaaaaa1", "v1.mp4"); e["jump"] = False; e["batch"] = "imp1"
        self.assertFalse([r for r in self._rows([e]) if r.get("kind") == "youtube"])

    def test_an_empty_book_leaves_the_cadence_exactly_as_it_was(self):
        items = [{"ep": "S01E0%d" % i, "source_name": "a%d.mkv" % i} for i in range(1, 4)]
        with mock.patch.object(server, "_up_next_cadence") as cad:
            cad.return_value = [{"kind": "episode", "ep": "S01E01"}]
            with mock.patch.object(youtube, "_priority", return_value=[]):
                rows = server.up_next(limit=10)
        self.assertEqual(rows, [{"kind": "episode", "ep": "S01E01"}])
