"""engine/nas_ftp.py — how the profile 7 lane moves files to and from the NAS over FTP and replaces
one in place. The network is a fake server held in memory (FakeServer): REST + RETR/STOR at byte
offsets, a rename that replaces an existing file, MDTM, SITE CHMOD and MLSD — the behaviour proven
on the NAS's smbftpd on 2026-10-01 — plus legs that drop part-way."""
import ftplib
import os
import tempfile
import threading
import unittest
from unittest import mock

import nas_ftp
import transfer


class FakeServer:
    def __init__(self):
        self.files, self.mtimes, self.modes, self.dirs = {}, {}, {}, {"/"}
        self.rests, self.chmods, self.renames = [], [], []
        self.drop_after = []          # per new transfer: die after this many bytes (None = never)

    def put(self, path, data, mtime="20261001155717", mode="0777"):
        self.files[path] = bytearray(data)
        self.mtimes[path], self.modes[path] = mtime, mode


class FakeConn:
    def __init__(self, srv, path, offset, store):
        self.srv, self.path, self.pos, self.store = srv, path, offset, store
        self.left = srv.drop_after.pop(0) if srv.drop_after else None

    def _budget(self, n):
        if self.left is None:
            return n
        if self.left <= 0:
            raise OSError("connection reset")
        n = min(n, self.left)
        self.left -= n
        return n

    def recv(self, n):
        data = self.srv.files[self.path]
        n = self._budget(min(n, max(0, len(data) - self.pos)) or 0) if self.pos < len(data) else 0
        out = bytes(data[self.pos:self.pos + n])
        self.pos += len(out)
        return out

    def sendall(self, b):
        f = self.srv.files.setdefault(self.path, bytearray())
        n = self._budget(len(b))
        if len(f) < self.pos:
            f.extend(b"\0" * (self.pos - len(f)))
        f[self.pos:self.pos + n] = b[:n]          # written AT the offset, like REST + STOR
        self.pos += n
        if n < len(b):
            raise OSError("connection reset")

    def close(self):
        pass


class FakeFTP:
    def __init__(self, srv, host="100.101.182.68"):
        self.srv, self.host = srv, host

    def voidcmd(self, cmd):
        return "200 ok"

    def size(self, path):
        if path not in self.srv.files:
            raise ftplib.error_perm("550 not found")
        return len(self.srv.files[path])

    def sendcmd(self, cmd):
        if cmd.startswith("MDTM "):
            p = cmd[5:]
            if p not in self.srv.files:
                raise ftplib.error_perm("550 not found")
            return "213 " + self.srv.mtimes[p]
        if cmd.startswith("SITE CHMOD "):
            mode, p = cmd[11:].split(" ", 1)
            self.srv.chmods.append((mode, p))
            self.srv.modes[p] = "0" + mode
            return "200 CHMOD command successful."
        raise ftplib.error_perm("500 unknown " + cmd)

    def transfercmd(self, cmd, rest=None):
        verb, path = cmd.split(" ", 1)
        self.srv.rests.append((verb, rest))
        if verb == "RETR" and path not in self.srv.files:
            raise ftplib.error_perm("550 not found")
        if verb == "STOR":
            if path.rsplit("/", 1)[0] not in self.srv.dirs:
                raise ftplib.error_perm(f"553 {path}: Permission denied.")   # smbftpd, missing folder
            self.srv.mtimes.setdefault(path, "20261001160000")
            self.srv.modes.setdefault(path, "0777")
        return FakeConn(self.srv, path, rest or 0, verb == "STOR")

    def voidresp(self):
        return "226 done"

    def mkd(self, path):
        if path in self.srv.dirs:
            raise ftplib.error_perm("550 exists")
        self.srv.dirs.add(path)

    def rmd(self, path):
        if path not in self.srv.dirs or any(p.startswith(path + "/") for p in self.srv.files):
            raise ftplib.error_perm("550 not empty")
        self.srv.dirs.discard(path)

    def delete(self, path):
        if path not in self.srv.files:
            raise ftplib.error_perm("550 not found")
        del self.srv.files[path]

    def rename(self, a, b):
        if a not in self.srv.files:
            raise ftplib.error_perm("550 not found")
        self.srv.renames.append((a, b))
        self.srv.files[b] = self.srv.files.pop(a)
        self.srv.modes[b] = self.srv.modes.pop(a)
        self.srv.mtimes[b] = self.srv.mtimes.pop(a)

    def mlsd(self, folder, facts=()):
        if "[" in folder:
            return iter(())                         # smbftpd globs a bracketed folder: empty
        out = []
        for p in self.srv.files:
            d, n = p.rsplit("/", 1)
            if d == folder:
                out.append((n, {"unix.mode": self.srv.modes[p]}))
        return iter(out)

    def quit(self):
        pass

    def close(self):
        pass


