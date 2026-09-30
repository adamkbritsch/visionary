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
