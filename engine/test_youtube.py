import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import transfer
import youtube

_ROTATION_PATCH = None


_BOOK_PATCHES = []


def setUpModule():
    """EVERY persisted book is redirected module-wide. all_pending() consults the rotation
    pointer AND — via _import_pending — the priority and imports books, so a test that
    doesn't redirect them inherits whatever is genuinely queued on this machine. That hole
    was latent for as long as the live books were empty; the first real playlist import
    put PewDiePie videos into three unrelated tests' expected orderings (2026-08-28).
    Classes that want their own books still override per-test; this is the floor."""
    d = tempfile.mkdtemp()
    for name, fn in (("ROTATION_FILE", "yt_rotation.json"),
                     ("PRIORITY_FILE", "yt_priority.json"),
                     ("IMPORTS_FILE", "yt_imports.json"),
                     ("DONE_FILE", "yt_done.json"),
                     ("QUEUE_FILE", "yt_queue.json")):
        p = mock.patch.object(youtube, name, os.path.join(d, fn))
        p.start()
        _BOOK_PATCHES.append(p)


def tearDownModule():
    for p in _BOOK_PATCHES:
        p.stop()



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

    def test_send_goes_to_the_FRONT_so_it_actually_runs_next(self):
        """locate_priority() serves the first eligible entry in book order, so a send that is
        appended sits behind everything already queued — with a book full of link-imports the
        button would not run next in any meaningful sense. Most recent send wins."""
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=True):
            youtube.send_priority("dQw4w9WgXcQ", title="first")
            youtube.send_priority("aaaaaaaaaaa", title="second")
        book = youtube._priority()
        self.assertEqual([e["vid"] for e in book], ["aaaaaaaaaaa", "dQw4w9WgXcQ"])
        self.assertEqual(book[0]["title"], "second")

    def test_send_preempts_an_import_already_in_the_book(self):
        """A link IMPORT does not jump the queue; a send must land ahead of one regardless of
        when each arrived."""
        import youtarr
        youtube._save_priority([{"vid": "imported0001", "jump": False, "batch": "b", "seq": 1}])
        with mock.patch.object(youtarr, "download_videos", return_value=True):
            youtube.send_priority("dQw4w9WgXcQ", title="sent")
        book = youtube._priority()
        self.assertEqual(book[0]["vid"], "dQw4w9WgXcQ")
        self.assertTrue(youtube._jumps(book[0]))
        self.assertFalse(youtube._jumps(book[1]))

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


class YoutarrContract(unittest.TestCase):
    """youtarr's own settings are a contract Visionary depends on, and they used to be
    hand-set with nothing checking them. A wrong one fails QUIETLY: raw downloads landing
    in the Plex library, masters shipping without sidecars, or nothing downloading."""

    GOOD = {"youtubeOutputDirectory": "/volume1/Media/YouTube-raw",
            "channelAutoDownload": True, "writeVideoNfoFiles": True,
            "writeVideoFanart": True, "writeChannelPosters": True,
            "subtitlesEnabled": True}

    def test_host_path_matches_the_ftp_staging_path_by_suffix(self):
        # youtarr writes /volume1/Media/YouTube-raw; Visionary reads /Media/YouTube-raw over
        # FTP. Demanding equality would fail forever on a correct setup.
        rows = {r["key"]: r for r in youtube.youtarr_contract(self.GOOD)}
        self.assertTrue(rows["youtubeOutputDirectory"]["ok"])

    def test_output_pointing_at_the_plex_library_is_flagged(self):
        bad = dict(self.GOOD, youtubeOutputDirectory="/volume1/Media/YouTube")
        rows = {r["key"]: r for r in youtube.youtarr_contract(bad)}
        self.assertFalse(rows["youtubeOutputDirectory"]["ok"])

    def test_each_missing_toggle_is_reported(self):
        for key in ("channelAutoDownload", "writeVideoNfoFiles", "writeVideoFanart",
                    "writeChannelPosters", "subtitlesEnabled"):
            rows = {r["key"]: r for r in youtube.youtarr_contract(dict(self.GOOD, **{key: False}))}
            self.assertFalse(rows[key]["ok"], key)
            self.assertTrue(rows[key]["why"], key)

    def test_apply_patches_only_what_is_wrong(self):
        sent = {}
        bad = dict(self.GOOD, writeVideoFanart=False, subtitlesEnabled=False)
        with mock.patch("youtarr.get_config", return_value=bad), \
             mock.patch("youtarr.update_config",
                        side_effect=lambda p, **k: sent.update(p) or True):
            out = youtube.apply_youtarr_contract()
        self.assertEqual(sent, {"writeVideoFanart": True, "subtitlesEnabled": True})
        self.assertTrue(out["ok"])

    def test_apply_never_rewrites_the_output_directory(self):
        # Only youtarr knows its mount layout — writing an absolute path we guessed would
        # strand every future download somewhere Visionary cannot read.
        sent = {}
        bad = dict(self.GOOD, youtubeOutputDirectory="/somewhere/else")
        with mock.patch("youtarr.get_config", return_value=bad), \
             mock.patch("youtarr.update_config",
                        side_effect=lambda p, **k: sent.update(p) or True):
            youtube.apply_youtarr_contract()
        self.assertNotIn("youtubeOutputDirectory", sent)

    def test_nothing_to_do_makes_no_call(self):
        with mock.patch("youtarr.get_config", return_value=self.GOOD), \
             mock.patch("youtarr.update_config",
                        side_effect=AssertionError("must not write")):
            self.assertEqual(youtube.apply_youtarr_contract(), {"changed": {}, "ok": True})

    def test_unreachable_youtarr_is_reported_not_crashed(self):
        with mock.patch("youtarr.get_config", return_value=None):
            self.assertEqual(youtube.youtarr_config_status()["error"], "youtarr-unreachable")
            self.assertEqual(youtube.apply_youtarr_contract()["error"], "youtarr-unreachable")