class _Net(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer()
        self.patches = [mock.patch.object(nas_ftp, "_connect", side_effect=lambda timeout=0: FakeFTP(self.srv)),
                        mock.patch.object(nas_ftp, "link", return_value={}),
                        mock.patch.object(nas_ftp, "_pause")]
        for p in self.patches:
            p.start()
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        for p in self.patches:
            p.stop()


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


HOST = "/volume2/MediaVolume2/Movies/Joker (2019) [2160p DV].mkv"
FTP = "/MediaVolume2/Movies/Joker (2019) [2160p DV].mkv"


class Paths(unittest.TestCase):
    def test_ftp_to_host(self):
        self.assertEqual(nas_ftp.ftp_to_host("/Media/Movies/a.mkv"), "/volume1/Media/Movies/a.mkv")
        self.assertEqual(nas_ftp.ftp_to_host("/MediaVolume3/Movies/D (2011)/d.mkv"),
                         "/volume3/MediaVolume3/Movies/D (2011)/d.mkv")
        self.assertIsNone(nas_ftp.ftp_to_host("/Elsewhere/a.mkv"))
        self.assertIsNone(nas_ftp.ftp_to_host("a.mkv"))

    def test_host_to_ftp_is_the_inverse(self):
        for f in ("/Media/Movies/a.mkv", "/MediaVolume3/Movies/D (2011)/d [x].mkv"):
            self.assertEqual(nas_ftp.host_to_ftp(nas_ftp.ftp_to_host(f)), f)
        self.assertIsNone(nas_ftp.host_to_ftp("/volume2/Media/Movies/a.mkv"))   # share/volume mismatch
        self.assertIsNone(nas_ftp.host_to_ftp("/volume1/Media"))
        self.assertIsNone(nas_ftp.host_to_ftp("/home/x/a.mkv"))

    def test_host_to_plex_is_the_containers_view(self):
        self.assertEqual(nas_ftp.host_to_plex("/volume1/Media/Movies"), "/media/Movies")
        self.assertEqual(nas_ftp.host_to_plex("/volume2/MediaVolume2/Movies"), "/media/vol2/Movies")
        self.assertEqual(nas_ftp.host_to_plex("/volume3/MediaVolume3/Movies/Drive (2011)"),
                         "/media/vol3/Movies/Drive (2011)")
        self.assertIsNone(nas_ftp.host_to_plex("/volume2/Media/Movies"))

    def test_staging_is_on_the_same_share_and_never_a_video_name(self):
        st = nas_ftp.stage_path_for(HOST)
        self.assertTrue(st.startswith("/volume2/MediaVolume2/_claude-tmp/"))
        self.assertTrue(st.endswith(".part"))
        self.assertNotIn("[", st)                     # SITE CHMOD and MLSD glob brackets
        self.assertEqual(nas_ftp.share_root(st), nas_ftp.share_root(HOST))
        self.assertNotEqual(st, nas_ftp.stage_path_for("/volume2/MediaVolume2/Movies/y.mkv"))

    def test_a_stage_keeps_the_key_the_ssh_lane_used(self):
        # a movie part-uploaded before the move to FTP resumes onto the same staged file
        self.assertEqual(nas_ftp.stage_path_for("/volume3/MediaVolume3/Movies/Planes, Trains & "
                                                "Automobiles (1987) [2160p UHD BluRay REMUX HDR10 DV "
                                                "HEVC 10bit DTS-HD MA 5.1]-FraMeSToR.mkv"),
                         "/volume3/MediaVolume3/_claude-tmp/7fc5ed2de231.part")



class PanelLink(unittest.TestCase):
    """The lane's panel reported "unresolved" all day while mDNS was dead (2026-10-06): it must
    show the link the transfers really take once Tailscale proves the NAS's LAN address."""
    LAN = {"iface": "en12", "bound": True, "wired": True, "kind": "Ethernet", "ip": "192.168.1.195",
           "src": "192.168.1.92", "fresh": True, "speed": "2.5 GbE"}
    SILENT = {"iface": None, "bound": False, "ip": None, "unresolved": True, "fresh": False}

    def _link(self, mdns, alt):
        import nas_link
        with mock.patch.object(nas_ftp, "_lan_name", return_value="adamsnas.local"), \
             mock.patch.object(nas_link, "detect", return_value=mdns), \
             mock.patch.object(nas_link, "lan_link", side_effect=lambda h, wait=True: alt) as ll, \
             mock.patch.object(nas_link, "remember", side_effect=lambda ln: ln), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.101.182.68", "adamsnas.local"]):
            return nas_ftp.link(), ll

    def test_a_silent_mdns_name_shows_the_proven_lan_link(self):
        ln, ll = self._link(self.SILENT, self.LAN)
        ll.assert_called_once_with(["100.101.182.68", "adamsnas.local"], wait=False)   # no ping on a leg
        self.assertEqual((ln["iface"], ln["kind"], ln["ip"]), ("en12", "Ethernet", "192.168.1.195"))
        self.assertIn("via", ln)

    def test_a_fresh_mdns_link_is_used_as_before(self):
        ln, ll = self._link(self.LAN, None)
        ll.assert_not_called()
        self.assertEqual(ln["iface"], "en12")

    def test_the_relink_check_never_pings(self):
        with mock.patch.object(transfer, "_route_order", return_value=[]) as ro, \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.101.182.68"]):
            nas_ftp._lan_offered()
        self.assertEqual(ro.call_args.kwargs.get("wait"), False)

    def test_no_proof_keeps_the_unresolved_answer(self):
        ln, _ = self._link(self.SILENT, None)
        self.assertTrue(ln["unresolved"])


class Pure(unittest.TestCase):
    def test_mdtm_is_utc(self):
        # the pair read on the NAS: MDTM 20261001155717 for a file whose stat %Y was 1790870237
        self.assertEqual(nas_ftp.mdtm_epoch("213 20261001155717"), 1790870237)
        self.assertIsNone(nas_ftp.mdtm_epoch("550 no"))
        self.assertIsNone(nas_ftp.mdtm_epoch(""))

    def test_samples_cover_head_and_tail_without_overlap(self):
        size = 50 * 1024 ** 3
        r = nas_ftp.sample_ranges(size)
        self.assertEqual(r[0], (0, nas_ftp.SAMPLE_SPAN))
        self.assertEqual(r[-1][0] + r[-1][1], size)
        self.assertEqual(len(r), nas_ftp.SAMPLES)
        for (a, n), (b, _m) in zip(r, r[1:]):
            self.assertLessEqual(a + n, b)

    def test_samples_of_a_small_file(self):
        self.assertEqual(nas_ftp.sample_ranges(10), [(0, 10)])
        span = nas_ftp.SAMPLE_SPAN                  # up to SAMPLES spans: every byte is compared
        self.assertEqual(nas_ftp.sample_ranges(2 * span + 3), [(0, span), (span, span), (2 * span, 3)])
        self.assertEqual(nas_ftp.sample_ranges(0), [])
        r = nas_ftp.sample_ranges(3 * nas_ftp.SAMPLE_SPAN)
        self.assertEqual(r[-1][0] + r[-1][1], 3 * nas_ftp.SAMPLE_SPAN)
        for (a, n), (b, _m) in zip(r, r[1:]):
            self.assertLessEqual(a + n, b)

    def test_the_key_tracks_interface_availability_and_setting(self):
        a = {"iface": "en12", "bound": True, "priority": "ethernet"}
        self.assertNotEqual(nas_ftp.link_key(a), nas_ftp.link_key(dict(a, iface="en0")))
        self.assertEqual(nas_ftp.link_key(a), nas_ftp.link_key(dict(a, priority="ethernet_only")))
        self.assertNotEqual(nas_ftp.link_key(a), nas_ftp.link_key({"unavailable": True}))
        self.assertEqual(nas_ftp.link_key(a), nas_ftp.link_key(dict(a, speed="2.5 GbE")))


class Leg(unittest.TestCase):
    def _io(self, n):
        import io
        return io.BytesIO(b"x" * n), io.BytesIO()

    def test_moves_everything_and_reports_progress(self):
        src, sink = self._io(nas_ftp.CHUNK * 2 + 5)
        seen = []
        moved = nas_ftp._leg(src.read, sink.write, moved0=7, total=99, abort=None, limit=None,
                             on_progress=lambda b, t: seen.append(b))
        self.assertEqual(moved, nas_ftp.CHUNK * 2 + 5)
        self.assertEqual(seen[-1], 7 + moved)

    def test_stops_on_abort(self):
        ev = threading.Event()
        ev.set()
        src, sink = self._io(10)
        with self.assertRaises(nas_ftp.Stopped):
            nas_ftp._leg(src.read, sink.write, moved0=0, total=10, abort=ev, limit=None,
                         on_progress=None)

    def test_a_cap_paces_the_leg(self):
        naps = []
        src, sink = self._io(nas_ftp.CHUNK * 3)
        with mock.patch.object(nas_ftp.time, "sleep", side_effect=naps.append):
            nas_ftp._leg(src.read, sink.write, moved0=0, total=0, abort=None,
                         limit=lambda: nas_ftp.CHUNK, on_progress=None)
        self.assertTrue(naps and all(0 < n <= nas_ftp.LIMIT_POLL_SECS for n in naps))

    def test_a_changed_link_ends_the_leg(self):
        src, sink = self._io(10)
        with self.assertRaises(nas_ftp._Relink):
            nas_ftp._leg(src.read, sink.write, moved0=0, total=10, abort=None, limit=None,
                         on_progress=None, relink=lambda: True)


class Download(_Net):
    def test_a_part_download_resumes_at_its_end(self):
        data = os.urandom(3 * nas_ftp.SAMPLE_SPAN + 17)
        self.srv.put(FTP, data)
        local = os.path.join(self.d, "src.mkv")
        with open(local, "wb") as fh:
            fh.write(data[:1000])
        nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)
        self.assertEqual(self.srv.rests[0], ("RETR", 1000))

    def test_a_dropped_leg_resumes_where_it_stopped(self):
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN)
        self.srv.put(FTP, data)
        self.srv.drop_after = [12345]
        local = os.path.join(self.d, "src.mkv")
        nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)
        self.assertEqual([r for v, r in self.srv.rests if v == "RETR"][:2], [0, 12345])

    def test_a_part_download_longer_than_the_nas_file_starts_over(self):
        # the NAS file was replaced by a shorter one since: never REST past its end
        data = os.urandom(nas_ftp.SAMPLE_SPAN)
        self.srv.put(FTP, data)
        local = os.path.join(self.d, "src.mkv")
        with open(local, "wb") as fh:
            fh.write(b"y" * (len(data) + 50))
        nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)
        self.assertEqual(self.srv.rests[0], ("RETR", 0))

    def test_a_nas_file_shorter_than_expected_fails_instead_of_spinning(self):
        self.srv.put(FTP, os.urandom(1000))
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            nas_ftp.download(HOST, os.path.join(self.d, "src.mkv"), 1010)
        self.assertEqual(nas_ftp._pause.call_count, nas_ftp.MAX_FAILURES)

    def test_a_corrupt_copy_is_refused_and_removed(self):
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN)
        self.srv.put(FTP, data)
        local = os.path.join(self.d, "src.mkv")
        with open(local, "wb") as fh:
            fh.write(b"XXXX" + data[4:100])           # a part download whose head went bad
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            nas_ftp.download(HOST, local, len(data))
        self.assertFalse(os.path.exists(local))

    def test_relinks_do_not_use_up_the_attempts(self):
        data = os.urandom(1000)
        self.srv.put(FTP, data)
        n = {"legs": 0}
        orig = nas_ftp._leg
        def leg(read, write, **kw):
            n["legs"] += 1
            if n["legs"] <= 20:                       # more relinks than MAX_FAILURES
                raise nas_ftp._Relink("flap")
            return orig(read, write, **kw)
        local = os.path.join(self.d, "src.mkv")
        with mock.patch.object(nas_ftp, "_leg", side_effect=leg):
            nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)
        nas_ftp._pause.assert_not_called()

    def test_stopping_keeps_the_bytes_and_raises(self):
        self.srv.put(FTP, os.urandom(1000))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(nas_ftp.Stopped):
            nas_ftp.download(HOST, os.path.join(self.d, "src.mkv"), 1000, abort=ev)

    def test_ethernet_only_without_a_cable_is_not_a_failure_but_a_wait(self):
        with mock.patch.object(nas_ftp, "_connect", side_effect=nas_ftp.NoLink("no cable")):
            with self.assertRaises(nas_ftp.NoLink):
                nas_ftp.download(HOST, os.path.join(self.d, "src.mkv"), 1000)
        nas_ftp._pause.assert_not_called()


