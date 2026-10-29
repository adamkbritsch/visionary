"""The helper, Mac side (helper.py). NOTHING here reaches a real host: every Helper gets an
injected `argv` that runs a local FAKE vworker speaking the wire protocol (it decodes the stream
with the real zstd and stands in for x265) — optionally behind a fake login shell that prints junk
first — every route check runs on mocked command output, and status_summary's refresher only ever
asks injected fakes (setUpModule pins _summary_helper to one that refuses)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import dvcap
import helper
import settings

ZSTD_OK = os.path.exists(helper.ZSTD)

# ---- fakes (also used by test_dvcap's end-to-end scheduler test) ------------------------------

FAKE_FFMPEG = r'''
import os, sys, time
args = sys.argv[1:]
n = int(args[args.index("-frames:v") + 1]) if "-frames:v" in args else 10
w, h = int(os.environ.get("FAKE_W", "4")), int(os.environ.get("FAKE_H", "2"))
delay = float(os.environ.get("FAKE_DEC_SLEEP", "0"))
fail_at = int(os.environ.get("FAKE_DEC_FAIL_AT", "-1"))
fs = w * h * 3                      # 4:2:0, 10-bit in 16-bit words
out = sys.stdout.buffer
out.write(b"YUV4MPEG2 W%d H%d F24000:1001 Ip A1:1 C420p10 XYSCSS=420P10\n" % (w, h))
for i in range(n):
    if i == fail_at:
        out.flush()
        sys.exit(1)
    half = fs // 2                  # half noise: zstd streams real video, not runs of zeros
    out.write(b"FRAME\n" + os.urandom(half) + bytes([i % 256]) * (fs - half))
    out.flush()
    if delay:
        time.sleep(delay)
'''

FAKE_X265 = r'''
import re, sys
args = sys.argv[1:]
out = args[args.index("--output") + 1]
inp = sys.stdin.buffer
hdr = inp.readline()
w = int(re.search(rb" W(\d+)", hdr).group(1)); h = int(re.search(rb" H(\d+)", hdr).group(1))
fs, n = w * h * 3, 0
with open(out, "wb") as f:
    while inp.readline():
        if len(inp.read(fs)) < fs:
            break
        n += 1
        f.write(b"F")
        sys.stderr.write("%d frames: 2.90 fps, 100.00 kb/s\r" % n)
sys.stderr.write("\nencoded %d frames in 0.10s (2.90 fps), 100.00 kb/s, Avg QP:20.00\n" % n)
'''

# one access-unit delimiter NAL (4-byte start code, type 35): the fake helper's "frame"
AUD = b"\x00\x00\x00\x01\x46\x01\x50"

FAKE_VWORKER = r'''
import base64, json, os, re, shutil, subprocess, sys, time
MAGIC, AUD = b"VWHEVC1\n", b"\x00\x00\x00\x01\x46\x01\x50"
root, mode, verb, args = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
jobs = os.path.join(root, "jobs")
kind, _sep, at = mode.partition(":")
at = int(at) if at else None

def log(name, obj):
    with open(os.path.join(root, name), "a") as f:
        f.write(json.dumps(obj) + "\n")

def final(result, why, frames, rc, code=None):
    rec = {"result": result, "why": why, "frames": frames}
    if code:
        rec["code"] = code
    sys.stderr.write("\nVWORKER " + json.dumps(rec) + "\n")
    sys.stderr.flush()
    sys.exit(rc)

if verb == "status":
    log("calls.log", {"verb": "status"})
    p = os.path.join(root, "status.json")
    print("Welcome to Bazzite")          # a login banner line before the JSON
    print(open(p).read().strip() if os.path.exists(p) else json.dumps(
        {"ok": True, "why": "", "ac": True, "ethernet": True, "game": None, "heavy": None,
         "free_gb": 400.0, "busy": False, "x265": "4.1+1-1d117be", "version": 1}))
    sys.exit(0)
if verb == "kill":                       # vworker kill [<job>]
    job = args[0] if args else None
    log("calls.log", {"verb": "kill", "job": job})
    shutil.rmtree(os.path.join(jobs, job) if job else jobs, ignore_errors=True)
    open(os.path.join(root, "killed"), "w").close()
    sys.exit(0)
if verb == "put":                        # vworker put <job> <name> <bytes>
    job, name, nbytes = args
    if kind == "put255":
        sys.exit(255)
    if kind == "putfail":
        sys.exit(1)
    if kind == "puthang":
        time.sleep(30)
        sys.exit(1)
    data = sys.stdin.buffer.read()
    log("calls.log", {"verb": "put", "job": job, "name": name, "bytes": int(nbytes), "got": len(data)})
    if len(data) != int(nbytes):
        print(json.dumps({"ok": False, "why": "short read"}))
        sys.exit(1)
    d = os.path.join(jobs, job)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(data)
    if kind != "putsilent":              # putsilent: exit 0 with no answer at all
        print(json.dumps({"ok": True, "bytes": len(data) + (1 if kind == "putlie" else 0), "sha256": "x"}))
    sys.exit(0)
if verb != "encode":
    sys.exit(1)
job, b64 = args
spec = json.loads(base64.b64decode(b64))
d = os.path.join(jobs, job)
os.makedirs(d, exist_ok=True)
rpu = os.path.join(d, "rpu.bin")
log("encode.log", {"job": job, "spec": spec,
                   "rpu": open(rpu, "rb").read().decode("latin-1") if os.path.exists(rpu) else None})
try:
    if kind == "refuse":
        final("refused", "game running: Test Game", 0, 77, "game")
    if kind == "refuse_nocode":          # an older vworker: no reason code on its line
        final("refused", "game running: Test Game", 0, 77)
    if kind == "die255":
        sys.exit(255)
    if kind == "stall":                  # the link is up but nothing moves: no progress, no HEVC
        time.sleep(30)
        sys.exit(76)
    z = subprocess.Popen(["/opt/homebrew/bin/zstd", "-d", "-q", "-c"], stdin=sys.stdin,
                         stdout=subprocess.PIPE)
    hdr = z.stdout.readline()
    w = int(re.search(rb" W(\d+)", hdr).group(1)); h = int(re.search(rb" H(\d+)", hdr).group(1))
    fs, n, out = w * h * 3, 0, sys.stdout.buffer
    if kind != "nomagic":                # nomagic: the bitstream with no stream marker before it
        out.write(MAGIC); out.flush()
    while z.stdout.readline():
        if len(z.stdout.read(fs)) < fs:
            break
        n += 1
        out.write(b"F" if kind == "badnal" else AUD); out.flush()   # badnal: not HEVC at all
        sys.stderr.write("%d frames: 8.06 fps, 100.00 kb/s\r" % n); sys.stderr.flush()
        if at is not None and n >= at:
            z.kill()
            if kind == "yield":
                final("yielded", "heavy: TestApp", n, 75, "heavy")
            if kind == "disconnect":
                final("disconnected", "parent sshd gone", n, 76)
            final("error", "x265 crashed", n, 1)
    z.wait()
    if n != spec["frames"]:
        final("disconnected", "stdin ended after %d of %d frames" % (n, spec["frames"]), n, 76)
    sys.stderr.write("\nencoded %d frames in 0.10s (8.06 fps), 100.00 kb/s, Avg QP:20.00\n" % n)
    final("ok", "", n - 1 if kind == "short" else n, 0)
finally:
    shutil.rmtree(d, ignore_errors=True)
'''


def write_fakes(dirpath: str) -> dict:
    """The three fake programs as executable files in `dirpath` (each also runs as
    `python3 <path>`): {"ffmpeg", "x265", "vworker"} -> path."""
    paths = {}
    for name, src in (("ffmpeg", FAKE_FFMPEG), ("x265", FAKE_X265), ("vworker", FAKE_VWORKER)):
        p = os.path.join(dirpath, f"fake_{name}.py")
        with open(p, "w") as f:
            f.write(f"#!{sys.executable}\n" + src)
        os.chmod(p, 0o755)
        paths[name] = p
    return paths


X265_TOKEN = "4.1+1-1d117be"            # the fakes' x265 build, injected as "this Mac's" too
ROUTE = (True, "wired via en12 (192.168.1.133)", "192.168.1.133", "en12")


# what a chatty login shell prints before running the command: a motd, a near-miss of the
# stream marker and bytes that look like the start of an HEVC stream — all of it on STDOUT
SHELL_JUNK = "Last login: Tue Oct 28\\nWelcome to Bazzite\\n\\000\\000\\000\\001\\106\\001VWHEVC0\\nVWHEV"


def fake_argv(vworker: str, root: str, mode: str = "ok", junk: str | None = None):
    """An ssh_argv stand-in: `vworker <remote...>` runs the local fake with its state in root.
    With `junk` (printf format) it runs through /bin/sh like a real ssh login, which prints that
    on stdout (and a line on stderr) before exec'ing vworker."""
    if junk is None:
        return lambda *remote: [sys.executable, vworker, root, mode, *map(str, remote)]
    script = 'printf "%s"; echo "stty: stdin is not a tty" >&2; exec "$@"' % junk
    return lambda *remote: ["/bin/sh", "-c", script, "sh", sys.executable, vworker, root, mode,
                            *map(str, remote)]


def fake_helper(vworker: str, root: str, mode: str = "ok", junk: str | None = None,
                **kw) -> helper.Helper:
    kw.setdefault("enabled", lambda: True)
    kw.setdefault("route_ok", lambda host: ROUTE)
    kw.setdefault("local_x265", lambda: X265_TOKEN)
    return helper.Helper("ally.test", "bazzite", "/nonexistent/key",
                         argv=fake_argv(vworker, root, mode, junk), **kw)


class _RefusingHelper:
    """status_summary's refresher Helper in every test that does not inject its own: never
    reaches anything."""

    def __init__(self, vals):
        self._id = (vals["helper_host"], vals["helper_user"], vals["helper_key"])
        self._route = None

    def available(self):
        return self.availability()[:2]

    def availability(self):
        return False, "test: no helper", "unreachable"


_NO_REAL_REFRESH = mock.patch.object(helper, "_summary_helper", _RefusingHelper)