class YoutarrOutputDirIsDerivedNotGuessed(unittest.TestCase):
    """youtarr only has to EXIST — Visionary configures it, including where downloads land.
    The same folder has three names (youtarr's host path, Visionary's FTP path, Plex's
    container path) and nothing can compute that mapping from first principles. youtarr's
    OWN current value reveals it: match its tail against a media path we know, and whatever
    precedes it is the prefix. No match -> report, never invent an absolute path."""

    def test_prefix_is_observed_from_the_current_value(self):
        self.assertEqual(youtube._youtarr_host_prefix("/volume1/Media/YouTube-raw"), "/volume1")
        self.assertEqual(youtube._youtarr_host_prefix("/Media/YouTube"), "")
        self.assertIsNone(youtube._youtarr_host_prefix("/downloads/yt"))
        self.assertIsNone(youtube._youtarr_host_prefix(""))

    def test_the_plex_library_misconfiguration_is_retargeted_to_staging(self):
        # The failure this prevents: raw 1080p downloads appearing in the Plex library.
        want = youtube.youtarr_desired_output_dir(
            {"youtubeOutputDirectory": "/volume1/Media/YouTube"})
        self.assertEqual(want, "/volume1/Media/YouTube-raw")

    def test_an_unrecognisable_path_is_never_rewritten(self):
        cfg = {"youtubeOutputDirectory": "/downloads/yt", "channelAutoDownload": True,
               "writeVideoNfoFiles": True, "writeVideoFanart": True,
               "writeChannelPosters": True, "subtitlesEnabled": True}
        row = {r["key"]: r for r in youtube.youtarr_contract(cfg)}["youtubeOutputDirectory"]
        self.assertFalse(row["ok"])
        self.assertIsNone(row["desired"])          # nothing to apply -> reported instead
        self.assertIn("cannot be derived", row["why"])

    def test_apply_creates_staging_before_pointing_youtarr_at_it(self):
        order = []
        cfg = {"youtubeOutputDirectory": "/volume1/Media/YouTube", "channelAutoDownload": True,
               "writeVideoNfoFiles": True, "writeVideoFanart": True,
               "writeChannelPosters": True, "subtitlesEnabled": True}
        with mock.patch("youtarr.get_config", return_value=cfg), \
             mock.patch.object(youtube, "ensure_staging_dir",
                               side_effect=lambda: order.append("mkdir") or True), \
             mock.patch("youtarr.update_config",
                        side_effect=lambda p, **k: order.append(("set", p)) or True):
            out = youtube.apply_youtarr_contract()
        self.assertEqual(order[0], "mkdir")        # folder first, then the pointer
        self.assertEqual(order[1][1]["youtubeOutputDirectory"], "/volume1/Media/YouTube-raw")
        self.assertTrue(out["ok"])

    def test_apply_skips_the_pointer_if_staging_cannot_be_created(self):
        cfg = {"youtubeOutputDirectory": "/volume1/Media/YouTube", "channelAutoDownload": True,
               "writeVideoNfoFiles": True, "writeVideoFanart": True,
               "writeChannelPosters": True, "subtitlesEnabled": True}
        sent = {}
        with mock.patch("youtarr.get_config", return_value=cfg), \
             mock.patch.object(youtube, "ensure_staging_dir", return_value=False), \
             mock.patch("youtarr.update_config",
                        side_effect=lambda p, **k: sent.update(p) or True):
            youtube.apply_youtarr_contract()
        self.assertNotIn("youtubeOutputDirectory", sent)   # never point at a missing folder


def _imp_entry(vid, seq, batch, path):
    return {"vid": vid, "title": None, "sent_at": 1, "jump": False, "seq": seq,
            "batch": batch, "channel": "Chan", "path": path}