class Upload(_Net):
    STAGE = nas_ftp.stage_path_for(HOST)

    def _local(self, data):
        p = os.path.join(self.d, "p81.mkv")
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_resumes_onto_what_is_already_staged(self):
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN + 9)
        self.srv.put(nas_ftp.host_to_ftp(self.STAGE), data[:5000])
        nas_ftp.upload(self._local(data), self.STAGE)
        self.assertEqual(bytes(self.srv.files[nas_ftp.host_to_ftp(self.STAGE)]), data)
        self.assertIn(("STOR", 5000), self.srv.rests)

    def test_a_dropped_leg_resumes_at_its_offset(self):
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN)
        self.srv.drop_after = [77777]
        nas_ftp.upload(self._local(data), self.STAGE)
        self.assertEqual(bytes(self.srv.files[nas_ftp.host_to_ftp(self.STAGE)]), data)
        self.assertEqual([r for v, r in self.srv.rests if v == "STOR"], [0, 77777])

    def test_a_staged_file_longer_than_the_new_one_starts_over(self):
        data = os.urandom(nas_ftp.SAMPLE_SPAN)
        self.srv.put(nas_ftp.host_to_ftp(self.STAGE), b"z" * (len(data) + 10))
        nas_ftp.upload(self._local(data), self.STAGE)
        self.assertEqual(bytes(self.srv.files[nas_ftp.host_to_ftp(self.STAGE)]), data)

    def test_a_staging_folder_removed_after_the_upload_began_is_made_again(self):
        # The other thread clears out a stage on the same share as this upload starts: its RMD of
        # the (still empty) shared folder lands between this upload's MKD and its STOR. Live
        # 2026-10-03: every leg got "553 Permission denied" and the attempt failed.
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN)
        real_mkd, calls = FakeFTP.mkd, []

        def mkd(ftp, path):
            real_mkd(ftp, path)
            calls.append(path)
            if len(calls) == 1:
                self.srv.dirs.discard(path)             # the other thread's RMD
        with mock.patch.object(FakeFTP, "mkd", mkd):
            nas_ftp.upload(self._local(data), self.STAGE)
        self.assertEqual(bytes(self.srv.files[nas_ftp.host_to_ftp(self.STAGE)]), data)
        self.assertEqual(len(calls), 2)                 # one leg lost, the next made it again

    def test_a_stopped_upload_makes_nothing_on_the_nas(self):
        # remove() cleans up after its 30 s wait even while a leg is still connecting: that leg must
        # not make the folder again and leave an empty .part behind
        ev = threading.Event()
        ev.set()
        with self.assertRaises(nas_ftp.Stopped):
            nas_ftp.upload(self._local(os.urandom(1000)), self.STAGE, abort=ev)
        self.assertEqual(self.srv.dirs, {"/"})
        self.assertNotIn("STOR", [v for v, _r in self.srv.rests])

    def test_a_bad_staged_copy_is_deleted_not_resumed_onto(self):
        data = os.urandom(2 * nas_ftp.SAMPLE_SPAN)
        stage = nas_ftp.host_to_ftp(self.STAGE)
        self.srv.put(stage, b"Q" * 100)                # an old, different file's first bytes
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            nas_ftp.upload(self._local(data), self.STAGE)
        self.assertNotIn(stage, self.srv.files)


