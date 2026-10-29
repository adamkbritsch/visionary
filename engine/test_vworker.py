"""Tests for the helper worker (engine/helper_worker/vworker.py). They run on the Mac with
no helper machine: /proc and /sys are fake trees, x265 is a fake script, zstd is the Mac's real one, and
the encode command is driven end to end as a subprocess through the VWORKER_* overrides.

RealX265 (opt-in, runs only where /opt/homebrew/bin/x265 exists) pushes a tiny clip through
`vworker encode` with the real x265 and checks the HEVC is byte-identical to a direct x265 run.

CHECK ONCE ON THE HELPER (the Mac cannot exercise it): PR_SET_PDEATHSIG. macOS has no prctl, so
no test here proves that zstd and x265 die with a SIGKILLed vworker. On the helper, start an
encode, `kill -9` the vworker python process, and confirm with `pgrep -a 'zstd|x265'` that both
children are gone within a second (and that the next `vworker sweep` removes the job dir).
Likewise SCHED_IDLE: `chrt -p <x265 pid>` during an encode must say SCHED_IDLE."""
import base64
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER_DIR = os.path.join(HERE, "helper_worker")
VWORKER = os.path.join(WORKER_DIR, "vworker.py")
sys.dont_write_bytecode = True
sys.path.insert(0, WORKER_DIR)

import dvcap      # noqa: E402
import vworker    # noqa: E402

ZSTD = "/opt/homebrew/bin/zstd" if os.path.exists("/opt/homebrew/bin/zstd") else shutil.which("zstd")
FAKE_PID = 123456          # above macOS's PID_MAX, so never one of our real processes
FRAME = 96                 # bytes per frame of the fake y4m (8x4, 4:2:0, 10-bit)
Y4M_HEADER = b"YUV4MPEG2 W8 H4 F24:1 Ip A1:1 C420p10\n"


# ---- fixtures -------------------------------------------------------------------------------

def stat_line(pid, comm, ppid, ticks=0, start=1000, state="S", flags=0):
    f = [state, ppid, 0, 0, 0, 0, flags, 0, 0, 0, 0, ticks, 0, 0, 0, 20, 0, 1, 0, start, 0, 0]
    return "%d (%s) %s\n" % (pid, comm, " ".join(str(x) for x in f))


def add_proc(proc_root, pid, comm, argv, ppid=1, ticks=0, start=1000, flags=0):
    """Create /proc/<pid> atomically (stat + cmdline appear together)."""
    tmp = os.path.join(proc_root, ".tmp-%d" % pid)
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "stat"), "w") as f:
        f.write(stat_line(pid, comm, ppid, ticks, start, flags=flags))
    with open(os.path.join(tmp, "cmdline"), "wb") as f:
        f.write(b"".join(a.encode() + b"\0" for a in argv))
    dest = os.path.join(proc_root, str(pid))
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)


def set_ticks(proc_root, pid, comm, ppid, ticks, start=1000):
    with open(os.path.join(proc_root, str(pid), "stat"), "w") as f:
        f.write(stat_line(pid, comm, ppid, ticks, start))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_sys(root, ac=True, eth=True, gpu=3, wifi=True):
    write(os.path.join(root, "class/power_supply/ACAD/type"), "Mains\n")
    write(os.path.join(root, "class/power_supply/ACAD/online"), "1\n" if ac else "0\n")
    write(os.path.join(root, "class/power_supply/BAT0/type"), "Battery\n")
    write(os.path.join(root, "class/power_supply/BAT0/online"), "1\n")
    write(os.path.join(root, "class/net/enp4s0/operstate"), "up\n" if eth else "down\n")
    write(os.path.join(root, "class/net/enp4s0/carrier"), "1\n" if eth else "0\n")
    if wifi:
        write(os.path.join(root, "class/net/wlan0/operstate"), "up\n")
        write(os.path.join(root, "class/net/wlan0/carrier"), "1\n")
    write(os.path.join(root, "class/drm/card0/device/gpu_busy_percent"), "%d\n" % gpu)
    os.makedirs(os.path.join(root, "class/drm/card0-DP-1"), exist_ok=True)


FAKE_X265 = r'''#!{python}
import os, signal, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    # status runs `x265 --version` once per sample, after it read the GPU: tests count the
    # samples (LOG), flip the GPU reading for the next sample (GPU_FILE/AFTER), or hold a
    # sample open (MARK + SLEEP) to signal an encode while it is still deciding
    env = os.environ.get
    if env("FAKE_X265_VERSION_LOG"):
        with open(env("FAKE_X265_VERSION_LOG"), "a") as f:
            f.write("version\n")
    if env("FAKE_GPU_FILE") and env("FAKE_GPU_AFTER"):
        with open(env("FAKE_GPU_FILE"), "w") as f:
            f.write(env("FAKE_GPU_AFTER") + "\n")
    if env("FAKE_X265_VERSION_MARK"):
        open(env("FAKE_X265_VERSION_MARK"), "w").close()
        time.sleep(float(env("FAKE_X265_VERSION_SLEEP", "0")))
    sys.stderr.write("x265 [info]: HEVC encoder version 4.1+1-1d117be\n")
    sys.stderr.write("x265 [info]: build info [Linux][GCC 15][64 bit] 8bit+10bit+12bit\n")
    sys.exit(0)
ran = os.environ.get("FAKE_X265_RAN")
if ran:
    with open(ran + ".args", "w") as f:
        f.write("\n".join(args))
    try:
        policy = os.sched_getscheduler(0)
    except AttributeError:                   # macOS has no sched_getscheduler
        policy = -1
    with open(ran + ".prio", "w") as f:
        f.write("%d %d" % (policy, os.getpriority(os.PRIO_PROCESS, 0)))
    with open(ran + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.rename(ran + ".tmp", ran)
if os.environ.get("FAKE_X265_SEGV"):
    os.kill(os.getpid(), signal.SIGSEGV)     # crash before reading any input
hang = os.environ.get("FAKE_X265_HANG", "")   # "closed" / "open" / "error"
early = os.environ.get("FAKE_X265_EARLY_RC")
summary = "\nencoded %d frames in 1.00s (8.00 fps), 1000.00 kb/s, Avg QP:20.00\n"
out = args[args.index("--output") + 1]
rpu = args[args.index("--dolby-vision-rpu") + 1] if "--dolby-vision-rpu" in args else None
rpu_bytes = open(rpu, "rb").read() if rpu else b""
sys.stderr.write("x265 [info]: fake encoder, output %s\n" % os.path.basename(out))
sys.stderr.flush()
inp = sys.stdin.buffer
n = 0
with open(out, "wb", buffering=0) as o:
    o.write(b"HEVC" + rpu_bytes)
    header = inp.readline()
    if early:                                 # fail after the header, input left unread
        sys.stderr.write("x265 [error]: fake early failure\n")
        sys.stderr.flush()
        sys.exit(int(early))
    if hang == "error":
        sys.stderr.write("x265 [error]: fake error, then a hang\n")
        sys.stderr.flush()
        time.sleep(60)
    if header:
        while True:
            if inp.readline() != b"FRAME\n":
                break
            if len(inp.read({frame})) < {frame}:
                break
            n += 1
            o.write(b"F%05d" % n)
            sys.stderr.write("%d frames: 8.00 fps, 1000.00 kb/s\r" % n)
            sys.stderr.flush()
    if hang == "open":                        # summary out, output still open, then a hang
        sys.stderr.write(summary % n)
        sys.stderr.flush()
        time.sleep(60)
sys.stderr.write(summary % n)
sys.stderr.flush()
if hang == "closed":                          # output closed, summary out, then a hang
    time.sleep(60)
sys.exit(int(os.environ.get("FAKE_X265_RC", "0")))
'''


def y4m(frames):
    return Y4M_HEADER + b"".join(b"FRAME\n" + bytes([i % 200 + 1]) * FRAME for i in range(frames))


def zstd_compress(data):
    return subprocess.run([ZSTD, "-1", "-q", "-c"], input=data, stdout=subprocess.PIPE,
                          check=True).stdout


def expected_hevc(frames, rpu=b""):
    """What `vworker encode` puts on stdout: the stream marker, then the fake x265's bytes."""
    return vworker.STREAM_MAGIC + b"HEVC" + rpu + b"".join(b"F%05d" % (i + 1) for i in range(frames))


def visionary_args(dv=True, **kw):
    """Visionary's real x265 argument list, binary dropped, with the wire placeholders."""
    return dvcap.build_x265_command("X", "{rpu}" if dv else None, "{out}", 50, dv=dv, **kw)[1:]


def b64spec(args, frames):
    return base64.b64encode(json.dumps({"x265_args": args, "frames": frames}).encode()).decode()


class FakeRunner:
    def __init__(self, x265=None, ip_out="", smi=None):
        self.x265, self.ip_out, self.smi, self.calls = x265, ip_out, smi, []

    def __call__(self, argv, timeout=2.0):
        self.calls.append(argv)
        if self.x265 and argv == [self.x265, "--version"]:
            return (0, "", "x265 [info]: HEVC encoder version 4.1+1-1d117be\n")
        if argv[0] == "ip":
            return (0, self.ip_out, "")
        if argv[0] == "nvidia-smi":
            return None if self.smi is None else (0, self.smi, "")
        return None


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vworker-test-")
        self.jobs = os.path.join(self.tmp, "jobs")
        self.proc = os.path.join(self.tmp, "proc")
        self.sys = os.path.join(self.tmp, "sys")
        os.makedirs(self.proc)
        make_sys(self.sys)
        self.x265 = os.path.join(self.tmp, "x265")
        with open(self.x265, "w") as f:
            f.write(FAKE_X265.replace("{python}", sys.executable).replace("{frame}", str(FRAME)))
        os.chmod(self.x265, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, **over):
        env = {"VWORKER_X265": self.x265, "VWORKER_ZSTD": ZSTD or "/nonexistent",
               "VWORKER_JOBS": self.jobs, "VWORKER_PROC": self.proc, "VWORKER_SYS": self.sys,
               "VWORKER_MIN_FREE_GB": "0", "VWORKER_STATUS_SAMPLE": "1"}
        env.update({("VWORKER_" + k.upper()): str(v) for k, v in over.items()})
        return vworker.Config(env)


# ---- argument allow-list --------------------------------------------------------------------

