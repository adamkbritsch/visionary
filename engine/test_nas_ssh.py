"""engine/nas_ssh.py — the path mapping and swap line the profile 7 lane replaces NAS files with, and
the pacing of its transfers. Nothing here touches the network: ssh is never spawned."""
import io
import threading
import unittest
from unittest import mock

import nas_ssh


class Paths(unittest.TestCase):
    def test_ftp_to_host(self):
        self.assertEqual(nas_ssh.ftp_to_host("/Media/Movies/a.mkv"), "/volume1/Media/Movies/a.mkv")
        self.assertEqual(nas_ssh.ftp_to_host("/MediaVolume3/Movies/D (2011)/d.mkv"),
                         "/volume3/MediaVolume3/Movies/D (2011)/d.mkv")
        self.assertIsNone(nas_ssh.ftp_to_host("/Elsewhere/a.mkv"))
        self.assertIsNone(nas_ssh.ftp_to_host("a.mkv"))

    def test_host_to_plex_is_the_containers_view(self):
        self.assertEqual(nas_ssh.host_to_plex("/volume1/Media/Movies"), "/media/Movies")
        self.assertEqual(nas_ssh.host_to_plex("/volume2/MediaVolume2/Movies"), "/media/vol2/Movies")
        self.assertEqual(nas_ssh.host_to_plex("/volume3/MediaVolume3/Movies/Drive (2011)"),
                         "/media/vol3/Movies/Drive (2011)")
        self.assertIsNone(nas_ssh.host_to_plex("/volume2/Media/Movies"))    # share/volume mismatch

    def test_staging_is_on_the_same_share_and_never_a_video_name(self):
        h = "/volume2/MediaVolume2/Movies/x.mkv"
        st = nas_ssh.stage_path_for(h)
        self.assertTrue(st.startswith("/volume2/MediaVolume2/_claude-tmp/"))
        self.assertTrue(st.endswith(".part"))
        self.assertEqual(nas_ssh.share_root(st), nas_ssh.share_root(h))
        self.assertNotEqual(st, nas_ssh.stage_path_for("/volume2/MediaVolume2/Movies/y.mkv"))


