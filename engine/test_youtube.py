import os
import tempfile
import time
import unittest
from unittest import mock

import youtube


class Helpers(unittest.TestCase):
    def test_video_id(self):
        self.assertEqual(youtube.video_id("LTT - Working 10 Hours [bda1GHblwis].mp4"), "bda1GHblwis")
        self.assertEqual(youtube.video_id("no bracket id.mp4"), "no bracket id")

    def test_video_title_strips_id_and_channel_prefix(self):
        self.assertEqual(youtube.video_title("al jokes - GTA 6 [EAYEWR8Uabc].mp4", "al jokes"), "GTA 6")
        self.assertEqual(youtube.video_title("Foo - Bar [abcdefghij1].mp4"), "Foo - Bar")


class Queue(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DONE_FILE", os.path.join(self.d, "done.json"))]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def test_add_unlimited_dedup_scope_default(self):
        for i in range(5):
            youtube.add_channel(f"UC{i}", f"chan {i}")
        q = youtube.get_queue()
        self.assertEqual(len(q), 5)                              # no cap — all queued
        self.assertEqual(q[0]["scope"], "popular")              # default scope
        youtube.add_channel("UC0", "dup")                       # dup ignored
        self.assertEqual(len(youtube.get_queue()), 5)

    def test_scope_and_remove(self):
        youtube.add_channel("UCa", "A"); youtube.add_channel("UCb", "B")
        youtube.set_scope("UCa", "all")
        self.assertEqual(youtube.get_queue()[0]["scope"], "all")
        youtube.set_scope("UCa", "bogus")                       # invalid → popular
        self.assertEqual(youtube.get_queue()[0]["scope"], "popular")
        youtube.remove_channel("UCa")
        self.assertEqual([e["channelId"] for e in youtube.get_queue()], ["UCb"])


def _vid(name, mtime=0):
    return {"name": name, "dir": "/d/" + name, "path": "/d/" + name + "/" + name,
            "mtime": mtime, "vid": youtube.video_id(name)}


class UpscaleFilter(unittest.TestCase):
    """channel_pending applies per-channel scope + the OPT-IN per-channel length cap over on-disk videos."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DONE_FILE", os.path.join(self.d, "done.json")),
                   mock.patch.object(youtube, "DURATIONS_FILE", os.path.join(self.d, "dur.json"))]
        for p in self.ps:
            p.start()
        # channel folder "Chan" has 3 videos on disk
        self.vids = [_vid("Chan - short A [aaaaaaaaaa1]", 300),
                     _vid("Chan - short B [aaaaaaaaaa2]", 200),
                     _vid("Chan - LONG C [aaaaaaaaaa3]", 100)]
        youtube._VIDEO_CACHE["Chan"] = self.vids
        youtube._META["UCx"] = {"popular": {"aaaaaaaaaa1"}}     # A is the only 'popular' one
        # A + B are ≤20min; C is 40min (over cap) — durations live in the PERSISTED cache now
        youtube._DURATIONS = {"aaaaaaaaaa1": 300, "aaaaaaaaaa2": 300, "aaaaaaaaaa3": 2400}
        self.entry = {"channelId": "UCx", "title": "Chan", "folder_name": "Chan", "scope": "popular"}

    def tearDown(self):
        for p in self.ps:
            p.stop()
        youtube._VIDEO_CACHE.clear(); youtube._META.clear(); youtube._DURATIONS = None

    def _cap20(self):
        return mock.patch("settings.get_settings", return_value={"max_youtube_minutes": 20})

    def test_popular_scope_keeps_only_popular(self):
        with self._cap20():
            p = youtube.channel_pending(self.entry)           # scope=popular, cap OFF
        self.assertEqual([v["vid"] for v in p], ["aaaaaaaaaa1"])   # only the popular one

    def test_all_scope_uncapped_keeps_every_length(self):
        self.entry["scope"] = "all"                           # capped defaults OFF
        with self._cap20():
            p = youtube.channel_pending(self.entry)
        # C (40 min) is NOT dropped — no cap on this channel
        self.assertEqual([v["vid"] for v in p], ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])

    def test_capped_channel_drops_over_limit(self):
        self.entry["scope"] = "all"; self.entry["capped"] = True   # opt IN to the ≤20-min cap
        with self._cap20():
            p = youtube.channel_pending(self.entry)
        self.assertEqual([v["vid"] for v in p], ["aaaaaaaaaa1", "aaaaaaaaaa2"])  # C dropped (>20 min)

    def test_done_excluded(self):
        self.entry["scope"] = "all"                           # uncapped → C stays
        youtube.mark_done("aaaaaaaaaa1")
        with self._cap20():
            p = youtube.channel_pending(self.entry)
        self.assertEqual([v["vid"] for v in p], ["aaaaaaaaaa2", "aaaaaaaaaa3"])

    def test_pending_batches_group_by_duration(self):
        # 4 x 8-min videos, cap 20 min → 8+8=16≤20, +8 overflows → [2, 2]
        youtube._save_queue([{"channelId": "UCy", "title": "C", "folder_name": "C", "scope": "all"}])
        vs = [_vid(f"C - v{i} [bbbbbbbbb0{i}]", 100 - i) for i in range(4)]
        youtube._VIDEO_CACHE["C"] = vs
        youtube._META["UCy"] = {"popular": set()}
        youtube._DURATIONS = {v["vid"]: 480 for v in vs}          # 8 min each (persisted cache)
        with self._cap20():
            batches = youtube.pending_batches(20 * 60)
        self.assertEqual([len(b) for b in batches], [2, 2])       # 2 per ~20-min batch

    def test_pending_batches_groups_even_with_unknown_durations(self):
        # regression for the "one big blob" bug: durations UNKNOWN → each counts as DEFAULT_YT_SECS so
        # grouping still forms (not one giant batch). 6 videos @ 300s default, cap 1200 → 4 then 2.
        youtube._save_queue([{"channelId": "UCz", "title": "Z", "folder_name": "Z", "scope": "all"}])
        vs = [_vid(f"Z - v{i} [ccccccccc0{i}]", 100 - i) for i in range(6)]
        youtube._VIDEO_CACHE["Z"] = vs
        youtube._META["UCz"] = {"popular": set()}
        youtube._DURATIONS = {}                                   # NOTHING measured yet
        with self._cap20():
            batches = youtube.pending_batches(20 * 60)
        self.assertEqual([len(b) for b in batches], [4, 2])       # grouped, NOT [6] one-blob


class RoundRobin(unittest.TestCase):
    """all_pending / next_due interleave videos across channels evenly — NO per-channel priority."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json"))
        self.p.start()
        youtube._DURATIONS = {}

    def tearDown(self):
        self.p.stop(); youtube._VIDEO_CACHE.clear(); youtube._META.clear(); youtube._DURATIONS = None

    def test_interleaves_channels_not_drain_by_queue_order(self):
        youtube._save_queue([{"channelId": "UCa", "title": "A", "folder_name": "A", "scope": "all"},
                             {"channelId": "UCb", "title": "B", "folder_name": "B", "scope": "all"}])
        youtube._VIDEO_CACHE["A"] = [_vid("A - a1 [aaaaaaaaaa1]", 3),
                                     _vid("A - a2 [aaaaaaaaaa2]", 2),
                                     _vid("A - a3 [aaaaaaaaaa3]", 1)]
        youtube._VIDEO_CACHE["B"] = [_vid("B - b1 [bbbbbbbbbb1]", 2),
                                     _vid("B - b2 [bbbbbbbbbb2]", 1)]
        youtube._META["UCa"] = {"popular": set()}
        youtube._META["UCb"] = {"popular": set()}
        order = [v["vid"] for v in youtube.all_pending()]
        # round-robin A,B,A,B,A (B runs out) — NOT A,A,A,B,B (that would be channel priority)
        self.assertEqual(order, ["aaaaaaaaaa1", "bbbbbbbbbb1", "aaaaaaaaaa2", "bbbbbbbbbb2", "aaaaaaaaaa3"])
        self.assertEqual(youtube.next_due()["vid"], "aaaaaaaaaa1")   # head of the round-robin


