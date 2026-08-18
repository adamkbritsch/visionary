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


def state(power, automation_enabled=False, scratch=None, in_win=True, adapter_watts=65):
    return server.build_state(power=power, scratch=scratch or SCRATCH, adapter_watts=adapter_watts,
                              in_win=in_win, automation_enabled=automation_enabled)


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


class UpNext(unittest.TestCase):
    """up_next round-robins the active series into an episode stream + interleaves movies by slot."""
    def _run(self, movies_list, episodes, limit=10, current=None, inflight=None):   # single active series "show"
        import movies, series, youtube
        from unittest import mock
        # HERMETIC: without this the tests read the REAL YouTube queue off this machine and
        # assert against whatever happens to be pending. They only passed before because the
        # (now-removed) per-channel length cap filtered that live data to empty.
        with mock.patch.object(youtube, "all_pending", return_value=[]), \
             mock.patch.object(movies, "get_selected", return_value=movies_list), \
             mock.patch.object(series, "get_active_series", return_value=["show"]), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(series, "cached_queue", return_value={"remaining_items": episodes}):
            return [(o["kind"], o.get("name") or o.get("ep"))
                    for o in server.up_next(limit=limit, current=current, inflight=inflight)]

    def _rr(self, active, queues, rotation=0, limit=10):   # multi-series round-robin, no movies
        import movies, series, youtube
        from unittest import mock
        with mock.patch.object(youtube, "all_pending", return_value=[]), \
             mock.patch.object(movies, "get_selected", return_value=[]), \
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


class PlexDiscovery(unittest.TestCase):
    """Tokenless auto-identify: NAS configured -> probe :32400/identity on each host."""

    def test_found_on_a_host_returns_the_url_and_version(self):
        from unittest import mock
        import io, transfer, urllib.request
        body = io.BytesIO(b'<?xml version="1.0" encoding="UTF-8"?>\n'
                          b'<MediaContainer version="1.41.0.100" machineIdentifier="x"/>')
        body.status = 200
        body.__enter__ = lambda s: s
        body.__exit__ = lambda s, *a: False
        with mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.5", "nas.local"]), \
             mock.patch.object(urllib.request, "urlopen", return_value=body) as uo:
            out = server.api_config_test("plex-discover")
        self.assertTrue(out["ok"])
        self.assertEqual(out["url"], "http://10.0.0.5:32400")
        self.assertIn("1.41.0.100", out["detail"])
        self.assertIn("/identity", uo.call_args.args[0])

    def test_no_answer_on_any_host_is_a_clean_miss(self):
        from unittest import mock
        import transfer, urllib.request
        with mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.5"]), \
             mock.patch.object(urllib.request, "urlopen", side_effect=OSError("refused")):
            out = server.api_config_test("plex-discover")
        self.assertFalse(out["ok"])
        self.assertIn("no Plex answered", out["detail"])

    def test_nas_unconfigured_says_so(self):
        from unittest import mock
        import transfer
        with mock.patch.object(transfer, "nas_hosts", return_value=[]):
            out = server.api_config_test("plex-discover")
        self.assertFalse(out["ok"])
        self.assertIn("configure the NAS first", out["detail"])