class Swap(unittest.TestCase):
    def test_the_container_line_sets_owner_and_mode_then_renames_once(self):
        line = nas_ssh.swap_argv_inner("/volume1/Media/_claude-tmp/k.part",
                                       "/volume1/Media/Movies/It's (2019).mkv", 911, 10, "644")
        self.assertEqual(line.count("mv -f"), 1)
        self.assertIn("chown 911:10 ", line)
        self.assertIn("chmod 644 ", line)
        self.assertLess(line.index("chown"), line.index("mv -f"))
        self.assertIn("'/volume1/Media/Movies/It'\"'\"'s (2019).mkv'", line)    # quoted safely
        self.assertNotIn("--reference", line)           # alpine's busybox has no --reference

    def _stat(self, *values):
        return mock.patch.object(nas_ssh, "stat", side_effect=list(values))

    def test_refuses_when_the_original_changed(self):
        with self._stat((100, 5, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "remote", side_effect=AssertionError("no rename")):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                nas_ssh.swap("/volume1/Media/_claude-tmp/k.part", "/volume1/Media/Movies/a.mkv",
                             expect_size=100, expect_mtime=4, new_size=90)

    def test_refuses_a_stage_on_another_share(self):
        with self._stat((100, 5, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "remote", side_effect=AssertionError("no rename")):
            with self.assertRaisesRegex(RuntimeError, "another share"):
                nas_ssh.swap("/volume2/MediaVolume2/_claude-tmp/k.part",
                             "/volume1/Media/Movies/a.mkv", expect_size=100, expect_mtime=5,
                             new_size=90)

    def test_checks_size_and_ownership_after_the_rename(self):
        with self._stat((100, 5, 911, 10, "644"), (90, 9, 1000, 10, "644")), \
             mock.patch.object(nas_ssh, "remote", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "owner"):
                nas_ssh.swap("/volume1/Media/_claude-tmp/k.part", "/volume1/Media/Movies/a.mkv",
                             expect_size=100, expect_mtime=5, new_size=90)
        with self._stat((100, 5, 911, 10, "644"), (90, 9, 911, 10, "644")), \
             mock.patch.object(nas_ssh, "remote", return_value="") as r:
            nas_ssh.swap("/volume1/Media/_claude-tmp/k.part", "/volume1/Media/Movies/a.mkv",
                         expect_size=100, expect_mtime=5, new_size=90)
        self.assertIn("docker run --rm -v /volume1/Media:/volume1/Media", r.call_args_list[0].args[0])


class Leg(unittest.TestCase):
    def test_moves_everything_and_reports_progress(self):
        src, sink, seen = io.BytesIO(b"x" * (nas_ssh.CHUNK * 2 + 5)), io.BytesIO(), []
        moved = nas_ssh._leg(src, sink, moved0=7, total=99, abort=None, limit=None,
                             on_progress=lambda b, t: seen.append(b))
        self.assertEqual(moved, nas_ssh.CHUNK * 2 + 5)
        self.assertEqual(seen[-1], 7 + moved)

    def test_stops_on_abort(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(nas_ssh.Stopped):
            nas_ssh._leg(io.BytesIO(b"x" * 10), io.BytesIO(), moved0=0, total=10, abort=ev,
                         limit=None, on_progress=None)

    def test_a_cap_paces_the_leg(self):
        naps = []
        with mock.patch.object(nas_ssh.time, "sleep", side_effect=naps.append):
            nas_ssh._leg(io.BytesIO(b"x" * (nas_ssh.CHUNK * 3)), io.BytesIO(), moved0=0,
                         total=0, abort=None, limit=lambda: nas_ssh.CHUNK, on_progress=None)
        self.assertTrue(naps and all(0 < n <= nas_ssh.LIMIT_POLL_SECS for n in naps))


class ConnectionBlips(unittest.TestCase):
    def _r(self, rc, out="", err=""):
        return mock.Mock(returncode=rc, stdout=out, stderr=err)

    def test_a_connection_failure_is_retried_then_succeeds(self):
        runs = [self._r(255, err="Connection timed out during banner exchange"), self._r(0, "ok")]
        with mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "run", side_effect=runs) as run, \
             mock.patch.object(nas_ssh.time, "sleep") as nap:
            self.assertEqual(nas_ssh.remote("stat x"), "ok")
        self.assertEqual(run.call_count, 2)
        nap.assert_called_once_with(nas_ssh.RETRY_WAITS[0])

    def test_a_command_failure_is_not_retried(self):
        with mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "run", return_value=self._r(1, err="no")) as run, \
             mock.patch.object(nas_ssh.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, r"failed \(1\)"):
                nas_ssh.remote("false")
        self.assertEqual(run.call_count, 1)

    def test_it_gives_up_after_the_retries(self):
        with mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "run", return_value=self._r(255)) as run, \
             mock.patch.object(nas_ssh.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, r"failed \(255\)"):
                nas_ssh.remote("stat x")
        self.assertEqual(run.call_count, len(nas_ssh.RETRY_WAITS) + 1)

    def test_the_rename_is_never_repeated(self):
        with mock.patch.object(nas_ssh, "stat", side_effect=[(100, 5, 911, 10, "644")]), \
             mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "run", return_value=self._r(255)) as run, \
             mock.patch.object(nas_ssh.time, "sleep"):
            with self.assertRaises(RuntimeError):
                nas_ssh.swap("/volume1/Media/_claude-tmp/k.part", "/volume1/Media/Movies/a.mkv",
                             expect_size=100, expect_mtime=5, new_size=90)
        self.assertEqual(run.call_count, 1)


class SshCommandLine(unittest.TestCase):
    WIRED = {"iface": "en12", "bound": True, "wired": True, "kind": "Ethernet", "ip": "192.168.1.195"}

    def test_a_wired_link_is_pinned_by_interface_and_address(self):
        a = nas_ssh.ssh_argv_for("adamkbritsch@adamsnas.local", self.WIRED)
        self.assertEqual(a[a.index("-B") + 1], "en12")
        self.assertEqual(a[-1], "adamkbritsch@192.168.1.195")
        self.assertIn("AddressFamily=inet", a)
        self.assertTrue(any(x.startswith("ControlPath=") and x.endswith("-en12") for x in a))

    def test_without_a_wired_link_it_still_refuses_ipv6_but_binds_nothing(self):
        a = nas_ssh.ssh_argv_for("adamkbritsch@adamsnas.local",
                                 {"iface": "utun6", "bound": False, "wired": False, "ip": None})
        self.assertNotIn("-B", a)
        self.assertIn("AddressFamily=inet", a)
        self.assertEqual(a[-1], "adamkbritsch@adamsnas.local")
        self.assertTrue(any(x == "ControlPath=/tmp/visionary-ssh-%r@%h:%p" for x in a))

    def test_wifi_first_binds_the_wifi(self):
        a = nas_ssh.ssh_argv_for("u@adamsnas.local", {"iface": "en0", "bound": True, "wired": False,
                                                       "ip": "192.168.1.195"})
        self.assertEqual(a[a.index("-B") + 1], "en0")

    def test_ethernet_only_without_a_cable_refuses_to_connect(self):
        with self.assertRaises(nas_ssh.NoLink):
            nas_ssh.ssh_argv_for("u@nas", {"iface": None, "bound": False, "unavailable": True})

    def test_no_detection_at_all(self):
        self.assertEqual(nas_ssh.ssh_argv_for("nas", None)[-1], "nas")


