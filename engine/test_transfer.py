import os
import tempfile
import unittest
from unittest import mock

import transfer


class FakeFTP:
    """Minimal stand-in for ftplib.FTP."""
    def __init__(self, tree=None, files=None):
        self.tree = tree or {}      # dir path -> [(name, facts), ...]
        self.files = files or {}    # file path -> size
        self.stored = {}            # STOR path -> bytes
        self.retrieved = []         # RETR paths
        self.deleted = []           # DELE paths
        self.made = []              # MKD paths

    def mlsd(self, path):
        if path not in self.tree:
            raise transfer.ftplib.error_perm("550 No such dir")
        return list(self.tree[path])

    def voidcmd(self, c): pass
    def size(self, path): return self.files.get(path)
    def set_pasv(self, v): pass
    def quit(self): pass

    def retrbinary(self, cmd, cb):
        p = cmd[len("RETR "):]
        self.retrieved.append(p)
        cb(b"x" * (self.files.get(p, 0)))

    def storbinary(self, cmd, fp, callback=None):
        data = fp.read()
        if callback:
            callback(data)
        path = cmd[len("STOR "):]
        self.stored[path] = data
        self.files[path] = len(data)   # so SIZE (size()) can verify the upload, like the NAS

    def delete(self, path):
        self.deleted.append(path)
        self.files.pop(path, None)

    def mkd(self, path):
        if path in self.made:
            raise transfer.ftplib.error_perm("550 exists")
        self.made.append(path)


class Settings(unittest.TestCase):
    def test_env_supplies_credentials(self):
        with mock.patch.dict(os.environ, {"TOPAZ_NAS_FTP_USER": "u", "TOPAZ_NAS_FTP_PASS": "p"}):
            s = transfer.ftp_settings()
            self.assertEqual((s["user"], s["passwd"]), ("u", "p"))

    def test_no_credentials_hardcoded(self):
        # default (no env/config) must be EMPTY — the user supplies user AND password
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(transfer, "_config", return_value={}):
            s = transfer.ftp_settings()
            self.assertEqual((s["user"], s["passwd"]), ("", ""))

    def test_no_hosts_hardcoded(self):
        # no env/config → NO baked-in hosts (open-source: nothing machine-specific in code)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(transfer, "_config", return_value={}):
            self.assertEqual(transfer.ftp_hosts(), [])

    def test_config_hosts_list_preserves_order(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(transfer, "_config",
                               return_value={"ftp_hosts": ["100.1.2.3", "nas.local"]}):
            self.assertEqual(transfer.ftp_hosts(), ["100.1.2.3", "nas.local"])

    def test_connect_fails_clearly_when_unconfigured(self):
        import ftplib
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(transfer, "_config", return_value={}):
            with self.assertRaises(ftplib.error_perm) as cm:
                transfer.connect(timeout=1)
            self.assertIn("no NAS FTP host configured", str(cm.exception))

    def test_forced_host_overrides_failover(self):
        with mock.patch.dict(os.environ, {"TOPAZ_NAS_FTP_HOST": "only"}):
            self.assertEqual(transfer.ftp_hosts(), ["only"])

    def test_the_lan_link_goes_first_bound_and_the_list_stays_behind_it(self):
        import nas_link
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(transfer, "_config", return_value={"ftp_hosts": ["100.1.2.3", "nas.local"]}), \
             mock.patch.object(nas_link, "lan_route", return_value=("192.168.1.195", "192.168.1.92")):
            self.assertEqual(transfer._route_order(transfer.ftp_hosts()),
                             [("192.168.1.195", "192.168.1.92"), ("100.1.2.3", None), ("nas.local", None)])

    def test_no_direct_link_or_a_detection_error_keeps_the_configured_order(self):
        import nas_link
        cfg = {"ftp_hosts": ["100.1.2.3", "nas.local"]}
        for side in ({"return_value": None}, {"side_effect": RuntimeError("ifconfig broke")}):
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(transfer, "_config", return_value=cfg), \
                 mock.patch.object(nas_link, "lan_route", **side):
                self.assertEqual(transfer._route_order(transfer.ftp_hosts()),
                                 [("100.1.2.3", None), ("nas.local", None)])

    def test_a_forced_host_is_never_second_guessed(self):
        import nas_link
        with mock.patch.dict(os.environ, {"TOPAZ_NAS_FTP_HOST": "only"}), \
             mock.patch.object(nas_link, "lan_route", side_effect=AssertionError("not asked")):
            self.assertEqual(transfer._route_order(transfer.ftp_hosts()), [("only", None)])

    def test_connect_binds_the_lan_attempt_and_falls_back_unbound(self):
        import ftplib
        calls = []
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                calls.append((host, source_address))
                if host == "192.168.1.195":
                    raise OSError("cable pulled")
            def login(self, *a): pass
            def sendcmd(self, *a): pass
            def set_pasv(self, *a): pass
        with mock.patch.object(transfer, "_WireFTP", Fake), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.1.2.3", "nas.local"]), \
             mock.patch.object(transfer, "_route_order",
                               return_value=[("192.168.1.195", "192.168.1.92"), ("100.1.2.3", None),
                                             ("nas.local", None)]):
            transfer.connect(timeout=1)
        self.assertEqual(calls, [("192.168.1.195", ("192.168.1.92", 0)), ("100.1.2.3", None)])

    def test_a_failure_on_the_lan_route_is_retried_once_over_the_configured_order(self):
        import nas_link
        seen = []
        @transfer._link_retry
        def job():
            plain = getattr(transfer._LINK, "plain", False)
            seen.append(plain)
            if not plain:
                transfer._LINK.bound = True          # connect() took the LAN route...
                return False, "x", "download failed: timed out"      # ...and the link died
            return True, "x", "ok"
        with mock.patch.object(nas_link, "forget") as forget:
            self.assertEqual(job()[0], True)
        self.assertEqual(seen, [False, True])
        forget.assert_called_once()
        self.assertFalse(transfer._LINK.plain)       # the thread is left as it was found

    def test_no_retry_for_success_an_abort_or_a_connection_that_never_took_the_lan(self):
        for bound, res in ((True, (True, "x", "ok")), (True, (False, "x", "aborted mid-download")),
                           (False, (False, "x", "download failed: 550"))):
            calls = []
            @transfer._link_retry
            def job():
                calls.append(1)
                transfer._LINK.bound = bound
                return res
            job()
            self.assertEqual(len(calls), 1, (bound, res))

    def test_the_retry_really_takes_the_configured_order(self):
        import nas_link
        cfg = {"ftp_hosts": ["100.1.2.3", "nas.local"]}
        transfer._LINK.plain = True
        try:
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(transfer, "_config", return_value=cfg), \
                 mock.patch.object(nas_link, "lan_route", return_value=("192.168.1.195", "192.168.1.92")):
                # a LAN route IS available — the retry must still not take it
                self.assertEqual(transfer._route_order(transfer.ftp_hosts()),
                                 [("100.1.2.3", None), ("nas.local", None)])
        finally:
            transfer._LINK.plain = False

    def _publish(self, remote_sizes):
        import tempfile
        d = tempfile.mkdtemp()
        local = os.path.join(d, "m.mkv")
        with open(local, "wb") as fh:
            fh.write(b"x" * 10)
        ftp = mock.Mock()
        sizes = iter(remote_sizes)
        with mock.patch.object(transfer, "connect", return_value=ftp), \
             mock.patch.object(transfer, "_makedirs"), \
             mock.patch.object(transfer, "_copy_sidecars", return_value=0), \
             mock.patch.object(transfer, "remote_size", side_effect=lambda f, p: next(sizes)):
            res = transfer.publish_master.__wrapped__(local, "/Media/YouTube/c/m.mkv", d, d)
        return res, ftp

    def test_publish_clears_a_partial_before_storing(self):
        (ok, _r, _w), ftp = self._publish([4, 10])        # a cut attempt left 4 bytes
        self.assertTrue(ok)
        ftp.delete.assert_called_once_with("/Media/YouTube/c/m.mkv")
        ftp.storbinary.assert_called_once()

    def test_publish_skips_the_store_when_a_complete_copy_is_there(self):
        (ok, _r, _w), ftp = self._publish([10, 10])
        self.assertTrue(ok)
        ftp.delete.assert_not_called()
        ftp.storbinary.assert_not_called()

    def test_publish_fresh(self):
        (ok, _r, _w), ftp = self._publish([None, 10])
        self.assertTrue(ok)
        ftp.delete.assert_not_called()
        ftp.storbinary.assert_called_once()

    def test_download_upload_and_publish_all_carry_the_retry(self):
        for fn in (transfer.download, transfer.upload, transfer.publish_master):
            self.assertTrue(hasattr(fn, "__wrapped__"), fn.__name__)

    def test_owner_is_gid10(self):
        self.assertEqual(transfer.MEDIA_OWNER, "1000:10")   # FTP yields this automatically