class Swap(_Net):
    STAGE = nas_ftp.stage_path_for(HOST)

    def setUp(self):
        super().setUp()
        self.srv.put(FTP, b"o" * 100, mtime="20250614023105", mode="0777")
        self.srv.put(nas_ftp.host_to_ftp(self.STAGE), b"n" * 90, mode="0770")
        self.t0 = nas_ftp.mdtm_epoch("213 20250614023105")

    def test_renames_once_over_the_original(self):
        nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=90)
        self.assertEqual(bytes(self.srv.files[FTP]), b"n" * 90)
        self.assertEqual(self.srv.renames, [(nas_ftp.host_to_ftp(self.STAGE), FTP)])

    def test_refuses_when_the_original_changed(self):
        with self.assertRaisesRegex(RuntimeError, "changed"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0 + 1, new_size=90)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=101, expect_mtime=self.t0, new_size=90)
        self.assertEqual(self.srv.renames, [])

    def test_refuses_when_the_original_is_gone(self):
        del self.srv.files[FTP]
        with self.assertRaisesRegex(RuntimeError, "gone"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=90)

    def test_refuses_a_stage_on_another_share(self):
        with self.assertRaisesRegex(RuntimeError, "another share"):
            nas_ftp.swap("/volume1/Media/_claude-tmp/k.part", HOST, expect_size=100,
                         expect_mtime=self.t0, new_size=90)

    def test_checks_the_size_after_the_rename(self):
        with self.assertRaisesRegex(RuntimeError, "wrong size"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=91)

    def test_carries_the_mode_over_on_the_stage_before_the_rename(self):
        # the stage's name has no brackets; SITE CHMOD on the release name would glob them
        with mock.patch.object(nas_ftp, "mode_of",
                               side_effect=lambda p: "0777" if p == HOST else "0770"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=90)
        self.assertEqual(self.srv.chmods, [("777", nas_ftp.host_to_ftp(self.STAGE))])
        self.assertEqual(self.srv.modes[FTP], "0777")

    def test_the_same_mode_needs_no_chmod(self):
        with mock.patch.object(nas_ftp, "mode_of", return_value="0777"):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=90)
        self.assertEqual(self.srv.chmods, [])

    def test_an_unknown_mode_changes_nothing(self):
        # Joker's folder lists fine, but a bracketed folder lists empty: no mode, no chmod
        with mock.patch.object(nas_ftp, "mode_of", return_value=None):
            nas_ftp.swap(self.STAGE, HOST, expect_size=100, expect_mtime=self.t0, new_size=90)
        self.assertEqual(self.srv.chmods, [])