class LinkImports(unittest.TestCase):
    """A pasted playlist/video link becomes ordinary priority-book entries with jump=False:
    durable + locatable like send-to-Visionary, but served at NORMAL CADENCE instead of
    preempting the pipeline (user-dictated)."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)
        youtube._DURATIONS = {}
        self.addCleanup(lambda: setattr(youtube, "_DURATIONS", None))
        self.addCleanup(youtube._VIDEO_CACHE.clear)
        self.addCleanup(youtube._META.clear)

    # ---- the load-bearing rule: imports must never preempt -----------------
    def test_imports_never_jump_the_queue(self):
        youtube._save_priority([_imp_entry("aaaaaaaaaa1", 0, "imp1", "/s/Chan/x/a.mp4")])
        self.assertIsNone(youtube.locate_priority())      # not served as a priority interrupt
        self.assertFalse(youtube.has_priority_ready())    # and never asks topaz to yield

    def test_send_to_visionary_still_jumps(self):
        youtube._save_priority([{"vid": "bbbbbbbbbb1", "title": "T", "sent_at": 1,
                                 "jump": True, "channel": "Chan", "path": "/s/Chan/y/b.mp4"}])
        self.assertTrue(youtube.has_priority_ready())
        self.assertEqual((youtube.locate_priority() or {}).get("vid"), "bbbbbbbbbb1")

    def test_a_book_written_before_imports_existed_still_jumps(self):
        # no `jump` key at all — must default to the old preempting behaviour
        youtube._save_priority([{"vid": "cccccccccc1", "title": "T", "sent_at": 1,
                                 "channel": "Chan", "path": "/s/Chan/z/c.mp4"}])
        self.assertTrue(youtube.has_priority_ready())
        self.assertEqual((youtube.locate_priority() or {}).get("vid"), "cccccccccc1")

    # ---- ordering ----------------------------------------------------------
    def test_playlist_order_holds_through_all_pending(self):
        youtube._save_priority([                       # deliberately stored out of order
            _imp_entry("ddddddddd03", 2, "imp1", "/s/Chan/3/c.mp4"),
            _imp_entry("ddddddddd01", 0, "imp1", "/s/Chan/1/a.mp4"),
            _imp_entry("ddddddddd02", 1, "imp1", "/s/Chan/2/b.mp4")])
        self.assertEqual([v["vid"] for v in youtube.all_pending()],
                         ["ddddddddd01", "ddddddddd02", "ddddddddd03"])

    def test_two_batches_interleave_like_channels_do(self):
        youtube._save_priority([_imp_entry("eeeeeeeee01", 0, "impA", "/s/C/1/a.mp4"),
                                _imp_entry("eeeeeeeee02", 1, "impA", "/s/C/2/b.mp4"),
                                _imp_entry("fffffffff01", 0, "impB", "/s/C/3/c.mp4")])
        self.assertEqual([v["vid"] for v in youtube.all_pending()],
                         ["eeeeeeeee01", "fffffffff01", "eeeeeeeee02"])

    def test_done_and_skipped_videos_drop_out(self):
        youtube._save_priority([_imp_entry("ggggggggg01", 0, "imp1", "/s/C/1/a.mp4"),
                                _imp_entry("ggggggggg02", 1, "imp1", "/s/C/2/b.mp4")])
        youtube.mark_done("ggggggggg01")               # finished → retired from the book
        self.assertEqual([v["vid"] for v in youtube.all_pending()], ["ggggggggg02"])
        self.assertEqual(youtube.all_pending(skip={"b"}), [])   # skip keys are file STEMS

    # ---- committing an import ---------------------------------------------
    def test_import_link_queues_a_playlist_in_playlist_order(self):
        import ytdata, youtarr
        ids = ["hhhhhhhhh01", "hhhhhhhhh02", "hhhhhhhhh03"]
        with mock.patch.object(ytdata, "playlist_video_ids", return_value=ids), \
             mock.patch.object(ytdata, "playlist_meta",
                               return_value={"title": "My List", "count": 3, "channel_title": "C"}), \
             mock.patch.object(youtarr, "download_videos", return_value=True) as dl:
            out = youtube.import_link("https://www.youtube.com/playlist?list=PLxyz")
        self.assertEqual(out["status"], "queued")
        self.assertEqual((out["count"], out["title"]), (3, "My List"))
        book = youtube._priority()
        self.assertEqual([e["vid"] for e in book], ids)
        self.assertEqual([e["seq"] for e in book], [0, 1, 2])
        self.assertTrue(all(e["jump"] is False for e in book))
        dl.assert_called_once_with(ids)                # youtarr actually fetches them
        self.assertEqual([r["title"] for r in youtube.imports_view()], ["My List"])

    def test_unreachable_youtarr_does_not_strand_the_book(self):
        import ytdata, youtarr
        with mock.patch.object(ytdata, "playlist_video_ids", return_value=["iiiiiiiii01"]), \
             mock.patch.object(ytdata, "playlist_meta", return_value={"title": "L", "count": 1,
                                                                      "channel_title": "C"}), \
             mock.patch.object(youtarr, "download_videos", return_value=False):
            out = youtube.import_link("https://www.youtube.com/playlist?list=PLxyz")
        self.assertEqual(out["status"], "youtarr-unreachable")
        self.assertEqual(youtube._priority(), [])      # rolled back — nothing will ever arrive
        self.assertEqual(youtube.imports_view(), [])

    def test_ambiguous_link_honours_the_choice(self):
        import ytdata, youtarr
        url = "https://www.youtube.com/watch?v=jjjjjjjjj01&list=PLxyz"
        with mock.patch.object(youtarr, "download_videos", return_value=True), \
             mock.patch.object(ytdata, "playlist_video_ids", return_value=["k1", "k2"]), \
             mock.patch.object(ytdata, "playlist_meta", return_value={"title": "L", "count": 2,
                                                                      "channel_title": "C"}):
            out = youtube.import_link(url, choice="video")
            self.assertEqual([e["vid"] for e in youtube._priority()], ["jjjjjjjjj01"])
            self.assertEqual(out["kind"], "video")
            youtube._save_priority([])
            out = youtube.import_link(url, choice="playlist")
            self.assertEqual([e["vid"] for e in youtube._priority()], ["k1", "k2"])
            self.assertEqual(out["kind"], "playlist")

    # ---- managing imports --------------------------------------------------
    def test_drop_import_removes_only_its_own_batch(self):
        youtube._save_priority([_imp_entry("lllllllll01", 0, "impA", "/s/C/1/a.mp4"),
                                _imp_entry("lllllllll02", 0, "impB", "/s/C/2/b.mp4")])
        youtube._save_imports([{"id": "impA", "kind": "playlist", "title": "A", "count": 1},
                               {"id": "impB", "kind": "playlist", "title": "B", "count": 1}])
        self.assertEqual(youtube.drop_import("impA"), {"status": "ok", "removed": 1})
        self.assertEqual([e["vid"] for e in youtube._priority()], ["lllllllll02"])
        self.assertEqual([r["id"] for r in youtube.imports_view()], ["impB"])

    def test_imports_view_forgets_a_finished_batch(self):
        youtube._save_priority([_imp_entry("mmmmmmmmm01", 0, "impA", "/s/C/1/a.mp4")])
        youtube._save_imports([{"id": "impA", "kind": "playlist", "title": "A", "count": 1}])
        self.assertEqual([r["remaining"] for r in youtube.imports_view()], [1])
        youtube.mark_done("mmmmmmmmm01")
        self.assertEqual(youtube.imports_view(), [])   # nothing left to manage

    # ---- channel links -----------------------------------------------------
    def test_channel_link_badged_only_when_not_a_subscription(self):
        import ytdata
        ch = {"channelId": "UCaaaaaaaaaaaaaaaaaaaaaa", "title": "Chan"}
        with mock.patch.object(ytdata, "channel_for", return_value=ch), \
             mock.patch.object(ytdata, "subscriptions", return_value=[]):
            out = youtube.import_link("https://www.youtube.com/@Chan")
        self.assertEqual(out["status"], "channel-queued")
        self.assertFalse(out["subscribed"])
        self.assertTrue(youtube.get_queue()[0]["via_link"])          # badged
        youtube._save_queue([])
        with mock.patch.object(ytdata, "channel_for", return_value=ch), \
             mock.patch.object(ytdata, "subscriptions", return_value=[ch]):
            out = youtube.import_link("https://www.youtube.com/@Chan")
        self.assertTrue(out["subscribed"])
        self.assertFalse(youtube.get_queue()[0]["via_link"])         # an ordinary subscription

    def test_junk_link_is_refused(self):
        self.assertEqual(youtube.import_link("https://example.com/x")["status"], "bad-url")
        self.assertEqual(youtube._priority(), [])


class ChannelsMustActuallyTakeTurns(unittest.TestCase):
    """Seven channels were queued, "one video of each, looping" — and ~90% of everything
    upscaled came from one channel (live-caught 2026-08-21). all_pending() interleaved the
    columns correctly, but next_due() takes the HEAD of that list, and the head is always
    the first channel's next video: serve one, rebuild, serve the same channel again. The
    fair-looking list was never a rotation, because nothing advanced the head."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._rot = youtube.ROTATION_FILE
        youtube.ROTATION_FILE = os.path.join(self.tmp, "rot.json")   # never the live pointer

    def tearDown(self):
        youtube.ROTATION_FILE = self._rot
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pending(self, chans):
        """chans = {folder: n_videos}. Returns all_pending() against that fake queue."""
        q = [{"folder_name": c} for c in chans]
        per = {c: [{"vid": "%s%02d" % (c, i), "channel": c, "source_name": "%s-%d.mp4" % (c, i),
                    "nas_dir": "/s/" + c, "video_path": "/s/%s/%s-%d.mp4" % (c, c, i),
                    "title": "%s %d" % (c, i)} for i in range(n)]
               for c, n in chans.items()}
        with mock.patch.object(youtube, "get_queue", return_value=q), \
             mock.patch.object(youtube, "channel_pending",
                               side_effect=lambda e, skip=(): per[e["folder_name"]]), \
             mock.patch.object(youtube, "_import_pending", return_value=[]), \
             mock.patch.object(youtube, "_durations", return_value={}):
            return youtube.all_pending()

    def _next(self, chans):
        out = self._pending(chans)
        return out[0]["channel"] if out else None

    def test_serving_a_channel_moves_the_head_to_the_next_one(self):
        chans = {"a": 5, "b": 5, "c": 5}
        self.assertEqual(self._next(chans), "a")
        youtube.advance_rotation("a")
        self.assertEqual(self._next(chans), "b")
        youtube.advance_rotation("b")
        self.assertEqual(self._next(chans), "c")
        youtube.advance_rotation("c")
        self.assertEqual(self._next(chans), "a")      # loops

    def test_seven_channels_each_get_one_before_any_gets_two(self):
        chans = {c: 9 for c in "abcdefg"}
        served = []
        for _ in range(7):
            c = self._next(chans)
            served.append(c)
            youtube.advance_rotation(c)
        self.assertEqual(sorted(served), sorted("abcdefg"))   # each exactly once

    def test_an_exhausted_channel_is_skipped_not_stalled_on(self):
        chans = {"a": 0, "b": 3, "c": 3}
        youtube.advance_rotation("c")                  # next in line is 'a', which has nothing
        self.assertEqual(self._next(chans), "b")

    def test_an_unknown_pointer_falls_back_to_the_front(self):
        youtube.advance_rotation("gone")               # channel removed from the queue
        self.assertEqual(self._next({"a": 2, "b": 2}), "a")

    def test_the_pointer_survives_a_relaunch(self):
        youtube.advance_rotation("b")
        self.assertEqual(youtube.get_rotation(), "b")  # read back from disk, not memory

    def test_a_single_channel_is_unaffected(self):
        youtube.advance_rotation("a")
        self.assertEqual(self._next({"a": 3}), "a")

    def test_the_whole_list_still_interleaves(self):
        out = self._pending({"a": 2, "b": 2, "c": 2})
        self.assertEqual([v["channel"] for v in out], ["a", "b", "c", "a", "b", "c"])

    def test_advancing_with_no_channel_is_a_no_op(self):
        youtube.advance_rotation("b")
        youtube.advance_rotation(None)
        self.assertEqual(youtube.get_rotation(), "b")


