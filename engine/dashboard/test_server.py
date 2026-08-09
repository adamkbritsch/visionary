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


class PreviewVariants(unittest.TestCase):
    def test_big_and_small_are_served_from_one_capture(self):
        # The enlarged view gets the 1080p-class encode; the card keeps the 420w tile.
        # Both come from the same screenshot and share one freshness clock.
        import time as _t
        old = dict(server._PREVIEW)
        try:
            server._PREVIEW.update(jpg=b"small", big=b"large", at=_t.time(), busy=False)
            self.assertEqual(server._preview_frame(), b"small")
            self.assertEqual(server._preview_frame(big=True), b"large")
            server._PREVIEW["at"] = _t.time() - 999          # stale → neither is served
            self.assertIsNone(server._preview_frame(big=True))
        finally:
            server._PREVIEW.update(old)


class CompanionEndpoint(unittest.TestCase):
    """POST /api/companion action routing + the DV combine-only add rejection."""

    def test_search_routes_to_companion(self):
        from unittest import mock
        import companion
        with mock.patch.object(companion, "start_search",
                               return_value={"status": "searching"}) as ss:
            out = server.api_companion({"action": "search", "name": "m.mkv",
                                        "dir": "/Media/Movies", "title": "M (2020)"})
        self.assertEqual(out["status"], "searching")
        ss.assert_called_once_with("m.mkv", "/Media/Movies", "M (2020)")

    def test_pair_requires_a_path(self):
        out = server.api_companion({"action": "pair", "name": "m.mkv"})
        self.assertIn("error", out)

    def test_confirm_queues_the_combine_item(self):
        from unittest import mock
        import companion, movies
        with mock.patch.object(companion, "confirm",
                               return_value={"status": "confirmed"}), \
             mock.patch.object(companion, "entry",
                               return_value={"dir": "/Media/Movies/M", "title": "M (2020)"}), \
             mock.patch.object(movies, "add_selected") as add, \
             mock.patch.object(movies, "selected_view", return_value={"items": []}), \
             mock.patch.object(movies, "get_selected", return_value=[]):
            out = server.api_companion({"action": "confirm", "name": "m.mkv"})
        self.assertEqual(out["status"], "confirmed")
        add.assert_called_once_with("m.mkv", "/Media/Movies/M", "M (2020)", combine=True)

    def test_confirm_not_ready_does_not_queue(self):
        from unittest import mock
        import companion, movies
        with mock.patch.object(companion, "confirm",
                               return_value={"status": "error", "error": "not ready"}), \
             mock.patch.object(movies, "add_selected",
                               side_effect=AssertionError("must not queue")), \
             mock.patch.object(movies, "selected_view", return_value={"items": []}):
            out = server.api_companion({"action": "confirm", "name": "m.mkv"})
        self.assertEqual(out["status"], "error")

    def test_dv_movie_plain_add_is_rejected(self):
        from unittest import mock
        import movies
        with mock.patch.object(movies, "peek_library",
                               return_value=[{"name": "dv.mkv", "has_dv": True}]), \
             mock.patch.object(movies, "add_selected",
                               side_effect=AssertionError("DV must not plain-add")), \
             mock.patch.object(movies, "selected_view", return_value={"items": []}):
            out = server.api_movie_queue({"action": "add", "name": "dv.mkv",
                                          "dir": "/m", "title": "DV"})
        self.assertIn("error", out)
        self.assertIn("companion", out["error"])

    def test_non_dv_add_still_works(self):
        from unittest import mock
        import movies
        with mock.patch.object(movies, "peek_library",
                               return_value=[{"name": "plain.mkv", "has_dv": False}]), \
             mock.patch.object(movies, "add_selected") as add, \
             mock.patch.object(movies, "selected_view", return_value={"items": []}):
            server.api_movie_queue({"action": "add", "name": "plain.mkv",
                                    "dir": "/m", "title": "P"})
        add.assert_called_once()