class Small(_Net):
    def test_stat_is_size_and_utc_mtime(self):
        self.srv.put(FTP, b"x" * 42, mtime="20261001155717")
        self.assertEqual(nas_ftp.stat(HOST), (42, 1790870237))
        self.assertIsNone(nas_ftp.stat("/volume2/MediaVolume2/Movies/missing.mkv"))

    def test_discard_deletes_the_stage_and_its_empty_folder(self):
        stage = nas_ftp.host_to_ftp(nas_ftp.stage_path_for(HOST))
        self.srv.dirs.add(stage.rsplit("/", 1)[0])
        self.srv.put(stage, b"p")
        nas_ftp.discard_stage(HOST)
        self.assertNotIn(stage, self.srv.files)
        self.assertNotIn(stage.rsplit("/", 1)[0], self.srv.dirs)
        nas_ftp.discard_stage(HOST)                     # nothing staged: fine

    def test_clearing_before_an_upload_keeps_the_staging_folder(self):
        stage = nas_ftp.host_to_ftp(nas_ftp.stage_path_for(HOST))
        folder = stage.rsplit("/", 1)[0]
        self.srv.dirs.add(folder)
        self.srv.put(stage, b"old")
        nas_ftp.discard_stage(HOST, keep_dir=True)
        self.assertNotIn(stage, self.srv.files)
        self.assertIn(folder, self.srv.dirs)

    def test_a_transfer_out_of_attempts_says_what_failed(self):
        data = os.urandom(1000)
        self.srv.put(FTP, data)
        with mock.patch.object(FakeFTP, "transfercmd", side_effect=ftplib.error_perm("553 no")):
            with self.assertRaisesRegex(RuntimeError, "download incomplete — last error: error_perm: 553 no"):
                nas_ftp.download(HOST, os.path.join(self.d, "src.mkv"), len(data))

    def test_the_dv_profile_comes_from_the_head(self):
        self.srv.put(FTP, b"h" * 100)
        with mock.patch.object(nas_ftp.subprocess, "run",
                               return_value=mock.Mock(stdout="7\n", stderr="")) as run:
            self.assertEqual(nas_ftp.remote_dv_profile(HOST), 7)
        self.assertIn("stream_side_data=dv_profile", run.call_args.args[0])

    def test_unreachable_is_false_not_an_error(self):
        with mock.patch.object(nas_ftp, "_connect", side_effect=OSError("no route")):
            self.assertFalse(nas_ftp.reachable())
        self.assertTrue(nas_ftp.reachable())