class ImportsRememberWhichVideosTheyContained(unittest.TestCase):
    """The Plex sweep runs long after a video finishes, and mark_done() drops that video's
    priority entry as it completes — so the vid -> batch link only survives on the imports
    book, which is never pruned."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._p = youtube.IMPORTS_FILE
        youtube.IMPORTS_FILE = os.path.join(self.tmp, "imports.json")

    def tearDown(self):
        youtube.IMPORTS_FILE = self._p
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rows):
        with open(youtube.IMPORTS_FILE, "w") as f:
            json.dump(rows, f)

    def test_a_playlists_videos_map_to_its_title(self):
        self._write([{"id": "imp1", "kind": "playlist", "title": "Best Builds",
                      "vids": ["aaaaaaaaaaa", "bbbbbbbbbbb"]}])
        self.assertEqual(youtube.playlist_title_by_vid(),
                         {"aaaaaaaaaaa": "Best Builds", "bbbbbbbbbbb": "Best Builds"})

    def test_a_single_video_import_is_not_a_playlist(self):
        self._write([{"id": "imp2", "kind": "video", "title": "", "vids": ["ccccccccccc"]}])
        self.assertEqual(youtube.playlist_title_by_vid(), {})

    def test_an_untitled_playlist_is_skipped_rather_than_named_blank(self):
        self._write([{"id": "imp3", "kind": "playlist", "title": "  ", "vids": ["ddddddddddd"]}])
        self.assertEqual(youtube.playlist_title_by_vid(), {})

    def test_a_book_written_before_vids_existed_does_not_crash(self):
        self._write([{"id": "imp4", "kind": "playlist", "title": "Old One"}])
        self.assertEqual(youtube.playlist_title_by_vid(), {})

    def test_no_book_at_all_is_empty(self):
        self.assertEqual(youtube.playlist_title_by_vid(), {})

    def test_import_records_the_vids_it_queued(self):
        import inspect
        src = inspect.getsource(youtube)
        self.assertIn('"vids": queued_vids', src)
        self.assertIn("queued_vids.append(vid)", src)


class VideoCacheMissesNeverBlockThePoll(unittest.TestCase):
    """Same live-caught disease as series.cached_queue (2026-08-25): the miss path listed the
    channel's staging folder over FTP, and queue_view walks EVERY queued channel — so a cold
    process during a NAS outage hung /api/state on the first poll."""

    def setUp(self):
        youtube._VIDEO_CACHE.clear()
        with youtube._VIDEO_WARM_LOCK:
            youtube._VIDEO_WARMING.clear()

    def test_a_miss_is_instant_and_the_warm_lands_behind(self):
        import threading, time
        ev = threading.Event()
        def slow(folder):
            ev.wait(0.4)
            return [{"vid": "aaaaaaaaaaa"}]
        with mock.patch.object(youtube, "list_video_files", side_effect=slow):
            t0 = time.time()
            self.assertEqual(youtube.cached_videos("Chan"), [])
            self.assertLess(time.time() - t0, 0.05)
            ev.set(); time.sleep(0.25)
            self.assertEqual(youtube.cached_videos("Chan"), [{"vid": "aaaaaaaaaaa"}])

    def test_one_warmer_per_folder(self):
        import threading, time
        ev, calls = threading.Event(), []
        with mock.patch.object(youtube, "list_video_files",
                               side_effect=lambda f: (calls.append(f), ev.wait(0.3), [])[2]):
            for _ in range(4):
                youtube.cached_videos("Chan")
            ev.set(); time.sleep(0.2)
        self.assertEqual(len(calls), 1)

    def test_a_crashed_warm_releases_its_slot_and_caches_nothing(self):
        import time
        with mock.patch.object(youtube, "list_video_files", side_effect=OSError("nas gone")):
            self.assertEqual(youtube.cached_videos("Chan"), [])
            time.sleep(0.15)
        self.assertNotIn("Chan", youtube._VIDEO_CACHE)
        with youtube._VIDEO_WARM_LOCK:
            self.assertNotIn("Chan", youtube._VIDEO_WARMING)

    def test_a_warm_cache_is_served_synchronously(self):
        youtube._VIDEO_CACHE["Chan"] = [{"vid": "x"}]
        with mock.patch.object(youtube, "list_video_files",
                               side_effect=AssertionError("must not list")):
            self.assertEqual(youtube.cached_videos("Chan"), [{"vid": "x"}])


class SendToVisionaryCollections(unittest.TestCase):
    """The companion app gained playlist/channel Send buttons, capability-gated on the
    engine advertising them. Contract: the button POSTs once and re-POSTs on retry, so
    every path is idempotent; a collection send must NEVER flood — a playlist joins the
    ordinary cadence as an import batch (jump=False) and a channel becomes a queued
    channel, only the per-video button preempts. Statuses stay in the app's vocabulary."""

    PL = "https://www.youtube.com/playlist?list=PLabc123_-xyzABC456"
    IDS = ["aaaaaaaaaa%d" % i for i in range(1, 4)]

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)

    def _send(self, url, *, ids=None, meta=None, download_ok=True, title=None,
              channel=None, subs=None):
        import ytdata, youtarr
        with mock.patch.object(ytdata, "playlist_video_ids", return_value=ids), \
             mock.patch.object(ytdata, "playlist_meta", return_value=meta or {}), \
             mock.patch.object(ytdata, "channel_for", return_value=channel), \
             mock.patch.object(ytdata, "subscriptions", return_value=subs or []), \
             mock.patch.object(youtarr, "download_videos", return_value=download_ok):
            return youtube.send_to_visionary(url, title=title)

    # ---- capability advertisement ------------------------------------------
    def test_the_state_advertises_exactly_what_the_router_delivers(self):
        self.assertEqual(youtube.SEND_CAPABILITIES, ("video", "playlist", "channel"))
        with mock.patch.object(youtube, "_connected", return_value=True), \
             mock.patch.object(youtube, "imports_view", return_value=[]):
            view = youtube.queue_view()
        self.assertEqual(view["send_capabilities"], ["video", "playlist", "channel"])

    # ---- playlists -----------------------------------------------------------
    def test_a_playlist_queues_as_a_cadence_batch_never_a_jump(self):
        out = self._send(self.PL, ids=self.IDS, meta={"title": "Best Builds", "count": 3})
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["count"], 3)
        book = youtube._priority()
        self.assertEqual(len(book), 3)
        self.assertTrue(all(e.get("jump") is False for e in book))   # rides the cadence
        self.assertFalse(youtube.has_priority_ready())               # never preempts topaz

    def test_resending_the_same_playlist_is_already_queued(self):
        self._send(self.PL, ids=self.IDS, meta={"title": "Best Builds"})
        out = self._send(self.PL, ids=self.IDS, meta={"title": "Best Builds"})
        self.assertEqual(out["status"], "already-queued")
        self.assertEqual(len(youtube._priority()), 3)                # nothing duplicated

    def test_a_fully_upscaled_playlist_says_already_upscaled(self):
        youtube._save_done(set(self.IDS))
        out = self._send(self.PL, ids=self.IDS, meta={"title": "Best Builds"})
        self.assertEqual(out["status"], "already-upscaled")

    def test_private_and_session_lists_are_bad_url(self):
        for pid in ("WL", "LL", "LM", "RDaaaaaaaaaa1", "RDMM"):
            out = self._send("https://www.youtube.com/playlist?list=" + pid, ids=self.IDS)
            self.assertEqual(out["status"], "bad-url", pid)
        self.assertEqual(youtube._priority(), [])                    # nothing leaked in

    def test_albums_and_uploads_lists_are_public_enough(self):
        for n, pid in enumerate(("OLAK5uy_abcdefghij123456789012345678901",
                                 "UUabcdefghijklmnopqrstuv")):
            out = self._send("https://www.youtube.com/playlist?list=" + pid,
                             ids=["bbbbbbbbb%02d" % n], meta={"title": "X"})
            self.assertEqual(out["status"], "queued", pid)

    def test_an_unreadable_playlist_is_bad_url(self):
        # ids=None = the API could not list it: private or deleted
        out = self._send(self.PL, ids=None)
        self.assertEqual(out["status"], "bad-url")

    def test_youtarr_down_reports_and_strands_nothing(self):
        out = self._send(self.PL, ids=self.IDS, meta={"title": "X"}, download_ok=False)
        self.assertEqual(out["status"], "youtarr-unreachable")
        self.assertEqual(youtube._priority(), [])                    # batch rolled back

    def test_the_senders_title_names_an_untitled_batch(self):
        self._send(self.PL, ids=self.IDS, meta={}, title="From SmartTube")
        rows = youtube._imports()
        self.assertEqual(rows[0]["title"], "From SmartTube")

    # ---- channels ------------------------------------------------------------
    CH = {"channelId": "UC" + "a" * 22, "title": "Veritasium"}

    def test_a_channel_send_becomes_a_queued_channel(self):
        out = self._send("https://www.youtube.com/channel/" + self.CH["channelId"],
                         channel=self.CH)
        self.assertEqual(out["status"], "queued")
        q = youtube.get_queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["channelId"], self.CH["channelId"])
        self.assertTrue(q[0]["via_link"])            # not a subscription -> badged
        self.assertEqual(youtube._priority(), [])    # a channel is never a bulk enqueue

    def test_resending_the_channel_is_already_queued(self):
        url = "https://www.youtube.com/channel/" + self.CH["channelId"]
        self._send(url, channel=self.CH)
        out = self._send(url, channel=self.CH)
        self.assertEqual(out["status"], "already-queued")
        self.assertEqual(len(youtube.get_queue()), 1)

    def test_a_subscribed_channel_is_not_badged_via_link(self):
        self._send("https://www.youtube.com/channel/" + self.CH["channelId"],
                   channel=self.CH, subs=[self.CH])
        self.assertFalse(youtube.get_queue()[0]["via_link"])

    def test_a_malformed_channel_id_is_bad_url_before_any_api_call(self):
        out = self._send("https://www.youtube.com/channel/UCtooshort", channel=self.CH)
        self.assertEqual(out["status"], "bad-url")

    # ---- routing -------------------------------------------------------------
    def test_a_video_url_still_takes_the_jump_path(self):
        import youtarr
        with mock.patch.object(youtarr, "download_videos", return_value=True):
            out = youtube.send_to_visionary("https://youtu.be/cccccccccc1", title="V")
        self.assertEqual(out["status"], "queued")
        self.assertTrue(youtube.has_priority_ready is not None)
        book = youtube._priority()
        self.assertEqual(len(book), 1)
        self.assertNotIn("jump", book[0])            # the classic entry shape = preempts

    def test_garbage_is_bad_url(self):
        self.assertEqual(youtube.send_to_visionary("not a url")["status"], "bad-url")