class ArgValidation(unittest.TestCase):
    def test_accepts_visionarys_real_dv_command(self):
        for kw in ({}, {"max_cll": "1000,400"},
                   {"master_display": "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)"
                                      "L(40000000,50)", "max_cll": "1000,400"},
                   {"crf": 16.5, "preset": "medium"}):
            args = visionary_args(True, **kw)
            self.assertEqual(vworker.validate_x265_args(args), args, kw)

    def test_accepts_visionarys_real_sdr_command(self):
        args = visionary_args(False)
        self.assertNotIn("--dolby-vision-rpu", args)
        self.assertEqual(vworker.validate_x265_args(args), args)

    def test_substitutes_placeholders_only_as_values(self):
        args = visionary_args(True)
        out = vworker.substitute(args, "/j/x/rpu.bin", "/j/x/out.fifo")
        self.assertEqual(out[out.index("--output") + 1], "/j/x/out.fifo")
        self.assertEqual(out[out.index("--dolby-vision-rpu") + 1], "/j/x/rpu.bin")
        self.assertEqual(len(out), len(args))

    def reject(self, args, why=None):
        with self.assertRaises(ValueError) as cm:
            vworker.validate_x265_args(args)
        if why:
            self.assertIn(why, str(cm.exception))

    def test_rejects_extra_flags(self):
        base = visionary_args(True)
        for extra in (["--frames", "5"], ["--csv", "log.csv"], ["--analysis-save", "a"],
                      ["--pools", "4"], ["--log-level", "full"]):
            self.reject(base + extra, "not allowed")

    def test_rejects_absolute_paths_and_dotdot(self):
        base = visionary_args(True, max_cll="1000,400")

        def swap(flag, value):
            a = list(base)
            a[a.index(flag) + 1] = value
            return a
        self.reject(swap("--output", "/home/bazzite/out.hevc"))
        self.reject(swap("--output", "{rpu}"))
        self.reject(swap("--dolby-vision-rpu", "/etc/passwd"))
        self.reject(swap("--dolby-vision-rpu", "../rpu.bin"))
        self.reject(swap("--preset", "../fast"))
        self.reject(swap("--transfer", "/smpte2084"))
        self.reject(swap("--preset", "fast;rm"))
        self.reject(swap("--crf", "-5"))
        self.reject(swap("--master-display", "G(1,2)"))
        self.reject(swap("--max-cll", "1000"))
        self.reject(swap("--dolby-vision-profile", "8"))

    def test_rejects_malformed_lists(self):
        base = visionary_args(False)
        self.reject(base + ["--crf", "18"], "twice")
        self.reject(base + ["--aud"], "twice")
        self.reject([a for a in base if a != "-"], "'-'")
        self.reject([a for a in base if a != "--y4m"], "--y4m")
        self.reject(base[:2] + base[4:], "--output")
        self.reject(base + ["--max-cll"], "needs a value")
        self.reject(["--y4m", "-", "--output", "{out}", "--preset", "--hrd"])
        self.reject(base + [5])
        self.reject("--y4m - --output {out}")
        self.reject([])

    def test_ids_and_names_match_whole_strings(self):
        self.assertTrue(vworker.JOB_ID_RE.fullmatch("seg_0001-abc"))
        for bad in ("abc\n", "a/b", "..", "", "x" * 65, "a.b"):
            self.assertIsNone(vworker.JOB_ID_RE.fullmatch(bad), bad)

    def test_decode_spec(self):
        args = visionary_args(True)
        self.assertEqual(vworker.decode_spec(b64spec(args, 240)), (args, 240))
        url = base64.urlsafe_b64encode(json.dumps({"x265_args": args, "frames": 3}).encode())
        self.assertEqual(vworker.decode_spec(url.decode().rstrip("="))[1], 3)
        for frames in (0, -1, True, "240", 2.5, None):
            with self.assertRaises(ValueError):
                vworker.decode_spec(b64spec(args, frames))
        for bad in ("not base64!", base64.b64encode(b"[1,2]").decode(),
                    base64.b64encode(b"{nope").decode()):
            with self.assertRaises(ValueError):
                vworker.decode_spec(bad)


# ---- detection: games, own tree, CPU, GPU ---------------------------------------------------

def P(pid, comm, argv, ppid=1, ticks=0, start=1000, flags=0):
    return vworker.Proc(pid, comm, "S", ppid, flags, ticks, start, list(argv))


def procs(*ps):
    return {p.pid: p for p in ps}


STEAM_DIR = "/var/home/bazzite/.local/share/Steam"


def shortcut_tree(app, base=50):
    """Steam (gamepadui) launching a non-Steam shortcut, as `ps` shows it on the Ally:
    steam -> reaper SteamLaunch AppId=<2^31 + n> -> steam-launch-wrapper -> the program.
    app: "firefox" / "dolphin" (native programs) or "proton" (a Windows game via Proton)."""
    wrap = STEAM_DIR + "/ubuntu12_32/steam-launch-wrapper"
    cmd = {"firefox": ["/usr/bin/flatpak", "run", "org.mozilla.firefox"],
           "dolphin": ["/usr/bin/dolphin"],
           "proton": [STEAM_DIR + "/ubuntu12_32/steam-runtime/..sniper/_v2-entry-point",
                      "--verb=waitforexitandrun", "--",
                      STEAM_DIR + "/steamapps/common/Proton 9.0 (Beta)/proton",
                      "waitforexitandrun", "/var/home/bazzite/Games/Celeste/Celeste.exe"]}[app]
    appid = {"firefox": 3141592653, "dolphin": 2898765432, "proton": 3000000001}[app]
    tree = [P(base, "steam", [STEAM_DIR + "/ubuntu12_32/steam", "-gamepadui", "-steamos3"]),
            P(base + 1, "reaper", [STEAM_DIR + "/ubuntu12_32/reaper", "SteamLaunch",
                                   "AppId=%d" % appid, "--", wrap, "--"] + cmd, ppid=base),
            P(base + 2, "steam-launch-wr", [wrap, "--"] + cmd, ppid=base + 1)]
    if app == "firefox":
        tree += [P(base + 3, "bwrap", ["bwrap", "--args", "40", "firefox"], ppid=base + 2),
                 P(base + 4, "firefox", ["/app/lib/firefox/firefox"], ppid=base + 3),
                 P(base + 5, "Isolated Web Co", ["/app/lib/firefox/firefox", "-contentproc",
                                                 "-childID", "1"], ppid=base + 4)]
    elif app == "dolphin":
        tree += [P(base + 3, "dolphin", ["/usr/bin/dolphin"], ppid=base + 2)]
    else:
        tree += [P(base + 3, "python3", ["python3", cmd[3], "waitforexitandrun", cmd[5]],
                   ppid=base + 2),
                 P(base + 4, "wineserver", ["wineserver"], ppid=base + 3),
                 P(base + 5, "Celeste.exe", ["Z:\\var\\home\\bazzite\\Games\\Celeste\\Celeste.exe"],
                   ppid=base + 3)]
    return tree