class LiveRefresh(unittest.TestCase):
    """refresh_downloads re-scans staging + fills durations for NEW ids only (no popular search)."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DURATIONS_FILE", os.path.join(self.d, "dur.json")),
                   mock.patch.object(youtube, "PUBLISHED_FILE", os.path.join(self.d, "pub.json"))]
        for p in self.ps:
            p.start()
        youtube._DURATIONS = {}; youtube._PUBLISHED = {}
        youtube._save_queue([{"channelId": "UCx", "title": "Chan", "folder_name": "Chan", "scope": "all"}])

    def tearDown(self):
        for p in self.ps:
            p.stop()
        youtube._VIDEO_CACHE.clear(); youtube._META.clear()
        youtube._DURATIONS = None; youtube._PUBLISHED = None

    def test_picks_up_new_downloads_and_fetches_only_missing(self):
        import ytdata
        # first scan: 1 video on disk → its duration + publish date fetched (one call) + PERSISTED
        with mock.patch.object(youtube, "list_video_files",
                               return_value=[_vid("Chan - a [aaaaaaaaaa1]", 1)]), \
             mock.patch.object(ytdata, "video_meta",
                               return_value={"aaaaaaaaaa1": {"secs": 120, "pub": 1700000000}}) as vm1:
            youtube.refresh_downloads()
        self.assertEqual([v["vid"] for v in youtube.cached_videos("Chan")], ["aaaaaaaaaa1"])
        self.assertEqual(youtube.video_secs("aaaaaaaaaa1"), 120)
        self.assertEqual(youtube.video_published("aaaaaaaaaa1"), 1700000000)
        vm1.assert_called_once_with(["aaaaaaaaaa1"])
        # second scan: a NEW video → ONLY the new id is fetched (the known one is already persisted)
        with mock.patch.object(youtube, "list_video_files",
                               return_value=[_vid("Chan - b [aaaaaaaaaa2]", 2),
                                             _vid("Chan - a [aaaaaaaaaa1]", 1)]), \
             mock.patch.object(ytdata, "video_meta",
                               return_value={"aaaaaaaaaa2": {"secs": 90, "pub": 1700000100}}) as vm2:
            youtube.refresh_downloads()
            vm2.assert_called_once_with(["aaaaaaaaaa2"])
        self.assertEqual(len(youtube.cached_videos("Chan")), 2)
        self.assertEqual(youtube.video_secs("aaaaaaaaaa2"), 90)

    def test_fetches_date_even_when_duration_already_known(self):
        import ytdata
        youtube._DURATIONS = {"aaaaaaaaaa1": 120}            # duration cached, but NO publish date
        youtube._PUBLISHED = {}
        with mock.patch.object(youtube, "list_video_files",
                               return_value=[_vid("Chan - a [aaaaaaaaaa1]", 1)]), \
             mock.patch.object(ytdata, "video_meta",
                               return_value={"aaaaaaaaaa1": {"secs": 120, "pub": 1700000000}}) as vm:
            youtube.refresh_downloads()
            vm.assert_called_once_with(["aaaaaaaaaa1"])      # fetched despite known duration (needed the date)
        self.assertEqual(youtube.video_published("aaaaaaaaaa1"), 1700000000)

    def test_durations_persist_across_reload(self):
        # a fetched duration is written to disk → a fresh _DURATIONS load (post-relaunch) still has it
        youtube.remember_durations({"zzzzzzzzzzz": 456})
        youtube._DURATIONS = None                                # simulate a relaunch (in-memory lost)
        self.assertEqual(youtube.video_secs("zzzzzzzzzzz"), 456)  # reloaded from disk


class WipeChannel(unittest.TestCase):
    """Removing a channel wipes BOTH roots, forgets its archive ids, and clears its done entries."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DONE_FILE", os.path.join(self.d, "done.json"))]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        youtube._VIDEO_CACHE.clear()

    def test_deletes_both_roots_forgets_and_clears_done(self):
        youtube._VIDEO_CACHE["Chan"] = [_vid("Chan - a [aaaaaaaaaa1]", 1)]
        youtube.mark_done("aaaaaaaaaa1"); youtube.mark_done("keepme00000")
        deleted = []
        with mock.patch("transfer.delete_tree", side_effect=lambda p: deleted.append(p) or True), \
             mock.patch("youtarr.channel_video_ids", return_value=["aaaaaaaaaa1", "bbbbbbbbbb2"]), \
             mock.patch("youtarr.forget_downloads", return_value=2) as fg, \
             mock.patch("youtarr.channel_folder", return_value="Chan"), \
             mock.patch.object(youtube, "configure_youtarr") as cfg:
            youtube.wipe_channel("UCx", "Chan")
        self.assertIn("/Media/YouTube-raw/Chan", deleted)   # raw staging folder
        self.assertIn("/Media/YouTube/Chan", deleted)       # published 4K masters
        self.assertEqual(set(fg.call_args[0][0]), {"aaaaaaaaaa1", "bbbbbbbbbb2"})  # union of ids forgotten
        self.assertEqual(youtube.get_done(), {"keepme00000"})   # wiped id dropped, unrelated kept
        self.assertNotIn("Chan", youtube._VIDEO_CACHE)      # channel cache dropped
        cfg.assert_called_once()                            # unsubscribe happens AFTER, in the wipe

    def test_unsafe_folder_deletes_nothing(self):
        self.assertEqual(youtube._safe_folder("../../etc"), "")
        self.assertEqual(youtube._safe_folder("A/B"), "")
        self.assertEqual(youtube._safe_folder(".."), "")
        self.assertEqual(youtube._safe_folder("  All Gas No Brakes  "), "All Gas No Brakes")
        deleted = []
        with mock.patch("transfer.delete_tree", side_effect=lambda p: deleted.append(p) or True), \
             mock.patch("youtarr.channel_video_ids", return_value=[]), \
             mock.patch("youtarr.forget_downloads", return_value=0), \
             mock.patch("youtarr.channel_folder", return_value="../../etc"), \
             mock.patch.object(youtube, "configure_youtarr"):
            youtube.wipe_channel("UCx", "../../etc")
        self.assertEqual(deleted, [])                       # a traversal folder → NO delete at all