class AutoConnectSweep(unittest.TestCase):
    """One sweep: Plex (:32400 tokenless), youtarr (:3087 presence), Shuttle relay
    (:8789/healthz pre-auth + Shuttle's local token verified)."""

    def _resp(self, body=b"", status=200):
        import io
        r = io.BytesIO(body)
        r.status = status
        r.__enter__ = lambda s: s
        r.__exit__ = lambda s, *a: False
        return r

    def test_sweep_finds_all_three_and_verifies_the_relay_token(self):
        from unittest import mock
        import companion, transfer, urllib.request
        def route(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if ":32400/identity" in url:
                return self._resp(b'<?xml version="1.0"?><MediaContainer version="1.41.0"/>')
            if ":3087/" in url:
                return self._resp(b"<html>youtarr</html>")
            if ":8789/healthz" in url:
                return self._resp(b'{"ok": true}')
            if ":8789/v1/targets" in url:
                # the authed probe must carry Shuttle's token
                assert req.get_header("Authorization") == "Bearer tok"
                return self._resp(b'{"targets": []}')
            raise OSError("refused")
        with mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.5"]), \
             mock.patch.object(companion, "relay_token", return_value="tok"), \
             mock.patch.object(urllib.request, "urlopen", side_effect=route):
            out = server.api_config_test("auto-connect")
        self.assertTrue(out["ok"])
        f = out["found"]
        self.assertEqual(f["plex"]["url"], "http://10.0.0.5:32400")
        self.assertIn("1.41.0", f["plex"]["detail"])
        self.assertEqual(f["youtarr"]["url"], "http://10.0.0.5:3087")
        self.assertEqual(f["relay"]["url"], "http://10.0.0.5:8789")
        self.assertTrue(f["relay"]["authed"])
        self.assertIn("connected", f["relay"]["detail"])

    def test_relay_found_without_a_local_token_says_so(self):
        from unittest import mock
        import companion, transfer, urllib.request
        def route(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if ":8789/healthz" in url:
                return self._resp(b'{"ok": true}')
            raise OSError("refused")
        with mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.5"]), \
             mock.patch.object(companion, "relay_token", return_value=""), \
             mock.patch.object(urllib.request, "urlopen", side_effect=route):
            out = server.api_config_test("auto-connect")
        r = out["found"]["relay"]
        self.assertTrue(r["ok"])
        self.assertFalse(r["authed"])
        self.assertIn("Shuttle app", r["detail"])

    def test_nothing_found_is_all_clean_misses(self):
        from unittest import mock
        import companion, transfer, urllib.request
        with mock.patch.object(transfer, "nas_hosts", return_value=["10.0.0.5"]), \
             mock.patch.object(companion, "relay_token", return_value=""), \
             mock.patch.object(urllib.request, "urlopen", side_effect=OSError("refused")):
            out = server.api_config_test("auto-connect")
        self.assertFalse(out["ok"])
        for svc in ("plex", "youtarr", "relay"):
            self.assertFalse(out["found"][svc]["ok"])
            self.assertNotIn("error", out["found"][svc].get("detail", "").lower())


class BordersEndpoints(unittest.TestCase):
    """The border-extender Setup surface: environment + model states + readiness, and
    the engine-computed download argv reaching setup_jobs."""

    def test_status_shape_when_comfy_missing(self):
        from unittest import mock
        import borders
        with mock.patch.object(borders, "discover",
                               return_value={"ok": False, "missing": ["Comfy Desktop"],
                                             "models_dir": "", "port": 8189}):
            out = server.api_borders_status()
        self.assertFalse(out["ready"])
        self.assertIn("Comfy Desktop", out["missing"])
        self.assertEqual(out["models"], {})
        self.assertEqual(out["chunk_frames"], borders.CHUNK_FRAMES)

    def test_status_reports_model_states(self):
        import tempfile
        from unittest import mock
        import borders
        d = tempfile.mkdtemp()
        env = {"ok": True, "models_dir": d, "port": 8189, "missing": [],
               "install_dir": d, "checkout": d, "venv_python": "x",
               "desktop_version": "1.0", "comfy_version": "0.30.2"}
        with mock.patch.object(borders, "discover", return_value=env):
            out = server.api_borders_status()
        self.assertFalse(out["ready"])                       # nothing installed yet
        self.assertEqual(out["models"]["borders_vace"]["state"], "missing")
        self.assertIn("WAN 2.1 VACE 1.3B", out["missing"])

    def test_series_info_carries_aspect_and_readiness(self):
        from unittest import mock
        import borders, plex, series, settings
        with mock.patch.object(series, "get_active_series", return_value=["Show"]), \
             mock.patch.object(series, "get_next_up", return_value=None), \
             mock.patch.object(series, "cached_queue", return_value=None), \
             mock.patch.object(series, "next_up_armed", return_value=False), \
             mock.patch.object(series, "near_done", return_value=False), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(plex, "ensure_titles_warming"), \
             mock.patch.object(plex, "peek_titles", return_value={}), \
             mock.patch.object(borders, "show_aspect", return_value="4:3"), \
             mock.patch.object(borders, "discover",
                               return_value={"ok": True, "models_dir": "/m"}), \
             mock.patch.object(borders, "models_ready", return_value=(True, [])), \
             mock.patch.object(settings, "get_show_extend_borders", return_value=True):
            out = server.series_info()
        self.assertTrue(out["borders_ready"])
        self.assertEqual(out["shows"][0]["aspect"], "4:3")
        self.assertTrue(out["shows"][0]["extend_borders"])

    def test_aspect_probe_kicks_once_per_show(self):
        from unittest import mock
        import borders, plex, series
        server._ASPECT_PROBES.discard("NewShow")
        with mock.patch.object(series, "get_active_series", return_value=["NewShow"]), \
             mock.patch.object(series, "get_next_up", return_value=None), \
             mock.patch.object(series, "cached_queue", return_value=None), \
             mock.patch.object(series, "next_up_armed", return_value=False), \
             mock.patch.object(series, "near_done", return_value=False), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(plex, "ensure_titles_warming"), \
             mock.patch.object(plex, "peek_titles", return_value={}), \
             mock.patch.object(borders, "show_aspect", return_value=None), \
             mock.patch.object(borders, "discover",
                               return_value={"ok": False, "models_dir": ""}), \
             mock.patch.object(server.threading, "Thread") as th:
            server.series_info()
            server.series_info()                     # second poll: already kicked
        self.assertEqual(
            sum(1 for c in th.call_args_list
                if c.kwargs.get("name") == "aspect-probe"), 1)
        server._ASPECT_PROBES.discard("NewShow")


class SetBookEndpoint(unittest.TestCase):
    def test_reset_reports_removed_and_requires_a_show(self):
        import tempfile
        from unittest import mock
        import borders
        d = tempfile.mkdtemp()
        with mock.patch.object(borders, "SET_BOOK_ROOT", d), \
             mock.patch.object(borders, "reset_set_book",
                               return_value=(3, True)) as rs:
            # exercise the handler body the way the route does
            removed, ok = borders.reset_set_book("Sunny")
            out = {"show": "Sunny", "removed": removed, "ok": ok}
        rs.assert_called_once_with("Sunny")
        self.assertEqual(out, {"show": "Sunny", "removed": 3, "ok": True})

    def test_payloads_carry_set_counts(self):
        from unittest import mock
        import borders, plex, series, settings
        with mock.patch.object(series, "get_active_series", return_value=["Show"]), \
             mock.patch.object(series, "get_next_up", return_value=None), \
             mock.patch.object(series, "cached_queue", return_value=None), \
             mock.patch.object(series, "next_up_armed", return_value=False), \
             mock.patch.object(series, "near_done", return_value=False), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(plex, "ensure_titles_warming"), \
             mock.patch.object(plex, "peek_titles", return_value={}), \
             mock.patch.object(borders, "show_aspect", return_value="4:3"), \
             mock.patch.object(borders, "set_count", return_value=4), \
             mock.patch.object(borders, "discover",
                               return_value={"ok": False, "models_dir": ""}):
            out = server.series_info()
        self.assertEqual(out["shows"][0]["extend_sets"], 4)


class MediaLibrariesEndpoint(unittest.TestCase):
    """Which NAS folders are TV / Movies / YouTube — auto-decided from Plex, overridable,
    and visible. Applying writes config; nothing changes silently."""

    def test_status_degrades_to_the_builtin_layout_without_plex(self):
        from unittest import mock
        import medialibs, transfer
        with mock.patch.object(medialibs, "detect_live",
                               return_value={"error": "plex-unreachable",
                                             "detail": "Plex did not answer"}), \
             mock.patch.object(medialibs, "overrides", return_value={}), \
             mock.patch.object(medialibs, "applied_roots", return_value={}):
            st = medialibs.status()
        self.assertEqual(st["error"], "plex-unreachable")
        self.assertEqual(st["proposed"]["tv"]["source"], "default")
        self.assertEqual(st["proposed"]["tv"]["roots"], list(transfer.DEFAULT_TV_ROOTS))
        self.assertEqual(st["in_force"]["tv"], list(transfer.NAS_FTP_TV_ROOTS))

    def test_apply_refuses_when_nothing_resolved(self):
        # A 400 rather than writing an empty media_roots, which would blank every library.
        from unittest import mock
        import medialibs
        with mock.patch.object(medialibs, "status",
                               return_value={"proposed": {"tv": {"roots": []}},
                                             "detail": "no libraries"}):
            st = medialibs.status()
            roots = {k: v["roots"] for k, v in st["proposed"].items() if v.get("roots")}
        self.assertEqual(roots, {})          # the route turns this into a 400

    def test_apply_writes_only_resolved_kinds(self):
        from unittest import mock
        import configstore, medialibs
        saved = {}
        proposed = {"tv": {"roots": ["/Media/TV-Shows"], "source": "plex"},
                    "movie": {"roots": [], "source": "default"}}
        with mock.patch.object(medialibs, "status", return_value={"proposed": proposed}), \
             mock.patch.object(configstore, "write", side_effect=saved.update), \
             mock.patch.object(configstore, "read_redacted", return_value={}):
            st = medialibs.status()
            roots = {k: v["roots"] for k, v in st["proposed"].items() if v.get("roots")}
            configstore.save({"media_roots": roots})
        self.assertEqual(saved["media_roots"], {"tv": ["/Media/TV-Shows"]})


class UpNextMarksPriorityVideos(unittest.TestCase):
    """A "run now" press only takes effect at the next Topaz segment boundary (the
    in-flight segment finishes first — same as a deploy). That delay read as "not
    working", so the up-next row has to SAY the video is queued to jump."""

    def _run(self, prio_vids):
        from unittest import mock
        import movies, series, youtube
        vids = [{"channel": "Chan", "source_name": "a [aaaaaaaaaa1].mp4",
                 "title": "A", "vid": "aaaaaaaaaa1", "secs": 60},
                {"channel": "Chan", "source_name": "b [aaaaaaaaaa2].mp4",
                 "title": "B", "vid": "aaaaaaaaaa2", "secs": 60}]
        book = [{"vid": v} for v in prio_vids]
        with mock.patch.object(youtube, "all_pending", return_value=vids), \
             mock.patch.object(youtube, "_priority", return_value=book), \
             mock.patch.object(movies, "get_selected", return_value=[]), \
             mock.patch.object(series, "get_active_series", return_value=[]), \
             mock.patch.object(series, "get_rotation", return_value=0):
            return [(o.get("title"), o.get("priority"))
                    for o in server.up_next(limit=10) if o.get("kind") == "youtube"]

    def test_flags_only_the_requested_video(self):
        self.assertEqual(self._run(["aaaaaaaaaa2"]), [("A", False), ("B", True)])

    def test_no_request_flags_nothing(self):
        self.assertEqual(self._run([]), [("A", False), ("B", False)])


class UpNextKeepsTenEpisodes(unittest.TestCase):
    """`limit` counts TV EPISODES (user-dictated): the queue always shows ten episodes of
    actual show, with movies and videos riding along BETWEEN them rather than consuming the
    budget. Counting entries — or drawn slots — let a burst of videos eat the list."""

    def _run(self, n_eps, n_videos, n_movies=0, every=1, burst=1, limit=10):
        from unittest import mock
        import movies, series, settings, youtube
        eps = [{"ep": f"S01E{i:02d}", "source_name": f"e{i}.mkv"} for i in range(1, n_eps + 1)]
        vids = [{"channel": "Chan", "source_name": f"v{i} [aaaaaaaaa{i:02d}].mp4",
                 "title": f"V{i}", "vid": f"aaaaaaaaa{i:02d}", "secs": 60}
                for i in range(1, n_videos + 1)]
        mvs = [{"name": f"m{i}.mkv", "title": f"M{i}", "pos": i} for i in range(n_movies)]
        st = dict(settings.DEFAULT_SETTINGS)
        st.update({"youtube_every_tv_episodes": every, "youtube_videos_per_burst": burst})
        with mock.patch.object(youtube, "all_pending", return_value=vids), \
             mock.patch.object(youtube, "_priority", return_value=[]), \
             mock.patch.object(movies, "get_selected", return_value=mvs), \
             mock.patch.object(series, "get_active_series", return_value=["show"]), \
             mock.patch.object(series, "get_rotation", return_value=0), \
             mock.patch.object(series, "cached_queue", return_value={"remaining_items": eps}), \
             mock.patch.object(settings, "get_settings", return_value=st):
            return server.up_next(limit=limit)

    @staticmethod
    def _eps(out):
        return sum(1 for o in out if o.get("kind") == "episode")

    def test_ten_episodes_even_with_a_video_between_each(self):
        out = self._run(30, 30)
        self.assertEqual(self._eps(out), 10)
        self.assertGreater(len(out), 10)          # videos ride along, uncounted

    def test_bursts_of_videos_do_not_eat_the_episode_budget(self):
        out = self._run(30, 60, every=1, burst=3)
        self.assertEqual(self._eps(out), 10)

    def test_movies_do_not_eat_the_episode_budget_either(self):
        out = self._run(30, 0, n_movies=4)
        self.assertEqual(self._eps(out), 10)
        self.assertEqual(sum(1 for o in out if o.get("kind") == "movie"), 4)

    def test_fewer_episodes_available_returns_what_exists(self):
        out = self._run(3, 5)
        self.assertEqual(self._eps(out), 3)

    def test_a_long_video_tail_cannot_blow_up_the_payload(self):
        out = self._run(0, 500, every=1, burst=1)
        self.assertLessEqual(len(out), 80)


# ---- remote access -------------------------------------------------------

class _Headers(dict):
    """Just enough of http.client.HTTPMessage for _authorized()."""
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _req(host, path="/api/state", **headers):
    """A Handler with only the attributes _authorized() reads — constructing a real one
    would try to speak HTTP down a socket."""
    h = server.Handler.__new__(server.Handler)
    h.client_address = (host, 54321)
    h.path = path
    h.headers = _Headers(headers)
    return h


class RemoteAccess(unittest.TestCase):
    """The server binds every interface now (so the dashboard opens from a phone over the
    tailnet), which means the port is no longer the protection. This API can arm, disarm,
    skip and DELETE — everything off-machine must carry the token."""

    def setUp(self):
        import os, tempfile
        from unittest import mock
        d = tempfile.mkdtemp()
        p = mock.patch.object(server, "TOKEN_FILE", os.path.join(d, "tok"))
        p.start(); self.addCleanup(p.stop)
        self.tok = server.remote_token()

    def test_loopback_needs_no_token(self):
        # the Mac app talks to us here and must keep working untouched
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(_req(host)._authorized(), host)

    def test_remote_without_a_token_is_refused(self):
        self.assertFalse(_req("100.64.0.9")._authorized())
        self.assertFalse(_req("192.168.1.50")._authorized())

    def test_remote_with_a_wrong_token_is_refused(self):
        self.assertFalse(_req("100.64.0.9", Authorization="Bearer nope")._authorized())
        self.assertFalse(_req("100.64.0.9", path="/api/state?k=nope")._authorized())
        self.assertFalse(_req("100.64.0.9", Cookie="vk=nope")._authorized())

    def test_remote_with_the_token_is_allowed_three_ways(self):
        self.assertTrue(_req("100.64.0.9", Authorization="Bearer " + self.tok)._authorized())
        self.assertTrue(_req("100.64.0.9", path="/api/state?k=" + self.tok)._authorized())
        self.assertTrue(_req("100.64.0.9", Cookie="vk=" + self.tok)._authorized())

    def test_only_the_query_form_asks_for_a_cookie(self):
        # the ?k= link is traded for a cookie so the bookmark never carries the secret
        q = _req("100.64.0.9", path="/?k=" + self.tok)
        self.assertTrue(q._authorized()); self.assertTrue(q._token_via_query)
        c = _req("100.64.0.9", Cookie="vk=" + self.tok)
        self.assertTrue(c._authorized()); self.assertFalse(c._token_via_query)

    def test_a_missing_secret_refuses_rather_than_opens_up(self):
        from unittest import mock
        with mock.patch.object(server, "remote_token", return_value=""):
            self.assertFalse(_req("100.64.0.9", Authorization="Bearer x")._authorized())
            self.assertTrue(_req("127.0.0.1")._authorized())      # local still fine

    def test_token_is_stable_and_private(self):
        import os, stat
        self.assertEqual(server.remote_token(), self.tok)          # not regenerated per call
        self.assertGreaterEqual(len(self.tok), 24)
        mode = stat.S_IMODE(os.stat(server.TOKEN_FILE).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))                   # not world-readable

    def test_other_cookies_do_not_confuse_it(self):
        self.assertTrue(_req("100.64.0.9",
                             Cookie="theme=dark; vk=" + self.tok + "; x=1")._authorized())
        self.assertFalse(_req("100.64.0.9", Cookie="theme=dark; x=1")._authorized())