class GameDetection(unittest.TestCase):
    def game(self, *ps, exclude=()):
        return vworker.detect_game(procs(*ps), exclude)

    def test_steam_game_launches(self):
        reaper = P(10, "reaper", ["/home/b/.steam/steam/ubuntu12_32/reaper", "SteamLaunch",
                                  "AppId=1091500", "--", "/s/proton", "waitforexitandrun",
                                  "/games/Cyberpunk2077/bin/x64/Cyberpunk2077.exe"])
        self.assertEqual(self.game(reaper), "Cyberpunk2077.exe")
        self.assertEqual(self.game(P(11, "reaper", ["reaper", "AppId=620", "--", "./portal2"])),
                         "Steam app 620")
        self.assertIsNotNone(self.game(P(12, "pv-adverb", ["pv-adverb", "SteamLaunch", "x"])))

    def test_steam_client_and_menus_are_not_games(self):
        self.assertIsNone(self.game(
            P(20, "steam", ["/home/b/.steam/steam/ubuntu12_32/steam", "-gamepadui", "-steamos3"]),
            P(21, "steamwebhelper", ["./steamwebhelper", "-lang=en_US", "AppId=7", "SteamLaunch"]),
            P(22, "gamescope", ["gamescope", "-e", "--", "steam", "-gamepadui"]),
            P(23, "firefox", ["/usr/lib64/firefox/firefox"]),
            P(24, "dolphin", ["/usr/bin/dolphin"]),
            P(25, "someapp", ["someapp", "AppId=5"]),          # AppId= outside reaper
            P(26, "legendary", ["/usr/bin/python3", "/usr/bin/legendary", "status"]),
            P(27, "heroic", ["/opt/Heroic/heroic", "--no-sandbox"])))

    def test_wine_proton_lutris_heroic(self):
        self.assertIsNotNone(self.game(P(30, "wine64-preloader", ["wine64-preloader", "x.exe"])))
        self.assertIsNotNone(self.game(P(31, "wineserver", ["/usr/bin/wineserver"])))
        self.assertIsNotNone(self.game(P(32, "python3", ["python3", "/s/proton", "run", "g.exe"])))
        self.assertIsNotNone(self.game(P(33, "lutris-wrapper", ["lutris-wrapper", "Game", "0"])))
        self.assertIsNotNone(self.game(P(34, "python3", ["python3", "/usr/bin/legendary",
                                                         "launch", "Fortnite"])))
        self.assertIsNotNone(self.game(P(35, "gogdl", ["/opt/Heroic/gogdl", "--auth-config-path",
                                                       "a", "launch", "/g", "123"])))
        self.assertEqual(self.game(P(36, "Cyberpunk2077.e", ["Z:\\games\\Cyberpunk2077.exe"])),
                         "Cyberpunk2077.exe")

    def test_own_tree_and_kernel_threads_excluded(self):
        g = P(40, "wineserver", ["wineserver"])
        self.assertIsNone(self.game(g, exclude={40}))
        self.assertIsNone(self.game(P(41, "wine", ["wine"], ppid=2)))

    def test_reads_fake_proc_tree(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        add_proc(root, 50, "reaper", ["reaper", "SteamLaunch", "AppId=1"])
        add_proc(root, 51, "a (weird) name", ["x"], ppid=50)
        with open(os.path.join(root, "uptime"), "w") as f:
            f.write("100.0 50.0\n")
        ps = vworker.list_procs(root)
        self.assertEqual(sorted(ps), [50, 51])
        self.assertEqual(ps[51].comm, "a (weird) name")
        self.assertEqual(ps[51].ppid, 50)
        self.assertEqual(vworker.detect_game(ps), "Steam app 1")

    # -- non-Steam shortcuts (AppId >= 2^31)
    def test_firefox_and_dolphin_as_non_steam_shortcuts_are_not_games(self):
        tree = shortcut_tree("firefox") + shortcut_tree("dolphin", base=60)
        self.assertIsNone(self.game(*tree))
        # the boundary: 2^31 is a shortcut's AppId, 2^31 - 1 a real Steam app's
        native = ["--", "/usr/bin/dolphin"]
        self.assertIsNone(self.game(P(70, "reaper", ["reaper", "SteamLaunch", "AppId=2147483648"]
                                       + native)))
        self.assertEqual(self.game(P(71, "reaper", ["reaper", "SteamLaunch", "AppId=2147483647"]
                                      + native)), "Steam app 2147483647")
        # a SteamLaunch helper with no AppId of its own inherits the shortcut's from reaper
        self.assertIsNone(self.game(P(72, "reaper", ["reaper", "SteamLaunch", "AppId=3141592653",
                                                     "--", "pv-adverb"]),
                                    P(73, "pv-adverb", ["pv-adverb", "SteamLaunch", "x"], ppid=72),
                                    P(74, "firefox", ["/usr/lib64/firefox/firefox"], ppid=73)))

    def test_proton_game_as_a_shortcut_is_a_game_at_once(self):
        # at the instant of launch only reaper exists, and its own argv already runs Proton
        reaper = shortcut_tree("proton")[1]
        self.assertEqual(self.game(reaper), "Celeste.exe")
        self.assertEqual(self.game(*shortcut_tree("proton")), "Celeste.exe")
        # Wine anywhere below a shortcut whose own command line looks native
        tree = [P(80, "reaper", ["reaper", "SteamLaunch", "AppId=3000000002", "--",
                                 "/usr/bin/umu-run", "/games/x/start"]),
                P(81, "umu-run", ["/usr/bin/umu-run", "/games/x/start"], ppid=80),
                P(82, "bash", ["/bin/bash", "/games/x/start"], ppid=81),
                P(83, "wineserver", ["/opt/wine/bin/wineserver"], ppid=82)]
        self.assertEqual(self.game(*tree), "non-Steam game (wineserver)")
        tree[3] = P(83, "Game.exe", ["C:\\games\\x\\Game.exe"], ppid=82)
        self.assertEqual(self.game(*tree), "Game.exe")

    def test_real_steam_app_is_a_game_at_once(self):
        # a native Linux Steam game: no Wine anywhere, the AppId alone decides
        self.assertEqual(self.game(P(90, "reaper", ["reaper", "SteamLaunch", "AppId=570", "--",
                                                    "/s/steamapps/common/dota 2 beta/game/dota.sh"]),
                                   P(91, "dota2", ["./dota2"], ppid=90)),
                         "Steam app 570")


class OwnTree(unittest.TestCase):
    def test_us_children_and_session_ancestors_only(self):
        ps = procs(P(1, "systemd", ["/sbin/init"], ppid=0),
                   P(100, "sshd", ["sshd: /usr/sbin/sshd -D"]),
                   P(200, "sshd-session", ["sshd-session: bazzite [priv]"], ppid=100),
                   P(201, "sshd-session", ["sshd-session: bazzite@notty"], ppid=200),
                   P(300, "bash", ["bash", "-c", "vworker encode"], ppid=201),
                   P(400, "python3", ["python3", "vworker", "encode"], ppid=300),
                   P(401, "zstd", ["zstd", "-d"], ppid=400),
                   P(402, "x265", ["x265"], ppid=400),
                   P(210, "sshd-session", ["sshd-session: other"], ppid=100),
                   P(500, "firefox", ["firefox"]),
                   P(501, "Isolated Web Co", ["firefox", "-contentproc"], ppid=500))
        own = vworker.own_tree(ps, 400, 300)
        self.assertEqual(own, {100, 200, 201, 300, 400, 401, 402})

    def test_stops_at_user_systemd(self):
        ps = procs(P(900, "systemd", ["/usr/lib/systemd/systemd", "--user"]),
                   P(910, "konsole", ["konsole"], ppid=900),
                   P(920, "python3", ["vworker"], ppid=910))
        self.assertEqual(vworker.own_tree(ps, 920, 910), {910, 920})


class CpuAndHeavy(unittest.TestCase):
    def test_sampler_counts_others_only(self):
        s = vworker.CpuSampler(clk_tck=100)
        own = {400, 401}
        base = procs(P(400, "python3", [], ticks=0), P(401, "x265", [], ppid=400, ticks=0),
                     P(500, "ffmpeg", [], ticks=1000), P(600, "kworker", [], ppid=2, ticks=0),
                     P(700, "firefox", [], ticks=50))
        self.assertIsNone(s.sample(base, own, 10.0))
        later = procs(P(400, "python3", [], ticks=50), P(401, "x265", [], ppid=400, ticks=1600),
                      P(500, "ffmpeg", [], ticks=1000 + 500), P(600, "kworker", [], ppid=2, ticks=900),
                      P(700, "firefox", [], ticks=50 + 20))
        cores, what = s.sample(later, own, 12.0)
        self.assertAlmostEqual(cores, 2.6)                   # (500 + 20) / 100 / 2 s
        self.assertEqual(what, "ffmpeg 2.5 cores")

    def test_new_processes_count_only_if_born_in_the_interval(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        with open(os.path.join(root, "uptime"), "w") as f:
            f.write("1000.0 0\n")
        s = vworker.CpuSampler(root, clk_tck=100)
        s.sample({}, set(), 0.0)
        ps = procs(P(10, "make", [], ticks=300, start=int(999.5 * 100)),   # born 0.5 s ago
                   P(11, "chrome", [], ticks=90000, start=100 * 100))      # just unreadable before
        cores, what = s.sample(ps, set(), 2.0)
        self.assertAlmostEqual(cores, 1.5)
        self.assertTrue(what.startswith("make"))

    def test_pid_reuse_is_a_new_process(self):
        s = vworker.CpuSampler(clk_tck=100)
        s.sample(procs(P(10, "a", [], ticks=5000, start=1)), set(), 0.0)
        cores, _ = s.sample(procs(P(10, "b", [], ticks=40, start=999)), set(), 2.0)
        self.assertAlmostEqual(cores, 0.2)

    def test_sustained_load_trips_after_ten_seconds(self):
        h = vworker.HeavyTracker(10.0)
        t, hit = 0.0, None
        for _ in range(5):
            t += 2.0
            hit = h.update(t, t - 2.0, (2.0, "ffmpeg 2.0 cores"), (5.0, "GPU"))
            if t < 10.0:
                self.assertIsNone(hit)
        self.assertEqual(hit, "ffmpeg 2.0 cores")

    def test_momentary_spikes_never_trip(self):
        h = vworker.HeavyTracker(10.0)
        t = 0.0
        for i in range(30):
            t += 2.0
            busy = i % 4 != 3                     # 6 s heavy, 2 s quiet, repeat
            self.assertIsNone(h.update(t, t - 2.0, (4.0 if busy else 0.2, "x"), (0.0, "")))

    def test_gpu_alone_trips(self):
        h = vworker.HeavyTracker(10.0)
        hits = [h.update(t, t - 2.0, (0.1, "idle"), (85.0, "GPU card0 85% busy"))
                for t in (2.0, 4.0, 6.0, 8.0, 10.0)]
        self.assertEqual(hits[:4], [None] * 4)
        self.assertEqual(hits[4], "GPU card0 85% busy")

    def test_thresholds_are_exclusive(self):
        h = vworker.HeavyTracker(2.0)
        self.assertIsNone(h.update(2.0, 0.0, (1.5, "x"), (30.0, "g")))
        self.assertIsNone(h.update(4.0, 2.0, (1.5, "x"), (30.0, "g")))


class Gpu(unittest.TestCase):
    def test_drm_cards_and_nvidia(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        write(os.path.join(root, "class/drm/card0/device/gpu_busy_percent"), "12\n")
        write(os.path.join(root, "class/drm/card1/device/gpu_busy_percent"), "47\n")
        write(os.path.join(root, "class/drm/card1-HDMI-A-1/device/gpu_busy_percent"), "99\n")
        r = FakeRunner(smi="80\n")
        self.assertEqual(vworker.gpu_busy(root, r), (47.0, "GPU card1 47% busy"))
        self.assertEqual(r.calls, [])                        # no nvidia module: never asked
        os.makedirs(os.path.join(root, "module/nvidia"))
        self.assertEqual(vworker.gpu_busy(root, r)[0], 80.0)
        self.assertEqual(vworker.gpu_busy(root, FakeRunner(smi=None))[0], 47.0)   # no nvidia-smi


# ---- status ---------------------------------------------------------------------------------

class Status(Tmp):
    IP_OUT = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
              "2: enp4s0    inet 192.168.1.20/24 brd 192.168.1.255 scope global enp4s0\\  x\n"
              "3: wlan0    inet 192.168.1.30/24 brd 192.168.1.255 scope global wlan0\\  x\n"
              "3: wlan0    inet6 fe80::1234/64 scope link \\       valid_lft forever\n")

    def status(self, cfg=None, sleep=None, **kw):
        clock = iter(range(0, 1000, 1))
        kw.setdefault("runner", FakeRunner(self.x265, self.IP_OUT))
        kw.setdefault("self_pid", 4000)
        kw.setdefault("parent_pid", 3999)
        return vworker.compute_status(cfg or self.cfg(), clock=lambda: float(next(clock)),
                                      sleep=sleep or (lambda s: None), **kw)

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_ready(self):
        st = self.status()
        self.assertEqual(set(st), {"ok", "why", "code", "ac", "ethernet", "game", "heavy",
                                   "free_gb", "busy", "x265", "version"})
        self.assertEqual(st, dict(st, ok=True, why="ready", code="ready", ac=True, ethernet=True,
                                  game=None, heavy=None, busy=False, x265="4.1+1-1d117be",
                                  version=1))
        self.assertIsInstance(st["free_gb"], float)
        json.dumps(st)

    def test_on_battery(self):
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        write(os.path.join(self.sys, "class/power_supply/ucsi-source-psy-1/type"), "USB\n")
        write(os.path.join(self.sys, "class/power_supply/ucsi-source-psy-1/online"), "1\n")
        st = self.status()
        self.assertEqual((st["ok"], st["ac"], st["why"]), (False, False, "on battery"))
        self.assertEqual(st["code"], "battery")

    def test_no_ethernet(self):
        write(os.path.join(self.sys, "class/net/enp4s0/carrier"), "0\n")
        st = self.status()
        self.assertEqual((st["ok"], st["ethernet"], st["why"]), (False, False, "no Ethernet"))
        self.assertEqual(st["code"], "no_ethernet")
        write(os.path.join(self.sys, "class/net/enp4s0/carrier"), "1\n")
        write(os.path.join(self.sys, "class/net/enp4s0/operstate"), "dormant\n")
        self.assertFalse(self.status()["ethernet"])

    def test_ssh_must_arrive_on_ethernet(self):
        st = self.status(ssh_connection="192.168.1.5 50000 192.168.1.30 22")
        self.assertEqual((st["ok"], st["ethernet"]), (False, False))
        self.assertIn("wlan0", st["why"])
        self.assertEqual(st["code"], "no_ethernet")
        self.assertTrue(self.status(ssh_connection="192.168.1.5 50000 192.168.1.20 22")["ok"])
        self.assertFalse(self.status(ssh_connection="fe80::9 50000 fe80::1234 22")["ok"])
        self.assertTrue(self.status(ssh_connection="10.9.9.9 1 10.9.9.8 22")["ok"])  # unknown

    # the Ally's /sys as measured on it (2026-10-28): AC0 Mains online=1, the dock's USB
    # Ethernet enp11s0f3u1u2, Wi-Fi wlp6s0, tailscale0
    ALLY_IP_OUT = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "2: enp11s0f3u1u2    inet 192.168.1.133/24 brd 192.168.1.255 scope global dynamic "
        "noprefixroute enp11s0f3u1u2\\       valid_lft 85000sec\n"
        "3: wlp6s0    inet 192.168.1.134/24 brd 192.168.1.255 scope global dynamic wlp6s0\\  x\n"
        "3: wlp6s0    inet6 fe80::aa:bb/64 scope link noprefixroute \\       valid_lft forever\n"
        "4: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       x\n")

    def ally_sys(self):
        root = os.path.join(self.tmp, "ally-sys")
        write(os.path.join(root, "class/power_supply/AC0/type"), "Mains\n")
        write(os.path.join(root, "class/power_supply/AC0/online"), "1\n")
        write(os.path.join(root, "class/power_supply/BAT0/type"), "Battery\n")
        write(os.path.join(root, "class/power_supply/BAT0/online"), "1\n")
        for name, state in (("enp11s0f3u1u2", "up"), ("wlp6s0", "up"), ("tailscale0", "unknown"),
                            ("lo", "unknown")):
            write(os.path.join(root, "class/net", name, "operstate"), state + "\n")
            write(os.path.join(root, "class/net", name, "carrier"), "1\n")
        write(os.path.join(root, "class/drm/card1/device/gpu_busy_percent"), "2\n")
        return root

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_measured_ally_layout(self):
        root = self.ally_sys()
        cfg = self.cfg(sys=root)
        run = FakeRunner(self.x265, self.ALLY_IP_OUT)
        self.assertEqual(vworker.ethernet_links(root), ["enp11s0f3u1u2"])
        st = self.status(cfg, runner=run, ssh_connection="192.168.1.50 52000 192.168.1.133 22")
        self.assertEqual((st["ok"], st["why"], st["ac"], st["ethernet"], st["x265"],
                          st["version"]), (True, "ready", True, True, "4.1+1-1d117be", 1))
        for addr, iface in (("192.168.1.134", "wlp6s0"), ("fe80::aa:bb", "wlp6s0"),
                            ("100.101.102.103", "tailscale0")):
            st = self.status(cfg, runner=run, ssh_connection="10.0.0.1 52000 %s 22" % addr)
            self.assertEqual((st["ok"], st["ethernet"], st["why"]),
                             (False, False, "no Ethernet: ssh arrives on %s" % iface), addr)
        # the dock's cable pulled: Wi-Fi and Tailscale up are not Ethernet
        write(os.path.join(root, "class/net/enp11s0f3u1u2/carrier"), "0\n")
        st = self.status(cfg, runner=run)
        self.assertEqual((st["ok"], st["why"]), (False, "no Ethernet"))
        # undocked: AC0 offline
        write(os.path.join(root, "class/net/enp11s0f3u1u2/carrier"), "1\n")
        write(os.path.join(root, "class/power_supply/AC0/online"), "0\n")
        self.assertEqual(self.status(cfg, runner=run)["why"], "on battery")

    def test_game(self):
        add_proc(self.proc, 700, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                            "/s/proton", "waitforexitandrun", "Hades.exe"])
        st = self.status()
        self.assertEqual((st["ok"], st["game"], st["why"], st["code"]),
                         (False, "Hades.exe", "game running: Hades.exe", "game"))

    def test_heavy_cpu_but_not_our_own(self):
        add_proc(self.proc, 4000, "python3", ["python3", "vworker", "status"], ppid=3999)
        add_proc(self.proc, 4001, "x265", ["x265"], ppid=4000)
        add_proc(self.proc, 3999, "bash", ["bash"], ppid=1)
        clk = vworker._clk_tck()

        def own_busy(_):                       # our tree burns 8 cores during the sample
            set_ticks(self.proc, 4000, "python3", 3999, int(4 * clk))
            set_ticks(self.proc, 4001, "x265", 4000, int(4 * clk))
            set_ticks(self.proc, 3999, "bash", 1, int(2 * clk))
        self.assertTrue(self.status(sleep=own_busy)["ok"])

        add_proc(self.proc, 800, "ffmpeg", ["ffmpeg"], ticks=0)

        def other_busy(_):
            set_ticks(self.proc, 800, "ffmpeg", 1, int(2.5 * clk))
        st = self.status(sleep=other_busy)
        self.assertEqual((st["ok"], st["heavy"]), (False, "ffmpeg 2.5 cores"))
        self.assertEqual((st["why"], st["code"]), ("busy: ffmpeg 2.5 cores", "heavy"))

    def test_light_load_is_fine(self):
        add_proc(self.proc, 800, "steamwebhelper", ["steamwebhelper"], ticks=0)
        clk = vworker._clk_tck()
        st = self.status(sleep=lambda _: set_ticks(self.proc, 800, "steamwebhelper", 1,
                                                   int(0.4 * clk)))
        self.assertTrue(st["ok"], st)

    def test_heavy_gpu(self):
        write(os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent"), "85\n")
        st = self.status()
        self.assertEqual((st["ok"], st["heavy"], st["code"]), (False, "GPU card0 85% busy", "heavy"))

    def test_low_disk(self):
        st = self.status(self.cfg(min_free_gb=10 ** 9))
        self.assertFalse(st["ok"])
        self.assertTrue(st["why"].startswith("low disk"))
        self.assertEqual(st["code"], "low_disk")

    def test_x265_missing(self):
        os.unlink(self.x265)
        st = self.status()
        self.assertEqual((st["ok"], st["x265"]), (False, ""))
        self.assertIn("x265 missing", st["why"])
        self.assertEqual(st["code"], "x265_missing")

    def test_zstd_missing(self):
        st = self.status(self.cfg(zstd=os.path.join(self.tmp, "no-zstd")))
        self.assertFalse(st["ok"])
        self.assertTrue(st["why"].startswith("zstd missing"))
        self.assertEqual(st["code"], "zstd_missing")

    def test_busy_with_another_live_job_but_not_our_own(self):
        d = os.path.join(self.jobs, "other_job")
        os.makedirs(d)
        vworker.write_marker(d, "encode", self.proc)         # owned by this live test process
        st = self.status()
        self.assertEqual((st["ok"], st["busy"], st["why"], st["code"]),
                         (False, True, "busy: encoding job other_job", "busy"))
        self.assertTrue(self.status(exclude_job="other_job")["ok"])
        vworker.write_marker(d, "put", self.proc)            # a staged dir is not busy
        self.assertTrue(self.status()["ok"])

    def test_cli_answers_one_json_line_fast(self):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys, VWORKER_MIN_FREE_GB="0",
                   VWORKER_ZSTD=ZSTD or "/nonexistent")
        env.pop("VWORKER_STATUS_SAMPLE", None)
        env.pop("SSH_CONNECTION", None)
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, VWORKER, "status"], env=env, capture_output=True,
                           timeout=10)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["ok"], lines[0])
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        r = subprocess.run([sys.executable, VWORKER, "status"], env=env, capture_output=True,
                           timeout=10)
        self.assertEqual(r.returncode, 0)                    # exit 0 even when not ok
        self.assertFalse(json.loads(r.stdout)["ok"])


