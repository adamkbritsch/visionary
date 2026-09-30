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


class NeverFromATest(unittest.TestCase):
    def test_a_test_cannot_open_an_ssh_connection(self):
        with self.assertRaisesRegex(RuntimeError, "mock it"):
            nas_ssh.ssh_argv()


if __name__ == "__main__":
    unittest.main()
