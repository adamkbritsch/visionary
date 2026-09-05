import time
import unittest
import urllib.error
from unittest import mock

import youtarr


class Client(unittest.TestCase):
    def test_no_creds_returns_none_without_network(self):
        with mock.patch.object(youtarr, "_creds", return_value=("", "")), \
             mock.patch.object(youtarr, "_post", side_effect=AssertionError("no network")):
            self.assertIsNone(youtarr.subscribed_channels())

    def test_login_then_getchannels_returns_uploader_names(self):
        youtarr._TOKEN["token"] = None
        with mock.patch.object(youtarr, "_creds", return_value=("u", "p")), \
             mock.patch.object(youtarr, "base_urls", return_value=["http://x:3087"]), \
             mock.patch.object(youtarr, "_post", return_value={"token": "TK"}) as lg, \
             mock.patch.object(youtarr, "_get",
                               return_value=[{"uploader": "Wizards with Guns"}, {"uploader": "al jokes"}]):
            names = youtarr.subscribed_channels()
        self.assertEqual(names, ["Wizards with Guns", "al jokes"])   # sorted uploaders
        lg.assert_called_once()                                      # logged in once

    def test_dict_response_and_dedup(self):
        youtarr._TOKEN["token"] = "cached"
        with mock.patch.object(youtarr, "_creds", return_value=("u", "p")), \
             mock.patch.object(youtarr, "base_urls", return_value=["http://x:3087"]), \
             mock.patch.object(youtarr, "_get",
                               return_value={"channels": [{"uploader": "A"}, {"uploader": "A"}, {"title": "B"}]}):
            self.assertEqual(youtarr.subscribed_channels(), ["A", "B"])


class Forget(unittest.TestCase):
    class FakeFTP:
        def __init__(self, content): self.content = content.encode(); self.stored = None
        def size(self, path): return len(self.content)
        def retrbinary(self, cmd, cb): cb(self.content)
        def storbinary(self, cmd, fp): self.stored = fp.read()
        def delete(self, path): pass
        def rename(self, a, b): pass
        def quit(self): pass

    def test_strips_only_the_given_ids(self):
        archive = "youtube aaaaaaaaaa1\nyoutube bbbbbbbbbb2\nyoutube ccccccccc33\n"
        f = self.FakeFTP(archive)
        with mock.patch("transfer.connect", return_value=f):
            removed = youtarr.forget_downloads(["aaaaaaaaaa1", "ccccccccc33", "zzzznotthere"])
        self.assertEqual(removed, 2)
        self.assertEqual(f.stored.decode(), "youtube bbbbbbbbbb2\n")   # only the untouched line remains

    def test_no_ids_never_touches_the_archive(self):
        with mock.patch("transfer.connect", side_effect=AssertionError("should not connect")):
            self.assertEqual(youtarr.forget_downloads([]), 0)

    def test_channel_video_ids_parses_both_key_shapes(self):
        with mock.patch.object(youtarr, "_call",
                               return_value={"videos": [{"youtube_id": "aaaaaaaaaa1"},
                                                        {"youtubeId": "bbbbbbbbbb2"}, {"nope": 1}]}):
            self.assertEqual(youtarr.channel_video_ids("UCx"), ["aaaaaaaaaa1", "bbbbbbbbbb2"])


class ArchivePath(unittest.TestCase):
    def test_defaults_to_the_ugreen_docker_layout(self):
        with mock.patch.dict("os.environ", {"TOPAZ_YOUTARR_ARCHIVE": ""}), \
             mock.patch.object(youtarr, "_config", return_value={}):
            self.assertEqual(youtarr.archive_ftp_path(), youtarr.ARCHIVE_FTP_DEFAULT)

    def test_env_var_overrides_the_path(self):
        with mock.patch.dict("os.environ", {"TOPAZ_YOUTARR_ARCHIVE": "/volume1/docker/youtarr/config/complete.list"}), \
             mock.patch.object(youtarr, "_config", return_value={}):
            self.assertEqual(youtarr.archive_ftp_path(), "/volume1/docker/youtarr/config/complete.list")

    def test_config_key_overrides_the_path(self):
        with mock.patch.dict("os.environ", {"TOPAZ_YOUTARR_ARCHIVE": ""}), \
             mock.patch.object(youtarr, "_config",
                               return_value={"youtarr_archive": "/appdata/youtarr/config/complete.list"}):
            self.assertEqual(youtarr.archive_ftp_path(), "/appdata/youtarr/config/complete.list")