class ImportedContentIsConfigurable(unittest.TestCase):
    """An imported playlist is its own configurable thing — the same normalize/output/pause
    levers a queued channel has (user-asked 2026-08-28). Its settings live under
    "import:<batch-id>" in the ordinary show-profiles store, and its VIDEOS resolve their
    stage-time settings through that key rather than whatever channel folder they land in."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)

    def _batch(self, bid="imp1", vids=("aaaaaaaaaa1", "aaaaaaaaaa2"), **extra):
        with youtube._IMPORTS_LOCK:
            rows = youtube._imports()
            rows.append({"id": bid, "kind": "playlist", "title": "Best Builds",
                         "vids": list(vids), **extra})
            youtube._save_imports(rows)
        with youtube._PRIORITY_LOCK:
            book = youtube._priority()
            for i, v in enumerate(vids):
                book.append({"vid": v, "jump": False, "seq": i, "batch": bid,
                             "channel": "Chan", "path": "/s/Chan/x/%s [%s].mp4" % (v, v)})
            youtube._save_priority(book)

    def test_a_batch_video_resolves_to_the_batch_key(self):
        self._batch()
        self.assertEqual(youtube.settings_scope_for_vid("aaaaaaaaaa2"), "import:imp1")
        self.assertIsNone(youtube.settings_scope_for_vid("bbbbbbbbbb1"))

    def test_the_view_carries_the_channel_grade_controls(self):
        self._batch()
        row = youtube.imports_view()[0]
        self.assertEqual(row["settings_key"], "import:imp1")
        for k in ("normalize_audio", "output_mode", "output_mode_effective", "paused"):
            self.assertIn(k, row)

    def test_pausing_a_batch_stops_serving_but_keeps_the_book(self):
        self._batch()
        self.assertEqual(sum(len(c) for c in youtube._import_pending()), 2)
        youtube.set_import_paused("imp1", True)
        self.assertEqual(youtube._import_pending(), [])            # not served
        self.assertEqual(len(youtube._priority()), 2)              # not dropped
        youtube.set_import_paused("imp1", False)
        self.assertEqual(sum(len(c) for c in youtube._import_pending()), 2)

    def test_pausing_an_unknown_batch_is_not_ok(self):
        self.assertEqual(youtube.set_import_paused("nope", True)["status"], "unknown-batch")

    def test_a_finished_batch_is_archived_never_deleted(self):
        # THE PLEX PIN: playlist_title_by_vid reads the imports book long after completion
        # (Plex creates items late; the sweep is delayed on purpose). imports_view used to
        # DELETE a finished batch at the state poll, so the last video of every playlist
        # lost its collection tag forever.
        self._batch()
        youtube._save_done({"aaaaaaaaaa1", "aaaaaaaaaa2"})         # everything upscaled
        self.assertEqual(youtube.imports_view(), [])               # out of the VIEW
        rows = youtube._imports()
        self.assertEqual(len(rows), 1)                             # still in the BOOK
        self.assertTrue(rows[0]["archived"])
        self.assertEqual(youtube.playlist_title_by_vid(),
                         {"aaaaaaaaaa1": "Best Builds", "aaaaaaaaaa2": "Best Builds"})

    def test_drop_still_deletes_outright(self):
        # the trash button means FORGET — that one should remove the row
        self._batch()
        youtube.drop_import("imp1")
        self.assertEqual(youtube._imports(), [])


class SingleVideoImportsNameThemselves(unittest.TestCase):
    """"Single video" told you nothing (user-asked 2026-08-28). The batch takes the sender's
    title when one came with the send, and an untitled batch fills in from the located
    file's own name — youtarr embeds the real title in the filename."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)

    def _single(self, title="", path=None, entry_title=None):
        with youtube._IMPORTS_LOCK:
            youtube._save_imports([{"id": "imp1", "kind": "video", "title": title,
                                    "vids": ["aaaaaaaaaa1"]}])
        with youtube._PRIORITY_LOCK:
            youtube._save_priority([{"vid": "aaaaaaaaaa1", "jump": False, "seq": 0,
                                     "batch": "imp1", "channel": "DIY Perks",
                                     "title": entry_title, "path": path}])

    def test_a_stored_title_is_shown_as_is(self):
        self._single(title="True Wireless Power")
        self.assertEqual(youtube.imports_view()[0]["title"], "True Wireless Power")

    def test_an_untitled_single_takes_the_located_files_name(self):
        self._single(path="/s/DIY Perks/x/DIY Perks - True Wireless Power [aaaaaaaaaa1].mp4")
        self.assertEqual(youtube.imports_view()[0]["title"], "True Wireless Power")

    def test_not_yet_located_stays_blank_rather_than_guessing(self):
        self._single(path=None)
        self.assertEqual(youtube.imports_view()[0]["title"], "")

    def test_import_link_stores_the_senders_title_for_a_single(self):
        import youtarr, ytdata
        with mock.patch.object(youtarr, "download_videos", return_value=True), \
             mock.patch.object(ytdata, "playlist_video_ids", return_value=None):
            out = youtube.import_link("https://youtu.be/bbbbbbbbbb1",
                                      title_hint="A Sent Video")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(youtube._imports()[0]["title"], "A Sent Video")