# ---- sweep / put / kill ---------------------------------------------------------------------

def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class SweepAndPut(Tmp):
    def mkjob(self, name, marker=None, age=0.0):
        d = os.path.join(self.jobs, name)
        os.makedirs(d)
        with open(os.path.join(d, "rpu.bin"), "wb") as f:
            f.write(b"r")
        if marker is not None:
            with open(os.path.join(d, vworker.OWNER_FILE), "w") as f:
                json.dump(marker, f)
        if age:
            t = time.time() - age
            os.utime(d, (t, t))
        return d

    def test_sweep_rules(self):
        now = time.time()
        gone = dead_pid()
        live = os.getpid()
        dead_encode = self.mkjob("dead_encode", {"pid": gone, "role": "encode", "t": now})
        live_encode = self.mkjob("live_encode", {"pid": live, "role": "encode", "t": now - 999})
        fresh_put = self.mkjob("fresh_put", {"pid": gone, "role": "put", "t": now - 5})
        old_put = self.mkjob("old_put", {"pid": gone, "role": "put", "t": now - 120})
        fresh_bare = self.mkjob("fresh_bare")
        old_bare = self.mkjob("old_bare", age=120)
        foreign = self.mkjob("not.a.job", age=999)
        removed = vworker.sweep(self.cfg(), now)
        self.assertEqual(sorted(removed), ["dead_encode", "old_bare", "old_put"])
        for d in (dead_encode, old_put, old_bare):
            self.assertFalse(os.path.exists(d))
        for d in (live_encode, fresh_put, fresh_bare, foreign):
            self.assertTrue(os.path.exists(d))

    def test_sweep_without_jobs_dir_is_a_noop(self):
        self.assertEqual(vworker.sweep(self.cfg()), [])
        self.assertFalse(os.path.exists(self.jobs))

    def run_cli(self, *args, stdin=b""):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys)
        return subprocess.run([sys.executable, VWORKER] + list(args), input=stdin, env=env,
                              capture_output=True, timeout=20)

    def test_sweep_cli(self):
        self.mkjob("abandoned", {"pid": dead_pid(), "role": "encode", "t": time.time()})
        r = self.run_cli("sweep")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["removed"], ["abandoned"])
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "abandoned")))

    def test_put(self):
        data = os.urandom(5000)
        r = self.run_cli("put", "job1", "rpu.bin", "5000", stdin=data)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.jobs, "job1", "rpu.bin"), "rb") as f:
            self.assertEqual(f.read(), data)
        info = json.loads(r.stdout)
        self.assertEqual((info["ok"], info["bytes"]), (True, 5000))
        m = vworker.read_marker(os.path.join(self.jobs, "job1"))
        self.assertEqual(m["role"], "put")
        # a staged dir survives the next invocation's sweep (the put process is gone by design)
        self.assertEqual(self.run_cli("sweep").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "job1", "rpu.bin")))

    def test_put_rejects_bad_names(self):
        for job, name in (("job1", ".owner"), ("job1", ".."), ("job1", "."), ("job1", "out.fifo"),
                          ("job1", "a/b"), ("../x", "rpu.bin"), ("job 1", "rpu.bin"),
                          ("job1", ".lock")):
            r = self.run_cli("put", job, name, "1", stdin=b"x")
            self.assertEqual(r.returncode, 1, (job, name))
            self.assertNotIn(b"usage", r.stderr)                 # rejected, not mis-called
        left = os.listdir(self.jobs) if os.path.isdir(self.jobs) else []
        self.assertEqual([n for n in left if n != vworker.LOCK_FILE], [])

    def test_put_rejects_bad_sizes(self):
        for size in ("", "-1", "abc", "1.5", "0x10", " 5", "9" * 13,
                     str(vworker.PUT_MAX_BYTES + 1)):
            r = self.run_cli("put", "job1", "rpu.bin", size, stdin=b"x")
            self.assertEqual(r.returncode, 1, size)
            self.assertIn("bad size", json.loads(r.stdout)["why"], size)
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))
        # the old 3-argument form is a usage error, never a put of "all of stdin"
        r = self.run_cli("put", "job1", "rpu.bin", stdin=b"x")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"usage", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))

    def test_put_zero_bytes(self):
        r = self.run_cli("put", "job0", "rpu.bin", "0", stdin=b"")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["bytes"], 0)
        self.assertEqual(os.path.getsize(os.path.join(self.jobs, "job0", "rpu.bin")), 0)

    def test_put_short_read_deletes_the_job_dir(self):
        r = self.run_cli("put", "job1", "rpu.bin", "5000", stdin=os.urandom(4999))
        self.assertEqual(r.returncode, 1)
        info = json.loads(r.stdout)
        self.assertFalse(info["ok"])
        self.assertIn("short read: got 4999 of 5000 bytes", info["why"])
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))

    def test_put_long_read_deletes_the_job_dir(self):
        r = self.run_cli("put", "job1", "rpu.bin", "100", stdin=os.urandom(101))
        self.assertEqual(r.returncode, 1)
        self.assertIn("long read: more than 100 bytes", json.loads(r.stdout)["why"])
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))

    def test_put_short_read_over_a_staged_job_deletes_it_too(self):
        self.assertEqual(self.run_cli("put", "job1", "rpu.bin", "3", stdin=b"abc").returncode, 0)
        r = self.run_cli("put", "job1", "rpu.bin", "10", stdin=b"abc")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))

    def test_put_signals_delete_the_job_dir(self):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys)
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            job = "sigput%d" % sig
            jobdir = os.path.join(self.jobs, job)
            p = subprocess.Popen([sys.executable, VWORKER, "put", job, "rpu.bin", "5000"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
            try:
                p.stdin.write(b"x" * 100)                     # part of it; the rest never comes
                p.stdin.flush()
                part = os.path.join(jobdir, ".rpu.bin.part")
                deadline = time.monotonic() + 10
                while not os.path.exists(part):
                    if time.monotonic() > deadline:
                        self.fail("put never started writing")
                    time.sleep(0.02)
                p.send_signal(sig)
                out, _ = p.communicate(timeout=10)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            self.assertEqual(p.returncode, 1, sig)
            self.assertFalse(os.path.exists(jobdir), "job dir left behind after %s" % sig)
            info = json.loads(out.decode().splitlines()[-1])
            self.assertEqual(info["why"], "put stopped by %s" % signal.Signals(sig).name)

    def test_put_has_a_deadline(self):
        # 7b: a put that has not finished within its deadline deletes the job dir and exits 1
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys, VWORKER_PUT_DEADLINE="0.5")
        self.assertEqual(vworker.Config({}).put_deadline, 60.0)
        jobdir = os.path.join(self.jobs, "slowput")
        p = subprocess.Popen([sys.executable, VWORKER, "put", "slowput", "rpu.bin", "5000"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
        try:
            p.stdin.write(b"x" * 100)                         # part of it; the rest never comes,
            p.stdin.flush()                                   # and stdin stays OPEN (no EOF)
            t0 = time.monotonic()
            p.wait(10)
            elapsed = time.monotonic() - t0
            out = p.stdout.read()
        finally:
            if p.poll() is None:
                p.kill()
            p.communicate()
        self.assertEqual(p.returncode, 1)
        self.assertLess(elapsed, 5)
        self.assertFalse(os.path.exists(jobdir))
        self.assertEqual(json.loads(out.decode().splitlines()[-1])["why"],
                         "put timed out after 0.5 s")

    def test_a_put_that_finishes_in_time_is_not_cut_by_its_deadline(self):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys, VWORKER_PUT_DEADLINE="0.3")
        r = subprocess.run([sys.executable, VWORKER, "put", "fast", "rpu.bin", "3"], input=b"abc",
                           env=env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.5)                                      # past the deadline: nothing fires
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "fast", "rpu.bin")))

    def test_sweep_removes_a_dead_put_holding_a_part_at_once(self):
        now = time.time()
        dead = self.mkjob("dead_put", {"pid": dead_pid(), "role": "put", "t": now - 1})
        with open(os.path.join(dead, ".rpu.bin.part"), "wb") as f:
            f.write(b"half")
        live = self.mkjob("live_put", {"pid": os.getpid(), "role": "put", "t": now - 1})
        with open(os.path.join(live, ".rpu.bin.part"), "wb") as f:
            f.write(b"half")
        staged = self.mkjob("staged", {"pid": dead_pid(), "role": "put", "t": now - 1})
        self.assertEqual(vworker.sweep(self.cfg(), now), ["dead_put"])
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(live))                 # still writing
        self.assertTrue(os.path.exists(staged))               # staged, within STAGE_TTL

    def test_unknown_command(self):
        self.assertEqual(self.run_cli("frobnicate").returncode, 1)