if __name__ == "__main__":
    unittest.main()


class ChannelListingIsPaged(unittest.TestCase):
    """/getchannelvideos returns 50 at a time and reports the real size in totalCount. Taking
    only the first page truncated every channel to its 50 newest videos — and this list is the
    CANDIDATE POOL fetch-ahead asks youtarr to download, so the older tail could never be
    upscaled (user-caught 2026-09-05: Wizards with Guns read as finished at 50 of 56, with 5
    videos never processed; 10 across all channels)."""

    def _pages(self, *pages, total=None, streams=()):
        """Serve `pages` in order for the VIDEOS tab, honouring ?page= and ?tabType= like
        youtarr does; the streams tab serves `streams` as one page; any other tab is empty."""
        calls = []

        def fake(method, path, **kw):
            calls.append(path)
            tab = path.split("tabType=")[1].split("&")[0] if "tabType=" in path else "videos"
            n = int(path.split("page=")[1].split("&")[0]) if "page=" in path else 1
            if tab == "streams":
                body = list(streams) if n == 1 else []
                return {"videos": [{"youtube_id": v} for v in body], "totalCount": len(streams)}
            if tab != "videos":
                return {"videos": [], "totalCount": 0}
            body = list(pages[n - 1]) if 0 < n <= len(pages) else []
            return {"videos": [{"youtube_id": v} for v in body],
                    "totalCount": total if total is not None else sum(len(p) for p in pages)}
        return fake, calls

    def test_it_collects_every_page(self):
        big = ["v%03d" % i for i in range(50)]
        fake, calls = self._pages(big, ["tail1", "tail2"])
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx", page_size=50, tabs=("videos",))
        self.assertEqual(len(got), 52)
        self.assertEqual(got[-2:], ["tail1", "tail2"])
        self.assertEqual(len(calls), 2)

    def test_a_short_first_page_asks_for_nothing_more(self):
        fake, calls = self._pages(["a", "b"])
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            self.assertEqual(youtarr.channel_video_ids("UCx", page_size=50, tabs=("videos",)), ["a", "b"])
        self.assertEqual(len(calls), 1)

    def test_it_stops_at_totalcount_even_if_pages_keep_coming(self):
        full = ["v%03d" % i for i in range(50)]
        fake, calls = self._pages(full, full, total=50)
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx", page_size=50, tabs=("videos",))
        self.assertEqual(len(got), 50)
        self.assertEqual(len(calls), 1)

    def test_a_server_that_ignores_page_cannot_spin_forever(self):
        # every page identical: no NEW ids on page 2 -> stop rather than loop to max_pages
        full = ["v%03d" % i for i in range(50)]
        fake, calls = self._pages(full, full, full, full, total=999)
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx", page_size=50, tabs=("videos",))
        self.assertEqual(len(got), 50)
        self.assertEqual(len(calls), 2)

    def test_max_pages_bounds_the_walk(self):
        page = ["p%d-%d" % (0, i) for i in range(50)]

        def fake(method, path, **kw):
            n = int(path.split("page=")[1].split("&")[0])
            return {"videos": [{"youtube_id": "p%d-%d" % (n, i)} for i in range(50)],
                    "totalCount": 10 ** 6}
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx", max_pages=3, page_size=50, tabs=("videos",))
        self.assertEqual(len(got), 150)

    def test_an_empty_page_ends_it(self):
        fake, calls = self._pages(["v%03d" % i for i in range(50)], [], total=999)
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            self.assertEqual(len(youtarr.channel_video_ids("UCx", page_size=50, tabs=("videos",))), 50)

    def test_no_channel_id_calls_nothing(self):
        with mock.patch.object(youtarr, "_call", side_effect=AssertionError("must not call")):
            self.assertEqual(youtarr.channel_video_ids(""), [])

    def test_a_failed_call_is_empty_not_a_crash(self):
        with mock.patch.object(youtarr, "_call", return_value=None):
            self.assertEqual(youtarr.channel_video_ids("UCx"), [])


    # ---- tabs: videos + streams are the programme, shorts never are -------------------
    def test_streams_are_counted_after_the_videos_tab(self):
        fake, calls = self._pages(["v1", "v2"], streams=["live1"])
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx")
        self.assertEqual(got, ["v1", "v2", "live1"])
        self.assertTrue(any("tabType=videos" in c for c in calls))
        self.assertTrue(any("tabType=streams" in c for c in calls))

    def test_shorts_are_never_requested(self):
        # user-dictated 2026-09-05: "don't count shorts"
        fake, calls = self._pages(["v1"], streams=["live1"])
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            youtarr.channel_video_ids("UCx")
        self.assertFalse([c for c in calls if "tabType=shorts" in c])
        self.assertEqual(youtarr.CHANNEL_TABS, ("videos", "streams"))

    def test_each_tab_pages_against_its_own_total(self):
        # 100 videos over two pages, then one short streams page: totals are per tab, so
        # the streams page must still be asked for after the videos tab reaches ITS total
        page = ["v%03d" % i for i in range(50)]
        fake, calls = self._pages(page, ["w%03d" % i for i in range(50)], streams=["live1"])
        with mock.patch.object(youtarr, "_call", side_effect=fake):
            got = youtarr.channel_video_ids("UCx", page_size=50)
        self.assertEqual(len(got), 101)
        self.assertEqual(got[-1], "live1")