class Paused(unittest.TestCase):
    """A paused channel does no upscaling and is excluded from youtarr's subscriptions (files kept)."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json"))
        self.p.start()
        youtube._DURATIONS = {}

    def tearDown(self):
        self.p.stop(); youtube._VIDEO_CACHE.clear(); youtube._META.clear(); youtube._DURATIONS = None

    def test_paused_channel_has_no_pending(self):
        youtube._VIDEO_CACHE["Chan"] = [_vid("Chan - a [aaaaaaaaaa1]", 1)]
        entry = {"channelId": "UCx", "folder_name": "Chan", "scope": "all", "paused": True}
        self.assertEqual(youtube.channel_pending(entry), [])          # paused → nothing to upscale
        entry["paused"] = False
        self.assertEqual(len(youtube.channel_pending(entry)), 1)      # active → the video is pending

    def test_configure_youtarr_excludes_paused(self):
        youtube._save_queue([{"channelId": "UCa", "paused": False, "folder_name": "A"},
                             {"channelId": "UCb", "paused": True, "folder_name": "B"}])
        with mock.patch("youtarr.sync_subscriptions", return_value=True) as sync, \
             mock.patch("youtarr.channel_folder", return_value=None), \
             mock.patch.object(youtube, "refresh_meta") as rm:
            youtube.configure_youtarr()
        self.assertEqual([d["channelId"] for d in sync.call_args[0][0]], ["UCa"])   # paused UCb unsubscribed
        self.assertEqual([c.args[0]["channelId"] for c in rm.call_args_list], ["UCa"])  # no meta for paused

    def test_set_paused_toggles_and_shows_in_queue(self):
        youtube.add_channel("UCx", "X")
        self.assertFalse(youtube.get_queue()[0]["paused"])           # default active
        youtube.set_paused("UCx", True)
        self.assertTrue(youtube.get_queue()[0]["paused"])


class MaxAge(unittest.TestCase):
    """Per-channel max-age: channel_pending skips too-old + prune_old DELETES them (download-then-delete)."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DONE_FILE", os.path.join(self.d, "done.json")),
                   mock.patch.object(youtube, "PUBLISHED_FILE", os.path.join(self.d, "pub.json"))]
        for p in self.ps:
            p.start()
        youtube._DURATIONS = {}
        now = int(time.time())
        youtube._VIDEO_CACHE["Chan"] = [_vid("Chan - recent [aaaaaaaaaa1]", 2),
                                        _vid("Chan - old [aaaaaaaaaa2]", 1)]
        youtube._PUBLISHED = {"aaaaaaaaaa1": now - 2 * 86400, "aaaaaaaaaa2": now - 400 * 86400}
        self.entry = {"channelId": "UCx", "folder_name": "Chan", "scope": "all", "max_age_days": 30}

    def tearDown(self):
        for p in self.ps:
            p.stop()
        youtube._VIDEO_CACHE.clear(); youtube._DURATIONS = None; youtube._PUBLISHED = None

    def test_pending_skips_too_old(self):
        self.assertEqual([v["vid"] for v in youtube.channel_pending(self.entry)], ["aaaaaaaaaa1"])
        self.entry["max_age_days"] = 0                       # no limit → both
        self.assertEqual(len(youtube.channel_pending(self.entry)), 2)

    def test_prune_deletes_only_the_too_old(self):
        deleted = []
        with mock.patch("transfer.delete_tree", side_effect=lambda p: deleted.append(p) or True), \
             mock.patch("youtarr.ignore_video", return_value=True) as ig, \
             mock.patch.object(youtube, "refresh_videos"):
            n = youtube.prune_old(self.entry)
        self.assertEqual(n, 1)                               # only the 400-day-old one
        self.assertEqual(deleted, ["/d/Chan - old [aaaaaaaaaa2]"])   # its staging dir
        ig.assert_called_once_with("UCx", "aaaaaaaaaa2")     # youtarr told to not re-download it
        self.assertIn("aaaaaaaaaa2", youtube.get_done())     # marked done so it won't re-queue

    def test_prune_noop_without_limit(self):
        self.entry["max_age_days"] = 0
        with mock.patch("transfer.delete_tree", side_effect=AssertionError("should not delete")):
            self.assertEqual(youtube.prune_old(self.entry), 0)