class Walk(unittest.TestCase):
    def test_recurses_seasons_and_collects_files(self):
        tree = {
            "/Media/TV-Shows/Show": [("S01", {"type": "dir"}), ("poster.jpg", {"type": "file"})],
            "/Media/TV-Shows/Show/S01": [("e01 (Extended Cut).mp4", {"type": "file"}),
                                         (".", {"type": "cdir"})],
        }
        files = transfer.ftp_walk_files(FakeFTP(tree=tree), "/Media/TV-Shows/Show")
        self.assertIn("e01 (Extended Cut).mp4", files)
        self.assertIn("poster.jpg", files)

    def test_with_dirs_reports_the_real_containing_folder(self):
        # Season folders are NOT reliably "S01" — the caller needs the ACTUAL dir so the
        # download path isn't synthesized (a wrong path 550s).
        tree = {
            "/Media/TV-Shows/Show": [("Season 1", {"type": "dir"})],
            "/Media/TV-Shows/Show/Season 1": [("Show.S01E01.mkv", {"type": "file"})],
        }
        pairs = transfer.ftp_walk_files(FakeFTP(tree=tree), "/Media/TV-Shows/Show", with_dirs=True)
        self.assertEqual(pairs, [("/Media/TV-Shows/Show/Season 1", "Show.S01E01.mkv")])


class TransferOps(unittest.TestCase):
    def test_upload_sends_spacey_path_verbatim(self):
        f = FakeFTP()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as t:
            t.write(b"hello"); local = t.name
        try:
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, final, _ = transfer.upload(local, "/Media/TV-Shows/The Office  (X)/S02")
            expect = "/Media/TV-Shows/The Office  (X)/S02/" + os.path.basename(local)
            self.assertEqual(final, expect)          # spaces+parens, no quoting
            self.assertIn(expect, f.stored)          # STOR sent it verbatim
            self.assertTrue(ok)
        finally:
            os.remove(local)

    def test_download_retr_and_size_verify(self):
        rp = "/Media/TV-Shows/Show/S01/ep (Extended Cut).mp4"
        f = FakeFTP(files={rp: 4})
        d = tempfile.mkdtemp()
        with mock.patch.object(transfer, "connect", return_value=f):
            ok, local, _ = transfer.download(rp, d)
        self.assertIn(rp, f.retrieved)
        self.assertTrue(ok)
        self.assertTrue(local.endswith("ep (Extended Cut).mp4"))


