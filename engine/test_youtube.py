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

    def test_length_cap_is_gone_even_with_the_legacy_flag_set(self):
        """The cap was removed 2026-08-17: YouTube skips Topaz and ships Resolve's render
        stream-copied, so a long video costs time in proportion to its length instead of an
        hour-class pass. A queue file still carrying `capped: True` must NOT filter."""
        self.entry["scope"] = "all"; self.entry["capped"] = True   # legacy flag, now inert
        with self._cap20():
            p = youtube.channel_pending(self.entry)
        self.assertEqual([v["vid"] for v in p],
                         ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])   # 40-min video kept

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


class SendToVisionary(unittest.TestCase):
    """The companion YouTube app's button: POST /api/send-to-visionary → youtarr grabs
    exactly that video, a durable priority book remembers it, and selection serves it as
    the NEXT item (cadence-exempt) the moment its file is on staging."""

    def setUp(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        patcher = mock.patch.object(youtube, "PRIORITY_FILE", os.path.join(d, "p.json"))
        patcher.start(); self.addCleanup(patcher.stop)
        dp = mock.patch.object(youtube, "DONE_FILE", os.path.join(d, "done.json"))
        dp.start(); self.addCleanup(dp.stop)

    def test_parse_video_id_forms(self):
        for t, want in [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ?t=5", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("not a url at all", ""),
            ("", ""),
        ]:
            self.assertEqual(youtube.parse_video_id(t), want, t)

    def test_send_queues_once_and_is_idempotent(self):
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=True) as dl:
            r1 = youtube.send_priority("https://youtu.be/dQw4w9WgXcQ", title="A Video")
            r2 = youtube.send_priority("dQw4w9WgXcQ")
        self.assertEqual(r1["status"], "queued")
        self.assertEqual(r2["status"], "already-queued")
        dl.assert_called_once_with(["dQw4w9WgXcQ"])
        self.assertEqual(youtube._priority()[0]["title"], "A Video")

    def test_send_reports_youtarr_down_and_records_nothing(self):
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=None):
            r = youtube.send_priority("dQw4w9WgXcQ")
        self.assertEqual(r["status"], "youtarr-unreachable")
        self.assertEqual(youtube._priority(), [])          # retry later re-sends

    def test_send_refuses_junk_and_already_done(self):
        self.assertEqual(youtube.send_priority("nope")["status"], "bad-url")
        youtube.mark_done("dQw4w9WgXcQ")
        self.assertEqual(youtube.send_priority("dQw4w9WgXcQ")["status"], "already-upscaled")

    def test_mark_done_retires_the_priority_entry(self):
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=True):
            youtube.send_priority("dQw4w9WgXcQ")
        self.assertEqual(len(youtube._priority()), 1)
        youtube.mark_done("dQw4w9WgXcQ")                   # finished (either path)
        self.assertEqual(youtube._priority(), [])

    def test_locate_returns_only_on_staging_and_respects_skip(self):
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=True):
            youtube.send_priority("dQw4w9WgXcQ", title="T")
        # not located yet → None (scan mocked quiet)
        with mock.patch.object(youtube, "_locate_scan"):
            self.assertIsNone(youtube.locate_priority())
        # located → served; skip by the file STEM hides it (in-flight elsewhere)
        book = youtube._priority()
        book[0].update(channel="Chan", path="/staging/Chan/vid/My Video [dQw4w9WgXcQ].mp4")
        youtube._save_priority(book)
        with mock.patch.object(youtube, "_locate_scan"):
            got = youtube.locate_priority()
            self.assertEqual(got["channel"], "Chan")
            self.assertEqual(got["vid"], "dQw4w9WgXcQ")
            self.assertIsNone(youtube.locate_priority(
                skip={"My Video [dQw4w9WgXcQ]"}))