# ---- exit classification, child priority (units) --------------------------------------------

ZSTD_READ_FAIL = b"/*stdin*\\ : Read error (39) : premature end \n"     # zstd 1.5.7, verbatim
ZSTD_UNEXPECTED_END = b"zstd: /*stdin*\\: unexpected end of file \n"
ZSTD_WRITE_FAIL = b"zstd: error 70 : Write error : cannot write block : Broken pipe \n"  # rc 70


class Classify(Tmp):
    """EncodeJob._classify on synthetic end states: "disconnected" only when the Mac's input
    broke; x265 failing (zstd then dying on the dead pipe) is an "error" with x265's tail."""

    def classify(self, xrc, zrc, x265_err=b"", zstd_err=b"", frames=5, zstd_killed=False,
                 hung=None, **cfg):
        j = vworker.EncodeJob(self.cfg(**cfg), "seg_c")
        j.frames = frames
        j.x265 = types.SimpleNamespace(returncode=xrc)
        j.zstd = types.SimpleNamespace(returncode=zrc)
        j.err_tail, j.zstd_err, j.zstd_killed, j.hung = x265_err, zstd_err, zstd_killed, hung
        return j._classify()

    @staticmethod
    def summary(n):
        return (b"x265 [info]: tool\n%d frames: 8 fps\r\n"
                b"encoded %d frames in 1.00s (8.00 fps)\n" % (n, n))

    def test_x265_failure_with_zstd_dying_on_the_pipe_is_an_error(self):
        err = b"x265 [info]: fake\nx265 [error]: unable to open RPU file\n"
        for zrc, zerr in ((-signal.SIGPIPE, b""), (-signal.SIGPIPE, b"zstd: "),
                          (70, ZSTD_WRITE_FAIL), (0, b"")):
            r = self.classify(4, zrc, err, zerr)
            self.assertEqual(r, ("error", "x265 exited 4: x265 [error]: unable to open RPU file",
                                 0), (zrc, zerr))

    def test_x265_crash_is_an_error(self):
        r = self.classify(-signal.SIGSEGV, -signal.SIGPIPE)
        self.assertEqual(r, ("error", "x265 killed by SIGSEGV", 0))
        r = self.classify(-signal.SIGSEGV, 70, b"3 frames: 8 fps\r", ZSTD_WRITE_FAIL)
        self.assertEqual(r[0], "error")
        self.assertIn("x265 killed by SIGSEGV", r[1])

    def test_a_broken_input_stream_is_a_disconnect(self):
        for zerr in (ZSTD_READ_FAIL, ZSTD_UNEXPECTED_END):
            # x265 then failed on its truncated input ...
            r = self.classify(1, 1, b"x265 [error]: bad y4m frame\n", zerr)
            self.assertEqual(r[0], "disconnected", zerr)
            self.assertIn("input stream broken (zstd exited 1:", r[1])
            # ... or finished the frames it got
            r = self.classify(0, 1, self.summary(3), zerr)
            self.assertEqual((r[0], r[2]), ("disconnected", 3), zerr)
        # zstd failed while x265 exited 0: whatever zstd says, the input broke
        self.assertEqual(self.classify(0, 70, self.summary(5), ZSTD_WRITE_FAIL)[0], "disconnected")

    def test_a_zstd_we_killed_is_no_evidence(self):
        r = self.classify(1, -signal.SIGKILL, b"x265 [error]: boom\n", ZSTD_READ_FAIL,
                          zstd_killed=True)
        self.assertEqual(r[0], "error")

    def test_clean_exits(self):
        self.assertEqual(self.classify(0, 0, self.summary(5)), ("ok", "", 5))
        self.assertEqual(self.classify(0, 0, self.summary(3)),
                         ("disconnected", "input ended early: 3 of 5 frames", 3))
        self.assertEqual(self.classify(0, 0, self.summary(6)),
                         ("error", "x265 encoded 6 frames, expected 5", 6))
        self.assertEqual(self.classify(0, 0, b"no summary"),
                         ("error", "x265 reported no frame count", 0))

    def test_hung_x265(self):
        ok = self.classify(-signal.SIGKILL, 0, self.summary(5), hung=("its summary", True))
        self.assertEqual(ok, ("ok", "", 5))
        r = self.classify(-signal.SIGKILL, 0, self.summary(5), hung=("its summary", False))
        self.assertEqual(r, ("error", "x265 did not exit 5 s after its summary with its output "
                                      "still open", 5))
        r = self.classify(-signal.SIGKILL, 0, self.summary(4), hung=("its summary", True))
        self.assertEqual(r, ("error", "x265 did not exit 5 s after its summary (encoded 4 of 5 "
                                      "frames)", 4))
        r = self.classify(-signal.SIGKILL, -signal.SIGPIPE, b"x265 [error]: stuck\n",
                          hung=("an error line", False), exit_grace=0.5)
        self.assertEqual(r, ("error", "x265 did not exit 0.5 s after an error line: "
                                      "x265 [error]: stuck", 0))


class EarlySignals(Tmp):
    """7c: main installs SIGTERM/SIGHUP/SIGINT handlers before anything else, and a signal that
    lands that early still ends an encode or a put cleanly. In-process: the handlers are put
    back after each test."""

    def setUp(self):
        super().setUp()
        self.saved = {s: signal.getsignal(s) for s in vworker.STOP_SIGNALS + (signal.SIGALRM,)}
        vworker._EARLY.clear()

    def tearDown(self):
        for s, h in self.saved.items():
            signal.signal(s, h)
        vworker._EARLY.clear()
        super().tearDown()

    def test_main_installs_them_before_the_sweep(self):
        seen = {}

        def fake_sweep(cfg, now=None):
            seen.update({s: signal.getsignal(s) for s in vworker.STOP_SIGNALS})
            return []
        import io
        with mock.patch.object(vworker, "sweep", fake_sweep), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(vworker.main(["sweep"], self.cfg()), 0)
        for s in vworker.STOP_SIGNALS:
            self.assertIs(seen[s], vworker._note_early_signal, s)

    def test_an_early_signal_is_recorded_not_fatal(self):
        vworker.install_early_signals()
        os.kill(os.getpid(), signal.SIGHUP)
        deadline = time.monotonic() + 2
        while not vworker._EARLY and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(vworker._EARLY, [signal.SIGHUP])

    def test_an_early_signal_ends_an_encode_before_it_claims_anything(self):
        job = "seg_early"
        jobdir = os.path.join(self.jobs, job)
        os.makedirs(jobdir)
        with open(os.path.join(jobdir, vworker.OWNER_FILE), "w") as f:     # a staged put, its
            json.dump({"pid": dead_pid(), "role": "put", "t": time.time()}, f)  # put long gone
        vworker._EARLY.append(signal.SIGHUP)
        j = vworker.EncodeJob(self.cfg(), job)
        j.install_signals()
        self.assertEqual(j.prepare(b64spec(visionary_args(False), 5)),
                         ("disconnected", "terminated by SIGHUP"))
        self.assertTrue(j.owns_dir)                          # nobody else owns the staged dir
        self.assertEqual(j.run(), ("disconnected", "terminated by SIGHUP", 0))   # spawns nothing
        self.assertIsNone(j.x265)

    def test_an_early_signal_fails_a_put_at_once(self):
        import io
        vworker._EARLY.append(signal.SIGTERM)
        with mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            rc = vworker.cmd_put(self.cfg(), "job1", "rpu.bin", "3", stdin=io.BytesIO(b"abc"))
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["why"], "put stopped by SIGTERM")
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "job1")))
        for s in vworker.STOP_SIGNALS + (signal.SIGALRM,):   # its handlers were put back
            self.assertIs(signal.getsignal(s), self.saved[s])


class IdlePriority(unittest.TestCase):
    """zstd and x265 run under SCHED_IDLE (the Ally has sched_autogroup on, so nice alone does
    not let the desktop win), with nice 19 only where SCHED_IDLE is unavailable."""

    def test_prefers_sched_idle(self):
        with mock.patch.object(vworker.os, "SCHED_IDLE", 5, create=True), \
                mock.patch.object(vworker.os, "sched_param", lambda p: ("param", p), create=True), \
                mock.patch.object(vworker.os, "sched_setscheduler", create=True) as setsched, \
                mock.patch.object(vworker.os, "setpriority") as setprio:
            self.assertEqual(vworker._set_idle_priority(), "idle")
        setsched.assert_called_once_with(0, 5, ("param", 0))
        setprio.assert_not_called()

    def test_falls_back_to_nice_19(self):
        with mock.patch.object(vworker.os, "SCHED_IDLE", 5, create=True), \
                mock.patch.object(vworker.os, "sched_param", lambda p: p, create=True), \
                mock.patch.object(vworker.os, "sched_setscheduler", create=True,
                                  side_effect=PermissionError(1, "not permitted")), \
                mock.patch.object(vworker.os, "setpriority") as setprio:
            self.assertEqual(vworker._set_idle_priority(), "nice")
        setprio.assert_called_once_with(vworker.os.PRIO_PROCESS, 0, 19)

    def test_platform_without_sched_idle_uses_nice(self):
        with mock.patch.object(vworker.os, "setpriority") as setprio:
            for name in ("SCHED_IDLE", "sched_setscheduler", "sched_param"):
                if hasattr(vworker.os, name):
                    self.skipTest("this platform has SCHED_IDLE")
            self.assertEqual(vworker._set_idle_priority(), "nice")
        setprio.assert_called_once_with(vworker.os.PRIO_PROCESS, 0, 19)

    def test_child_setup_runs_it(self):
        # _LIBC off: in THIS process prctl(PDEATHSIG) and the parent check must not run
        with mock.patch.object(vworker, "_LIBC", None), \
                mock.patch.object(vworker, "_set_idle_priority") as idle:
            vworker._make_child_setup(os.getpid())()
        idle.assert_called_once_with()