def setUpModule():
    _NO_REAL_REFRESH.start()


def tearDownModule():
    helper.reset_state()
    _NO_REAL_REFRESH.stop()


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ---- the ssh command line ---------------------------------------------------------------------

class SshArgv(unittest.TestCase):
    def test_the_protocol_command_line(self):
        # D1: the CHECKED IPv4 (-4), the host only as the known_hosts alias, bound to the wired
        # interface, only the configured key, and nothing from ssh_config can multiplex/jump/tty
        argv = helper.ssh_argv("bazzite.local", "bazzite", "~/.ssh/id_ed25519_ally_claude",
                               "192.168.1.133", "en12", "encode", "abc123", "eyJ4Ijox+/=")
        self.assertEqual(argv, [
            "/usr/bin/ssh", "-4", "-i", os.path.expanduser("~/.ssh/id_ed25519_ally_claude"),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
            "-o", "HostKeyAlias=bazzite.local", "-o", "BindInterface=en12",
            "-o", "IdentitiesOnly=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ProxyJump=none", "-o", "RequestTTY=no",
            "bazzite@192.168.1.133",
            "~/visionary-worker/bin/vworker", "encode", "abc123", "eyJ4Ijox+/="])

    def test_the_put_command_carries_the_byte_count(self):
        argv = helper.ssh_argv("h", "u", "/k", "10.0.0.2", "en5", "put", "job1", "rpu.bin", 4096)
        self.assertEqual(argv[-5:], [helper.VWORKER, "put", "job1", "rpu.bin", "4096"])

    def test_an_unsafe_remote_argument_is_quoted(self):
        self.assertEqual(helper.ssh_argv("h", "u", "/k", "10.0.0.2", "en5", "a b;rm")[-1], "'a b;rm'")

    def test_argv_is_none_until_a_route_was_checked_and_follows_the_check(self):
        verdict = [(True, "wired via en12 (192.168.1.133)", "192.168.1.133", "en12")]
        h = helper.Helper("bazzite.local", "bazzite", "~/.ssh/k", route_ok=lambda host: verdict[0])
        self.assertIsNone(h.argv("status"))                # nothing over an unchecked route
        self.assertEqual(h.check_route(), (True, "wired via en12 (192.168.1.133)"))
        argv = h.argv("status")
        self.assertIn("bazzite@192.168.1.133", argv)
        self.assertIn("BindInterface=en12", argv)
        self.assertIn("HostKeyAlias=bazzite.local", argv)
        verdict[0] = (False, "the route to bazzite.local is Wi-Fi (en0)", "192.168.1.133", "en0")
        self.assertFalse(h.check_route()[0])
        self.assertIsNone(h.argv("status"))                # a failed check leaves no route

    def test_an_ok_verdict_without_address_or_interface_is_refused(self):
        for r in ((True, "wired"), (True, "wired", None, "en12"), (True, "wired", "10.0.0.2", "")):
            h = helper.Helper("h", "u", "/k", route_ok=lambda host, _r=r: _r)
            ok, why = h.check_route()
            self.assertFalse(ok, r)
            self.assertIn("no address and interface", why)
            self.assertIsNone(h.argv("status"))


# ---- is the route wired? ----------------------------------------------------------------------

ROUTE_EN12 = """   route to: 192.168.1.20
destination: 192.168.1.20
  interface: en12
      flags: <UP,HOST,DONE,LLINFO,WASCLONED,IFSCOPE,IFREF>
"""
ROUTE_EN0 = ROUTE_EN12.replace("en12", "en0")
ROUTE_UTUN = ROUTE_EN12.replace("en12", "utun4")
PORTS = """
Hardware Port: Ethernet Adapter (en4)
Device: en4
Ethernet Address: ba:da:d1:5f:f4:27

Hardware Port: USB 10/100/1G/2.5G/5G LAN
Device: en12
Ethernet Address: 6c:1f:f7:26:b4:3a

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 3c:a6:f6:00:00:01

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 36:dd:81:74:6b:80

VLAN Configurations
===================
"""


class WiredRoute(unittest.TestCase):
    def setUp(self):
        helper.reset_state()

    def _runner(self, route_out, ports_out=PORTS, route_rc=0, ports_rc=0):
        calls = []

        def run(cmd, timeout):
            calls.append(cmd)
            if cmd[0] == helper.ROUTE:
                return route_rc, route_out, ""
            if cmd[0] == helper.NETWORKSETUP:
                return ports_rc, ports_out, ""
            raise AssertionError(f"unexpected command {cmd}")
        return run, calls

    def test_parsers(self):
        self.assertEqual(helper.parse_route_interface(ROUTE_EN12), "en12")
        self.assertIsNone(helper.parse_route_interface("route: writing to routing socket: not in table"))
        self.assertEqual(helper.parse_wifi_device(PORTS), "en0")
        self.assertIsNone(helper.parse_wifi_device(PORTS.replace("Wi-Fi", "Wi-Fo")))

    def test_a_wired_usb_ethernet_route_is_ok(self):
        run, calls = self._runner(ROUTE_EN12)
        ok, why, ip, iface, code = helper.wired_route_ok("bazzite.local", run=run,
                                                         resolve=lambda h: "192.168.1.20")
        self.assertTrue(ok, why)
        self.assertIn("en12", why)
        self.assertEqual((ip, iface), ("192.168.1.20", "en12"))   # what the ssh connects over
        self.assertEqual(code, "wired")
        self.assertEqual(calls[0], [helper.ROUTE, "-n", "get", "192.168.1.20"])

    def test_a_wifi_route_is_refused(self):
        run, _calls = self._runner(ROUTE_EN0)
        ok, why, _ip, _iface, code = helper.wired_route_ok("bazzite.local", run=run,
                                                           resolve=lambda h: "192.168.1.20")
        self.assertFalse(ok)
        self.assertIn("Wi-Fi", why)
        self.assertEqual(code, "wifi")

    def test_a_tunnel_or_unknown_route_is_refused(self):
        for route_out, rc in ((ROUTE_UTUN, 0), ("", 1)):
            helper.reset_state()
            run, _c = self._runner(route_out, route_rc=rc)
            ok, _why, _ip, _iface, code = helper.wired_route_ok("h", run=run,
                                                                resolve=lambda h: "10.0.0.2")
            self.assertFalse(ok, route_out)
            self.assertEqual(code, "not_wired" if rc == 0 else "unreachable", route_out)

    def test_unlisted_ports_or_no_address_is_refused(self):
        run, _c = self._runner(ROUTE_EN12, ports_rc=1)
        r = helper.wired_route_ok("h", run=run, resolve=lambda h: "10.0.0.2")
        self.assertEqual((r[0], r[4]), (False, "route_check"))
        helper.reset_state()
        r = helper.wired_route_ok("h", run=run, resolve=lambda h: None)
        self.assertEqual((r[0], r[4]), (False, "unreachable"))

    def test_a_mac_without_wifi_is_judged_by_the_interface(self):
        run, _c = self._runner(ROUTE_EN12, ports_out=PORTS.replace("Wi-Fi", "Ethernet X"))
        self.assertTrue(helper.wired_route_ok("h", run=run, resolve=lambda h: "10.0.0.2")[0])

    def test_the_verdict_is_never_cached(self):
        # D2: a cached verdict could outlive a pulled cable — every call runs the check again
        out = [ROUTE_EN12]
        calls = []

        def run(cmd, timeout):
            calls.append(cmd)
            return 0, (out[0] if cmd[0] == helper.ROUTE else PORTS), ""
        self.assertTrue(helper.wired_route_ok("h", run=run, resolve=lambda h: "10.0.0.2")[0])
        n = len(calls)
        self.assertTrue(helper.wired_route_ok("h", run=run, resolve=lambda h: "10.0.0.2")[0])
        self.assertEqual(len(calls), 2 * n)
        out[0] = ROUTE_EN0                                 # the cable is pulled: Wi-Fi takes over
        self.assertFalse(helper.wired_route_ok("h", run=run, resolve=lambda h: "10.0.0.2")[0])

    def test_an_ip_literal_is_not_resolved(self):
        self.assertEqual(helper._resolve_ipv4("192.168.1.20"), "192.168.1.20")

    def test_resolution_asks_for_ipv4_only(self):
        # bazzite.local resolved to an IPv6 link-local that rode Wi-Fi (55 Mbps): AF_INET only
        with mock.patch.object(helper.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("192.168.1.133", 0))]) as gai:
            self.assertEqual(helper._resolve_ipv4("bazzite.local"), "192.168.1.133")
        self.assertEqual(gai.call_args[0][:3], ("bazzite.local", None, helper.socket.AF_INET))
        with mock.patch.object(helper.socket, "getaddrinfo", side_effect=OSError("nope")):
            self.assertIsNone(helper._resolve_ipv4("bazzite.local"))

    def test_a_hanging_resolver_is_bounded(self):
        gate = threading.Event()
        with mock.patch.object(helper.socket, "getaddrinfo", side_effect=lambda *a: gate.wait(5)):
            t0 = time.monotonic()
            self.assertIsNone(helper._resolve_ipv4("bazzite.local", timeout=0.2))
            self.assertLess(time.monotonic() - t0, 2.0)
        gate.set()

    def test_never_raises(self):
        def boom(cmd, timeout):
            raise RuntimeError("no")
        ok, why, ip, iface, code = helper.wired_route_ok("h", run=boom, resolve=lambda h: "10.0.0.2")
        self.assertFalse(ok)
        self.assertIn("failed", why)
        self.assertEqual((ip, iface, code), (None, None, "route_check"))


# ---- this Mac's x265 build (D5) ----------------------------------------------------------------

X265_VERSION_OUT = """x265 [info]: HEVC encoder version 4.1+1-1d117be
x265 [info]: build info [Mac OS X][clang 16.0.0][64 bit] 8bit+10bit+12bit
x265 [info]: using cpu capabilities: NEON Neon_DotProd Neon_I8MM
"""