class PublishOrderAfterAPause(unittest.TestCase):
    """A channel paused for weeks must resume at the NEWEST video and work back.
    cached_videos is ordered by file mtime = WHEN YOUTARR FETCHED IT, so a channel
    unpaused after a long gap backfills its missed videos with the newest mtimes; those
    old videos sorted to the front and the pipeline carried on from where it paused
    (user-reported 2026-08-17). Ordering is by PUBLISH date now."""

    def _entry(self):
        return {"channelId": "C1", "folder_name": "Chan", "scope": "all", "capped": False}

    def _videos(self):
        # mtime order (download order) is the INVERSE of publish order — the backfill case.
        return [
            {"vid": "old1", "name": "old1 [old1].mp4", "dir": "/d", "path": "/d/old1.mp4",
             "mtime": 9000},          # backfilled last -> newest mtime
            {"vid": "old2", "name": "old2 [old2].mp4", "dir": "/d", "path": "/d/old2.mp4",
             "mtime": 8000},
            {"vid": "new1", "name": "new1 [new1].mp4", "dir": "/d", "path": "/d/new1.mp4",
             "mtime": 1000},          # downloaded before the pause -> oldest mtime
        ]

    def _pending(self, pubs):
        with mock.patch.object(youtube, "cached_videos", return_value=self._videos()), \
             mock.patch.object(youtube, "_durations", return_value={}), \
             mock.patch.object(youtube, "_published", return_value=pubs), \
             mock.patch.object(youtube, "get_done", return_value=set()), \
             mock.patch.object(youtube, "resume_first", return_value=None), \
             mock.patch.object(youtube, "video_title", side_effect=lambda n, f: n):
            return [v["vid"] for v in youtube.channel_pending(self._entry())]

    def test_publish_date_beats_download_time(self):
        pubs = {"new1": 2_000_000, "old1": 1_000_000, "old2": 1_500_000}
        self.assertEqual(self._pending(pubs), ["new1", "old2", "old1"])

    def test_unknown_publish_dates_fall_back_to_mtime(self):
        self.assertEqual(self._pending({}), ["old1", "old2", "new1"])   # mtime order


