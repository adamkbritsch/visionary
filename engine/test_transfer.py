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
            def __init__(self): self.encoding = "utf-8"
            def connect(self, *a, **k): pass
            def login(self, *a, **k): pass
            def set_pasv(self, v): pass
        rec = Rec()
        with mock.patch.object(transfer.ftplib, "FTP", return_value=rec), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["h"]), \
             mock.patch.object(transfer, "ftp_settings",
                               return_value={"port": 21, "user": "u", "passwd": "p"}):
            ftp = transfer.connect()
        self.assertEqual(ftp.encoding, "latin-1")


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