class PlaylistsAreNeverDownloadedAsPlaylists(unittest.TestCase):
    """A playlist reaches youtarr as INDIVIDUAL watch URLs, never as its list URL. Per-video
    downloads file each video under its own UPLOADER on staging; publishing mirrors that
    path; Plex channel collections read it back. So a compiler's playlist lands under
    PewDiePie/Paint/etc. and the playlist AUTHOR's name appears nowhere — verified live
    2026-08-28 on a compiler playlist whose staging folders were exactly the uploaders.
    The playlist itself still exists as its own Plex collection (playlist_title_by_vid);
    the author would only ever appear if their name is part of the playlist's title."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)

    def test_the_download_request_is_per_video_watch_urls(self):
        import youtarr, ytdata
        ids = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"]
        seen = {}
        with mock.patch.object(ytdata, "playlist_video_ids", return_value=ids), \
             mock.patch.object(ytdata, "playlist_meta", return_value={"title": "Comp"}), \
             mock.patch.object(youtarr, "download_videos",
                               side_effect=lambda v, **k: seen.update(got=list(v)) or True):
            out = youtube.import_link("https://www.youtube.com/playlist?list=PLabc123_-x")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(seen["got"], ids)                 # ids, one per video
        for v in seen["got"]:
            self.assertNotIn("list=", str(v))              # never the playlist URL

    def test_no_channel_is_ever_queued_for_a_playlist_send(self):
        # the compiler must not become a youtarr subscription or a queued channel
        import youtarr, ytdata
        with mock.patch.object(ytdata, "playlist_video_ids", return_value=["bbbbbbbbbb1"]), \
             mock.patch.object(ytdata, "playlist_meta", return_value={"title": "Comp"}), \
             mock.patch.object(youtarr, "download_videos", return_value=True):
            youtube.send_collection("https://www.youtube.com/playlist?list=PLabc123_-x")
        self.assertEqual(youtube.get_queue(), [])


class NonAsciiChannelFoldersResolve(unittest.TestCase):
    """The Kurzgesagt channel (an en dash in its name) reported 0 pending videos for eight
    days while 12 sat downloaded on staging (user-caught 2026-08-29). Two independent
    faults, both reachable only through a non-ASCII folder name:

    1. We never sent OPTS UTF8 ON (ftplib only sends it when ITS encoding is utf-8, and ours
       is deliberately latin-1), so smbftpd transcoded names to GB18030 on the wire --
       the en dash encodes to b'\\xa8C' in gbk -- and a lookup by the real spelling never
       matched anything the server would answer to.
    2. _channel_base built the base path in DISPLAY form, then concatenated names that came
       back from a listing in WIRE form. to_wire cannot latin-1 encode the mixed result, so
       it re-encoded the whole string as UTF-8 and DOUBLE-encoded the half that was already
       wire; every per-video listdir then answered "No such file or directory".
    """

    FOLDER = "Kurzgesagt – In a Nutshell"
    WIRE_DASH = "\u00e2\u0080\u0093"     # the en dash's UTF-8 bytes, seen through latin-1

    def test_the_base_path_is_wire_form(self):
        base = youtube._channel_base(self.FOLDER)
        base.encode("latin-1")                        # would raise if it were display form
        self.assertIn(self.WIRE_DASH, base)

    def test_wire_base_concatenated_with_a_wire_listing_stays_stable(self):
        # THE REGRESSION: to_wire must pass the joined path through untouched. A display-form
        # base made this double-encode, which is what produced "No such file or directory".
        base = youtube._channel_base(self.FOLDER)
        sub = "Kurzgesagt %s In a Nutshell - GERMANY IS OVER - n-gYFcVx-8Y" % self.WIRE_DASH
        joined = base + "/" + sub
        self.assertEqual(transfer.to_wire(joined), joined)
        self.assertEqual(joined.encode("latin-1").decode("utf-8"),
                         "/Media/YouTube-raw/%s/%s" % (
                             self.FOLDER, sub.encode("latin-1").decode("utf-8")))

    def test_an_ascii_folder_is_completely_unchanged(self):
        self.assertEqual(youtube._channel_base("DIY Perks"),
                         "/Media/YouTube-raw/DIY Perks")

    def test_the_walk_finds_videos_under_a_non_ascii_folder(self):
        base = youtube._channel_base(self.FOLDER)
        leaf = "Kurzgesagt %s In a Nutshell - X - Cyl3X88KEgg" % self.WIRE_DASH
        vdir = base + "/" + leaf
        listings = {base: [leaf], vdir: ["Kurzgesagt - X [Cyl3X88KEgg].mp4"]}
        with mock.patch.object(youtube, "ftp_connect", return_value=mock.MagicMock()), \
             mock.patch.object(youtube, "ftp_listdir",
                               side_effect=lambda f, d: listings.get(d, [])), \
             mock.patch.object(youtube, "remote_mtime", return_value=1):
            out = youtube.list_video_files(self.FOLDER)
        self.assertEqual([v["vid"] for v in out], ["Cyl3X88KEgg"])


class TheFtpSessionNegotiatesUtf8(unittest.TestCase):
    """Without OPTS UTF8 ON the NAS answers in its legacy codepage, so every non-ASCII
    filename on the wire is GB18030 while the disk is clean UTF-8 (verified live 2026-08-29:
    the same LIST returned the gbk bytes before and the utf-8 bytes after). ftplib will not
    send it for us -- it only does so when its own encoding is utf-8, and ours is
    deliberately latin-1 so stray bytes round-trip."""

    def _connect(self, sendcmd):
        ftp = mock.MagicMock()
        ftp.sendcmd = sendcmd
        with mock.patch.object(transfer, "_WireFTP", return_value=ftp), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["nas"]), \
             mock.patch.object(transfer, "ftp_settings",
                               return_value={"port": 21, "user": "u", "passwd": "p"}):
            return transfer.connect(timeout=1), ftp

    def test_it_asks_for_utf8_after_login(self):
        got, ftp = self._connect(mock.Mock(return_value="200 OK, UTF-8 enabled"))
        ftp.sendcmd.assert_called_once_with("OPTS UTF8 ON")
        self.assertEqual(ftp.encoding, "latin-1")     # byte round-trip is unchanged
        ftp.login.assert_called_once()

    def test_a_server_without_the_extension_still_connects(self):
        import ftplib as _f
        got, ftp = self._connect(mock.Mock(side_effect=_f.error_perm("500 unknown")))
        self.assertIs(got, ftp)                       # best-effort, never fatal
        ftp.set_pasv.assert_called_once_with(True)


class SendsGetLocatedWhileAnItemRuns(unittest.TestCase):
    """Locating used to happen only at selection, and the locate scan's staging-wide walk
    skipped QUEUED channels' folders on the assumption that the channel cache covered them
    — but that cache is itself only re-listed at selection. A send delivered to a queued
    channel mid-item was therefore invisible until the next item boundary, and the
    segment-boundary yield never once fired (user-caught 2026-09-05)."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)
        youtube._priority_scan_at = 0.0
        self.addCleanup(setattr, youtube, "_priority_scan_at", 0.0)

    def _send(self, vid="aaaaaaaaaa1"):
        with youtube._PRIORITY_LOCK:
            youtube._save_priority([{"vid": vid, "title": "Sent", "sent_at": 1}])

    def test_nothing_pending_means_no_scan_at_all(self):
        with mock.patch.object(youtube, "_locate_scan", side_effect=AssertionError("no scan")):
            self.assertFalse(youtube.locate_pending_priority())

    def test_an_unlocated_import_does_not_trigger_the_locator(self):
        with youtube._PRIORITY_LOCK:
            youtube._save_priority([{"vid": "aaaaaaaaaa1", "jump": False, "batch": "imp1"}])
        with mock.patch.object(youtube, "_locate_scan", side_effect=AssertionError("no scan")):
            self.assertFalse(youtube.locate_pending_priority())

    def test_a_send_in_a_queued_channels_folder_is_found_by_relisting(self):
        # THE BUG: cache empty (only re-listed at selection), staging walk skips queued
        # folders -> never located. Tier 1.5 re-lists the queued folder live.
        self._send()
        youtube.add_channel("UC" + "x" * 22, "Almost Friday TV")
        with youtube._QUEUE_LOCK if hasattr(youtube, "_QUEUE_LOCK") else mock.MagicMock():
            q = youtube.get_queue(); q[0]["folder_name"] = "Almost Friday TV"; youtube._save_queue(q)
        hit = [{"vid": "aaaaaaaaaa1", "name": "v [aaaaaaaaaa1].mp4",
                "path": "/Media/YouTube-raw/Almost Friday TV/x/v [aaaaaaaaaa1].mp4", "mtime": 1}]
        with mock.patch.object(youtube, "cached_videos", return_value=[]), \
             mock.patch.object(youtube, "refresh_videos", return_value=hit), \
             mock.patch.object(youtube, "ftp_connect", side_effect=OSError("no walk needed")):
            self.assertTrue(youtube.locate_pending_priority())
        e = youtube._priority()[0]
        self.assertEqual(e["channel"], "Almost Friday TV")
        self.assertTrue(e["path"].endswith("[aaaaaaaaaa1].mp4"))
        self.assertTrue(youtube.has_priority_ready())          # the boundary poll now sees it

    def test_a_second_look_inside_the_throttle_does_not_relist(self):
        self._send()
        youtube.add_channel("UC" + "x" * 22, "Chan")
        q = youtube.get_queue(); q[0]["folder_name"] = "Chan"; youtube._save_queue(q)
        with mock.patch.object(youtube, "cached_videos", return_value=[]), \
             mock.patch.object(youtube, "refresh_videos", return_value=[]) as rv, \
             mock.patch.object(youtube, "ftp_connect", side_effect=OSError("x")):
            youtube.locate_pending_priority()
            youtube.locate_pending_priority()
        self.assertEqual(rv.call_count, 1)                     # 45 s gap honoured