class ResumePinExpires(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch.object(youtube, "RESUME_FIRST_FILE",
                              os.path.join(self.d, "rf.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_fresh_pin_is_honoured(self):
        youtube.set_resume_first("Chan", "vid123")
        self.assertEqual(youtube.resume_first("Chan"), "vid123")

    def test_weeks_old_pin_expires(self):
        youtube.set_resume_first("Chan", "vid123")
        old = time.time() + youtube.RESUME_FIRST_MAX_AGE + 60
        with mock.patch.object(youtube.time, "time", return_value=old):
            self.assertIsNone(youtube.resume_first("Chan"))

    def test_legacy_untimestamped_pin_is_treated_as_stale(self):
        import json as _json
        with open(youtube.RESUME_FIRST_FILE, "w") as f:
            _json.dump({"Chan": "vid123"}, f)     # the old bare-string form
        self.assertIsNone(youtube.resume_first("Chan"))


class PrioritizePending(unittest.TestCase):
    """"Run this video now": an already-downloaded pending video jumps to the FRONT of the
    priority book, so the very next selection serves it — cadence-exempt and ahead of due
    movies. send_priority covers the companion-app push; this covers the queue rows."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = mock.patch.object(youtube, "PRIORITY_FILE",
                              os.path.join(self.d, "prio.json"))
        p.start()
        self.addCleanup(p.stop)
        self.pending = [
            {"vid": "aaaaaaaaaa1", "channel": "Chan", "title": "First",
             "video_path": "/staging/Chan/a1/a1.mp4", "source_name": "a1 [aaaaaaaaaa1].mp4"},
            {"vid": "aaaaaaaaaa2", "channel": "Chan", "title": "Second",
             "video_path": "/staging/Chan/a2/a2.mp4", "source_name": "a2 [aaaaaaaaaa2].mp4"},
        ]

    def _prio(self, vid, done=()):
        with mock.patch.object(youtube, "all_pending", return_value=self.pending), \
             mock.patch.object(youtube, "get_done", return_value=set(done)):
            return youtube.prioritize_pending(vid)

    def test_queues_with_its_path_so_no_staging_scan_is_needed(self):
        out = self._prio("aaaaaaaaaa2")
        self.assertEqual(out["status"], "queued")
        book = youtube._priority()
        self.assertEqual(len(book), 1)
        self.assertEqual(book[0]["path"], "/staging/Chan/a2/a2.mp4")
        self.assertEqual(book[0]["channel"], "Chan")

    def test_most_recent_request_wins_the_front(self):
        self._prio("aaaaaaaaaa1")
        self._prio("aaaaaaaaaa2")
        self.assertEqual([e["vid"] for e in youtube._priority()],
                         ["aaaaaaaaaa2", "aaaaaaaaaa1"])

    def test_refusals_are_explicit_not_silent(self):
        self.assertEqual(self._prio("")["status"], "bad-id")
        self.assertEqual(self._prio("zzzzzzzzzzz")["status"], "not-pending")
        self.assertEqual(self._prio("aaaaaaaaaa1", done=["aaaaaaaaaa1"])["status"],
                         "already-upscaled")
        self._prio("aaaaaaaaaa1")
        self.assertEqual(self._prio("aaaaaaaaaa1")["status"], "already-first")

    def test_readiness_probe_is_book_only_and_skips_the_current_item(self):
        # Polled between Topaz segments, so it must never trigger the FTP staging walk.
        with mock.patch.object(youtube, "_locate_scan",
                               side_effect=AssertionError("must not scan")), \
             mock.patch.object(youtube, "get_done", return_value=set()):
            self.assertFalse(youtube.has_priority_ready())
            self._prio("aaaaaaaaaa2")
            self.assertTrue(youtube.has_priority_ready())
            # the video already running IS the priority pick — it must not yield to itself
            self.assertFalse(youtube.has_priority_ready(skip={"a2"}))


class FetchAhead(unittest.TestCase):
    """Visionary only SUBSCRIBED youtarr and waited for its schedule, so the upscale queue
    could run dry with the pipeline idle and the NAS quiet. fetch_ahead tops each channel's
    staging buffer up to a target, newest-first."""

    def setUp(self):
        self.entry = {"channelId": "C1", "folder_name": "Chan", "scope": "all"}
        self.on_disk = [{"vid": "have1", "name": "a [have1].mp4", "dir": "/d",
                         "path": "/d/a.mp4", "mtime": 1}]
        self.known = ["new3", "new1", "have1", "new2", "old1"]
        self.NOW = 1_000_000                       # a coherent clock for the age filter
        self.pubs = {"new3": self.NOW - 100, "new1": self.NOW - 200,
                     "have1": self.NOW - 300, "new2": self.NOW - 400,
                     "old1": self.NOW - 5 * 86400}

    def _wanted(self, target, done=(), max_age=None, scope="all", popular=None):
        e = dict(self.entry, scope=scope)
        if max_age:
            e["max_age_days"] = max_age
        with mock.patch.object(youtube, "cached_videos", return_value=self.on_disk), \
             mock.patch.object(youtube, "get_done", return_value=set(done)), \
             mock.patch.object(youtube, "_published", return_value=self.pubs), \
             mock.patch.dict(youtube._META, {"C1": {"popular": popular or set()}}, clear=False), \
             mock.patch("youtarr.channel_video_ids", return_value=self.known):
            return youtube.wanted_ids(e, target)

    def test_newest_first_excluding_what_is_already_there(self):
        self.assertEqual(self._wanted(10), ["new3", "new1", "new2", "old1"])

    def test_target_bounds_the_ask(self):
        self.assertEqual(self._wanted(2), ["new3", "new1"])

    def test_done_and_age_filters_apply(self):
        self.assertNotIn("new3", self._wanted(10, done=["new3"]))
        # old1 is 5 days old; a 1-day limit drops it and keeps the rest
        with mock.patch.object(youtube.time, "time", return_value=self.NOW):
            got = self._wanted(10, max_age=1)
        self.assertNotIn("old1", got)
        self.assertIn("new3", got)          # a recent one is still wanted

    def test_popular_scope_only_asks_for_the_popular_set(self):
        got = self._wanted(10, scope="popular", popular={"new2"})
        self.assertEqual(got, ["new2"])

    def test_paused_channel_is_never_fetched(self):
        e = dict(self.entry, paused=True)
        with mock.patch.object(youtube, "cached_videos", return_value=[]):
            self.assertEqual(youtube.wanted_ids(e, 10), [])

    def test_fetch_ahead_off_by_setting_asks_nothing(self):
        import settings
        with mock.patch.object(settings, "get_settings", return_value={"youtube_fetch_ahead": 0}), \
             mock.patch("youtarr.download_videos",
                        side_effect=AssertionError("must not ask youtarr")):
            self.assertEqual(youtube.fetch_ahead(force=True), {})

    def test_fetch_ahead_asks_only_for_the_shortfall(self):
        import settings
        asked = {}
        with mock.patch.object(settings, "get_settings", return_value={"youtube_fetch_ahead": 3}), \
             mock.patch.object(youtube, "get_queue", return_value=[self.entry]), \
             mock.patch.object(youtube, "cached_videos", return_value=self.on_disk), \
             mock.patch.object(youtube, "get_done", return_value=set()), \
             mock.patch.object(youtube, "_published", return_value=self.pubs), \
             mock.patch("youtarr.channel_video_ids", return_value=self.known), \
             mock.patch("youtarr.download_videos",
                        side_effect=lambda ids, **k: asked.update({"ids": list(ids)}) or True):
            out = youtube.fetch_ahead(force=True)
        self.assertEqual(asked["ids"], ["new3", "new1"])     # target 3, one already on disk
        self.assertIn("Chan", out)

    def test_rate_limited_between_ticks(self):
        import settings
        with mock.patch.object(settings, "get_settings", return_value={"youtube_fetch_ahead": 3}), \
             mock.patch.object(youtube, "get_queue", return_value=[]):
            youtube.fetch_ahead(force=True)          # stamps the clock
            self.assertEqual(youtube.fetch_ahead(), {})   # immediate re-tick does nothing