class SetupEndpoints(unittest.TestCase):
    """The in-app onboarding surface: full preflight with fix strings, redacted config,
    connectivity tests, guarded Resolve import, dv_probe upload."""

    def test_preflight_payload_carries_fixes_and_summary(self):
        from unittest import mock
        import preflight
        fake = {"ok": False, "hard_ok": True,
                "checks": [{"id": "brew_tools", "ok": False, "severity": "fail",
                            "detail": "missing x265", "fix": "brew install x265"}]}
        server._FULL_PREFLIGHT["result"] = None
        with mock.patch.object(preflight, "run_checks", return_value=dict(fake)) as rc:
            out = server.api_preflight(fresh=True)
        rc.assert_called_once_with(in_app=True)
        self.assertFalse(out["setup_complete"])
        self.assertIn("brew_present", out)
        self.assertEqual(out["checks"][0]["fix"], "brew install x265")
        server._FULL_PREFLIGHT["result"] = None          # don't poison other tests

    def test_preflight_cache_serves_within_window(self):
        from unittest import mock
        import preflight
        server._FULL_PREFLIGHT["result"] = None
        with mock.patch.object(preflight, "run_checks",
                               return_value={"ok": True, "hard_ok": True, "checks": []}) as rc:
            server.api_preflight()
            server.api_preflight()                        # second call inside 5 s → cached
        self.assertEqual(rc.call_count, 1)
        server._FULL_PREFLIGHT["result"] = None

    def test_config_test_ftp_unconfigured_is_clean(self):
        from unittest import mock
        import transfer
        with mock.patch.object(transfer, "nas_hosts", return_value=[]):
            out = server.api_config_test("ftp")
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "not configured")

    def test_config_test_ftp_connects_and_quits(self):
        from unittest import mock
        import transfer
        ftp = mock.Mock()
        ftp.host = "nas.local"
        with mock.patch.object(transfer, "nas_hosts", return_value=["nas.local"]), \
             mock.patch.object(transfer, "connect", return_value=ftp):
            out = server.api_config_test("ftp")
        self.assertTrue(out["ok"])
        self.assertIn("nas.local", out["detail"])
        ftp.quit.assert_called_once()

    def test_config_test_failure_detail_has_no_secret(self):
        from unittest import mock
        import ftplib
        import transfer
        with mock.patch.object(transfer, "nas_hosts", return_value=["nas.local"]), \
             mock.patch.object(transfer, "connect",
                               side_effect=ftplib.error_perm("530 Login incorrect")):
            out = server.api_config_test("ftp")
        self.assertFalse(out["ok"])
        self.assertIn("530", out["detail"])

    def test_import_resolve_refuses_while_resolve_runs(self):
        from unittest import mock
        r = mock.Mock(returncode=0)                       # pgrep found Resolve
        with mock.patch.object(server.subprocess, "run", return_value=r), \
             mock.patch.object(server.os.path, "exists", return_value=True):
            out = server.api_import_resolve()
        self.assertEqual(out["error"], "resolve-running")
        self.assertIn("Quit DaVinci Resolve", out["detail"])

    def test_import_resolve_starts_the_bundled_script(self):
        from unittest import mock
        import setup_jobs
        r = mock.Mock(returncode=1)                       # pgrep: not running
        seen = {}
        def fake_start(what, argv=None):
            seen["what"], seen["argv"] = what, argv
            return {"what": what, "state": "running"}
        with mock.patch.object(server.subprocess, "run", return_value=r), \
             mock.patch.object(server.os.path, "exists", return_value=True), \
             mock.patch.object(setup_jobs, "start", side_effect=fake_start):
            out = server.api_import_resolve()
        self.assertEqual(out["state"], "running")
        self.assertEqual(seen["what"], "import_resolve")
        self.assertTrue(seen["argv"][1].endswith("setup/import_resolve.py"))
        self.assertIn("--json", seen["argv"])

    def test_dv_probe_upload_returns_the_cron_line(self):
        from unittest import mock
        import transfer
        ftp = mock.Mock()
        with mock.patch.object(server.os.path, "exists", return_value=True), \
             mock.patch.object(transfer, "connect", return_value=ftp), \
             mock.patch.object(transfer, "upload",
                               return_value=(True, "/Media/Config/dv_probe.py", "ok")):
            out = server.api_install_dv_probe()
        self.assertTrue(out["ok"])
        self.assertEqual(out["uploaded_to"], "/Media/Config/dv_probe.py")
        self.assertEqual(out["cron"],
                         "0 5 * * * /usr/bin/python3 /volume1/Media/Config/dv_probe.py all")
        self.assertTrue(out["optional"])

    def test_dv_probe_ftp_down_is_a_clean_failure(self):
        from unittest import mock
        import ftplib
        import transfer
        with mock.patch.object(server.os.path, "exists", return_value=True), \
             mock.patch.object(transfer, "connect",
                               side_effect=ftplib.error_perm("no NAS FTP host configured")):
            out = server.api_install_dv_probe()
        self.assertFalse(out["ok"])
        self.assertIn("no NAS FTP host", out["detail"])