# ---- encode, end to end ---------------------------------------------------------------------

class Running:
    """A vworker subprocess whose stdin we keep open; stdout/stderr drained in threads, and
    reaped as soon as it exits (as sshd does on the helper) so it never lingers as a zombie."""

    def __init__(self, argv, env, close_stdout=False):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        self.out, self.err = [], []
        self.threads = [threading.Thread(target=self._drain, args=(self.p.stderr, self.err))]
        threading.Thread(target=self.p.wait, daemon=True).start()
        if close_stdout:
            self.p.stdout.close()
        else:
            self.threads.append(threading.Thread(target=self._drain, args=(self.p.stdout, self.out)))
        for t in self.threads:
            t.daemon = True
            t.start()

    @staticmethod
    def _drain(f, into):
        for chunk in iter(lambda: f.read1(65536), b""):
            into.append(chunk)

    def feed(self, data, close=True):
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
            if close:
                self.p.stdin.close()
        except BrokenPipeError:
            pass

    def wait(self, timeout=20):
        try:
            rc = self.p.wait(timeout)
        finally:
            if self.p.poll() is None:
                self.p.kill()
        for t in self.threads:
            t.join(5)
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                f.close()
            except (BrokenPipeError, OSError):
                pass
        return rc

    @property
    def stdout(self):
        return b"".join(self.out)

    @property
    def stderr(self):
        return b"".join(self.err)

    def vline(self):
        lines = self.stderr.decode("utf-8", "replace").splitlines()
        last = lines[-1] if lines else ""
        assert last.startswith("VWORKER "), self.stderr[-500:]
        return json.loads(last[len("VWORKER "):])