class Replace(unittest.TestCase):
    """replace_original (behind the per-item `replace_source` setting): deletes the
    superseded source ONLY after the uploaded master's remote size matches local."""

    def _local(self, n=4):
        t = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        t.write(b"x" * n); t.close()
        return t.name

    def test_deletes_original_only_when_master_verified(self):
        master = "/Media/TV/ep (Extended Cut) HDR10 DV.mp4"
        original = "/Media/TV/ep (Extended Cut).mp4"
        local = self._local(4)
        try:
            f = FakeFTP(files={master: 4, original: 999})   # remote master == local (4)
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, msg = transfer.replace_original(master, original, local)
            self.assertTrue(ok)
            self.assertIn(original, f.deleted)              # source removed
            self.assertNotIn(master, f.deleted)             # 4K master untouched
        finally:
            os.remove(local)

    def test_keeps_original_when_master_not_verified(self):
        master = "/Media/TV/ep HDR10 DV.mp4"
        original = "/Media/TV/ep.mp4"
        local = self._local(4)
        try:
            f = FakeFTP(files={master: 99, original: 999})  # remote master != local (4)
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, msg = transfer.replace_original(master, original, local)
            self.assertFalse(ok)
            self.assertNotIn(original, f.deleted)           # irreplaceable source kept
        finally:
            os.remove(local)


class FolderSplitPublish(unittest.TestCase):
    """YouTube folder-split: master publishes to the Plex lib + sidecars copied, videos/junk skipped."""
    SRC = "/Media/YouTube-raw/Chan/Chan - T - id"
    DST = "/Media/YouTube/Chan/Chan - T - id"

    def _master(self, n=6):
        t = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        t.write(b"m" * n); t.close()
        return t.name

    def _tree(self):
        # the staging video folder: the video itself + real sidecars + a transient .part
        names = ["Chan - T [id].mp4", "Chan - T [id].nfo", "Chan - T [id].jpg",
                 "Chan - T [id].en.srt", "Chan - T [id].mp4.part"]
        tree = {self.SRC: [(n, {"type": "file"}) for n in names]}
        files = {self.SRC + "/" + n: 3 for n in names}
        return tree, files

    def test_publishes_master_and_copies_only_sidecars(self):
        tree, files = self._tree()
        f = FakeFTP(tree=tree, files=files)
        scratch = tempfile.mkdtemp()
        local = self._master(6)
        master_remote = self.DST + "/Chan - T [id].mp4"
        try:
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, remote, msg = transfer.publish_master(local, master_remote, self.SRC, scratch)
            self.assertTrue(ok, msg)
            self.assertEqual(remote, master_remote)
            self.assertIn(master_remote, f.stored)                      # master landed in the Plex lib
            self.assertIn(self.DST, f.made)                             # dest dir tree created (mkdir -p)
            # sidecars copied; the video + .part are NOT
            self.assertIn(self.DST + "/Chan - T [id].nfo", f.stored)
            self.assertIn(self.DST + "/Chan - T [id].jpg", f.stored)
            self.assertIn(self.DST + "/Chan - T [id].en.srt", f.stored)
            self.assertNotIn(self.DST + "/Chan - T [id].mp4.part", f.stored)
            self.assertNotIn(self.SRC + "/Chan - T [id].mp4", f.retrieved)   # never re-copies the video
            self.assertIn("3 sidecar(s)", msg)
        finally:
            os.remove(local)

    def test_rejects_on_size_mismatch(self):
        tree, files = self._tree()
        f = FakeFTP(tree=tree, files=files)
        local = self._master(6)
        master_remote = self.DST + "/Chan - T [id].mp4"
        try:
            # storbinary sets files[master]=len; force a mismatch by overriding size()
            with mock.patch.object(transfer, "connect", return_value=f), \
                 mock.patch.object(transfer, "remote_size", return_value=999):
                ok, remote, msg = transfer.publish_master(local, master_remote, self.SRC, tempfile.mkdtemp())
            self.assertFalse(ok)
            self.assertIn("size mismatch", msg)
        finally:
            os.remove(local)

    def test_makedirs_creates_each_component(self):
        f = FakeFTP()
        transfer._makedirs(f, "/Media/YouTube/Chan/vid")
        self.assertEqual(f.made, ["/Media", "/Media/YouTube", "/Media/YouTube/Chan",
                                  "/Media/YouTube/Chan/vid"])


class SafeDeletePath(unittest.TestCase):
    """delete_tree's guard must reject traversal / relative / near-root paths (a wrong recursive
    delete would nuke a whole media library)."""
    def test_allows_a_normal_channel_folder(self):
        self.assertTrue(transfer._safe_delete_path("/Media/YouTube-raw/All Gas No Brakes"))
        self.assertTrue(transfer._safe_delete_path("/Media/YouTube/Chan/vid-folder"))

    def test_rejects_traversal_relative_and_near_root(self):
        for bad in ("/Media/YouTube/..", "/Media/YouTube/.", "/Media/YouTube-raw/../../../etc",
                    "/Media", "/Media/", "", "relative/path", "/", "/Media/YouTube/../../TV-Shows"):
            self.assertFalse(transfer._safe_delete_path(bad), bad)


class Connect(unittest.TestCase):
    def test_sets_latin1_encoding(self):
        # A stray non-UTF-8 filename byte (0xa1) must not crash mlsd()/listings —
        # latin-1 decodes any byte and round-trips, so connect() must set it.
        class Rec:
            def __init__(self): self.encoding = "utf-8"; self.cmds = []
            def connect(self, *a, **k): pass
            def login(self, *a, **k): pass
            def sendcmd(self, c): self.cmds.append(c); return "200 ok"
            def set_pasv(self, v): pass
        rec = Rec()
        with mock.patch.object(transfer, "_WireFTP", return_value=rec), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["h"]), \
             mock.patch.object(transfer, "ftp_settings",
                               return_value={"port": 21, "user": "u", "passwd": "p"}):
            ftp = transfer.connect()
        self.assertEqual(ftp.encoding, "latin-1")   # both directions, unchanged: stray NAS
                                                    # bytes must still round-trip exactly
        # ...and BECAUSE the encoding is latin-1, ftplib will not negotiate UTF-8 for us, so
        # connect() must ask explicitly or the server answers in its legacy codepage
        self.assertIn("OPTS UTF8 ON", rec.cmds)


def _reset_breaker():
    with transfer._BREAKER_LOCK:
        transfer._BREAKER.update(open=False, since=None, until=0.0, fails=0, login_secs=None,
                                 checking=False, seen=0.0)