class LoginRateLimitBacksOff(unittest.TestCase):
    """youtarr rate-limits /auth/login (5 per 15 min per IP) and answers 429. That was
    swallowed like any error, so every caller read "no data" with nothing said, and each
    later call with an empty token cache tried to log in AGAIN (live-caught 2026-09-05:
    a burst of fresh processes turned one 429 into a storm and every channel looked
    empty). A 429 arms a back-off; a real login clears it."""

    def setUp(self):
        youtarr._LOGIN_BLOCKED_UNTIL[0] = 0.0
        youtarr._TOKEN["token"] = None
        self.addCleanup(lambda: youtarr._LOGIN_BLOCKED_UNTIL.__setitem__(0, 0.0))
        self.addCleanup(lambda: youtarr._TOKEN.__setitem__("token", None))
        p = mock.patch.object(youtarr, "_creds", return_value=("u", "p"))
        p.start(); self.addCleanup(p.stop)

    @staticmethod
    def _http(code, retry_after=None):
        import io, email.message
        hdrs = email.message.Message()
        if retry_after is not None:
            hdrs["Retry-After"] = str(retry_after)
        return urllib.error.HTTPError("http://x/auth/login", code, "x", hdrs, io.BytesIO(b"{}"))

    def test_a_429_arms_the_backoff_and_returns_none(self):
        with mock.patch.object(youtarr, "_post", side_effect=self._http(429)):
            self.assertIsNone(youtarr._login("http://x"))
        self.assertGreater(youtarr._LOGIN_BLOCKED_UNTIL[0], time.time() + 200)

    def test_retry_after_wins_over_the_default(self):
        with mock.patch.object(youtarr, "_post", side_effect=self._http(429, retry_after=42)):
            youtarr._login("http://x")
        self.assertAlmostEqual(youtarr._LOGIN_BLOCKED_UNTIL[0] - time.time(), 42, delta=3)

    def test_while_backing_off_no_login_is_even_attempted(self):
        youtarr._LOGIN_BLOCKED_UNTIL[0] = time.time() + 60
        with mock.patch.object(youtarr, "_post", side_effect=AssertionError("must not hit login")):
            self.assertIsNone(youtarr._login("http://x"))
            # _call fails fast too, rather than digging the hole deeper
            with mock.patch.object(youtarr, "base_urls", return_value=["http://x"]):
                self.assertIsNone(youtarr._call("GET", "/anything"))

    def test_a_successful_login_clears_the_backoff(self):
        youtarr._LOGIN_BLOCKED_UNTIL[0] = time.time() - 1          # lapsed
        with mock.patch.object(youtarr, "_post", return_value={"token": "T"}):
            self.assertEqual(youtarr._login("http://x"), "T")
        self.assertEqual(youtarr._LOGIN_BLOCKED_UNTIL[0], 0.0)
        self.assertEqual(youtarr._TOKEN["token"], "T")

    def test_other_http_errors_do_not_arm_it(self):
        with mock.patch.object(youtarr, "_post", side_effect=self._http(500)):
            self.assertIsNone(youtarr._login("http://x"))
        self.assertEqual(youtarr._LOGIN_BLOCKED_UNTIL[0], 0.0)

    def test_it_logs_once_when_arming(self):
        import logbook
        with mock.patch.object(youtarr, "_post", side_effect=self._http(429)), \
             mock.patch.object(logbook, "event") as ev:
            youtarr._login("http://x")
        ev.assert_called_once()
        self.assertIn("rate-limited", ev.call_args[0][0])