class LocalX265(unittest.TestCase):
    def setUp(self):
        helper.reset_state()

    def tearDown(self):
        helper.reset_state()

    def test_the_token_is_the_first_lines_version(self):
        self.assertEqual(helper.parse_x265_version(X265_VERSION_OUT), "4.1+1-1d117be")
        self.assertEqual(helper.parse_x265_version("x265 [info]: build info version 9\n"), "9")
        self.assertEqual(helper.parse_x265_version("garbage\nx265 [info]: HEVC encoder version 4.1"), "")
        self.assertEqual(helper.parse_x265_version(""), "")

    def test_read_from_stderr_once_per_process(self):
        calls = []

        def run(cmd, timeout):
            calls.append(cmd)
            return 0, "", X265_VERSION_OUT                 # x265 logs on stderr
        self.assertEqual(helper.local_x265_version("/x/x265", run=run), "4.1+1-1d117be")
        self.assertEqual(helper.local_x265_version("/x/x265", run=run), "4.1+1-1d117be")
        self.assertEqual(calls, [["/x/x265", "--version"]])

    def test_a_failure_is_empty_and_not_cached(self):
        answers = [(None, "", "no such file"), (1, "", X265_VERSION_OUT), (0, "", X265_VERSION_OUT)]
        run = lambda cmd, timeout: answers.pop(0)          # noqa: E731
        self.assertEqual(helper.local_x265_version("/x/x265", run=run), "")
        self.assertEqual(helper.local_x265_version("/x/x265", run=run), "")
        self.assertEqual(helper.local_x265_version("/x/x265", run=run), "4.1+1-1d117be")

    def test_the_default_is_dvcaps_x265(self):
        seen = []
        helper.local_x265_version(run=lambda cmd, timeout: seen.append(cmd) or (0, "", X265_VERSION_OUT))
        self.assertEqual(seen, [[dvcap.X265, "--version"]])


# ---- status / availability -----------------------------------------------------------------

@unittest.skipUnless(ZSTD_OK, "needs /opt/homebrew/bin/zstd")
class FakeRemoteCase(unittest.TestCase):
    def setUp(self):
        helper.reset_state()
        self.td = tempfile.mkdtemp()
        self.fakes = write_fakes(self.td)
        self.root = os.path.join(self.td, "ally")
        os.makedirs(self.root)
        self.env = mock.patch.dict(os.environ, {"FAKE_W": "4", "FAKE_H": "2", "FAKE_DEC_SLEEP": "0",
                                                "FAKE_DEC_FAIL_AT": "-1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        dvcap.resume_encoders()
        shutil.rmtree(self.td, ignore_errors=True)
        helper.reset_state()

    def helper(self, mode="ok", **kw):
        return fake_helper(self.fakes["vworker"], self.root, mode, **kw)

    def calls(self, name="calls.log"):
        p = os.path.join(self.root, name)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f]

    def set_status(self, **fields):
        base = {"ok": True, "why": "", "ac": True, "ethernet": True, "game": None, "heavy": None,
                "free_gb": 400.0, "busy": False, "x265": X265_TOKEN, "version": 1}
        base.update(fields)
        with open(os.path.join(self.root, "status.json"), "w") as f:
            json.dump(base, f)

    def counting_route(self, verdict=ROUTE):
        """A route_ok that counts its calls; `box["v"]` is the verdict it answers."""
        box = {"n": 0, "v": verdict}

        def route_ok(host):
            box["n"] += 1
            return box["v"]
        return box, route_ok


class StatusAndAvailability(FakeRemoteCase):
    def checked(self, **kw):
        h = self.helper(**kw)
        self.assertTrue(h.check_route()[0])
        return h

    def test_status_reads_the_last_json_line(self):
        st = self.checked().status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["version"], 1)

    def test_status_is_none_on_garbage_or_no_answer(self):
        with mock.patch.object(helper, "_run", return_value=(0, "not json\n", "")):
            self.assertIsNone(self.checked().status())
        with mock.patch.object(helper, "_run", return_value=(None, "", "timed out")):
            self.assertIsNone(self.checked().status())

    def test_status_is_never_asked_over_an_unchecked_route(self):
        with mock.patch.object(helper, "_run") as run:
            self.assertIsNone(self.helper().status())
        run.assert_not_called()

    def test_status_uses_its_timeout(self):
        with mock.patch.object(helper, "_run", return_value=(255, "", "")) as run:
            self.checked().status()
        self.assertEqual(run.call_args[0][1], helper.STATUS_TIMEOUT)

    def test_the_route_is_checked_on_every_call_even_with_a_cached_ok(self):
        # D2: only the ssh status probe is cached; the route check runs every time
        box, route_ok = self.counting_route()
        h = self.helper(route_ok=route_ok)
        for _ in range(3):
            self.assertEqual(h.available(), (True, "ready"))
        self.assertEqual(box["n"], 3)
        self.assertEqual(len(self.calls()), 1)             # one status ssh for all three
        box["v"] = (False, "the route to ally.test is Wi-Fi (en0)", "192.168.1.133", "en0")
        ok, why = h.available()                            # the cached ok does not outlive it
        self.assertFalse(ok)
        self.assertIn("Wi-Fi", why)
        self.assertEqual(len(self.calls()), 1)

    def test_the_protocol_version_must_be_one(self):
        for v in (2, 0, "1", True, None):
            helper.reset_state()
            self.set_status(version=v)
            ok, why = self.helper().available()
            self.assertFalse(ok, v)
            self.assertIn("protocol", why)
        helper.reset_state()
        with open(os.path.join(self.root, "status.json"), "w") as f:
            json.dump({"ok": True, "why": "", "busy": False, "x265": X265_TOKEN}, f)
        self.assertFalse(self.helper().available()[0])     # no version at all: refused too

    def test_the_x265_build_must_be_this_macs(self):
        self.set_status(x265="4.1")                        # an older/other build: not bit-identical
        ok, why = self.helper().available()
        self.assertFalse(ok)
        self.assertIn("x265 builds differ", why)
        helper.reset_state()
        self.set_status()
        ok, why = self.helper(local_x265=lambda: "").available()
        self.assertEqual((ok, why), (False, "cannot read this Mac's x265 version"))
        helper.reset_state()
        self.assertEqual(self.helper().available(), (True, "ready"))

    def test_the_x265_gate_reads_this_macs_real_token_by_default(self):
        seen = []

        def fake_run(cmd, timeout):
            if cmd[-1] == "--version":
                seen.append(cmd)
                return 0, "", "x265 [info]: HEVC encoder version 4.1+1-1d117be\n"
            return _real_run(cmd, timeout)
        _real_run = helper._run
        h = helper.Helper("ally.test", "bazzite", "/nonexistent/key",
                          argv=fake_argv(self.fakes["vworker"], self.root), enabled=lambda: True,
                          route_ok=lambda host: ROUTE)
        with mock.patch.object(helper, "_run", side_effect=fake_run):
            self.assertEqual(h.available(), (True, "ready"))
        self.assertEqual(seen, [[dvcap.X265, "--version"]])

    def test_a_busy_answer_is_never_cached(self):
        # another machine's (or another process's) job: free again the moment it ends
        self.set_status(ok=False, why="busy: job abc running", busy=True)
        h = self.helper()
        self.assertFalse(h.available()[0])
        self.assertFalse(h.available()[0])
        self.assertEqual(len(self.calls()), 2)             # asked again, not served from cache
        self.set_status()
        self.assertEqual(h.available(), (True, "ready"))

    def test_a_heavy_only_refusal_is_never_cached(self):
        # one 1-s sample read heavy: ask again next time instead of sitting out AVAIL_TTL
        for code in ("heavy", None):                       # None: an older vworker sends no code
            helper.reset_state()
            fields = dict(ok=False, why="busy: GPU card0 90% busy", heavy="GPU card0 90% busy")
            if code:
                fields["code"] = code
            self.set_status(**fields)
            os.path.exists(os.path.join(self.root, "calls.log")) and \
                os.remove(os.path.join(self.root, "calls.log"))
            h = self.helper()
            self.assertEqual(h.availability(), (False, "busy: GPU card0 90% busy", "heavy"))
            self.assertFalse(h.available()[0])
            self.assertEqual(len(self.calls()), 2, code)   # asked again, not served from cache
            self.set_status()
            self.assertEqual(h.available(), (True, "ready"))

    def test_heavy_with_anything_else_wrong_is_cached_as_usual(self):
        self.set_status(ok=False, why="on battery", ac=False, heavy="GPU card0 90% busy",
                        code="battery")
        h = self.helper()
        self.assertEqual(h.availability(), (False, "on battery", "battery"))
        self.assertFalse(h.available()[0])
        self.assertEqual(len(self.calls()), 1)             # a real refusal: cached

    def test_this_process_holding_the_slot_is_busy_without_a_probe(self):
        a, b = self.helper(), self.helper()                # two lanes, one Ally
        self.assertTrue(a.claim())
        try:
            self.assertEqual(b.available(), (False, "busy: another remux is using the helper"))
            self.assertEqual(self.calls(), [])             # no ssh, nothing cached
        finally:
            a.release()
        self.assertEqual(b.available(), (True, "ready"))
        self.assertEqual(len(self.calls()), 1)

    def test_available_needs_the_switch_the_route_and_an_ok_status(self):
        self.assertEqual(self.helper(enabled=lambda: False).available(), (False, "helper is off"))
        helper.reset_state()
        ok, why = self.helper(route_ok=lambda h: (False, "the route to h is Wi-Fi (en0)")).available()
        self.assertFalse(ok)
        self.assertIn("Wi-Fi", why)
        self.assertEqual(self.calls(), [])                 # no route, no ssh at all
        helper.reset_state()
        self.set_status(ok=False, why="on battery", ac=False)
        self.assertEqual(self.helper().available(), (False, "on battery"))
        helper.reset_state()
        self.set_status()
        self.assertEqual(self.helper().available(), (True, "ready"))

    def test_available_is_cached_for_a_minute(self):
        t = [500.0]
        h = self.helper(clock=lambda: t[0])
        self.assertTrue(h.available()[0])
        self.assertTrue(h.available()[0])
        self.assertEqual(len(self.calls()), 1)             # one status ssh for both
        t[0] += helper.AVAIL_TTL + 1
        self.assertTrue(h.available()[0])
        self.assertEqual(len(self.calls()), 2)

    def test_the_switch_is_live_even_with_a_cached_ok(self):
        on = [True]
        h = self.helper(enabled=lambda: on[0])
        self.assertTrue(h.available()[0])
        on[0] = False
        self.assertEqual(h.available(), (False, "helper is off"))

    def test_a_yield_cools_down_for_five_minutes(self):
        t = [100.0]
        h = self.helper(clock=lambda: t[0])
        self.assertTrue(h.available()[0])
        h._note("yielded")
        t[0] += helper.COOLDOWN - 1
        self.assertEqual(h.available(), (False, "cooling down"))
        t[0] += 2
        self.assertTrue(h.available()[0])

    def test_a_refusal_cools_down_like_a_yield(self):
        # D4: "refused" (a game or a heavy app on the helper) behaves exactly like "yielded"
        t = [100.0]
        h = self.helper(clock=lambda: t[0])
        self.assertTrue(h.available()[0])
        h._note("refused")
        t[0] += helper.COOLDOWN - 1
        self.assertEqual(h.available(), (False, "cooling down"))
        t[0] += 2
        self.assertTrue(h.available()[0])

    def test_a_lost_link_or_error_asks_again_next_time(self):
        h = self.helper()
        self.assertTrue(h.available()[0])
        for kind in ("disconnected", "error"):
            h._note(kind)
            self.assertTrue(h.available()[0])              # no cooldown
        self.assertEqual(len(self.calls()), 3)             # re-asked after each

    def test_ok_aborted_and_suspended_change_nothing(self):
        h = self.helper()
        self.assertTrue(h.available()[0])
        for kind in ("ok", "aborted", "suspended"):
            h._note(kind)
            self.assertEqual(h.available(), (True, "ready"))
        self.assertEqual(len(self.calls()), 1)             # still the cached answer, no cooldown

    def test_the_cache_is_shared_by_every_helper_for_the_same_ally(self):
        self.helper().available()
        self.helper().available()                          # a second lane's helper
        self.assertEqual(len(self.calls()), 1)

    def test_one_job_slot_per_ally(self):
        a, b = self.helper(), self.helper()
        self.assertTrue(a.claim())
        self.assertFalse(b.claim())
        a.release()
        self.assertTrue(b.claim())
        b.release()
        b.release()                                        # a stray release is harmless