class Relink(unittest.TestCase):
    def test_a_changed_link_ends_the_leg(self):
        with self.assertRaises(nas_ssh._Relink):
            nas_ssh._leg(io.BytesIO(b"x" * 10), io.BytesIO(), moved0=0, total=10, abort=None,
                         limit=None, on_progress=None, relink=lambda: True)

    def test_the_key_tracks_interface_availability_and_setting(self):
        a = {"iface": "en12", "bound": True, "priority": "ethernet"}
        self.assertNotEqual(nas_ssh.link_key(a), nas_ssh.link_key(dict(a, iface="en0")))
        # same cable under another setting: the same route, no restart
        self.assertEqual(nas_ssh.link_key(a), nas_ssh.link_key(dict(a, priority="ethernet_only")))
        self.assertNotEqual(nas_ssh.link_key(a), nas_ssh.link_key({"unavailable": True}))
        self.assertEqual(nas_ssh.link_key(a), nas_ssh.link_key(dict(a, speed="2.5 GbE")))

    def test_download_resumes_on_the_new_link_without_pausing(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        local = os.path.join(d, "f")
        links = iter([{"iface": "en0", "bound": True, "priority": "wifi"}] * 3
                     + [{"iface": "en12", "bound": True, "priority": "ethernet"}] * 50)
        procs = []
        class P:
            def __init__(self, data):
                self.stdout = io.BytesIO(data)
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        def popen(argv, **kw):
            n = len(procs)
            procs.append(argv)
            return P(b"a" * 4 if n == 0 else b"b" * 6)
        calls = {"relink": 0}
        orig_leg = nas_ssh._leg
        def leg(src, sink, **kw):
            if len(procs) == 1:                  # the first leg sees the link change mid-way
                sink.write(src.read(4))
                raise nas_ssh._Relink("changed")
            return orig_leg(src, sink, **kw)
        with mock.patch.object(nas_ssh, "link", side_effect=lambda: next(links)), \
             mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(nas_ssh, "_leg", side_effect=leg), \
             mock.patch.object(nas_ssh, "_pause", side_effect=AssertionError("no pause on a relink")), \
             mock.patch.object(nas_ssh, "local_hash", return_value="h"), \
             mock.patch.object(nas_ssh, "remote_hash", return_value="h"):
            nas_ssh.download("/volume1/Media/x.mkv", local, 10)
        self.assertEqual(open(local, "rb").read(), b"aaaabbbbbb")
        self.assertIn("tail -c +5 ", procs[1][-1])              # resumed from the 4 bytes


class UploadAtOffset(unittest.TestCase):
    def test_each_leg_writes_at_its_own_offset_never_appends(self):
        c = nas_ssh.upload_leg_cmd("/volume1/Media/_claude-tmp/k.part", 123456789)
        self.assertIn("seek=123456789", c)
        self.assertIn("oflag=seek_bytes", c)
        self.assertIn("conv=notrunc", c)
        self.assertIn("iflag=fullblock", c)
        self.assertNotIn(">>", c)

    def test_relinks_do_not_use_up_the_attempts(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        local = os.path.join(d, "f")
        n = {"legs": 0}
        class P:
            stdout = None
            def wait(self, timeout=None): return 0
            def kill(self): pass
        def popen(argv, **kw):
            return P()
        def leg(src, sink, **kw):
            n["legs"] += 1
            if n["legs"] <= 20:                  # 20 relinks in a row — more than the 12 attempts
                raise nas_ssh._Relink("flap")
            sink.write(b"z" * 5)
            return 5
        with mock.patch.object(nas_ssh, "link", return_value={}), \
             mock.patch.object(nas_ssh, "ssh_argv", return_value=["ssh"]), \
             mock.patch.object(nas_ssh.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(nas_ssh, "_leg", side_effect=leg), \
             mock.patch.object(nas_ssh, "_pause"), \
             mock.patch.object(nas_ssh, "local_hash", return_value="h"), \
             mock.patch.object(nas_ssh, "remote_hash", return_value="h"):
            nas_ssh.download("/volume1/Media/x.mkv", local, 5)
        self.assertEqual(os.path.getsize(local), 5)


class NeverFromATest(unittest.TestCase):
    def test_a_test_cannot_open_an_ssh_connection(self):
        with self.assertRaisesRegex(RuntimeError, "mock it"):
            nas_ssh.ssh_argv()


if __name__ == "__main__":
    unittest.main()