class LoginBreaker(unittest.TestCase):
    """Live 2026-10-06: the NAS answered the banner at once but took 138 s to finish a login.
    Every caller gave up at 8-30 s and retried, leaving the NAS running each abandoned password
    check — and the whole day read "NAS unreachable"."""

    SETTINGS = {"port": 21, "user": "u", "passwd": "p"}

    def setUp(self):
        _reset_breaker()

    tearDown = setUp

    def _fake(self, login_raises=None, calls=None):
        calls = [] if calls is None else calls
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                calls.append(host)
                return "220 adamsnas FTP server ready."
            def login(self, *a):
                if login_raises:
                    raise login_raises
            def sendcmd(self, *a): return "200 ok"
            def set_pasv(self, *a): pass
            def close(self): pass
            def quit(self): pass
        return Fake, calls

    def _connect(self, fake, timeout=15):
        with mock.patch.object(transfer, "_WireFTP", fake), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["a", "b"]), \
             mock.patch.object(transfer, "ftp_settings", return_value=self.SETTINGS), \
             mock.patch.object(transfer, "_route_order", return_value=[("a", None), ("b", None)]):
            return transfer.connect(timeout=timeout)

    def test_a_patient_login_that_stalls_opens_the_breaker_and_skips_the_other_routes(self):
        import socket
        fake, calls = self._fake(login_raises=socket.timeout("timed out"))
        with self.assertRaises(transfer.NasBusy) as cm:
            self._connect(fake)
        self.assertEqual(calls, ["a"])                 # same NAS behind every route: one try
        self.assertIsNotNone(transfer.nas_busy())
        self.assertIn("NAS overloaded", str(cm.exception))

    def test_python39_timeouts_trip_it_too(self):
        # the app's engine runs on Python 3.9, where socket.timeout is NOT TimeoutError
        fake, _ = self._fake(login_raises=TimeoutError("timed out"))
        with self.assertRaises(transfer.NasBusy):
            self._connect(fake)

    def test_while_overloaded_a_quick_probe_never_touches_the_network(self):
        import socket
        fake, _ = self._fake(login_raises=socket.timeout("timed out"))
        with self.assertRaises(transfer.NasBusy):
            self._connect(fake)
        boom = mock.Mock(side_effect=AssertionError("no new connection for a probe"))
        with mock.patch.object(transfer, "_WireFTP", boom), \
             mock.patch.object(transfer, "_route_order", side_effect=AssertionError("no routing")):
            for t in (6, 8, 10):
                with self.assertRaises(transfer.NasBusy):
                    transfer.connect(timeout=t)
        boom.assert_not_called()

    def _overloaded_fake(self, login_secs, timeouts, live=None, peak=None):
        """A fake whose login 'takes' login_secs (monotonic jumps) and records socket timeouts."""
        import threading
        live = live if live is not None else [0]
        peak = peak if peak is not None else [0]
        lock = threading.Lock()
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                self.sock = mock.Mock()
                self.sock.settimeout.side_effect = lambda t: timeouts.append(t)
                return "220 ready"
            def login(self, *a):
                with lock:
                    live[0] += 1; peak[0] = max(peak[0], live[0])
                import time as _t; _t.sleep(0.05)
                with lock:
                    live[0] -= 1
            def sendcmd(self, *a): return "200"
            def set_pasv(self, *a): pass
            def close(self): pass
        return Fake

    def test_overload_mode_still_logs_in_one_patient_login_at_a_time(self):
        transfer._trip(138.0)
        timeouts = []
        fake = self._overloaded_fake(26.0, timeouts)
        ftp = self._connect(fake, timeout=30)
        self.assertIsNotNone(ftp)
        # the login alone gets the overload patience; the caller's own timeout comes back after
        self.assertEqual(timeouts, [transfer.LOGIN_PATIENT, 30])

    def test_concurrent_callers_never_stack_logins_on_an_overloaded_nas(self):
        import threading
        transfer._trip(138.0)
        live, peak, errors = [0], [0], []
        fake = self._overloaded_fake(26.0, [], live, peak)
        with mock.patch.object(transfer, "_WireFTP", fake), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["a"]), \
             mock.patch.object(transfer, "ftp_settings", return_value=self.SETTINGS), \
             mock.patch.object(transfer, "_route_order", return_value=[("a", None)]), \
             mock.patch.object(transfer, "LOGIN_HEALTHY", -1):     # every login counts as slow
            def go():
                try:
                    transfer.connect(timeout=30)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            ts = [threading.Thread(target=go) for _ in range(5)]
            [t.start() for t in ts]; [t.join(5) for t in ts]
        self.assertEqual((peak[0], errors), (1, []))      # five logins, never two at once

    def test_a_healthy_login_ends_overload_mode(self):
        transfer._trip(138.0)
        fake = self._overloaded_fake(0.05, [])
        self._connect(fake, timeout=30)                   # a fast login now
        self.assertIsNone(transfer.nas_busy())

    def test_a_slow_successful_login_keeps_overload_mode_and_says_how_slow(self):
        transfer._trip(138.0)
        fake = self._overloaded_fake(26.0, [])
        clock = iter([1000.0, 1026.0])
        real = transfer.time.monotonic
        with mock.patch.object(transfer.time, "monotonic",
                               side_effect=lambda: next(clock, None) or real()):
            self._connect(fake, timeout=30)
        self.assertEqual(int(transfer.nas_busy()["login_secs"]), 26)

    def test_no_turn_within_the_patience_is_busy(self):
        transfer._trip(138.0)
        self.assertTrue(transfer._LOGIN_SLOT.acquire(timeout=1))     # someone else's long login
        try:
            with mock.patch.object(transfer, "LOGIN_PATIENT", 0.05), \
                 mock.patch.object(transfer, "_WireFTP", side_effect=AssertionError("no login")):
                with self.assertRaises(transfer.NasBusy):
                    transfer.connect(timeout=30)
        finally:
            transfer._LOGIN_SLOT.release()

    def test_the_recovery_check_stands_aside_while_real_logins_measure(self):
        transfer._trip(138.0)
        with transfer._BREAKER_LOCK:
            transfer._BREAKER.update(until=0.0, seen=transfer.time.time())
        sleeps = []
        def sleep(s):
            sleeps.append(s)
            with transfer._BREAKER_LOCK:                  # meanwhile a real login found it healthy
                transfer._BREAKER["open"] = False
        with mock.patch.object(transfer, "_recover_once",
                               side_effect=AssertionError("no extra password check")), \
             mock.patch.object(transfer.time, "sleep", side_effect=sleep):
            transfer._recover()
        self.assertEqual(len(sleeps), 1)

    def test_it_is_an_ftp_error_every_caller_already_handles(self):
        import ftplib
        self.assertTrue(issubclass(transfer.NasBusy, ftplib.error_temp))
        self.assertTrue(issubclass(transfer.NasBusy, ftplib.all_errors))

    def test_an_impatient_callers_timeout_says_nothing_about_the_nas(self):
        import socket
        fake, calls = self._fake(login_raises=socket.timeout("timed out"))
        with self.assertRaises(socket.timeout):
            self._connect(fake, timeout=6)             # a UI probe: its own business
        self.assertIsNone(transfer.nas_busy())

    def test_a_refused_login_is_not_an_overload(self):
        import ftplib
        fake, _ = self._fake(login_raises=ftplib.error_perm("530 Login incorrect."))
        with self.assertRaises(ftplib.error_perm):
            self._connect(fake)
        self.assertIsNone(transfer.nas_busy())

    def test_a_healthy_recovery_login_closes_it(self):
        transfer._trip(138.0)
        with mock.patch.object(transfer, "_open", return_value=mock.Mock()):
            self.assertTrue(transfer._recover_once())
        self.assertIsNone(transfer.nas_busy())

    def test_a_login_that_completes_slowly_is_still_overloaded_and_backs_off(self):
        transfer._trip(138.0)
        clock = iter([0.0, 141.0])
        with mock.patch.object(transfer, "_open", return_value=mock.Mock()), \
             mock.patch.object(transfer.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertFalse(transfer._recover_once())
        b = transfer.nas_busy()
        self.assertEqual(int(b["login_secs"]), 141)
        first = b["retry_in"]
        with mock.patch.object(transfer, "_open", side_effect=transfer.NasBusy("still")):
            self.assertFalse(transfer._recover_once())
        self.assertGreater(transfer.nas_busy()["retry_in"], first)      # it backs off further...
        for _ in range(10):
            with mock.patch.object(transfer, "_open", side_effect=transfer.NasBusy("still")):
                transfer._recover_once()
        self.assertLessEqual(transfer.nas_busy()["retry_in"], transfer.BREAKER_MAX)   # ...to a cap

    def test_a_nas_that_stops_answering_at_all_is_unreachable_not_overloaded(self):
        transfer._trip(138.0)
        with mock.patch.object(transfer, "_open", side_effect=OSError("No route to host")):
            self.assertTrue(transfer._recover_once())
        self.assertIsNone(transfer.nas_busy())

    def test_the_message_says_what_happened_and_when_it_looks_again(self):
        transfer._trip(138.4)
        t = transfer.busy_text()
        self.assertIn("a login took 138 s", t)
        self.assertIn("next check in", t)
        _reset_breaker()
        self.assertEqual(transfer.busy_text(), "")

    def test_a_connect_racing_the_close_never_reopens_it(self):
        self.assertFalse(transfer._ensure_recovery())   # closed: nothing to do, and still closed
        self.assertIsNone(transfer.nas_busy())

    def _connect_routes(self, fake, routes):
        with mock.patch.object(transfer, "_WireFTP", fake), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.1.2.3", "nas.local"]), \
             mock.patch.object(transfer, "ftp_settings", return_value=self.SETTINGS), \
             mock.patch.object(transfer, "_route_order", return_value=routes):
            return transfer.connect(timeout=15)

    def test_a_kernel_timeout_is_a_dead_path_and_the_next_route_is_tried(self):
        import errno
        calls = []
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                calls.append(host); self._h = host
                return "220 ready"
            def login(self, *a):
                if self._h == "192.168.1.195":
                    raise TimeoutError(errno.ETIMEDOUT, "Operation timed out")
            def sendcmd(self, *a): return "200"
            def set_pasv(self, *a): pass
            def close(self): pass
        self._connect_routes(Fake, [("192.168.1.195", "192.168.1.92"), ("100.1.2.3", None)])
        self.assertEqual(calls, ["192.168.1.195", "100.1.2.3"])
        self.assertIsNone(transfer.nas_busy())

    def _lan_login_stalls(self, still_offered):
        import nas_link, socket
        calls = []
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                calls.append(host); self._h = host
                return "220 ready"
            def login(self, *a):
                if self._h == "192.168.1.195":
                    raise socket.timeout("timed out")
            def sendcmd(self, *a): return "200"
            def set_pasv(self, *a): pass
            def close(self): pass
        with mock.patch.object(nas_link, "local_address_active", return_value=still_offered) as la, \
             mock.patch.object(nas_link, "lan_route",
                               side_effect=AssertionError("never re-prove against a stalling NAS")), \
             mock.patch.object(nas_link, "forget", side_effect=AssertionError("no forget")):
            try:
                self._connect_routes(Fake, [("192.168.1.195", "192.168.1.92"), ("100.1.2.3", None)])
            except transfer.NasBusy:
                pass
        la.assert_called_once_with("192.168.1.92")
        return calls

    def test_a_cable_pulled_after_the_banner_falls_back_instead_of_tripping(self):
        self.assertEqual(self._lan_login_stalls(still_offered=False), ["192.168.1.195", "100.1.2.3"])
        self.assertIsNone(transfer.nas_busy())

    def test_a_lan_route_still_offered_means_the_nas_itself_stalled(self):
        self.assertEqual(self._lan_login_stalls(still_offered=True), ["192.168.1.195"])
        self.assertIsNotNone(transfer.nas_busy())

    def test_no_test_ever_reaches_the_nas(self):
        with self.assertRaises(OSError) as cm:
            transfer._WireFTP().connect("192.168.1.195", 21, timeout=1)
        self.assertIn("mock transfer._WireFTP", str(cm.exception))

    def test_no_recovery_thread_from_a_test(self):
        with mock.patch.object(transfer.threading, "Thread",
                               side_effect=AssertionError("no thread under test")):
            transfer._trip(5.0)
        self.assertFalse(transfer._BREAKER["checking"])

    def test_the_recovery_loop_waits_out_the_window_then_checks(self):
        transfer._trip(138.0)
        with transfer._BREAKER_LOCK:
            transfer._BREAKER["until"] = 0.0
        with mock.patch.object(transfer, "_recover_once", return_value=True) as once:
            transfer._recover()
        once.assert_called_once()
        self.assertFalse(transfer._BREAKER["checking"])


class NasTransferSlot(unittest.TestCase):
    """One NAS transfer at a time, across apps (Expurgate takes the same lock file). Live
    2026-10-07: a 47 GB Expurgate download crawled at 36 MB/s beside Visionary's 82 GB Titanic
    upload while the NAS sat in login-overload for 18 minutes."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.lock = os.path.join(self.d, "nas-transfer.lock")
        for p in (mock.patch.object(transfer, "LOCK_POLL_SECS", 0.02),
                  mock.patch.object(transfer, "HANDOFF_SECS", 0),
                  mock.patch.object(transfer, "_lock_path", return_value=self.lock)):
            p.start(); self.addCleanup(p.stop)

    def test_never_the_real_lock_file_from_a_test(self):
        mock.patch.stopall()
        self.assertIsNone(transfer._lock_path())

    def test_the_lock_file_is_the_one_expurgate_takes(self):
        self.assertEqual(transfer.LOCK_FILE,
                         os.path.expanduser("~/Library/Caches/nas-transfer.lock"))

    def test_another_app_holding_it_makes_us_wait_and_say_so(self):
        import fcntl, threading
        other = open(self.lock, "a")                 # another process's open file = its own lock
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        notes, got = [], threading.Event()
        def run():
            with transfer.nas_slot(on_wait=notes.append):
                got.set()
        t = threading.Thread(target=run); t.start()
        self.assertFalse(got.wait(0.2))              # held off while the other app transfers
        fcntl.flock(other, fcntl.LOCK_UN); other.close()
        self.assertTrue(got.wait(2)); t.join(2)
        self.assertEqual(notes, [transfer.WAIT_OTHER_APP, None])

    def test_a_stop_while_waiting_is_honoured(self):
        import fcntl, threading
        other = open(self.lock, "a")
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event(); stop.set()
        with self.assertRaises(transfer.SlotStopped):
            with transfer.nas_slot(abort=stop):
                self.fail("must not run")
        other.close()

    def test_our_own_transfers_take_turns_too(self):
        import threading
        live, peak, notes = [0], [0], []
        lock = threading.Lock()
        def work():
            with transfer.nas_slot(on_wait=notes.append):
                with lock:
                    live[0] += 1; peak[0] = max(peak[0], live[0])
                import time as _t; _t.sleep(0.05)
                with lock:
                    live[0] -= 1
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]; [t.join(5) for t in ts]
        self.assertEqual(peak[0], 1)
        self.assertIn(transfer.WAIT_HERE, notes)

    def test_a_foreground_transfer_asks_a_background_holder_to_step_aside(self):
        import threading
        holding, asked, done = threading.Event(), [], threading.Event()
        def background():
            with transfer.nas_slot(prio=transfer.BACKGROUND) as body_abort:
                holding.set()
                for _ in range(200):                     # a DV leg checks its abort as it goes
                    if body_abort.is_set():
                        asked.append(True)
                        return
                    import time as _t; _t.sleep(0.01)
        t = threading.Thread(target=background); t.start()
        self.assertTrue(holding.wait(2))
        with transfer.nas_slot(prio=transfer.FOREGROUND):
            done.set()
        t.join(2)
        self.assertEqual(asked, [True])
        self.assertTrue(done.is_set())

    def test_a_foreground_holder_is_never_asked_to_step_aside(self):
        with transfer.nas_slot(prio=transfer.FOREGROUND) as body_abort:
            self.assertIsNone(body_abort)                # nothing but the caller's own abort

    def test_foreground_waiters_go_before_background_ones(self):
        import threading
        order, gate = [], threading.Event()
        def hold():
            with transfer.nas_slot(prio=transfer.FOREGROUND):
                gate.wait(2)
        def take(name, prio):
            with transfer.nas_slot(prio=prio):
                order.append(name)
        h = threading.Thread(target=hold); h.start()
        import time as _t; _t.sleep(0.05)
        bg = threading.Thread(target=take, args=("background", transfer.BACKGROUND)); bg.start()
        _t.sleep(0.05)
        fg = threading.Thread(target=take, args=("foreground", transfer.FOREGROUND)); fg.start()
        _t.sleep(0.05)
        gate.set()
        for t in (h, bg, fg):
            t.join(3)
        self.assertEqual(order, ["foreground", "background"])

    def test_the_other_app_gets_a_turn_between_two_of_ours(self):
        import time as _t
        with mock.patch.object(transfer, "HANDOFF_SECS", 0.3):
            with transfer.nas_slot():
                pass
            t0 = _t.monotonic()
            with transfer.nas_slot():                     # the lock stays free for the handoff
                pass
            self.assertGreaterEqual(_t.monotonic() - t0, 0.25)

    def test_the_note_follows_what_it_waits_for(self):
        import fcntl, threading, time as _t
        notes, release = [], threading.Event()
        with mock.patch.object(transfer, "HANDOFF_SECS", 0.5):
            def holder():
                with transfer.nas_slot():
                    release.wait(2)
            h = threading.Thread(target=holder); h.start()
            _t.sleep(0.05)
            def waiter():
                with transfer.nas_slot(on_wait=notes.append):
                    pass
            w = threading.Thread(target=waiter); w.start()
            _t.sleep(0.1)                                  # queued behind our own transfer
            release.set(); h.join(2)                       # ours is done: the handoff window opens
            other = open(self.lock, "a")                   # ...and the other app takes its turn in it
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _t.sleep(0.7)
            fcntl.flock(other, fcntl.LOCK_UN); other.close()
            w.join(3)
        self.assertEqual(notes, [transfer.WAIT_HERE, transfer.WAIT_OTHER_APP, None])

    def test_a_background_body_still_honours_the_callers_own_stop(self):
        stop = __import__("threading").Event()
        with transfer.nas_slot(abort=stop, prio=transfer.BACKGROUND) as body_abort:
            self.assertFalse(body_abort.is_set())
            stop.set()
            self.assertTrue(body_abort.is_set())          # the caller's own stop still counts

    def test_the_combined_abort_can_be_waited_on_like_an_event(self):
        # review 2026-10-07: the DV lane's retry pause calls abort.wait(); without it the first
        # dropped connection of every DV transfer raised AttributeError and failed the movie
        import threading, time as _t
        a, y = threading.Event(), threading.Event()
        any_ = transfer._AnyEvent(a, y)
        t0 = _t.monotonic()
        self.assertFalse(any_.wait(0.1))
        self.assertGreaterEqual(_t.monotonic() - t0, 0.09)
        threading.Timer(0.05, y.set).start()
        self.assertTrue(any_.wait(2))
        import nas_ftp
        with self.assertRaises(nas_ftp.Stopped):
            nas_ftp._pause(any_, 5)                    # set already: stops at once, no crash

    def test_a_background_waiter_steps_aside_even_before_it_has_the_lock(self):
        import fcntl, threading, time as _t
        other = open(self.lock, "a")                   # the other app transfers
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        order = []
        def background():
            with transfer.nas_slot(prio=transfer.BACKGROUND):
                order.append("background")
        def foreground():
            with transfer.nas_slot(prio=transfer.FOREGROUND):
                order.append("foreground")
        b = threading.Thread(target=background); b.start()
        _t.sleep(0.1)                                  # background holds the gate, waits on the app
        f = threading.Thread(target=foreground); f.start()
        _t.sleep(0.1)                                  # it must hand the gate to the foreground
        fcntl.flock(other, fcntl.LOCK_UN); other.close()
        b.join(3); f.join(3)
        self.assertEqual(order, ["foreground", "background"])

    def test_a_foreground_waiter_keeps_priority_between_its_polls(self):
        transfer._GATE.enter(transfer.FOREGROUND)       # a foreground waiter between two polls
        try:
            self.assertFalse(transfer._GATE.acquire(transfer.BACKGROUND, None, 0.05))
        finally:
            transfer._GATE.leave(transfer.FOREGROUND)
        self.assertTrue(transfer._GATE.acquire(transfer.BACKGROUND, None, 0.05))
        transfer._GATE.release()

    def test_a_small_upload_never_queues(self):
        small = os.path.join(self.d, "dv_probe.py")
        open(small, "w").write("x" * 1000)
        with mock.patch.object(transfer, "nas_slot", side_effect=AssertionError("no slot for a few KB")), \
             mock.patch.object(transfer, "connect", side_effect=transfer.ftplib.error_temp("offline")):
            ok, _r, why = transfer.upload(small, "/Media/x")
        self.assertFalse(ok)
        self.assertIn("offline", why)

    def test_a_stopped_download_closes_hard_instead_of_waiting_on_quit(self):
        import threading
        stop = threading.Event()
        closed, quit_called = [], []
        class Fake(object):
            sock = object()
            def voidcmd(self, *a): return "200"
            def size(self, path): return 10
            def retrbinary(self, cmd, cb):
                stop.set(); cb(b"x")
            def close(self):
                closed.append(1); self.sock = None
            def quit(self):
                quit_called.append(1)
        with mock.patch.object(transfer, "connect", return_value=Fake()):
            ok, _l, why = transfer.download("/Media/x.mkv", self.d, abort=stop)
        self.assertEqual((ok, why), (False, "aborted mid-download"))
        self.assertEqual((closed, quit_called), ([1], []))

    def test_a_nested_transfer_on_the_same_thread_never_deadlocks(self):
        with transfer.nas_slot():
            with transfer.nas_slot():
                pass

    def test_a_lock_file_that_cannot_be_opened_never_stops_a_transfer(self):
        with mock.patch.object(transfer, "_lock_path", return_value="/nonexistent/dir/x.lock"), \
             mock.patch("os.makedirs", side_effect=OSError("read-only")):
            with transfer.nas_slot():
                pass

    def test_the_slot_is_released_when_the_transfer_raises(self):
        with self.assertRaises(RuntimeError):
            with transfer.nas_slot():
                raise RuntimeError("boom")
        with transfer.nas_slot():                     # free again
            pass

    def test_one_slot_for_a_whole_transfer_including_its_lan_retry(self):
        entered = []
        real = transfer.nas_slot
        def counting(abort=None, on_wait=None, prio=transfer.FOREGROUND):
            entered.append(1)
            return real(abort, on_wait, prio)
        attempts = []
        def fake_body(*a, **kw):
            attempts.append(1)
            transfer._LINK.bound = True               # took the LAN route...
            return False, "x", "download failed: link lost"      # ...and lost it
        with mock.patch.object(transfer, "nas_slot", side_effect=counting), \
             mock.patch.object(transfer, "connect", side_effect=AssertionError("unused")):
            wrapped = transfer._one_transfer(transfer._link_retry(fake_body))
            wrapped("/Media/x.mkv", "/tmp")
        self.assertEqual((len(entered), len(attempts)), (1, 2))

    def test_a_stop_while_waiting_reads_as_an_abort_to_callers(self):
        import fcntl, threading
        other = open(self.lock, "a")
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event(); stop.set()
        ok, _local, why = transfer.download("/Media/x.mkv", self.d, abort=stop)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("aborted"), why)
        other.close()

    def test_every_big_transfer_is_wrapped_and_short_ones_are_not(self):
        import inspect
        src = inspect.getsource(transfer)
        import re
        for name in ("download", "upload", "publish_master"):
            self.assertRegex(src, r"@_one_transfer(\(size_of=_local_size\))?\n@_link_retry\ndef %s\(" % name)
        for name in ("download_head", "remote_size", "ftp_listdir", "ftp_walk_files"):
            self.assertNotIn("@_one_transfer\ndef %s(" % name, src)


class LearnsOnlyOverAProvenRoute(unittest.TestCase):
    SETTINGS = {"port": 21, "user": "u", "passwd": "p"}

    def _connect(self, routes):
        import nas_link
        class Fake(transfer._WireFTP):
            def connect(self, host, port, timeout=None, source_address=None):
                return "220 ready"
            def login(self, *a): pass
            def sendcmd(self, *a): return "200"
            def set_pasv(self, *a): pass
        with mock.patch.object(transfer, "_WireFTP", Fake), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.101.182.68"]), \
             mock.patch.object(transfer, "ftp_settings", return_value=self.SETTINGS), \
             mock.patch.object(transfer, "_route_order", return_value=routes), \
             mock.patch.object(nas_link, "learn_mac") as learn:
            transfer.connect(timeout=15)
        return learn

    def test_a_login_over_the_proven_lan_route_teaches_its_address(self):
        self._connect([("192.168.1.195", "192.168.1.92")]).assert_called_once_with(
            "192.168.1.195", "192.168.1.92")

    def test_a_plain_route_login_teaches_nothing(self):
        self._connect([("100.101.182.68", None)]).assert_not_called()


if __name__ == "__main__":
    unittest.main()


class UploadIdempotency(unittest.TestCase):
    """An upload cut AFTER its last byte (app relaunch between transfer and verify) leaves
    a complete master at the target. Re-STORing it was refused by the NAS (553 on an
    existing file, live-caught 2026-08-05) — presence at the EXACT local size is the same
    verification a fresh STOR requires, so it now counts as shipped without a transfer."""

    def _local(self, n):
        t = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False)
        t.write(b"x" * n); t.close()
        return t.name

    def test_complete_remote_skips_the_stor(self):
        local = self._local(5)
        rp = "/MediaVolume3/TV-Shows/Lost (2004)/Lost (2004) S02/" + os.path.basename(local)
        f = FakeFTP(files={rp: 5})                     # already there, byte-identical size
        try:
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, final, reason = transfer.upload(local, os.path.dirname(rp))
            self.assertTrue(ok)
            self.assertEqual(final, rp)
            self.assertEqual(f.stored, {})             # no STOR — nothing re-sent
            self.assertIn("skipped", reason)
        finally:
            os.remove(local)

    def test_partial_remote_is_cleared_and_reuploaded(self):
        local = self._local(5)
        rp = "/Media/TV-Shows/Show/S01/" + os.path.basename(local)
        f = FakeFTP(files={rp: 3})                     # a stub from a cut transfer
        try:
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, final, _ = transfer.upload(local, os.path.dirname(rp))
            self.assertTrue(ok)
            self.assertIn(rp, f.deleted)               # partial cleared first (STOR may 553)
            self.assertIn(rp, f.stored)                # then re-sent whole
        finally:
            os.remove(local)

    def test_absent_remote_uploads_normally(self):
        local = self._local(4)
        f = FakeFTP()
        try:
            with mock.patch.object(transfer, "connect", return_value=f):
                ok, final, _ = transfer.upload(local, "/Media/TV-Shows/Show/S01")
            self.assertTrue(ok)
            self.assertEqual(f.deleted, [])            # nothing to clear
            self.assertIn(final, f.stored)
        finally:
            os.remove(local)


class UnicodePathsOnTheWire(unittest.TestCase):
    """connect() reads latin-1 so any stray NAS byte round-trips — but latin-1 cannot ENCODE
    above U+00FF, so a path built from real Unicode raised on every command that touched it.
    Adding "Kurzgesagt – In a Nutshell" (en dash) therefore crashed the whole state poll, and
    a UI that cannot refresh cannot clear a pending tab switch either — the app froze on the
    YouTube tab and the channel never appeared (live-caught 2026-08-20)."""

    NAME = "Kurzgesagt – In a Nutshell"

    def test_ascii_is_untouched(self):
        for n in ("Plain Name", "DIY Perks", "a/b/c.mp4", ""):
            self.assertEqual(transfer.to_wire(n), n)

    def test_real_unicode_becomes_latin1_safe(self):
        w = transfer.to_wire(self.NAME)
        w.encode("latin-1")                      # must not raise — this is the whole point
        self.assertNotEqual(w, self.NAME)

    def test_it_is_the_inverse_of_display_name(self):
        self.assertEqual(transfer.display_name(transfer.to_wire(self.NAME)), self.NAME)

    def test_an_already_wire_string_passes_through(self):
        w = transfer.to_wire(self.NAME)
        self.assertEqual(transfer.to_wire(w), w)   # idempotent — never double-encodes

    def test_the_ftp_subclass_converts_command_lines(self):
        sent = []
        ftp = transfer._WireFTP()
        with mock.patch.object(transfer.ftplib.FTP, "putcmd",
                               side_effect=lambda line: sent.append(line)):
            ftp.putcmd("CWD /YouTube-raw/" + self.NAME)
        self.assertEqual(len(sent), 1)
        sent[0].encode("latin-1")                # would have raised before
        self.assertIn("Kurzgesagt", sent[0])

    def test_non_strings_are_left_alone(self):
        self.assertIsNone(transfer.to_wire(None))