if __name__ == "__main__":
    unittest.main()


class ResumeFirst(unittest.TestCase):
    """A channel PAUSE that interrupts a video makes channel_pending serve THAT video first on resume."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ps = [mock.patch.object(youtube, "QUEUE_FILE", os.path.join(self.d, "q.json")),
                   mock.patch.object(youtube, "DONE_FILE", os.path.join(self.d, "done.json")),
                   mock.patch.object(youtube, "DURATIONS_FILE", os.path.join(self.d, "dur.json")),
                   mock.patch.object(youtube, "RESUME_FIRST_FILE", os.path.join(self.d, "rf.json"))]
        for p in self.ps:
            p.start()
        youtube._VIDEO_CACHE["Chan"] = [_vid("Chan - A [aaaaaaaaaa1]", 300),
                                        _vid("Chan - B [aaaaaaaaaa2]", 200),
                                        _vid("Chan - C [aaaaaaaaaa3]", 100)]
        self.entry = {"channelId": "UCx", "title": "Chan", "folder_name": "Chan", "scope": "all"}

    def tearDown(self):
        for p in self.ps:
            p.stop()
        youtube._VIDEO_CACHE.clear()

    def _order(self):
        return [v["vid"] for v in youtube.channel_pending(self.entry)]

    def test_default_order_unchanged(self):
        self.assertEqual(self._order(), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])

    def test_interrupted_video_comes_back_first(self):
        youtube.set_resume_first("Chan", "aaaaaaaaaa3")          # C was interrupted by a pause
        self.assertEqual(self._order(), ["aaaaaaaaaa3", "aaaaaaaaaa1", "aaaaaaaaaa2"])

    def test_persisted_across_reload(self):
        youtube.set_resume_first("Chan", "aaaaaaaaaa2")
        self.assertEqual(youtube.resume_first("Chan"), "aaaaaaaaaa2")   # read from disk, not memory

    def test_clear_restores_normal_order(self):
        youtube.set_resume_first("Chan", "aaaaaaaaaa3")
        youtube.clear_resume_first("Chan")
        self.assertIsNone(youtube.resume_first("Chan"))
        self.assertEqual(self._order(), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])

    def test_stale_marker_is_ignored(self):
        youtube.set_resume_first("Chan", "not_present")          # video no longer on disk → no crash, no move
        self.assertEqual(self._order(), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])