class Review20261001(_Net):
    """The fixes from the review of the move to FTP (2026-10-01)."""

    ACC_HOST = "/volume2/MediaVolume2/Movies/Pokémon Detective Pikachu (2019) [2160p].mkv"

    def test_paths_go_on_the_wire_as_their_utf8_bytes(self):
        self.assertEqual(nas_ftp.wire("Pokémon").encode("latin-1"), "Pokémon".encode("utf-8"))
        self.assertEqual(nas_ftp.wire("a – b").encode("latin-1"), "a – b".encode("utf-8"))
        self.assertEqual(nas_ftp.wire("/Media/Movies/Plain.mkv"), "/Media/Movies/Plain.mkv")

    def test_an_accented_original_is_found(self):
        # the server matches the UTF-8 bytes on disk: é as the single byte 0xE9 found nothing
        self.srv.put(nas_ftp.wire(nas_ftp.host_to_ftp(self.ACC_HOST)), b"x" * 5)
        self.assertEqual(nas_ftp.stat(self.ACC_HOST)[0], 5)

    def test_an_accented_name_gets_its_mode_from_the_listing(self):
        self.srv.put(nas_ftp.wire(nas_ftp.host_to_ftp(self.ACC_HOST)), b"x", mode="0775")
        self.assertEqual(nas_ftp.mode_of(self.ACC_HOST), "0775")

    def _flaky(self, method, fails, exc):
        orig = getattr(FakeFTP, method)
        n = {"left": fails}
        def f(this, *a, **k):
            if n["left"] > 0:
                n["left"] -= 1
                raise exc
            return orig(this, *a, **k)
        return mock.patch.object(FakeFTP, method, f)

    def test_a_short_exchange_survives_a_blip(self):
        self.srv.put(FTP, b"x" * 7)
        with self._flaky("size", 2, OSError("reset")):
            self.assertEqual(nas_ftp.stat(HOST)[0], 7)
        self.assertEqual([c.args[1] for c in nas_ftp._pause.call_args_list], list(nas_ftp.SHORT_RETRY_WAITS))

    def test_a_refusal_is_an_answer_not_a_blip(self):
        with self._flaky("sendcmd", 1, ftplib.error_perm("550 no")):
            self.srv.put(FTP, b"x")
            with self.assertRaises(ftplib.error_perm):
                nas_ftp.stat(HOST)
        nas_ftp._pause.assert_not_called()

    def test_the_rename_is_never_sent_twice(self):
        self.srv.put(FTP, b"o" * 100, mtime="20250614023105")
        stage = nas_ftp.stage_path_for(HOST)
        self.srv.put(nas_ftp.host_to_ftp(stage), b"n" * 90)
        calls = []
        def rename(this, a, b):
            calls.append(a)
            raise OSError("reply lost")
        with mock.patch.object(FakeFTP, "rename", rename):
            with self.assertRaises(OSError):
                nas_ftp.swap(stage, HOST, expect_size=100,
                             expect_mtime=nas_ftp.mdtm_epoch("213 20250614023105"), new_size=90)
        self.assertEqual(len(calls), 1)

    def test_a_read_back_survives_a_blip(self):
        self.srv.put(FTP, b"abcdef")
        with self._flaky("transfercmd", 1, OSError("reset")):
            self.assertEqual(nas_ftp.read_range(HOST, 2, 3), b"cde")

    def test_an_unreadable_verify_keeps_the_download(self):
        # the copy may be fine: only a byte mismatch throws a download away (review 2026-10-01)
        data = os.urandom(1000)
        self.srv.put(FTP, data)
        local = os.path.join(self.d, "src.mkv")
        with mock.patch.object(nas_ftp, "read_range", side_effect=OSError("NAS gone")):
            with self.assertRaises(OSError):
                nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)

    def test_a_nas_file_that_grew_costs_one_download_not_a_dozen(self):
        data = os.urandom(nas_ftp.SAMPLE_SPAN)
        self.srv.put(FTP, data + b"more bytes appended since it was classified")
        local = os.path.join(self.d, "src.mkv")
        nas_ftp.download(HOST, local, len(data))
        self.assertEqual(_read(local), data)
        self.assertEqual(len([v for v, _r in self.srv.rests if v == "RETR"]),
                         1 + len(nas_ftp.sample_ranges(len(data))))   # one leg, then the read-back

    def test_a_failed_size_check_between_upload_legs_counts_like_a_dropped_leg(self):
        data = os.urandom(nas_ftp.SAMPLE_SPAN)
        local = os.path.join(self.d, "p81.mkv")
        with open(local, "wb") as fh:
            fh.write(data)
        stage = nas_ftp.stage_path_for(HOST)
        with self._flaky("size", 3, OSError("reset")):     # outlasts the short exchange's retries
            nas_ftp.upload(local, stage)
        self.assertEqual(bytes(self.srv.files[nas_ftp.host_to_ftp(stage)]), data)
        self.assertIn(mock.call(None, nas_ftp.FAIL_PAUSE_SECS), nas_ftp._pause.call_args_list)