class EncodeBase(Tmp):
    """Runs `vworker encode` as a subprocess against the fake /proc and /sys."""

    def setUp(self):
        super().setUp()
        self.ran = os.path.join(self.tmp, "x265.ran")

    def env(self, **over):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_ZSTD=ZSTD,
                   VWORKER_JOBS=self.jobs, VWORKER_PROC=self.proc, VWORKER_SYS=self.sys,
                   VWORKER_STATUS_SAMPLE="0.05", VWORKER_MIN_FREE_GB="0",
                   FAKE_X265_RAN=self.ran)
        env.pop("SSH_CONNECTION", None)
        env.update({k: str(v) for k, v in over.items()})
        return env

    def put_rpu(self, job, data=b"RPU-SLICE"):
        r = subprocess.run([sys.executable, VWORKER, "put", job, "rpu.bin", str(len(data))],
                           input=data, env=self.env(), capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)

    def start(self, job, frames, dv=True, args=None, close_stdout=False, **env):
        args = visionary_args(dv) if args is None else args
        return Running([sys.executable, VWORKER, "encode", job, b64spec(args, frames)],
                       self.env(**env), close_stdout)

    def wait_started(self, timeout=10):
        deadline = time.monotonic() + timeout
        while not os.path.exists(self.ran):
            if time.monotonic() > deadline:
                self.fail("fake x265 never started")
            time.sleep(0.02)
        with open(self.ran) as f:
            return int(f.read())

    def assert_cleaned(self, job, x265_pid=None):
        self.assertFalse(os.path.exists(os.path.join(self.jobs, job)), "job dir left behind")
        if x265_pid:
            deadline = time.monotonic() + 3
            while True:
                try:
                    os.kill(x265_pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() > deadline:
                    self.fail("x265 still running after vworker exited")
                time.sleep(0.02)


@unittest.skipUnless(ZSTD, "zstd not installed")
class EncodeEndToEnd(EncodeBase):
    # -- success
    def test_dv_success_streams_hevc_and_deletes_everything(self):
        self.put_rpu("seg_0007", b"RPU-SLICE-7")
        run = self.start("seg_0007", 5)
        run.feed(zstd_compress(y4m(5)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(5, b"RPU-SLICE-7"))
        self.assertEqual(run.vline(), {"result": "ok", "why": "", "frames": 5})
        self.assertIn(b"5 frames: 8.00 fps", run.stderr)         # x265's stderr is forwarded
        with open(self.ran + ".args") as f:
            used = f.read().splitlines()
        jobdir = os.path.join(self.jobs, "seg_0007")
        self.assertEqual(used[used.index("--output") + 1], os.path.join(jobdir, "out.fifo"))
        self.assertEqual(used[used.index("--dolby-vision-rpu") + 1], os.path.join(jobdir, "rpu.bin"))
        self.assert_cleaned("seg_0007")

    def test_sdr_success_without_rpu(self):
        run = self.start("seg_sdr", 40, dv=False)
        run.feed(zstd_compress(y4m(40)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(40))
        self.assertEqual(run.vline()["result"], "ok")
        self.assert_cleaned("seg_sdr")

    # -- yields
    def test_game_mid_encode_yields_within_3_seconds(self):
        self.put_rpu("seg_game")
        run = self.start("seg_game", 50)                      # default 2 s watchdog, stdin open
        pid = self.wait_started()
        time.sleep(0.3)
        add_proc(self.proc, FAKE_PID, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                                 "proton", "waitforexitandrun", "Hades.exe"])
        t0 = time.monotonic()
        rc = run.wait()
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(rc, 75, run.stderr[-800:])
        v = run.vline()
        self.assertEqual(v["result"], "yielded")
        self.assertEqual((v["why"], v["code"]), ("game running: Hades.exe", "game"))
        self.assert_cleaned("seg_game", pid)

    def test_sustained_gpu_load_yields(self):
        run = self.start("seg_gpu", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2, VWORKER_HEAVY_SECS=1.0)
        pid = self.wait_started()
        write(os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent"), "90\n")
        t0 = time.monotonic()
        rc = run.wait()
        self.assertGreater(time.monotonic() - t0, 0.6)        # not on the first busy sample
        self.assertEqual(rc, 75, run.stderr[-800:])
        self.assertEqual((run.vline()["why"], run.vline()["code"]),
                         ("heavy: GPU card0 90% busy", "heavy"))
        self.assert_cleaned("seg_gpu", pid)

    def test_momentary_load_does_not_yield(self):
        run = self.start("seg_spike", 8, dv=False, VWORKER_WATCH_INTERVAL=0.2,
                         VWORKER_HEAVY_SECS=1.5)
        self.wait_started()
        gpu = os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent")
        write(gpu, "95\n")
        time.sleep(0.5)
        write(gpu, "4\n")
        time.sleep(0.6)
        run.feed(zstd_compress(y4m(8)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(8))

    def test_undocking_stops_it(self):
        run = self.start("seg_undock", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        pid = self.wait_started()
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        self.assertEqual(run.wait(), 75)
        self.assertEqual((run.vline()["why"], run.vline()["code"]), ("on battery", "battery"))
        self.assert_cleaned("seg_undock", pid)

    # -- disconnects
    def test_truncated_stdin_is_a_disconnect(self):
        self.put_rpu("seg_trunc")
        blob = zstd_compress(y4m(30))
        run = self.start("seg_trunc", 30)
        run.feed(blob[:len(blob) // 2])
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        v = run.vline()
        self.assertEqual(v["result"], "disconnected")
        self.assertIn("zstd", v["why"])
        self.assert_cleaned("seg_trunc")

    def test_clean_stream_with_too_few_frames_is_a_disconnect(self):
        run = self.start("seg_short", 5, dv=False)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 76)
        self.assertEqual(run.vline(), {"result": "disconnected",
                                       "why": "input ended early: 3 of 5 frames", "frames": 3})
        self.assert_cleaned("seg_short")

    def test_too_many_frames_is_an_error(self):
        run = self.start("seg_long", 3, dv=False)
        run.feed(zstd_compress(y4m(5)))
        self.assertEqual(run.wait(), 1)
        self.assertEqual(run.vline()["result"], "error")
        self.assert_cleaned("seg_long")

    def test_x265_failure_is_an_error(self):
        run = self.start("seg_fail", 3, dv=False, FAKE_X265_RC=4)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 1)
        self.assertIn("x265 exited 4", run.vline()["why"])
        self.assert_cleaned("seg_fail")

    # -- x265 failing with its input unread: zstd dies on the dead pipe, still an "error"
    BIG = 3000                                  # ~300 KB of y4m: more than a pipe buffer

    def test_x265_failing_early_is_an_error_not_a_disconnect(self):
        run = self.start("seg_early", self.BIG, dv=False, FAKE_X265_EARLY_RC=4)
        run.feed(zstd_compress(y4m(self.BIG)))
        self.assertEqual(run.wait(), 1, run.stderr[-800:])
        v = run.vline()
        self.assertEqual(v, {"result": "error", "frames": 0,
                             "why": "x265 exited 4: x265 [error]: fake early failure"})
        self.assert_cleaned("seg_early")

    def test_x265_crashing_before_reading_is_an_error_not_a_disconnect(self):
        run = self.start("seg_segv", self.BIG, dv=False, FAKE_X265_SEGV=1)
        run.feed(zstd_compress(y4m(self.BIG)))
        self.assertEqual(run.wait(), 1, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "error", "why": "x265 killed by SIGSEGV",
                                       "frames": 0})
        self.assert_cleaned("seg_segv")

    # -- x265 hanging after its summary or an error line
    def test_x265_hanging_after_closing_its_output_is_ok(self):
        self.put_rpu("seg_hang_ok", b"RPU-H")
        run = self.start("seg_hang_ok", 6, FAKE_X265_HANG="closed", VWORKER_EXIT_GRACE=1)
        pid = self.wait_started()
        t0 = time.monotonic()
        run.feed(zstd_compress(y4m(6)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 15)              # not the fake's 60 s hang
        self.assertEqual(run.stdout, expected_hevc(6, b"RPU-H"))   # every byte copied
        self.assertEqual(run.vline(), {"result": "ok", "why": "", "frames": 6})
        self.assert_cleaned("seg_hang_ok", pid)

    def test_x265_hanging_with_the_wrong_count_is_an_error(self):
        run = self.start("seg_hang_short", 6, dv=False, FAKE_X265_HANG="closed",
                         VWORKER_EXIT_GRACE=1)
        pid = self.wait_started()
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 1, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "error", "frames": 4,
                                       "why": "x265 did not exit 1 s after its summary "
                                              "(encoded 4 of 6 frames)"})
        self.assert_cleaned("seg_hang_short", pid)

    def test_x265_hanging_with_its_output_open_is_an_error(self):
        run = self.start("seg_hang_open", 6, dv=False, FAKE_X265_HANG="open",
                         VWORKER_EXIT_GRACE=1)
        pid = self.wait_started()
        t0 = time.monotonic()
        run.feed(zstd_compress(y4m(6)))
        self.assertEqual(run.wait(), 1, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(run.vline(), {"result": "error", "frames": 6,
                                       "why": "x265 did not exit 1 s after its summary with "
                                              "its output still open"})
        self.assert_cleaned("seg_hang_open", pid)

    def test_x265_hanging_after_an_error_line_is_an_error(self):
        run = self.start("seg_hang_err", 6, dv=False, FAKE_X265_HANG="error",
                         VWORKER_EXIT_GRACE=1)
        pid = self.wait_started()
        t0 = time.monotonic()
        run.feed(zstd_compress(y4m(6)))
        self.assertEqual(run.wait(), 1, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 15)
        v = run.vline()
        self.assertEqual(v["result"], "error")
        self.assertEqual(v["why"], "x265 did not exit 1 s after an error line: "
                                   "x265 [error]: fake error, then a hang")
        self.assert_cleaned("seg_hang_err", pid)

    # -- non-Steam shortcuts: native apps are judged by the CPU/GPU rule only
    def add_tree(self, tree):
        for p in tree:
            add_proc(self.proc, p.pid, p.comm, p.argv, ppid=p.ppid)

    def test_firefox_and_dolphin_shortcuts_do_not_yield(self):
        run = self.start("seg_shortcut", 8, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        self.wait_started()
        self.add_tree(shortcut_tree("firefox", base=FAKE_PID))
        self.add_tree(shortcut_tree("dolphin", base=FAKE_PID + 10))
        time.sleep(1.0)                                      # five watchdog checks
        run.feed(zstd_compress(y4m(8)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(8))
        self.assertEqual(run.vline()["result"], "ok")

    def test_proton_shortcut_yields_at_once(self):
        run = self.start("seg_proton", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        pid = self.wait_started()
        self.add_tree(shortcut_tree("proton", base=FAKE_PID))
        t0 = time.monotonic()
        self.assertEqual(run.wait(), 75, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(run.vline()["why"], "game running: Celeste.exe")
        self.assert_cleaned("seg_proton", pid)

    # -- zstd and x265 run at idle priority
    def read_prio(self):
        with open(self.ran + ".prio") as f:
            policy, nice = (int(x) for x in f.read().split())
        return policy, nice

    @unittest.skipUnless(hasattr(os, "SCHED_IDLE") and hasattr(os, "sched_getscheduler"),
                         "no SCHED_IDLE here (macOS): check `chrt -p` on the helper once")
    def test_children_run_under_sched_idle(self):
        run = self.start("seg_idle", 3, dv=False)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(self.read_prio()[0], os.SCHED_IDLE)

    @unittest.skipIf(hasattr(os, "SCHED_IDLE"), "this platform has SCHED_IDLE")
    def test_children_fall_back_to_nice_19(self):
        run = self.start("seg_nice", 3, dv=False)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(self.read_prio()[1], 19)

    def test_broken_stdout_is_a_disconnect(self):
        # the stream marker is the first write: a broken stdout shows at once, before any data
        run = self.start("seg_pipe", 20, dv=False, close_stdout=True)
        run.feed(zstd_compress(y4m(20)), close=False)
        self.assertEqual(run.wait(), 76)
        self.assertTrue(run.vline()["why"].startswith("stdout broken"), run.vline())
        pid = None
        if os.path.exists(self.ran):
            with open(self.ran) as f:
                pid = int(f.read())
        self.assert_cleaned("seg_pipe", pid)

    def test_stall_is_a_disconnect(self):
        run = self.start("seg_stall", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2,
                         VWORKER_STALL_SECS=1.0)
        pid = self.wait_started()
        self.assertEqual(run.wait(), 76)
        self.assertTrue(run.vline()["why"].startswith("stalled"))
        self.assert_cleaned("seg_stall", pid)

    def signal_test(self, sig):
        job = "seg_sig%d" % sig
        self.put_rpu(job)
        run = self.start(job, 50)
        pid = self.wait_started()
        run.p.send_signal(sig)
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        v = run.vline()
        self.assertEqual(v["result"], "disconnected")
        self.assertIn(signal.Signals(sig).name, v["why"])
        self.assert_cleaned(job, pid)

    def test_sigterm(self):
        self.signal_test(signal.SIGTERM)

    def test_sighup(self):
        self.signal_test(signal.SIGHUP)

    def test_sigint(self):
        self.signal_test(signal.SIGINT)

    def test_a_signal_while_it_is_still_deciding_runs_nothing(self):
        # 7d: SIGTERM lands during prepare (inside its status sample): no x265, no dir, a
        # clean "disconnected"
        self.put_rpu("seg_prep")
        mark = os.path.join(self.tmp, "deciding")
        run = self.start("seg_prep", 5, FAKE_X265_VERSION_MARK=mark, FAKE_X265_VERSION_SLEEP=0.8)
        deadline = time.monotonic() + 10
        while not os.path.exists(mark):
            if time.monotonic() > deadline:
                self.fail("never reached its status sample")
            time.sleep(0.01)
        run.p.send_signal(signal.SIGTERM)
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "disconnected", "why": "terminated by SIGTERM",
                                       "frames": 0})
        self.assertEqual(run.stdout, b"")                    # no stream marker: nothing started
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_prep")

    # -- 7d: the machine's own side going away
    def test_ethernet_going_down_mid_encode_is_a_disconnect(self):
        run = self.start("seg_eth", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        pid = self.wait_started()
        write(os.path.join(self.sys, "class/net/enp4s0/carrier"), "0\n")
        t0 = time.monotonic()
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(run.vline(), {"result": "disconnected", "why": "Ethernet down", "frames": 0})
        self.assert_cleaned("seg_eth", pid)                  # dir gone AND x265 dead

    def test_parent_gone_is_a_disconnect(self):
        # the process that started vworker (sshd's session shell) dies: vworker is re-parented
        # and stops by itself, deleting everything
        job = "seg_orphan"
        pidfile = os.path.join(self.tmp, "vworker.pid")
        launcher = ("import os, sys, time\n"
                    "pid = os.fork()\n"
                    "if pid == 0:\n"
                    "    os.execv(sys.executable, [sys.executable] + sys.argv[2:])\n"
                    "with open(sys.argv[1] + '.tmp', 'w') as f:\n"
                    "    f.write(str(pid))\n"
                    "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                    "time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", launcher, pidfile, VWORKER, "encode", job,
                              b64spec(visionary_args(False), 50)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=self.env(VWORKER_WATCH_INTERVAL=0.2))
        box = {}
        readers = [threading.Thread(target=lambda: box.update(err=p.stderr.read()), daemon=True),
                   threading.Thread(target=lambda: box.update(out=p.stdout.read()), daemon=True)]
        for t in readers:
            t.start()
        try:
            x265_pid = self.wait_started()
            deadline = time.monotonic() + 10
            while not os.path.exists(pidfile):
                self.assertLess(time.monotonic(), deadline, "launcher never wrote the pid")
                time.sleep(0.01)
            with open(pidfile) as f:
                worker = int(f.read())
            p.kill()                                         # the parent is gone
            p.wait(5)
            for t in readers:
                t.join(15)                                   # EOF only once vworker has exited
            self.assertFalse(any(t.is_alive() for t in readers), "vworker never stopped")
        finally:
            p.stdin.close()
        last = box["err"].decode("utf-8", "replace").splitlines()[-1]
        self.assertEqual(json.loads(last[len("VWORKER "):]),
                         {"result": "disconnected", "why": "parent process gone", "frames": 0})
        self.assertTrue(box["out"].startswith(vworker.STREAM_MAGIC))
        self.assert_cleaned(job, x265_pid)
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(worker, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "vworker still running")
            time.sleep(0.05)

    # -- 7a: one heavy second at the start is not "anything heavy"
    def gpu_file(self):
        return os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent")

    def samples(self, log):
        if not os.path.exists(log):
            return 0
        with open(log) as f:
            return len(f.read().splitlines())

    def test_one_heavy_sample_at_the_start_does_not_refuse(self):
        write(self.gpu_file(), "90\n")
        log = os.path.join(self.tmp, "samples.log")
        run = self.start("seg_blip", 4, dv=False, FAKE_X265_VERSION_LOG=log,
                         FAKE_GPU_FILE=self.gpu_file(), FAKE_GPU_AFTER="4")
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(4))
        self.assertEqual(self.samples(log), 2)               # heavy, then a quiet re-sample

    def test_heavy_in_every_sample_refuses(self):
        write(self.gpu_file(), "90\n")
        log = os.path.join(self.tmp, "samples.log")
        run = self.start("seg_heavy", 4, dv=False, FAKE_X265_VERSION_LOG=log)
        run.feed(b"")
        self.assertEqual(run.wait(), 77, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "refused", "why": "busy: GPU card0 90% busy",
                                       "frames": 0, "code": "heavy"})
        self.assertEqual(vworker.HEAVY_SAMPLES, 3)
        self.assertEqual(self.samples(log), vworker.HEAVY_SAMPLES)
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_heavy")

    def test_a_game_battery_or_low_disk_refuse_without_resampling(self):
        log = os.path.join(self.tmp, "samples.log")
        add_proc(self.proc, FAKE_PID, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                                 "proton", "waitforexitandrun", "Hades.exe"])
        write(self.gpu_file(), "90\n")                       # heavy as well: the game decides
        for job, extra, why, code in (("seg_g", {}, "game running: Hades.exe", "game"),
                                      ("seg_d", {"VWORKER_MIN_FREE_GB": 10 ** 9}, "low disk",
                                       "low_disk")):
            if job == "seg_d":
                shutil.rmtree(os.path.join(self.proc, str(FAKE_PID)))
                write(self.gpu_file(), "3\n")
            if os.path.exists(log):
                os.unlink(log)
            run = self.start(job, 4, dv=False, FAKE_X265_VERSION_LOG=log, **extra)
            run.feed(b"")
            self.assertEqual(run.wait(), 77, run.stderr[-800:])
            self.assertTrue(run.vline()["why"].startswith(why), run.vline())
            self.assertEqual(run.vline()["code"], code)      # the Mac's line reads this word
            self.assertEqual(self.samples(log), 1, job)      # at once
            self.assert_cleaned(job)
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        write(self.gpu_file(), "90\n")
        os.unlink(log)
        run = self.start("seg_b", 4, dv=False, FAKE_X265_VERSION_LOG=log)
        run.feed(b"")
        self.assertEqual(run.wait(), 77)
        self.assertEqual(run.vline()["why"], "on battery")
        self.assertEqual(self.samples(log), 1)

    # -- refused / invalid
    def test_refused_when_not_ok_and_nothing_runs(self):
        self.put_rpu("seg_bat")
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        run = self.start("seg_bat", 5)
        run.feed(zstd_compress(y4m(5)))
        self.assertEqual(run.wait(), 77)
        self.assertEqual(run.vline(), {"result": "refused", "why": "on battery", "frames": 0,
                                       "code": "battery"})
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_bat")

    def test_refused_while_another_job_encodes(self):
        first = self.start("seg_a", 50, dv=False)
        self.wait_started()
        second = self.start("seg_b", 5, dv=False)
        second.feed(b"")
        self.assertEqual(second.wait(), 77)
        self.assertEqual(second.vline()["why"], "busy: encoding job seg_a")
        self.assertEqual(second.vline()["code"], "busy")
        self.assertTrue(os.path.isdir(os.path.join(self.jobs, "seg_a")))
        first.feed(zstd_compress(y4m(50)))
        self.assertEqual(first.wait(), 0, first.stderr[-800:])

    def test_disallowed_args_run_nothing(self):
        self.put_rpu("seg_bad")
        run = self.start("seg_bad", 5, args=visionary_args(True) + ["--csv", "x.csv"])
        run.feed(b"")
        self.assertEqual(run.wait(), 1)
        v = run.vline()
        self.assertEqual(v["result"], "error")
        self.assertIn("not allowed", v["why"])
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_bad")

    def test_missing_rpu_is_an_error(self):
        run = self.start("seg_norpu", 5)
        run.feed(b"")
        self.assertEqual(run.wait(), 1)
        self.assertIn("rpu.bin missing", run.vline()["why"])
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_norpu")

    def test_bad_job_id(self):
        run = Running([sys.executable, VWORKER, "encode", "../etc", b64spec(visionary_args(), 5)],
                      self.env())
        run.feed(b"")
        self.assertEqual(run.wait(), 1)
        self.assertEqual(run.vline()["why"], "bad job id")

    # -- kill
    def test_kill_stops_live_jobs_and_deletes_every_dir(self):
        run = self.start("seg_live", 50, dv=False)
        pid = self.wait_started()
        self.put_rpu("seg_staged")
        r = subprocess.run([sys.executable, VWORKER, "kill"], env=self.env(),
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["killed"], [run.p.pid])
        self.assertEqual(run.wait(), 76)
        self.assertIn("SIGTERM", run.vline()["why"])
        self.assert_cleaned("seg_live", pid)
        self.assertEqual([n for n in os.listdir(self.jobs) if n != vworker.LOCK_FILE], [])

    def kill(self, *job):
        return subprocess.run([sys.executable, VWORKER, "kill"] + list(job), env=self.env(),
                              capture_output=True, timeout=30)

    def test_kill_falls_back_to_sigkill(self):
        # 7d: a job process that ignores SIGTERM (wedged) gets SIGKILL after the wait — it and
        # its children — and its dir still goes
        job = "seg_stubborn"
        jobdir = os.path.join(self.jobs, job)
        os.makedirs(jobdir)
        code = ("import signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "c = subprocess.Popen(['sleep', '60'])\n"
                "print(c.pid, flush=True)\n"
                "time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
        try:
            child = int(p.stdout.readline())
            # the fake /proc: it looks like a vworker encode, with its child under it
            add_proc(self.proc, p.pid, "python3", ["python3", "vworker", "encode", job])
            add_proc(self.proc, child, "sleep", ["sleep", "60"], ppid=p.pid)
            with open(os.path.join(jobdir, vworker.OWNER_FILE), "w") as f:
                json.dump({"pid": p.pid, "start": None, "role": "encode", "t": time.time()}, f)
            t0 = time.monotonic()
            self.assertEqual(vworker.kill_all(self.cfg(), wait=0.5, job=job), [p.pid])
            self.assertGreaterEqual(time.monotonic() - t0, 0.5)   # SIGTERM first, then the wait
            self.assertEqual(p.wait(5), -signal.SIGKILL)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "the child survived the SIGKILL")
            time.sleep(0.05)
        self.assertFalse(os.path.exists(jobdir))

    def test_kill_with_a_job_id_touches_only_that_job(self):
        run = self.start("seg_live", 50, dv=False)
        pid = self.wait_started()
        self.put_rpu("seg_staged")
        self.put_rpu("seg_other")
        r = self.kill("seg_staged")                          # a staged put: no process to stop
        self.assertEqual((r.returncode, json.loads(r.stdout)["killed"]), (0, []), r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.jobs, "seg_staged")))
        for job in ("seg_other", "seg_live"):
            self.assertTrue(os.path.isdir(os.path.join(self.jobs, job)), job)
        self.assertIsNone(run.p.poll())                       # the live encode carries on
        r = self.kill("seg_live")
        self.assertEqual(json.loads(r.stdout)["killed"], [run.p.pid])
        self.assertEqual(run.wait(), 76)
        self.assertIn("SIGTERM", run.vline()["why"])
        self.assert_cleaned("seg_live", pid)
        self.assertTrue(os.path.isdir(os.path.join(self.jobs, "seg_other")))
        r = self.kill("no_such_job")                         # nothing there: a no-op
        self.assertEqual((r.returncode, json.loads(r.stdout)["killed"]), (0, []))
        for bad in (["../x"], ["a b"], ["seg_other", "extra"]):
            self.assertEqual(self.kill(*bad).returncode, 1, bad)
        self.assertTrue(os.path.isdir(os.path.join(self.jobs, "seg_other")))


# ---- opt-in: the real x265 ------------------------------------------------------------------

REAL_X265 = "/opt/homebrew/bin/x265"


def real_clip(frames, w=96, h=64, seed=7):
    """A tiny, deterministic 10-bit 4:2:0 y4m (limited range) with some motion and noise."""
    rnd = random.Random(seed)
    out = [b"YUV4MPEG2 W%d H%d F24000:1001 Ip A1:1 C420p10 XYSCSS=420P10\n" % (w, h)]
    for f in range(frames):
        y = [64 + (x * 5 + yy * 3 + f * 17 + rnd.randrange(24)) % 876
             for yy in range(h) for x in range(w)]
        c = [512 + ((x + yy + f) % 64) - 32 for yy in range(h // 2) for x in range(w // 2)]
        out.append(b"FRAME\n" + struct.pack("<%dH" % len(y), *y)
                   + struct.pack("<%dH" % len(c), *c) * 2)
    return b"".join(out)


@unittest.skipUnless(os.path.exists(REAL_X265) and ZSTD, "opt-in: needs %s and zstd" % REAL_X265)
class RealX265(EncodeBase):
    """Visionary's real SDR x265 command, through `vworker encode` (zstd stream in, FIFO out,
    HEVC on stdout) and directly: the two bitstreams must be byte-identical. (PDEATHSIG is
    Linux-only and must be checked once on the helper; see the module docstring.)"""

    def setUp(self):
        super().setUp()
        self.x265 = REAL_X265

    FRAMES = 6

    def compare(self, job, args, rpu=None):
        clip = real_clip(self.FRAMES)
        direct = os.path.join(self.tmp, "direct.hevc")
        r = subprocess.run([REAL_X265] + vworker.substitute(args, rpu, direct), input=clip,
                           capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        with open(direct, "rb") as f:
            want = f.read()
        self.assertGreater(len(want), 100)
        if rpu:
            with open(rpu, "rb") as f:
                self.put_rpu(job, f.read())
        run = Running([sys.executable, VWORKER, "encode", job, b64spec(args, self.FRAMES)],
                      self.env())
        run.feed(zstd_compress(clip))
        self.assertEqual(run.wait(120), 0, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "ok", "why": "", "frames": self.FRAMES})
        self.assertTrue(run.stdout.startswith(vworker.STREAM_MAGIC))
        got = run.stdout[len(vworker.STREAM_MAGIC):]
        self.assertEqual(len(got), len(want))
        self.assertTrue(got == want, "vworker's HEVC differs from a direct x265 run")
        self.assertIn(got[4] >> 1 & 0x3F, (32, 35))          # what the Mac checks: AUD or VPS first
        self.assert_cleaned(job)

    def test_sdr_output_is_byte_identical_to_a_direct_x265_run(self):
        self.compare("seg_real_sdr", visionary_args(False))

    @unittest.skipUnless(shutil.which("dovi_tool"), "needs dovi_tool to generate a test RPU")
    def test_dv_output_is_byte_identical_to_a_direct_x265_run(self):
        cfg = os.path.join(self.tmp, "rpu.json")
        with open(cfg, "w") as f:
            json.dump({"cm_version": "V40", "length": self.FRAMES,
                       "level6": {"max_display_mastering_luminance": 1000,
                                  "min_display_mastering_luminance": 1,
                                  "max_content_light_level": 1000,
                                  "max_frame_average_light_level": 400}}, f)
        rpu = os.path.join(self.tmp, "test-rpu.bin")
        r = subprocess.run(["dovi_tool", "generate", "-j", cfg, "-o", rpu],
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.compare("seg_real_dv", visionary_args(True, max_cll="1000,400"), rpu)


# ---- the shell scripts ----------------------------------------------------------------------

FAKE_SSH = r'''#!/bin/bash
# fake ssh for tests: record the call, drop the options, run the command locally in FAKE_HOME
echo "ssh $*" >> "$FAKE_SSH_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -i|-o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift                               # user@host
cd "$FAKE_HOME"
HOME="$FAKE_HOME" exec bash -c "$*"
'''

FAKE_SYSTEMCTL = '#!/bin/bash\necho "systemctl $*" >> "$FAKE_SSH_LOG"\n'


class Scripts(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ("install.sh", "build-x265.sh"):
            r = subprocess.run(["bash", "-n", os.path.join(WORKER_DIR, name)],
                               capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_build_script_pins_commit_and_version_file(self):
        with open(os.path.join(WORKER_DIR, "build-x265.sh")) as f:
            text = f.read()
        for needle in ("COMMIT=1d117be", 'WANT_VERSION="4.1+1-1d117be"', "BOX=visionary-worker",
                       "releasetagdistance: 1", "releasetag: 4.1", "repositorychangeset: 1d117be",
                       'rm -rf "$src/.git"', "-DLINKED_10BIT=ON", "-DLINKED_12BIT=ON",
                       "-DMAIN12=ON", "8bit+10bit+12bit", '"$ROOT/bin/x265"'):
            self.assertIn(needle, text)

    def test_install_over_fake_ssh_is_idempotent(self):
        tmp = tempfile.mkdtemp(prefix="vworker-install-")
        self.addCleanup(shutil.rmtree, tmp)
        fakebin, home = os.path.join(tmp, "bin"), os.path.join(tmp, "home")
        os.makedirs(fakebin)
        os.makedirs(home)
        for name, body in (("ssh", FAKE_SSH), ("systemctl", FAKE_SYSTEMCTL)):
            with open(os.path.join(fakebin, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(fakebin, name), 0o755)
        os.symlink(sys.executable, os.path.join(fakebin, "python3"))
        key = os.path.join(tmp, "id_ally")
        with open(key, "w") as f:
            f.write("not a real key\n")
        log = os.path.join(tmp, "ssh.log")
        env = {"PATH": fakebin + ":/usr/bin:/bin", "FAKE_HOME": home, "FAKE_SSH_LOG": log,
               "VWORKER_STATUS_SAMPLE": "0", "HOME": tmp}
        for _ in range(2):
            r = subprocess.run(["bash", os.path.join(WORKER_DIR, "install.sh"), "--host",
                                "ally.local", "--user", "bazzite", "--key", key],
                               env=env, capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
        installed = os.path.join(home, "visionary-worker", "bin", "vworker")
        with open(installed, "rb") as a, open(VWORKER, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertTrue(os.access(installed, os.X_OK))
        self.assertFalse(os.path.exists(installed.replace("vworker", ".vworker.new")))
        self.assertTrue(os.path.isdir(os.path.join(home, "visionary-scratch", "jobs")))
        units = os.path.join(home, ".config", "systemd", "user")
        with open(os.path.join(units, "visionary-worker-sweep.service")) as f:
            self.assertIn("ExecStart=%h/visionary-worker/bin/vworker sweep", f.read())
        with open(os.path.join(units, "visionary-worker-sweep.timer")) as f:
            timer = f.read()
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("WantedBy=timers.target", timer)
        with open(log) as f:
            calls = f.read()
        self.assertIn("-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 "
                      "-o ServerAliveCountMax=3 bazzite@ally.local", calls)
        self.assertEqual(calls.count("systemctl --user enable --now visionary-worker-sweep.timer"), 2)
        self.assertIn(b'"version":1', r.stdout)               # it printed the helper's status

    def test_install_usage_errors(self):
        r = subprocess.run(["bash", os.path.join(WORKER_DIR, "install.sh"), "--host", "x"],
                           capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(["bash", os.path.join(WORKER_DIR, "install.sh"), "-oProxyCommand=x",
                            "u", "k"], capture_output=True, timeout=10)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