# ---- one segment over the fake wire ----------------------------------------------------------

class EncodeSegment(FakeRemoteCase):
    ARGS = dvcap.build_x265_command("X265", "{rpu}", "{out}", 50, None, "1000,400")[1:]

    def run_seg(self, mode="ok", frames=20, rpu=b"RPU-SLICE", abort=None, on_frame=None, h=None):
        rpu_path = None
        if rpu is not None:
            rpu_path = os.path.join(self.td, "seg_0003.remote.rpu")
            with open(rpu_path, "wb") as f:
                f.write(rpu)
        part = os.path.join(self.td, "seg_0003.hevc.remote.part")
        dec = [sys.executable, self.fakes["ffmpeg"], "-frames:v", str(frames)]
        res = (h or self.helper(mode)).encode_segment(dec, self.ARGS, rpu_path, frames, part,
                                                      abort, on_frame)
        return res, part

    def assert_clean(self):
        self.assertEqual(len(dvcap._LIVE_ENCODERS), 0)     # nothing registered, stopped or running
        self.assertEqual(len(dvcap._LIVE_REMOTE), 0)
        jobs = os.path.join(self.root, "jobs")
        self.assertEqual(os.listdir(jobs) if os.path.isdir(jobs) else [], [])

    def kills(self):
        return [c.get("job") for c in self.calls() if c["verb"] == "kill"]

    def jobs_put(self):
        return [c["job"] for c in self.calls() if c["verb"] == "put"]

    def test_success_streams_the_hevc_back(self):
        ticks = []
        (got, why, kind), part = self.run_seg(on_frame=ticks.append)
        self.assertEqual((got, why, kind), (20, "ok", "ok"))
        with open(part, "rb") as f:
            self.assertEqual(f.read(), AUD * 20)              # the marker is not in the part
        self.assertEqual(ticks, sorted(ticks))
        self.assertEqual(ticks[-1], 20)                    # x265 progress, \r-separated, parsed
        put = [c for c in self.calls() if c["verb"] == "put"]
        enc = self.calls("encode.log")
        self.assertEqual(len(put), 1)
        self.assertEqual(put[0]["name"], "rpu.bin")
        self.assertEqual((put[0]["bytes"], put[0]["got"]), (9, 9))   # D6: the byte count is sent
        self.assertEqual(self.kills(), [])                 # a clean job needs no kill
        self.assertEqual(enc[0]["job"], put[0]["job"])     # the RPU went to the same job
        self.assertEqual(enc[0]["rpu"], "RPU-SLICE")
        self.assertEqual(enc[0]["spec"], {"x265_args": self.ARGS, "frames": 20})
        self.assertIn("{rpu}", enc[0]["spec"]["x265_args"])
        self.assertIn("{out}", enc[0]["spec"]["x265_args"])
        self.assert_clean()

    def test_sdr_uploads_no_rpu(self):
        (got, _why, kind), _part = self.run_seg(rpu=None)
        self.assertEqual((got, kind), (20, "ok"))
        self.assertEqual([c for c in self.calls() if c["verb"] == "put"], [])
        self.assertIsNone(self.calls("encode.log")[0]["rpu"])

    def test_a_yield_is_yielded_and_cools_down(self):
        h = self.helper("yield:5")
        (got, why, kind), part = self.run_seg(h=h)
        self.assertEqual((got, kind), (None, "yielded"))
        self.assertIn("heavy", why)
        self.assertFalse(os.path.exists(part))
        self.assertEqual(h.available(), (False, "cooling down"))
        self.assert_clean()

    def test_lost_links_are_disconnected(self):
        for mode in ("disconnect:5", "die255"):
            helper.reset_state()
            (got, _why, kind), part = self.run_seg(mode)
            self.assertEqual((got, kind), (None, "disconnected"), mode)
            self.assertFalse(os.path.exists(part))
        self.assert_clean()

    def test_no_vworker_line_after_a_put_kills_the_job(self):
        # D6: the put succeeded, then the stream died before any VWORKER line -> `vworker kill <job>`
        (got, _why, kind), _part = self.run_seg("die255")
        self.assertEqual((got, kind), (None, "disconnected"))
        self.assertEqual(self.kills(), self.jobs_put())
        self.assertEqual(len(self.kills()), 1)
        self.assert_clean()

    def test_an_sdr_stream_that_dies_unanswered_kills_the_job_too(self):
        (got, _why, kind), _part = self.run_seg("die255", rpu=None)
        self.assertEqual((got, kind), (None, "disconnected"))
        self.assertEqual(len(self.kills()), 1)
        self.assertEqual(self.kills()[0], self.calls("encode.log")[0]["job"])

    def test_an_answered_failure_needs_no_kill(self):
        # the VWORKER line arrived: the Ally already stopped and deleted the job itself
        for mode in ("refuse", "yield:3", "disconnect:3", "error:3"):
            helper.reset_state()
            (got, _why, _kind), _part = self.run_seg(mode)
            self.assertIsNone(got, mode)
        self.assertEqual(self.kills(), [])
        self.assert_clean()

    def test_a_refusal_is_refused(self):
        (got, why, kind), _part = self.run_seg("refuse")
        self.assertEqual((got, kind), (None, "refused"))
        self.assertIn("game running", why)
        self.assert_clean()

    def test_the_route_is_checked_again_right_before_the_encode(self):
        # D2: available() passed, then the cable came out: nothing is sent at all
        box, route_ok = self.counting_route()
        t = [100.0]
        h = self.helper(route_ok=route_ok, clock=lambda: t[0])
        self.assertTrue(h.available()[0])
        box["v"] = (False, "the route to ally.test is Wi-Fi (en0)", "192.168.1.133", "en0")
        (got, why, kind), part = self.run_seg(h=h)
        self.assertEqual((got, kind), (None, "refused"))
        self.assertIn("Wi-Fi", why)
        self.assertEqual(box["n"], 2)
        self.assertEqual([c["verb"] for c in self.calls()], ["status"])   # no put, no encode
        self.assertFalse(os.path.exists(part))
        box["v"] = ROUTE
        # a Mac-side check, not the helper refusing: no cooldown, and the cached ok is gone —
        # the helper is asked again at once
        self.assertEqual(h.available(), (True, "ready"))
        self.assertEqual([c["verb"] for c in self.calls()], ["status", "status"])
        self.assertNotIn("cool", helper._AVAIL[h._id])

    def test_a_route_gone_between_the_check_and_the_send_does_not_cool_down(self):
        real = fake_argv(self.fakes["vworker"], self.root)
        h = helper.Helper("ally.test", "bazzite", "/nonexistent/key", enabled=lambda: True,
                          route_ok=lambda host: ROUTE, local_x265=lambda: X265_TOKEN,
                          argv=lambda *remote: None if remote[0] == "encode" else real(*remote))
        self.assertTrue(h.available()[0])
        (got, why, kind), part = self.run_seg(h=h)
        self.assertEqual((got, kind), (None, "refused"))
        self.assertIn("not sent", why)
        self.assertEqual(self.kills(), self.jobs_put())      # its RPU was handed over: killed
        self.assertFalse(os.path.exists(part))
        self.assertEqual(h.available(), (True, "ready"))     # no cooldown, asked again
        self.assertEqual([c["verb"] for c in self.calls()].count("status"), 2)

    def test_a_refusal_keeps_the_helpers_reason_code_for_the_cooldown(self):
        for mode, code in (("refuse", "game"), ("yield:3", "heavy"), ("refuse_nocode", "busy")):
            helper.reset_state()
            h = self.helper(mode)
            (_got, _why, kind), _part = self.run_seg(h=h)
            self.assertIn(kind, ("refused", "yielded"), mode)
            self.assertEqual(helper._AVAIL[h._id]["cool_code"], code, mode)
            self.assertEqual(h.availability(), (False, "cooling down", "cooling"))

    def test_the_encode_connects_over_the_checked_route(self):
        # no injected argv: the real ssh_argv is built from the verdict the encode just checked
        seen = []

        def fake_popen(cmd, **kw):
            seen.append(cmd)
            raise OSError("not in a test")
        h = helper.Helper("ally.test", "bazzite", "/k", enabled=lambda: True,
                          route_ok=lambda host: ROUTE, local_x265=lambda: X265_TOKEN)
        with mock.patch.object(helper.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(helper, "_run", return_value=(255, "", "")):
            (got, _why, kind), _part = self.run_seg(h=h)
        self.assertEqual((got, kind), (None, "error"))
        self.assertEqual(seen[0][-5:-1], [helper.VWORKER, "put", seen[0][-3], "rpu.bin"])
        self.assertIn("bazzite@192.168.1.133", seen[0])
        self.assertIn("BindInterface=en12", seen[0])

    def test_errors_are_errors(self):
        for mode in ("error:3", "short"):                  # rc 1; rc 0 but the wrong frame count
            (got, _why, kind), part = self.run_seg(mode)
            self.assertEqual((got, kind), (None, "error"), mode)
            self.assertFalse(os.path.exists(part))

    def test_an_rpu_upload_failure_runs_nothing(self):
        for mode, kind in (("putfail", "error"), ("put255", "disconnected")):
            (got, _why, k), _part = self.run_seg(mode)
            self.assertEqual((got, k), (None, kind), mode)
        self.assertEqual(self.calls("encode.log"), [])
        self.assertEqual(self.kills(), [])                 # a failed put leaves nothing to kill

    def test_a_put_answer_that_is_not_the_whole_slice_is_an_error_and_killed(self):
        for mode in ("putlie", "putsilent"):               # another byte count; no answer at all
            helper.reset_state()
            (got, why, kind), _part = self.run_seg(mode)
            self.assertEqual((got, kind), (None, "error"), mode)
            self.assertIn("rpu upload", why)
        self.assertEqual(self.calls("encode.log"), [])
        self.assertEqual(len(self.kills()), 2)             # it may hold the file: killed
        self.assert_clean()

    def _put_in_thread(self, **kw):
        box = {}
        th = threading.Thread(target=lambda: box.update(res=self.run_seg("puthang", **kw)))
        t0 = time.monotonic()
        th.start()
        return th, box, t0

    def test_a_hanging_put_is_killed_on_an_abort(self):
        abort = threading.Event()
        th, box, t0 = self._put_in_thread(abort=abort)
        time.sleep(0.3)
        abort.set()
        th.join(10)
        (got, why, kind), _part = box["res"]
        self.assertEqual((got, why, kind), (None, "aborted", "aborted"))
        self.assertLess(time.monotonic() - t0, 5.0)        # nowhere near the fake's 30 s
        self.assertEqual(self.calls("encode.log"), [])

    def test_a_hanging_put_is_killed_on_a_suspension(self):
        th, box, t0 = self._put_in_thread()
        try:
            time.sleep(0.3)
            dvcap.suspend_encoders()
            th.join(10)
        finally:
            dvcap.resume_encoders()
        (got, _why, kind), _part = box["res"]
        self.assertEqual((got, kind), (None, "suspended"))
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_a_hanging_put_times_out_as_a_lost_link(self):
        self.assertLessEqual(helper.PUT_TIMEOUT, 15)
        with mock.patch.object(helper, "PUT_TIMEOUT", 0.5):
            th, box, t0 = self._put_in_thread()
            th.join(10)
        (got, why, kind), _part = box["res"]
        self.assertEqual((got, kind), (None, "disconnected"))
        self.assertIn("timed out", why)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_a_stalled_stream_is_killed_as_disconnected(self):
        # 7: no x265 progress line and no HEVC growth for STALL_SECS -> killed, "disconnected"
        self.assertEqual(helper.STALL_SECS, 600)
        with mock.patch.object(helper, "STALL_SECS", 0.6):
            t0 = time.monotonic()
            (got, why, kind), part = self.run_seg("stall")
            elapsed = time.monotonic() - t0
        self.assertEqual((got, kind), (None, "disconnected"))
        self.assertIn("no progress", why)
        self.assertLess(elapsed, 10.0)                     # not the fake's 30 s sleep
        self.assertFalse(os.path.exists(part))
        self.assertEqual(len(self.kills()), 1)             # never answered: killed on the helper
        self.assert_clean()

    def test_a_slow_stream_that_keeps_moving_is_not_a_stall(self):
        # (frames big enough that zstd emits a block per frame, as a real segment's do)
        with mock.patch.object(helper, "STALL_SECS", 0.6), \
                mock.patch.dict(os.environ, {"FAKE_W": "512", "FAKE_H": "512", "FAKE_DEC_SLEEP": "0.05"}):
            (got, _why, kind), _part = self.run_seg(frames=30)   # ~1.5 s, a frame every 50 ms
        self.assertEqual((got, kind), (30, "ok"))

    def test_an_unexpected_failure_is_an_error_and_kills_the_job(self):
        with mock.patch.object(helper.Helper, "_stream", side_effect=RuntimeError("boom")):
            (got, why, kind), _part = self.run_seg()
        self.assertEqual((got, kind), (None, "error"))
        self.assertIn("boom", why)
        self.assertEqual(self.kills(), self.jobs_put())
        self.assert_clean()

    def test_a_local_decode_failure_reads_as_a_cut_stream_and_names_the_decoder(self):
        # the helper sees its stdin end early (76); the segment goes back to the Mac, whose own
        # decode then fails for real — the reason still names the local exit code
        with mock.patch.dict(os.environ, {"FAKE_DEC_FAIL_AT": "5"}):
            (got, why, kind), _part = self.run_seg()
        self.assertEqual((got, kind), (None, "disconnected"))
        self.assertIn("decoder rc=1", why)
        self.assert_clean()

    def test_abort_kills_everything_and_cleans_the_ally(self):
        abort, seen = threading.Event(), {}

        def tick(n):
            if not seen:
                seen["pipe"] = next(iter(dvcap._LIVE_REMOTE))
            abort.set()
        with mock.patch.dict(os.environ, {"FAKE_W": "512", "FAKE_H": "512", "FAKE_DEC_SLEEP": "0.03"}):
            t0 = time.monotonic()
            (got, why, kind), part = self.run_seg(frames=300, abort=abort, on_frame=tick)
            elapsed = time.monotonic() - t0
        self.assertEqual((got, why, kind), (None, "aborted", "aborted"))
        self.assertLess(elapsed, 8.0)                      # nowhere near the 9 s the stream needs
        self.assertEqual(len(seen["pipe"].procs), 3)
        for p in seen["pipe"].procs:
            self.assertIsNotNone(p.poll())                 # all three local processes are gone
        self.assertFalse(os.path.exists(part))
        self.assertEqual(self.kills(), self.jobs_put())    # `vworker kill <job>` ran for THIS job
        self.assert_clean()

    def test_an_abort_before_the_start_does_nothing(self):
        ev = threading.Event()
        ev.set()
        (got, _why, kind), _part = self.run_seg(abort=ev)
        self.assertEqual((got, kind), (None, "aborted"))
        self.assertEqual(self.calls(), [])

    def _pipe(self):
        return next(iter(dvcap._LIVE_REMOTE), None)

    def test_a_suspension_kills_the_remote_pipe_as_suspended(self):
        # D3: never SIGSTOPped — killed at once, "suspended", the Ally's job killed, no cooldown
        ticks, box = [], {}
        h = self.helper()
        self.assertEqual(h.available(), (True, "ready"))
        with mock.patch.dict(os.environ, {"FAKE_W": "512", "FAKE_H": "512", "FAKE_DEC_SLEEP": "0.01"}):
            th = threading.Thread(target=lambda: box.update(
                res=self.run_seg(frames=150, on_frame=ticks.append, h=h)))
            th.start()
            try:
                self.assertTrue(_wait(lambda: len(ticks) >= 5))
                pipe = self._pipe()
                self.assertEqual(len(pipe.procs), 3)
                self.assertEqual(len(dvcap._LIVE_ENCODERS), 0)   # not in the SIGSTOP registry
                t0 = time.monotonic()
                self.assertGreaterEqual(dvcap.suspend_encoders(), 1)
                th.join(10)
                self.assertFalse(th.is_alive())                  # done WHILE still suspended
                self.assertLess(time.monotonic() - t0, 5.0)
                for p in pipe.procs:
                    self.assertIsNotNone(p.poll(), p.args)       # killed, not frozen
            finally:
                dvcap.resume_encoders()
                th.join(60)
        (got, why, kind), part = box["res"]
        self.assertEqual((got, kind), (None, "suspended"))
        self.assertIn("suspended", why)
        self.assertFalse(os.path.exists(part))
        self.assertEqual(self.kills(), self.jobs_put())
        self.assertEqual(h.available(), (True, "ready"))         # no cooldown, cache kept
        self.assertEqual(len([c for c in self.calls() if c["verb"] == "status"]), 1)
        self.assert_clean()

    def test_nothing_is_sent_during_a_suspension(self):
        dvcap.suspend_encoders()
        try:
            (got, _why, kind), part = self.run_seg()
        finally:
            dvcap.resume_encoders()
        self.assertEqual((got, kind), (None, "suspended"))
        self.assertEqual(self.calls(), [])                 # no put, no encode, no kill
        self.assertFalse(os.path.exists(part))
        self.assert_clean()

    def test_a_pipe_spawned_as_a_suspension_lands_is_killed_at_once(self):
        # the suspension came between the start check and the spawn: register_remote refuses
        with mock.patch.object(dvcap, "register_remote", return_value=False) as reg, \
                mock.patch.dict(os.environ, {"FAKE_W": "512", "FAKE_H": "512", "FAKE_DEC_SLEEP": "0.01"}):
            t0 = time.monotonic()
            (got, _why, kind), _part = self.run_seg(frames=150)
            elapsed = time.monotonic() - t0
        reg.assert_called_once()
        self.assertEqual((got, kind), (None, "suspended"))
        self.assertLess(elapsed, 5.0)                      # the 150-frame stream needs ~1.5 s+
        self.assertEqual(self.kills(), self.jobs_put())
        self.assert_clean()


# ---- the result mapping, without processes -------------------------------------------------

    # -- 2: the stream's framing over the fake wire ---------------------------------------------
    def test_shell_noise_before_the_stream_is_dropped(self):
        # the remote command runs through the helper user's login shell: whatever its rc files
        # print (a motd, bytes that look like HEVC, a near-miss of the marker) lands on stdout
        # first, and on stderr — none of it may reach the part, and status/put still work
        h = self.helper(junk=SHELL_JUNK)
        self.assertEqual(h.available(), (True, "ready"))
        (got, why, kind), part = self.run_seg(h=h)
        self.assertEqual((got, why, kind), (20, "ok", "ok"))
        with open(part, "rb") as f:
            self.assertEqual(f.read(), AUD * 20)
        self.assertEqual(len(self.jobs_put()), 1)
        self.assertEqual(self.kills(), [])
        self.assert_clean()

    def test_a_stream_without_the_marker_is_an_error(self):
        (got, why, kind), part = self.run_seg("nomagic")
        self.assertEqual((got, kind), (None, "error"))
        self.assertIn("never started", why)
        self.assertFalse(os.path.exists(part))
        self.assert_clean()

    def test_a_stream_that_does_not_open_with_an_aud_or_vps_is_an_error(self):
        h = self.helper("badnal")
        # frames big enough that zstd emits a block per frame (tiny ones all arrive at EOF), and
        # slow: the first NAL is judged, and the stream cut off, long before it could end
        with mock.patch.dict(os.environ, {"FAKE_W": "512", "FAKE_H": "512", "FAKE_DEC_SLEEP": "0.05"}):
            (got, why, kind), part = self.run_seg(h=h)
        self.assertEqual((got, kind), (None, "error"))
        self.assertIn("AUD or VPS", why)
        self.assertFalse(os.path.exists(part))
        self.assertEqual(self.kills(), self.jobs_put())    # cut off unanswered: killed there too
        self.assertEqual(h.available(), (True, "ready"))   # an error never cools the helper down
        self.assert_clean()

    def test_too_much_noise_before_the_marker_is_an_error(self):
        with mock.patch.object(helper, "JUNK_LIMIT", 16):
            (got, why, kind), part = self.run_seg(h=self.helper(junk=SHELL_JUNK))
        self.assertEqual((got, kind), (None, "error"))
        self.assertIn("without its stream marker", why)
        self.assertFalse(os.path.exists(part))
        self.assert_clean()

    def test_x265s_fps_reaches_a_callback_that_takes_it(self):
        seen = []
        (got, _why, kind), _part = self.run_seg(on_frame=lambda n, fps=None: seen.append((n, fps)))
        self.assertEqual((got, kind), (20, "ok"))
        self.assertEqual([n for n, _f in seen][-1], 20)
        self.assertTrue(seen and all(f == 8.06 for _n, f in seen), seen)

    # -- 4: a stop that lands after the helper finished ---------------------------------------
    def test_a_stop_after_the_stream_finished_keeps_the_segment(self):
        # the abort lands once ssh has already exited 0 with the whole stream and an ok VWORKER
        # line — while this side is still reaping it; the watcher sees it, and the segment is
        # STILL ok: it was finished and verified, nothing was cut off
        abort = threading.Event()
        real_popen = helper.subprocess.Popen

        class LatePopen(real_popen):
            def wait(self, timeout=None):
                if "encode" in list(self.args or ()) and not abort.is_set():
                    abort.set()
                    time.sleep(0.6)                       # three of the watcher's polls
                return super().wait(timeout)
        with mock.patch.object(helper.subprocess, "Popen", LatePopen):
            (got, why, kind), part = self.run_seg(abort=abort)
        self.assertTrue(abort.is_set())
        self.assertEqual((got, why, kind), (20, "ok", "ok"))
        with open(part, "rb") as f:
            self.assertEqual(f.read(), AUD * 20)
        self.assertEqual(self.kills(), [])
        self.assert_clean()


class StreamFraming(unittest.TestCase):
    """_StreamFramer / first_nal_verdict on synthetic bytes."""
    VPS = b"\x00\x00\x00\x01\x40\x01\x0c\x01"
    SPS = b"\x00\x00\x00\x01\x42\x01\x01"
    IDR = b"\x00\x00\x01\x26\x01\xaf"

    def test_first_nal_verdict(self):
        v = helper.first_nal_verdict
        self.assertIs(v(AUD), True)
        self.assertIs(v(b"\x00\x00\x01\x46\x01\x50"), True)    # a 3-byte start code
        self.assertIs(v(self.VPS), True)
        self.assertIs(v(self.SPS), False)
        self.assertIs(v(self.IDR), False)
        self.assertIs(v(b"\x00\x00\x00\x01\xc6\x01"), False)    # forbidden_zero_bit set
        for partial in (b"", b"\x00", b"\x00\x00", b"\x00\x00\x00", b"\x00\x00\x00\x01",
                        b"\x00\x00\x00\x01\x46", b"\x00\x00\x01\x46"):
            self.assertIsNone(v(partial), partial)
        for junk in (b"F", b"FFFF", b"\x00F", b"\x00\x00\x02\x46\x01", b"Welcome"):
            self.assertIs(v(junk), False, junk)

    def framed(self, chunks):
        import io
        out = io.BytesIO()
        fr = helper._StreamFramer(out)
        oks = [fr.feed(c) for c in chunks]
        fr.finish()
        return fr, out.getvalue(), oks

    def test_noise_is_dropped_even_byte_by_byte(self):
        noise = b"motd\n\x00\x00\x00\x01\x46\x01VWHEVC0\nVWHEV"
        body = AUD * 3 + self.SPS
        data = noise + helper.STREAM_MAGIC + body
        for chunks in ([data], [bytes([b]) for b in data],
                       [noise + helper.STREAM_MAGIC[:3], helper.STREAM_MAGIC[3:] + body[:2], body[2:]]):
            fr, out, oks = self.framed(chunks)
            self.assertEqual(out, body)
            self.assertTrue(all(oks))
            self.assertIsNone(fr.error)
            self.assertEqual(fr.junk, len(noise))

    def test_a_vps_first_is_fine_and_no_noise_is_fine(self):
        fr, out, _ = self.framed([helper.STREAM_MAGIC + self.VPS + AUD])
        self.assertEqual((out, fr.error, fr.junk), (self.VPS + AUD, None, 0))

    def test_no_marker_never_started(self):
        fr, out, oks = self.framed([AUD * 5])
        self.assertEqual(out, b"")
        self.assertTrue(all(oks))                          # noise until the end, within the limit
        self.assertIn("never started", fr.error)

    def test_a_wrong_first_nal_stops_the_stream(self):
        fr, out, oks = self.framed([helper.STREAM_MAGIC + b"FFFF", AUD])
        self.assertEqual(oks, [False, False])
        self.assertEqual(out, b"")
        self.assertIn("AUD or VPS", fr.error)
        self.assertEqual(fr.state, "bad")

    def test_a_cut_head_is_bad_and_a_bare_marker_is_left_to_the_frame_check(self):
        fr, out, _ = self.framed([helper.STREAM_MAGIC + b"\x00\x00"])
        self.assertEqual((fr.state, out), ("bad", b""))
        fr, out, _ = self.framed([helper.STREAM_MAGIC])
        self.assertEqual((fr.state, fr.error, out), ("head", None, b""))

    def test_the_noise_limit(self):
        with mock.patch.object(helper, "JUNK_LIMIT", 10):
            fr, out, oks = self.framed([b"x" * 30, helper.STREAM_MAGIC + AUD])
        self.assertEqual(oks, [False, False])
        self.assertEqual(out, b"")
        self.assertIn("without its stream marker", fr.error)

    def test_the_noise_limit_holds_when_the_marker_comes_in_the_same_read(self):
        with mock.patch.object(helper, "JUNK_LIMIT", 10):
            fr, out, oks = self.framed([b"x" * 11 + helper.STREAM_MAGIC + AUD, AUD])
            self.assertEqual((oks, out, fr.state), ([False, False], b"", "bad"))
            self.assertIn("without its stream marker", fr.error)
            fr, out, oks = self.framed([b"x" * 10 + helper.STREAM_MAGIC + AUD])   # at the limit
            self.assertEqual((oks, out, fr.error, fr.junk), ([True], AUD, None, 10))


class ResultMapping(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.part = os.path.join(self.td, "p")
        with open(self.part, "wb") as f:
            f.write(b"FFFF")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def kind(self, rc, final=None, tail=(), frames=4, dec_rc=0, zst_rc=0):
        return helper.Helper._result(rc, final, list(tail), frames, self.part, dec_rc, zst_rc)[2]

    def test_exit_codes(self):
        self.assertEqual(self.kind(0, {"result": "ok", "frames": 4}), "ok")
        self.assertEqual(self.kind(0, None, ["encoded 4 frames in 1s"]), "ok")   # no VWORKER line
        self.assertEqual(self.kind(0, {"result": "ok", "frames": 3}), "error")
        self.assertEqual(self.kind(75), "yielded")
        self.assertEqual(self.kind(76), "disconnected")
        self.assertEqual(self.kind(255), "disconnected")
        self.assertEqual(self.kind(77), "refused")
        self.assertEqual(self.kind(1), "error")
        self.assertEqual(self.kind(-9), "error")
        # ffmpeg exits 1 on the broken pipe a stopped Ally leaves, too: local codes never re-kind
        self.assertEqual(self.kind(76, dec_rc=1), "disconnected")
        self.assertEqual(self.kind(75, dec_rc=1), "yielded")
        why = helper.Helper._result(76, {"why": "stdin ended"}, [], 4, self.part, 1, 0)[1]
        self.assertIn("decoder rc=1", why)


# ---- C2: status_summary ----------------------------------------------------------------------

ON = {"helper_enabled": True, "helper_host": "ally.test", "helper_user": "bazzite",
      "helper_key": "/nonexistent/key"}
WORD = {"connected": "connected", "ready": "ready", "paused": "paused", "off": "off",
        "not_connected": "not connected"}


def ui_line(summary: dict) -> str:
    """How the remux lane card reads a summary: "Helper <state> · <detail>"."""
    head = "Helper " + WORD[summary["state"]]
    return head + (" · " + summary["detail"] if summary["detail"] else "")


class FakeProbe:
    """A refresher Helper stand-in: `answers` is what available() says (a callable may block)."""

    def __init__(self, answer=(True, "ready", "ready"), delay=0.0):
        self.answer, self.delay, self.calls = answer, delay, 0

    def __call__(self, vals):
        probe = self

        class H:
            _id = (vals["helper_host"], vals["helper_user"], vals["helper_key"])
            _route = None

            def availability(self):
                probe.calls += 1
                if probe.delay:
                    time.sleep(probe.delay)
                return probe.answer() if callable(probe.answer) else probe.answer

            def available(self):
                return self.availability()[:2]
        return H()


class StatusSummaryPure(unittest.TestCase):
    def test_the_approved_lines(self):
        cases = [
            ((True, "ready"), "Helper ready · waiting for the next segment"),
            ((False, "game"), "Helper paused · a game is running on it"),
            ((False, "heavy"), "Helper paused · it's busy"),
            ((False, "busy"), "Helper paused · it's busy"),
            ((False, "wifi"), "Helper off · the Mac is on Wi-Fi"),
            ((False, "no_ethernet"), "Helper off · it's not on Ethernet"),
            ((False, "battery"), "Helper off · it's on battery"),
            ((False, "switched_off"), "Helper off · switched off"),
            ((False, "unreachable"), "Helper not connected"),
            ((False, "not_wired"), "Helper not connected"),
            ((False, "route_check"), "Helper not connected"),
            ((False, "mismatch"), "Helper not connected"),
            ((False, "x265_missing"), "Helper not connected"),
            ((False, "error"), "Helper not connected"),
            ((False, ""), "Helper not connected"),
            # free text is never read, whatever words it holds
            ((False, "game running: Hades.exe"), "Helper not connected"),
            ((False, "the route to ally.test is Wi-Fi (en0)"), "Helper not connected"),
        ]
        for (ok, code), want in cases:
            state, detail = helper.summarize_verdict(ok, code)
            self.assertEqual(ui_line({"state": state, "detail": detail}), want, code)

    def test_a_cooldown_says_what_started_it(self):
        v = helper.summarize_verdict
        self.assertEqual(v(False, "cooling", "game"), ("paused", "a game is running on it"))
        self.assertEqual(v(False, "cooling", "heavy"), ("paused", "it's busy"))
        self.assertEqual(v(False, "cooling", "battery"), ("off", "it's on battery"))
        self.assertEqual(v(False, "cooling", "x265_missing"), ("paused", "it's busy"))
        self.assertEqual(v(False, "cooling"), ("paused", "it's busy"))

    def test_the_status_answers_code(self):
        c = helper.status_code
        base = {"ok": False, "ac": True, "ethernet": True, "game": None, "heavy": None,
                "busy": False}
        self.assertEqual(c(dict(base, code="low_disk")), "low_disk")       # vworker's own word
        self.assertEqual(c(dict(base, code="game", why="busy: wi-fi-tool")), "game")
        # an older vworker (no "code"): its structured fields, in vworker's own order
        self.assertEqual(c(dict(base, ac=False, game="Hades.exe")), "battery")
        self.assertEqual(c(dict(base, ethernet=False)), "no_ethernet")
        self.assertEqual(c(dict(base, game="Hades.exe", busy=True)), "game")
        self.assertEqual(c(dict(base, busy=True, heavy="cpu")), "busy")
        self.assertEqual(c(dict(base, heavy="GPU card0 90% busy")), "heavy")
        self.assertEqual(c(dict(base, why="low disk: 2.0 GB free, need 20")), "not_ready")
        self.assertEqual(c(dict(base, code="bogus", heavy="x")), "heavy")   # unknown word: derived
        self.assertTrue(helper.heavy_only(dict(base, heavy="x")))
        self.assertTrue(helper.heavy_only(dict(base, heavy="x", code="heavy")))
        for other in (dict(ac=False), dict(ethernet=False), dict(game="g"), dict(busy=True),
                      dict(code="low_disk"), dict(heavy=None), dict(ok=True)):
            self.assertFalse(helper.heavy_only(dict(dict(base, heavy="x"), **other)), other)

    def test_the_host_name_never_picks_the_line(self):
        # hosts whose names hold the words the old classifier matched on
        def no_route(cmd, timeout):
            return 1, "", ""
        cases = [("busybox.local", dict(resolve=lambda h: None)),
                 ("wi-fi-bridge.local", dict(resolve=lambda h: "10.0.0.5", run=no_route)),
                 ("gamebox-heavy.local", None)]
        for host, route in cases:
            helper.reset_state()
            if route is None:                    # routed fine, but no answer from its vworker
                h = helper.Helper(host, "u", "/k", argv=lambda *r: ["false"], enabled=lambda: True,
                                  route_ok=lambda _h: ROUTE, local_x265=lambda: X265_TOKEN)
                with mock.patch.object(helper, "_run", return_value=(255, "", "")):
                    ok, why, code = h.availability()
            else:
                h = helper.Helper(host, "u", "/k", enabled=lambda: True,
                                  route_ok=lambda _h, kw=route: helper.wired_route_ok(_h, **kw))
                ok, why, code = h.availability()
            self.assertFalse(ok)
            self.assertIn(host, why)                     # the words name it...
            self.assertEqual(code, "unreachable", why)
            self.assertEqual(helper.summarize_verdict(ok, code), ("not_connected", ""), why)
        helper.reset_state()

    def test_connected_detail(self):
        d = helper.connected_detail
        info = {"done": [0, 1, 9, 10], "helper_done": [9, 10], "mac": [2, 0.4],
                "helper": [8, 0.25, 7.94]}
        self.assertEqual(d(info), "on seg 9 · 7.9 fps · did 10–11")
        self.assertEqual(d(dict(info, helper=[8, 0.0, 0.0])), "on seg 9 · did 10–11")
        self.assertEqual(d(dict(info, helper_done=[3, 5, 6, 7])), "on seg 9 · 7.9 fps · did 4, 6–8")
        self.assertEqual(d(dict(info, helper=None, helper_done=[])), "")
        self.assertEqual(d(None), "")
        self.assertEqual(d({"helper": ["x"]}), "")

    def test_last_json_tolerates_noise_on_its_line(self):
        self.assertEqual(helper._last_json('VWHEV{"ok": true, "bytes": 9}\n'), {"ok": True, "bytes": 9})
        self.assertEqual(helper._last_json('a {b} {"ok": 1}'), {"ok": 1})
        self.assertIsNone(helper._last_json("motd {not json\n"))


class StatusSummary(FakeRemoteCase):
    ARGS = EncodeSegment.ARGS

    def setUp(self):
        super().setUp()
        self.fields = dict(ON)
        self.patches = [mock.patch.object(settings, "get_settings", side_effect=lambda: dict(self.fields)),
                        mock.patch.object(helper, "_log")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        helper.reset_state()
        for p in self.patches:
            p.stop()
        super().tearDown()

    def probe(self, **kw):
        fake = FakeProbe(**kw)
        p = mock.patch.object(helper, "_summary_helper", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def settle(self, pred, timeout=5.0):
        self.assertTrue(_wait(lambda: pred(helper.status_summary()), timeout),
                        helper.status_summary())
        return helper.status_summary()

    def test_off_means_nothing_runs(self):
        fake = self.probe()
        self.fields["helper_enabled"] = False
        st = helper.status_summary()
        self.assertEqual(st, {"enabled": False, "state": "off", "detail": "switched off",
                              "host": "ally.test", "tooltip": "ally.test"})
        time.sleep(0.1)
        self.assertEqual(fake.calls, 0)
        self.assertIsNone(helper._REFRESH["thread"])        # no refresher at all

    def test_ready_after_the_first_refresh_with_the_route_in_the_tooltip(self):
        self.probe()
        first = helper.status_summary()
        self.assertTrue(first["enabled"])
        self.assertIn(first["state"], ("not_connected", "ready"))
        st = self.settle(lambda s: s["state"] == "ready")
        self.assertEqual(ui_line(st), "Helper ready · waiting for the next segment")
        self.assertEqual(st["host"], "ally.test")
        self.assertNotIn("ally.test", st["detail"])         # the host name only in the tooltip
        self.helper().check_route()                          # the last wired verdict: en12's IPv4
        self.assertEqual(helper.status_summary()["tooltip"], "ally.test · 192.168.1.133 over Ethernet")

    def test_never_blocks_even_while_the_probe_hangs(self):
        fake = self.probe(delay=2.0)
        t0 = time.monotonic()
        for _ in range(20):
            st = helper.status_summary()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(st["state"], "not_connected")
        self.assertIn("checking", st["tooltip"])
        self.assertTrue(_wait(lambda: fake.calls == 1, 2))   # the refresher is the one asking

    def test_the_refresher_polls_every_minute_and_stops_when_switched_off(self):
        self.assertEqual(helper.REFRESH_SECS, 60)
        answers = [(False, "on battery", "battery")]
        fake = self.probe(answer=lambda: answers[-1])
        with mock.patch.object(helper, "REFRESH_SECS", 0.1):
            st = self.settle(lambda s: s["state"] == "off")
            self.assertEqual(ui_line(st), "Helper off · it's on battery")
            self.assertEqual(st["tooltip"], "ally.test · on battery")
            answers.append((False, "game running: Hades.exe", "game"))
            st = self.settle(lambda s: s["detail"] == "a game is running on it")
            self.assertEqual(ui_line(st), "Helper paused · a game is running on it")
            th = helper._REFRESH["thread"]
            self.assertTrue(th.is_alive())
            self.fields["helper_enabled"] = False
            th.join(3)
            self.assertFalse(th.is_alive())                  # switched off: it stopped by itself
            n = fake.calls
            time.sleep(0.3)
            self.assertEqual(fake.calls, n)

    def test_connected_while_a_remote_segment_is_in_flight(self):
        self.probe()
        h = self.helper()
        box = {}
        with mock.patch.dict(os.environ, {"FAKE_DEC_SLEEP": "0.03"}):
            th = threading.Thread(target=lambda: box.update(r=EncodeSegment.run_seg(self, h=h, frames=40)))
            th.start()
            try:
                st = self.settle(lambda s: s["state"] == "connected")
                self.assertEqual(ui_line(st), "Helper connected")
                h.note_segments({"done": [0, 9, 10], "helper_done": [9, 10], "mac": [1, 0.5],
                                 "helper": [8, 0.3, 7.94]})
                st = helper.status_summary()
                self.assertEqual(ui_line(st), "Helper connected · on seg 9 · 7.9 fps · did 10–11")
                self.assertEqual(st["tooltip"], "ally.test · 192.168.1.133 over Ethernet")
            finally:
                th.join(20)
        self.assertEqual(box["r"][0][2], "ok")
        self.assertNotEqual(helper.status_summary()["state"], "connected")

    def test_a_yield_reads_paused_with_its_reason_at_once(self):
        self.probe()
        self.settle(lambda s: s["state"] == "ready")
        self.helper()._note("yielded", "yielded (rc=75): game running: Hades.exe", "game")
        self.assertEqual(ui_line(helper.status_summary()), "Helper paused · a game is running on it")

    def test_a_refusal_over_the_wire_reads_its_code_not_its_words(self):
        self.probe()
        self.settle(lambda s: s["state"] == "ready")
        for mode, want in (("refuse", "Helper paused · a game is running on it"),
                           ("refuse_nocode", "Helper paused · it's busy")):
            helper._AVAIL.clear()
            (_got, _why, kind), _part = EncodeSegment.run_seg(self, h=self.helper(mode))
            self.assertEqual(kind, "refused")
            self.assertEqual(ui_line(helper.status_summary()), want, mode)

    def test_two_first_callers_start_one_refresher(self):
        # 2: the refresher is started under the lock — never two, and reset_state stops it
        def alive():
            return [t for t in threading.enumerate() if t.name == "helper-status" and t.is_alive()]
        self.assertTrue(_wait(lambda: not alive(), 5), alive())
        self.probe()
        real_start = threading.Thread.start

        def slow_start(t):
            if t.name == "helper-status":
                time.sleep(0.05)               # widen the window between registering and starting
            return real_start(t)
        with mock.patch.object(threading.Thread, "start", slow_start):
            callers = [threading.Thread(target=helper.status_summary) for _ in range(3)]
            for c in callers:
                c.start()
            for c in callers:
                c.join(5)
        self.assertEqual(len(alive()), 1)
        self.assertIs(alive()[0], helper._REFRESH["thread"])
        helper.reset_state()
        self.assertTrue(_wait(lambda: not alive(), 3), alive())

    def test_bad_settings_read_off_with_why_and_are_logged_once(self):
        fake = self.probe()
        self.fields.update(helper_host="bad host", helper_key="-i")
        for _ in range(3):
            st = helper.status_summary()
            self.assertEqual((st["enabled"], st["state"], st["detail"]),
                             (True, "off", "settings: bad host, key"))
            self.assertIsNone(helper.for_remux())
        self.assertEqual(ui_line(st), "Helper off · settings: bad host, key")
        logged = [c.args[0] for c in helper._log.call_args_list]
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("helper_host='bad host'", logged[0])
        self.assertIn("helper_key='-i'", logged[0])
        time.sleep(0.1)
        self.assertEqual(fake.calls, 0)                       # nothing is ever asked
        self.fields.update(helper_host="ally.test", helper_key="/nonexistent/key")
        self.settle(lambda s: s["state"] == "ready")         # fixed: the line follows

    def test_unreadable_settings_are_off(self):
        self.patches[0].stop()
        with mock.patch.object(settings, "get_settings", side_effect=OSError("unreadable")):
            st = helper.status_summary()
        self.patches[0].start()
        self.assertEqual((st["enabled"], st["state"]), (False, "off"))


# ---- settings and for_remux ------------------------------------------------------------------

class HelperSettings(unittest.TestCase):
    def test_defaults(self):
        d = settings.DEFAULT_SETTINGS
        self.assertIs(d["helper_enabled"], False)
        self.assertEqual(d["helper_host"], "bazzite.local")
        self.assertEqual(d["helper_user"], "bazzite")
        self.assertEqual(d["helper_key"], "~/.ssh/id_ed25519_ally_claude")
        self.assertIn("helper_enabled", settings.VALIDATORS)
        for k in ("helper_host", "helper_user", "helper_key"):
            self.assertNotIn(k, settings.VALIDATORS)        # 3: never replaced on write
            self.assertIn(k, settings.HELPER_FIELDS)
        for k in ("helper_enabled", "helper_host", "helper_user", "helper_key"):
            self.assertNotIn(k, settings.LIMITS)

    def test_enabled_is_off_unless_clearly_on(self):
        v = settings._valid_helper_enabled
        for on in (True, 1, "1", "true", "True", "on"):
            self.assertIs(v(on), True, on)
        for off in (False, 0, "0", "false", "off", None, "", "yes please", 2, []):
            self.assertIs(v(off), False, off)

    def test_host_and_user_are_plain_names(self):
        ok = settings.helper_field_ok
        for good in ("bazzite.local", "192.168.1.20", "ally_2", "rog-ally"):
            self.assertTrue(ok("helper_host", good), good)
            self.assertTrue(ok("helper_user", good), good)
        for bad in ("-oProxyCommand=x", "a b", "a;b", "", None, "h@x", "../x", 5):
            self.assertFalse(ok("helper_host", bad), bad)
            self.assertFalse(ok("helper_user", bad), bad)

    def test_key_is_a_path_string(self):
        ok = settings.helper_field_ok
        self.assertTrue(ok("helper_key", "~/.ssh/k"))
        for bad in ("", None, "-i", "a\nb", " ~/.ssh/k", 3):
            self.assertFalse(ok("helper_key", bad), bad)
        self.assertFalse(ok("helper_port", "22"))           # not a helper field at all

    def _saved(self, updates, before=None):
        d = tempfile.mkdtemp()
        try:
            with mock.patch.multiple(settings, CONFIG_DIR=d,
                                     SETTINGS_FILE=os.path.join(d, "settings.json")):
                if before:
                    settings.set_settings(before)
                s = settings.set_settings(updates)
                again = settings.get_settings()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return s, again

    def test_set_settings_keeps_a_bad_value_as_typed(self):
        # 3: a malformed host/user/key POSTed to /api/settings is SAVED AS TYPED (never swapped
        # for a default that would quietly ssh somewhere else); the switch is still normalised
        s, again = self._saved({"helper_enabled": "on", "helper_host": "bad host",
                                "helper_user": "deck", "helper_key": "-i"})
        for got in (s, again):
            self.assertEqual((got["helper_enabled"], got["helper_host"], got["helper_user"],
                              got["helper_key"]), (True, "bad host", "deck", "-i"))
        s, _ = self._saved({"helper_host": "ally.lan"}, before={"helper_host": "bad host"})
        self.assertEqual(s["helper_host"], "ally.lan")     # and fixing it is just saving again
        s, _ = self._saved({"plex_throttle": False})
        self.assertEqual((s["helper_host"], s["helper_user"], s["helper_key"]),
                         ("bazzite.local", "bazzite", "~/.ssh/id_ed25519_ally_claude"))

    def setUp(self):
        helper.reset_state()

    def tearDown(self):
        helper.reset_state()

    def test_for_remux(self):
        with mock.patch.object(settings, "get_settings", return_value={"helper_enabled": False}):
            self.assertIsNone(helper.for_remux())
        with mock.patch.object(settings, "get_settings", return_value={}):
            self.assertIsNone(helper.for_remux())          # absent = the default, off
        with mock.patch.object(settings, "get_settings",
                               return_value={"helper_enabled": True, "helper_host": "ally.lan",
                                             "helper_user": "deck", "helper_key": "~/.ssh/k"}):
            h = helper.for_remux()
        self.assertIsInstance(h, helper.Helper)
        self.assertEqual((h.host, h.user, h.key), ("ally.lan", "deck", "~/.ssh/k"))
        self.assertIsNone(h.argv("status"))                # nothing until a route is checked
        with mock.patch.object(settings, "get_settings", return_value={"helper_enabled": True}):
            h = helper.for_remux()                         # absent fields = the shipped defaults
        self.assertEqual((h.host, h.user, h.key),
                         ("bazzite.local", "bazzite", "~/.ssh/id_ed25519_ally_claude"))
        with mock.patch.object(settings, "get_settings", side_effect=OSError("unreadable")):
            self.assertIsNone(helper.for_remux())

    def test_a_malformed_field_is_no_helper_and_says_why_once(self):
        # 10: never a silent fallback to the defaults — a typo must not quietly ssh elsewhere
        logged = []
        bad = {"helper_enabled": True, "helper_host": "ally.lan", "helper_user": "-x",
               "helper_key": "~/.ssh/k"}
        with mock.patch.object(helper, "_log", side_effect=logged.append):
            for fields in (bad, bad, dict(bad, helper_host="a b"), {"helper_enabled": True,
                                                                     "helper_key": "-i"}):
                with mock.patch.object(settings, "get_settings", return_value=fields):
                    self.assertIsNone(helper.for_remux(), fields)
        self.assertEqual(len(logged), 3)                   # once per distinct problem
        self.assertIn("helper_user='-x'", logged[0])
        self.assertIn("helper_host='a b'", logged[1])
        self.assertIn("helper_user='-x'", logged[1])
        self.assertIn("helper_key='-i'", logged[2])
        for msg in logged:
            self.assertTrue(msg.startswith("Helper not used"), msg)
            self.assertNotIn("Ally", msg)

    def test_a_disabled_helper_never_judges_its_fields(self):
        logged = []
        with mock.patch.object(helper, "_log", side_effect=logged.append), \
                mock.patch.object(settings, "get_settings",
                                  return_value={"helper_enabled": False, "helper_host": "a b"}):
            self.assertIsNone(helper.for_remux())
        self.assertEqual(logged, [])


if __name__ == "__main__":
    unittest.main()