class OverloadIsNotADroppedLeg(unittest.TestCase):
    """Review 2026-10-06: with the login breaker open, twelve instant NasBusy failures burned a
    transfer's attempts in two minutes and charged the overload to the movie."""

    def test_the_first_overload_ends_the_transfer_uncounted(self):
        legs, pauses = [], []
        def run_leg(have):
            legs.append(have)
            raise transfer.NasBusy("NAS overloaded — a login took 138 s")
        with mock.patch.object(nas_ftp, "_pause", side_effect=lambda *a: pauses.append(a)):
            with self.assertRaises(transfer.NasBusy):
                nas_ftp._legs(run_leg, lambda: 0, 100, None)
        self.assertEqual((len(legs), pauses), (1, []))

    def test_a_short_exchange_does_not_retry_an_open_breaker(self):
        calls = []
        def fn():
            calls.append(1)
            raise transfer.NasBusy("busy")
        with mock.patch.object(nas_ftp, "_pause", side_effect=AssertionError("no 25 s of retries")):
            with self.assertRaises(transfer.NasBusy):
                nas_ftp._retrying(fn)
        self.assertEqual(len(calls), 1)

    def test_an_ordinary_blip_is_still_retried(self):
        seq = iter([OSError("reset"), "ok"])
        def fn():
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v
        with mock.patch.object(nas_ftp, "_pause"):
            self.assertEqual(nas_ftp._retrying(fn), "ok")