class ASendIsRecognisedAsASend(unittest.TestCase):
    """is_priority_video: book-only, so the download stage can refuse to yield to another
    send (a send yielding to a send ping-pongs aborted CFRs between them)."""

    def setUp(self):
        d = tempfile.mkdtemp()
        for name, fn in (("PRIORITY_FILE", "p.json"), ("IMPORTS_FILE", "i.json"),
                         ("DONE_FILE", "done.json"), ("QUEUE_FILE", "q.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start(); self.addCleanup(p.stop)

    def test_a_jump_entry_is_a_send(self):
        youtube._save_priority([{"vid": "aaaaaaaaaa1", "title": "S", "sent_at": 1}])
        self.assertTrue(youtube.is_priority_video("Chan - S [aaaaaaaaaa1].mp4"))

    def test_an_import_is_not(self):
        youtube._save_priority([{"vid": "aaaaaaaaaa1", "jump": False, "batch": "imp1"}])
        self.assertFalse(youtube.is_priority_video("Chan - S [aaaaaaaaaa1].mp4"))

    def test_unknown_or_unparseable_names_are_not(self):
        self.assertFalse(youtube.is_priority_video("Chan - S [bbbbbbbbbb1].mp4"))
        self.assertFalse(youtube.is_priority_video("no id here.mp4"))
        self.assertFalse(youtube.is_priority_video(None))


class DeleteVideo(unittest.TestCase):
    """"Skip & delete" on an imported or sent video did nothing: its up-next row carries the
    uploader's folder but no channelId (the uploader is not a queued channel), and
    delete_video needed one to find the file (user-caught 2026-09-13)."""

    VID = "p_gFpVf8C0w"
    NAME = "optimum - My endgame racing simulator [p_gFpVf8C0w].mp4"
    DIR = "/Media/YouTube-raw/optimum/optimum - My endgame racing simulator - p_gFpVf8C0w"

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name, fn in (("QUEUE_FILE", "q.json"), ("PRIORITY_FILE", "p.json"),
                         ("DONE_FILE", "d.json"), ("IMPORTS_FILE", "i.json")):
            p = mock.patch.object(youtube, name, os.path.join(d, fn))
            p.start()
            self.addCleanup(p.stop)
        self.deleted, self.ignored = [], []
        for target, fn in (("transfer.delete_tree", lambda x: self.deleted.append(x) or True),
                           ("youtarr.ignore_video", lambda c, v, **k: self.ignored.append((c, v)) or True),
                           ("youtarr.channel_folder", lambda c, **k: None)):
            p = mock.patch(target, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        # the trailing cache refresh must never touch the NAS in a test
        p = mock.patch.object(youtube, "refresh_videos", return_value=[])
        self.refresh = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(youtube._VIDEO_CACHE, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_an_imported_video_with_no_channel_is_deleted_from_its_book_path(self):
        youtube._save_priority([{"vid": self.VID, "channel": "optimum", "jump": False,
                                 "path": self.DIR + "/" + self.NAME}])
        self.assertTrue(youtube.delete_video(None, self.NAME, folder="optimum"))
        self.assertEqual(self.deleted, [self.DIR])
        self.assertEqual(self.ignored, [], "no subscribed channel — nothing to ignore in youtarr")
        self.assertIn(self.VID, youtube.get_done())
        self.assertFalse([e for e in youtube._priority() if e.get("vid") == self.VID],
                         "a deleted import leaves the book")

    def test_a_queued_channels_video_is_listed_live_when_the_cache_is_cold(self):
        youtube.add_channel("C1", "Chan")
        q = youtube.get_queue()
        q[0]["folder_name"] = "Chan"
        youtube._save_queue(q)
        listed = [{"vid": self.VID, "name": self.NAME, "dir": "/Media/YouTube-raw/Chan/x",
                   "path": "/Media/YouTube-raw/Chan/x/" + self.NAME, "mtime": 1}]
        self.refresh.side_effect = lambda folder: listed if folder == "Chan" else []
        self.refresh.return_value = None
        self.assertTrue(youtube.delete_video("C1", self.NAME))
        self.assertEqual(self.deleted, ["/Media/YouTube-raw/Chan/x"])
        self.assertEqual(self.ignored, [("C1", self.VID)])

    def test_a_video_nobody_can_find_is_still_retired(self):
        self.assertFalse(youtube.delete_video(None, self.NAME, folder="optimum"))
        self.assertEqual(self.deleted, [])
        self.assertIn(self.VID, youtube.get_done(), "never re-queued even when the file is gone")