class Relinker(unittest.TestCase):
    def _ftp(self, route):
        f = mock.Mock()
        f.visionary_route = route
        return f

    def test_a_link_change_ends_the_leg(self):
        links = iter([{"iface": "en0", "bound": True}, {"iface": "en12", "bound": True}])
        with mock.patch.object(nas_ftp, "link", side_effect=lambda: next(links)), \
             mock.patch.object(nas_ftp, "_lan_offered", return_value=False):
            self.assertTrue(nas_ftp._relinker(self._ftp("lan"))())

    def test_coming_home_moves_a_tailscale_leg_to_the_lan(self):
        offered = iter([False, True])
        with mock.patch.object(nas_ftp, "link", return_value={}), \
             mock.patch.object(nas_ftp, "_lan_offered", side_effect=lambda: next(offered)):
            self.assertTrue(nas_ftp._relinker(self._ftp("tailscale"))())

    def test_a_leg_the_lan_failed_stays_on_tailscale(self):
        with mock.patch.object(nas_ftp, "link", return_value={}), \
             mock.patch.object(nas_ftp, "_lan_offered", return_value=True):
            self.assertFalse(nas_ftp._relinker(self._ftp("tailscale"))())


class EthernetOnlyReasons(unittest.TestCase):
    def test_a_hand_set_host_is_named(self):
        with mock.patch.object(transfer, "_config", return_value={"ftp_host": "1.2.3.4"}):
            self.assertIn("set by hand", nas_ftp._ethernet_only_reason())

    def test_addresses_only_is_named(self):
        with mock.patch.object(transfer, "_config", return_value={}), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["100.101.182.68", "192.168.1.195"]):
            self.assertIn("network name", nas_ftp._ethernet_only_reason())

    def test_the_cable_is_the_default(self):
        with mock.patch.object(transfer, "_config", return_value={}), \
             mock.patch.object(transfer, "ftp_hosts", return_value=["adamsnas.local"]):
            self.assertEqual(nas_ftp._ethernet_only_reason(), "")


class Reasons(unittest.TestCase):
    def test_a_failure_with_no_message_is_named_by_its_type(self):
        import socket
        self.assertEqual(transfer._why(socket.timeout()), "TimeoutError")
        self.assertEqual(transfer._why(EOFError()), "EOFError")
        self.assertEqual(transfer._why(OSError("no route")), "no route")


class NeverFromATest(unittest.TestCase):
    def test_a_test_cannot_open_an_ftp_connection(self):
        with self.assertRaisesRegex(RuntimeError, "mock it"):
            nas_ftp._connect()


class LanOnly(unittest.TestCase):
    """transfer.connect(lan_only=True): the lane's Ethernet only never falls back to Tailscale."""

    def test_no_bound_route_raises_before_any_connection(self):
        with mock.patch.object(transfer, "ftp_hosts", return_value=["100.101.182.68"]), \
             mock.patch.object(transfer, "_route_order", return_value=[("100.101.182.68", None)]), \
             mock.patch.object(transfer, "_WireFTP", side_effect=AssertionError("connected")):
            with self.assertRaises(transfer.NoLanRoute):
                transfer.connect(lan_only=True)

    def test_only_the_bound_route_is_tried(self):
        tried = []
        class F:
            def __init__(self):
                pass
            def connect(self, host, port, timeout=None, source_address=None):
                tried.append(host)
                raise OSError("refused")
        with mock.patch.object(transfer, "ftp_hosts", return_value=["adamsnas.local", "100.101.182.68"]), \
             mock.patch.object(transfer, "_route_order",
                               return_value=[("192.168.1.195", "192.168.1.50"), ("100.101.182.68", None)]), \
             mock.patch.object(transfer, "_WireFTP", F):
            with self.assertRaises(OSError):
                transfer.connect(lan_only=True)
        self.assertEqual(tried, ["192.168.1.195"])


if __name__ == "__main__":
    unittest.main()
