#!/usr/bin/env python3
"""vworker -- Visionary's optional x265 helper, running on a Linux machine on Ethernet ("the
helper"; first measured on a docked handheld PC).

The Mac decodes + colour-converts a segment itself (its SDR->PQ step is the only bit-exact one)
and streams the raw y4m to us, zstd-compressed, over ssh. We only run x265 on it and stream the
HEVC back on stdout as it is produced. Nothing we make is kept on the helper: the only file a job
ever owns is its RPU slice, and the job directory is deleted on every exit path.

Wire protocol v1 (the Mac half lives in engine/helper.py):
    vworker status                 one JSON line on stdout, exit 0 always, answers in < 3 s;
                                   "x265" is the full version token (e.g. "4.1+1-1d117be");
                                   "code" is the reason as one word (REASON_CODES) next to "why";
                                   "sha256" is this script's own sha256 (the Mac compares it with
                                   its copy); "ipv4" lists the IPv4 addresses on the wired links;
                                   "sleep_block" says whether this session may hold off sleep
                                   (true / false, null without systemd-inhibit)
    vworker put <job> <name> <bytes>
                                   exactly <bytes> of stdin -> jobs/<job>/<name> (the RPU slice,
                                   "rpu.bin"); a short or long read, SIGHUP/SIGTERM/SIGINT, or
                                   no finish within PUT_DEADLINE s deletes the job dir and exits 1
    vworker encode <job> <b64>     zstd -d | x265; on stdout STREAM_MAGIC (b"VWHEVC1\n") and then
                                   the HEVC -- the command runs through this user's login shell,
                                   so the Mac drops anything an rc file printed before the magic;
                                   x265's stderr on stderr, then a last stderr line
                                   VWORKER {"result":..,"why":..,"frames":..} (+ "code", one of
                                   REASON_CODES, on a yield or a refusal)
                                   exit 0 ok / 75 yielded / 76 disconnected / 77 refused / 1 error
    vworker kill [<job>]           kill that live job (every live job without one) and delete
                                   its job dir (every job dir)
    vworker sweep                  delete job dirs nobody owns any more
Every invocation installs its SIGTERM/SIGHUP/SIGINT handlers first, then sweeps.

Exit classification: "disconnected" only when the Mac's side went away -- the input stream
broke (zstd reports a read failure such as a premature end, or zstd failed while x265 exited
0, or the stream ended early), stdout/stderr broke, SIGHUP/SIGTERM, the parent died, Ethernet
went down, or no data arrived for STALL_SECS. x265 failing or crashing (zstd then dies on the
broken pipe) is an "error" carrying x265's stderr tail. An x265 that has not exited EXIT_GRACE
seconds after its "encoded N frames" summary or an "x265 [error]:" line is killed; that is ok
only when N == frames and x265 had already closed its output (every byte copied), else error.

Passive self-protection (independent of the Mac): an encode starts only when status is ok --
a heavy CPU/GPU reading alone is re-sampled (HEAVY_SAMPLES in all, ~1 s each) and refuses only
when every sample is heavy; a game, battery, no Ethernet, another job or low disk refuse at once.
While encoding, a watchdog checks every 2 s and stops at once -- killing zstd and x265 and
deleting the job dir -- when a game starts, when any other app uses a real share of CPU
(> 1.5 cores) or GPU (> 30 %) for 10 s, when the machine leaves mains power or Ethernet, when
the stream turns out to arrive over its Wi-Fi (WifiStream), or when the Mac goes away (parent
gone, stdout broken, input truncated, no data for STALL_SECS). Ordinary apps (browser, file
manager, Steam's own menus) are below those thresholds and do not stop it. A Steam launch is a
game when its AppId is a real Steam app (< 2^31) or Wine/Proton runs under it; a non-Steam
shortcut (AppId >= 2^31) running a native Linux program (Firefox, Dolphin, ...) is judged by the
CPU/GPU rule only. The GPU share is counted per process (nvidia-smi pmon, or DRM fdinfo: engine
time, or Intel xe's busy cycles) and leaves out the desktop's own drawing (compositor, Xwayland,
shell) and the Steam client (GPU_EXCLUDED_NAMES): an idle desktop alone can keep a GPU 30-40 %
busy. Where no per-process reading exists, the whole-device one stands in (amdgpu's
gpu_busy_percent, nvidia-smi); nouveau has neither, so the GPU rule has no reading there.
On mains: a Mains supply online, or no system battery at all (a desktop, a mini PC, a VM); a
laptop or handheld on battery is refused. Ethernet: any wired link that is up, told by /sys and
not by its name (eth0, eno1, enp*, enx*, or a bridge or bond over one).
zstd and x265 run under SCHED_IDLE (nice 19 where that is unavailable): most desktop distros
turn sched_autogroup on, so nice alone would not let the desktop win. While they run, logind
sleep and idle blocks are asked for (systemd-inhibit, where it exists). polkit decides: over ssh
a stock setup usually refuses the sleep block (no seat, no agent), and then an idle helper on
automatic suspend can still sleep mid-segment. Status reports which it is as "sleep_block".

Stdlib only. Runs on Python 3.8 or newer (no annotations, nothing newer from the stdlib; the
Mac's tests run it too). Paths and timings can be overridden through VWORKER_* environment
variables (tests use them).
"""
import base64
import binascii
import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

try:                                   # only used for PR_SET_PDEATHSIG on Linux
    import ctypes
except ImportError:                    # pragma: no cover
    ctypes = None

PROTOCOL_VERSION = 1

EXIT_FOR = {"ok": 0, "error": 1, "yielded": 75, "disconnected": 76, "refused": 77}
STREAM_MAGIC = b"VWHEVC1\n"            # on stdout before the HEVC: the Mac skips shell noise up to it
# Why not ok, as one word: the Mac's status line is classified on these, never on the "why" text
REASON_CODES = ("ready", "battery", "no_ethernet", "game", "busy", "heavy", "low_disk",
                "x265_missing", "zstd_missing", "error")
HEAVY_SAMPLES = 3                      # status samples an encode takes before refusing as heavy
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")      # always used with fullmatch
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
OWNER_FILE = ".owner"                  # the job dir's owner marker (pid + start time + role)
FIFO_NAME = "out.fifo"
RPU_NAME = "rpu.bin"
LOCK_FILE = ".lock"                    # in the jobs root; serialises claim/sweep decisions
RESERVED_NAMES = {FIFO_NAME, OWNER_FILE, LOCK_FILE}

CPU_CORES_LIMIT = 1.5                  # others' CPU, in cores
GPU_BUSY_LIMIT = 30.0                  # percent, any GPU
CHILD_NICE = 19                        # only where SCHED_IDLE is unavailable
PUT_MAX_BYTES = 256 << 20              # an RPU slice is KBs; refuse anything absurd
# free disk an encode needs: jobs stream (only the RPU slice and a FIFO are ever stored), so
# this is headroom, not room for a segment
MIN_FREE_GB = 5.0
PF_KTHREAD = 0x00200000

# where zstd lives when it is not on the login shell's PATH (the first is named when missing)
ZSTD_PATHS = ("/usr/bin/zstd", "/usr/local/bin/zstd")
# held while an encode runs: logind then blocks suspend (and, beside it, the idle action) until
# they exit. Two blocks, not one "sleep:idle": polkit judges each kind on its own (older logind
# checked a combined mask as the lid-switch action), and over ssh it often allows idle, not sleep
INHIBIT_ARGS = ["--what=sleep", "--who=visionary-helper", "--why=encoding a segment",
                "--mode=block", "sleep", "infinity"]
IDLE_INHIBIT_ARGS = ["--what=idle", "--who=visionary-helper", "--why=encoding a segment",
                     "--mode=block", "sleep", "infinity"]
# status's probe: may this session take the sleep block? (taken and let go at once by `true`)
SLEEP_PROBE_ARGS = ["--what=sleep", "--mode=block", "--who=visionary-helper", "--why=probe",
                    "true"]
SLEEP_PROBE_TIMEOUT = 1.0
SLEEP_PROBE_TTL = 60.0                 # one probe a minute per process (an encode asks 1-3 times)

# processes that are never a game even if their arguments look like one
NOT_GAMES = {"steam", "steamwebhelper"}
# a process with one of these names IS a game (Windows program under Wine/Proton, Lutris)
GAME_PROCESS_NAMES = {"wine64-preloader", "wine-preloader", "wineserver", "wine", "wine64",
                      "lutris-wrapper", "proton"}
# Wine/Proton markers: under a non-Steam shortcut one of these makes it a game
WINE_PROCESS_NAMES = {"wine64-preloader", "wine-preloader", "wineserver", "wine", "wine64",
                      "proton"}
# Steam gives non-Steam shortcuts AppIds with the top bit set; real Steam apps are below
SHORTCUT_APPID_MIN = 1 << 31
# store launchers Heroic drives; they only count while launching a game ("... launch ...")
GAME_LAUNCHERS = {"legendary", "gogdl", "nile"}
_WIN_PATH = re.compile(r"[A-Za-z]:\\")
_APPID = re.compile(r"AppId=(\d+)")

# the desktop's own drawing: compositors, window managers, shells and X servers keep a GPU busy
# on an idle desktop, so their GPU use (and the Steam client's) is never "anything heavy".
# Cinnamon composites in its `cinnamon` process (muffin), COSMIC in cosmic-comp, Pantheon in
# gala; a name over 15 characters is also matched as the kernel cuts it (_GPU_EXCLUDED_CUT)
DESKTOP_PROCESS_NAMES = {"kwin_wayland", "kwin_x11", "kwin", "Xwayland", "Xorg", "X",
                         "gnome-shell", "mutter", "mutter-x11-frames", "plasmashell",
                         "xwaylandvideobridge", "sway", "Hyprland", "weston", "labwc", "wayfire",
                         "gamescope", "gamescope-wl", "cinnamon", "muffin", "cosmic-comp", "gala",
                         "budgie-wm", "xfwm4", "picom", "compton", "marco", "metacity",
                         "openbox", "compiz", "enlightenment", "niri", "river"}
GPU_EXCLUDED_NAMES = DESKTOP_PROCESS_NAMES | NOT_GAMES
_COMM_LEN = 15                         # the kernel cuts /proc/<pid>/comm to 15 characters
_GPU_EXCLUDED_CUT = {n[:_COMM_LEN] for n in GPU_EXCLUDED_NAMES if len(n) > _COMM_LEN}
PMON_ARGV = ["nvidia-smi", "pmon", "-c", "1", "-s", "u"]   # one ~1 s sample, sm % per process
PMON_TIMEOUT = 2.5


# ---- configuration --------------------------------------------------------------------------

def _env_float(env, name, default):
    try:
        return float(env.get(name, ""))
    except ValueError:
        return default


def _executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_zstd(path=None):
    """zstd on PATH, else in ZSTD_PATHS; ZSTD_PATHS[0] when there is none (status names it)."""
    found = shutil.which("zstd", path=path)
    if found:
        return found
    for p in ZSTD_PATHS:
        if _executable(p):
            return p
    return ZSTD_PATHS[0]


def find_inhibit(path=None):
    """systemd-inhibit, or "" where there is none (then no sleep block is taken)."""
    found = shutil.which("systemd-inhibit", path=path)
    if found:
        return found
    return "/usr/bin/systemd-inhibit" if _executable("/usr/bin/systemd-inhibit") else ""


_SLEEP_PROBE = {}                      # systemd-inhibit path -> (monotonic time, answer)


def sleep_block(inhibit, clock=time.monotonic):
    """May this session hold off sleep? Runs `systemd-inhibit --what=sleep --mode=block ...
    true` (SLEEP_PROBE_ARGS; stdin, stdout and stderr on /dev/null, so polkit has no agent to
    ask, as in an encode) -> True when it exits 0, False when it fails or takes longer than
    SLEEP_PROBE_TIMEOUT (polkit said no: over ssh a stock setup has no seat to allow it), None
    when there is no systemd-inhibit. The answer is kept SLEEP_PROBE_TTL s in this process."""
    if not inhibit:
        return None
    now = clock()
    hit = _SLEEP_PROBE.get(inhibit)
    if hit is not None and now - hit[0] < SLEEP_PROBE_TTL:
        return hit[1]
    try:
        r = subprocess.run([inhibit] + SLEEP_PROBE_ARGS, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=SLEEP_PROBE_TIMEOUT)
        got = r.returncode == 0
    except (FileNotFoundError, NotADirectoryError):
        got = None
    except (subprocess.SubprocessError, OSError, ValueError):     # a timeout included
        got = False
    _SLEEP_PROBE[inhibit] = (now, got)
    return got


def _start_sleep_probe(inhibit):
    """sleep_block() in a thread, so it runs beside status's 1 s sample. -> a function that
    waits for its answer (None if it never came)."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=sleep_block(inhibit)), name="sleep-probe",
                         daemon=True)
    t.start()

    def answer():
        t.join(SLEEP_PROBE_TIMEOUT + 2.0)
        return box.get("v")
    return answer


class Config:
    """Paths and timings. Everything is overridable from the environment (VWORKER_*)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = os.path.expanduser("~")
        self.x265 = env.get("VWORKER_X265") or os.path.join(home, "visionary-worker", "bin", "x265")
        self.zstd = env.get("VWORKER_ZSTD") or find_zstd(env.get("PATH"))
        self.inhibit = env.get("VWORKER_INHIBIT") or find_inhibit(env.get("PATH"))
        self.jobs = env.get("VWORKER_JOBS") or os.path.join(home, "visionary-scratch", "jobs")
        self.proc = env.get("VWORKER_PROC") or "/proc"
        self.sys = env.get("VWORKER_SYS") or "/sys"
        self.watch_interval = _env_float(env, "VWORKER_WATCH_INTERVAL", 2.0)
        self.heavy_secs = _env_float(env, "VWORKER_HEAVY_SECS", 10.0)
        self.status_sample = _env_float(env, "VWORKER_STATUS_SAMPLE", 1.0)
        self.stall_secs = _env_float(env, "VWORKER_STALL_SECS", 300.0)
        self.min_free_gb = _env_float(env, "VWORKER_MIN_FREE_GB", MIN_FREE_GB)
        self.stage_ttl = _env_float(env, "VWORKER_STAGE_TTL", 60.0)     # put -> encode gap
        self.put_deadline = _env_float(env, "VWORKER_PUT_DEADLINE", 60.0)   # a whole put
        self.orphan_ttl = _env_float(env, "VWORKER_ORPHAN_TTL", 60.0)   # dir without marker
        # how long x265 may live on after its summary or an error line before it is killed
        self.exit_grace = _env_float(env, "VWORKER_EXIT_GRACE", 5.0)


# ---- small helpers --------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def default_runner(argv, timeout=2.0):
    """Run a short command. -> (rc, stdout, stderr), or None when the program does not exist."""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return None
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except OSError as e:
        return (126, "", str(e))
    return (r.returncode, r.stdout, r.stderr)


def _clk_tck():
    try:
        return float(os.sysconf("SC_CLK_TCK")) or 100.0
    except (ValueError, OSError, AttributeError):
        return 100.0


# ---- /sys: power and network ----------------------------------------------------------------

# a system battery's status while the charger is in (with no Mains supply to ask)
ON_AC_BATTERY_STATUS = {"Charging", "Full", "Not charging"}


def ac_online(sys_root):
    """On mains power. A Mains supply that is online says yes. A machine with no system battery
    is mains-powered (a desktop, a mini PC, a VM; a controller's or a mouse's battery has scope
    "Device" and does not count). With a system battery: a Mains supply that exists but is not
    online says no (a USB-C source alone does not count); with no Mains supply at all, the
    batteries' status decides -- "Charging", "Full" or "Not charging" is on AC, "Discharging"
    (or nothing known) is on battery. A laptop or handheld on battery is refused."""
    mains, batteries = [], []
    for d in sorted(glob.glob(os.path.join(sys_root, "class", "power_supply", "*"))):
        kind = _read(os.path.join(d, "type"))
        if kind == "Mains":
            mains.append(_read(os.path.join(d, "online")) == "1")
        elif kind == "Battery" and _read(os.path.join(d, "scope")) != "Device":
            batteries.append(_read(os.path.join(d, "status")))
    if any(mains):
        return True
    if not batteries:
        return True
    if mains:
        return False
    return "Discharging" not in batteries and any(s in ON_AC_BATTERY_STATUS for s in batteries)


ARPHRD_ETHER = "1"                     # /sys/class/net/<if>/type of Ethernet (and its bridges)


def _link_up(d):
    """operstate "up", or "unknown" (a driver that does not track it) with carrier 1."""
    carrier = _read(os.path.join(d, "carrier"))
    if carrier == "0":
        return False
    state = _read(os.path.join(d, "operstate"))
    return state == "up" or (state == "unknown" and carrier == "1")


def _wireless(d):
    """Is /sys/class/net/<if> `d` a Wi-Fi interface (wireless/ or phy80211)?"""
    return (os.path.lexists(os.path.join(d, "wireless"))
            or os.path.lexists(os.path.join(d, "phy80211")))


def _lower_links(d):
    """A bridge's, bond's or VLAN's member interfaces: brif/*, bonding/slaves and lower_*."""
    names = set()
    try:
        names.update(os.listdir(os.path.join(d, "brif")))
    except OSError:
        pass
    names.update((_read(os.path.join(d, "bonding", "slaves")) or "").split())
    try:
        names.update(n[len("lower_"):] for n in os.listdir(d) if n.startswith("lower_"))
    except OSError:
        pass
    return sorted(names)


def is_wired(sys_root, name, _depth=0):
    """Is interface `name` an Ethernet link that is up? Told by /sys, never by the name: type 1
    (ARPHRD_ETHER), no wireless/ or phy80211, up (_link_up), and either a physical device (its
    /sys/class/net entry does not resolve under devices/virtual) or a bridge, bond or VLAN with
    such a member that is up. Loopback, tun, WireGuard and Tailscale are not type 1; docker,
    veth, virbr and tap are virtual with no physical member."""
    if _depth > 4 or not name or "/" in name or name.startswith("."):
        return False
    d = os.path.join(sys_root, "class", "net", name)
    if _read(os.path.join(d, "type")) != ARPHRD_ETHER:
        return False
    if _wireless(d):
        return False
    if not _link_up(d):
        return False
    virtual = os.path.realpath(os.path.join(sys_root, "devices", "virtual")) + os.sep
    if not os.path.realpath(d).startswith(virtual):
        return True
    return any(is_wired(sys_root, m, _depth + 1) for m in _lower_links(d))


def ethernet_links(sys_root):
    """Names of the wired interfaces that are up (is_wired), in name order."""
    try:
        names = sorted(os.listdir(os.path.join(sys_root, "class", "net")))
    except OSError:
        return []
    return [n for n in names if is_wired(sys_root, n)]


_IPV4 = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def wired_ipv4(links, runner):
    """The IPv4 addresses (no prefix) on the interfaces in `links`, in that order, from
    `ip -4 -o addr show`; [] when there are none or `ip` cannot tell."""
    if not links:
        return []
    try:
        argv = ["-4", "-o", "addr", "show"]
        r = runner(["ip"] + argv, 1.0) or runner(["/usr/sbin/ip"] + argv, 1.0)
        if not r or r[0] != 0:
            return []
        by_link = {}
        for line in (r[1] or "").splitlines():
            f = line.split()
            if len(f) < 4 or f[2] != "inet":
                continue
            addr = f[3].split("/")[0]
            if _IPV4.fullmatch(addr):
                by_link.setdefault(f[1].rstrip(":").split("@")[0], []).append(addr)
        out = []
        for link in links:
            for addr in by_link.get(link, ()):
                if addr not in out:
                    out.append(addr)
        return out
    except Exception:
        return []


def ssh_arrival_iface(ssh_connection, runner):
    """The local interface our ssh session arrived on (from SSH_CONNECTION's server address and
    `ip -o addr`), or None when it cannot be told."""
    parts = (ssh_connection or "").split()
    if len(parts) < 4:
        return None
    local = parts[2].split("%")[0].lower()
    if local.startswith("::ffff:"):
        local = local[7:]
    r = runner(["ip", "-o", "addr", "show"], 1.0) or runner(["/usr/sbin/ip", "-o", "addr", "show"], 1.0)
    if not r or r[0] != 0:
        return None
    for line in r[1].splitlines():
        f = line.split()
        if len(f) < 4 or f[2] not in ("inet", "inet6"):
            continue
        if f[3].split("/")[0].split("%")[0].lower() == local:
            return f[1].rstrip(":").split("@")[0]
    return None


def wireless_links(sys_root):
    """Names of the Wi-Fi interfaces that are up (_wireless, _link_up), in name order."""
    try:
        names = sorted(os.listdir(os.path.join(sys_root, "class", "net")))
    except OSError:
        return []
    out = []
    for n in names:
        d = os.path.join(sys_root, "class", "net", n)
        if _wireless(d) and _link_up(d):
            out.append(n)
    return out


def rx_bytes(sys_root, name):
    """Bytes interface `name` has received (statistics/rx_bytes), or None when unreadable."""
    v = _read(os.path.join(sys_root, "class", "net", name, "statistics", "rx_bytes"))
    return int(v) if v and v.isdigit() else None


def read_rchar(proc_root, pid):
    """Bytes process `pid` has read (rchar of /proc/<pid>/io, pipes included), or None."""
    for line in (_read(os.path.join(proc_root, str(pid), "io")) or "").splitlines():
        k, _sep, v = line.partition(":")
        if k.strip() == "rchar" and v.strip().isdigit():
            return int(v)
    return None


# the stream is on Wi-Fi when, WIFI_TICKS watchdog ticks in a row, the job read at least
# WIFI_MIN_BYTES of it, one Wi-Fi link received at least WIFI_SHARE of that, and no wired link did
WIFI_SHARE = 0.5
WIFI_MIN_BYTES = 8 << 20
WIFI_TICKS = 2
# the why of that stop (written as an escape: this file stays ASCII)
WIFI_WHY = "the stream is arriving over its Wi-Fi (%s) \u00b7 turn its Wi-Fi off"


class WifiStream:
    """Is the Mac's stream reaching us over Wi-Fi? ssh arrives at a wired address, but Linux
    answers ARP for any of its addresses on every interface, so the Mac can end up sending the
    stream to the Wi-Fi's MAC. Each watchdog tick compares the bytes the job itself read (rchar
    of zstd, which reads the ssh session's stdin directly) with what every up Wi-Fi link and
    every wired link received (statistics/rx_bytes) over the same tick. Traffic beside the
    stream never counts: a stream on the cable puts all of itself on a wired link, whatever the
    Wi-Fi receives meanwhile (a download). In a tick the job can read more than arrived only by
    what ssh's flow-control window held (a few MB), so two ticks of >= 8 MB each where no wired
    link received half of it cannot come from the cable."""

    def __init__(self, sys_root, proc_root):
        self.sys_root, self.proc_root = sys_root, proc_root
        self.prev = None                     # (job's rchar, {wifi: rx}, {wired: rx})
        self.runs = {}                       # Wi-Fi link -> ticks in a row it carried the stream

    def update(self, pid, wired):
        """One tick: `pid` reads the stream, `wired` are the wired links that are up. -> the
        Wi-Fi interface the stream arrives on once that held WIFI_TICKS ticks in a row, else
        None. The first call is a baseline only, and so is any tick with a counter unreadable
        or gone back (a link reset) where the answer depends on it."""
        job = read_rchar(self.proc_root, pid) if pid else None
        wifi = {n: rx_bytes(self.sys_root, n) for n in wireless_links(self.sys_root)}
        cable = {n: rx_bytes(self.sys_root, n) for n in wired}
        prev, self.prev = self.prev, (job, wifi, cable)
        runs, self.runs = self.runs, {}
        if prev is None or job is None or prev[0] is None:
            return None
        got = job - prev[0]
        if got < WIFI_MIN_BYTES:
            return None
        for n, v in cable.items():
            then = prev[2].get(n)
            if v is None or then is None or v < then or v - then >= WIFI_SHARE * got:
                return None                  # the cable carried it (or it cannot be told)
        for n in sorted(wifi):
            v, then = wifi[n], prev[1].get(n)
            if v is None or then is None or v - then < WIFI_SHARE * got:
                continue
            self.runs[n] = runs.get(n, 0) + 1
            if self.runs[n] >= WIFI_TICKS:
                return n
        return None


# ---- /proc: processes -----------------------------------------------------------------------

class Proc:
    __slots__ = ("pid", "comm", "state", "ppid", "flags", "ticks", "start", "argv")

    def __init__(self, pid, comm, state, ppid, flags, ticks, start, argv):
        self.pid, self.comm, self.state, self.ppid = pid, comm, state, ppid
        self.flags, self.ticks, self.start, self.argv = flags, ticks, start, argv

    @property
    def kthread(self):
        return self.pid == 2 or self.ppid == 2 or bool(self.flags & PF_KTHREAD)

    @property
    def name(self):
        return os.path.basename(self.argv[0]) if self.argv else self.comm


def parse_stat(pid, text):
    """/proc/<pid>/stat -> Proc (argv empty). comm may contain spaces and parentheses."""
    l, r = text.index("("), text.rindex(")")
    comm = text[l + 1:r]
    rest = text[r + 2:].split()
    return Proc(pid, comm, rest[0], int(rest[1]), int(rest[6]),
                int(rest[11]) + int(rest[12]), int(rest[19]), [])


def list_procs(proc_root):
    """{pid: Proc} for every readable /proc/<pid>. Vanishing processes are skipped."""
    out = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        stat = _read(os.path.join(proc_root, n, "stat"))
        if not stat:
            continue
        try:
            p = parse_stat(int(n), stat)
        except (ValueError, IndexError):
            continue
        raw = _read_bytes(os.path.join(proc_root, n, "cmdline")) or b""
        p.argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        out[p.pid] = p
    return out


def proc_start(proc_root, pid):
    st = _read(os.path.join(proc_root, str(pid), "stat"))
    if not st:
        return None
    try:
        return parse_stat(pid, st).start
    except (ValueError, IndexError):
        return None


def own_tree(procs, self_pid, parent_pid=None, extra=()):
    """Our own processes, excluded from 'others': us, our descendants (zstd, x265), the
    children we know by pid, and our ancestors up to (not including) init/systemd -- the
    shell and sshd processes that carry our stream are working for us."""
    own = {self_pid}
    own.update(p for p in extra if p)
    roots = set(own)
    if parent_pid and parent_pid > 1:
        own.add(parent_pid)
    p = procs.get(self_pid)
    anc = p.ppid if p else parent_pid
    seen = set()
    while anc and anc > 1 and anc not in seen:
        seen.add(anc)
        q = procs.get(anc)
        if q is None or q.comm == "systemd":
            break
        own.add(anc)
        anc = q.ppid
    children = {}
    for q in procs.values():
        children.setdefault(q.ppid, []).append(q.pid)
    stack = list(roots)
    while stack:
        for c in children.get(stack.pop(), ()):
            if c not in own:
                own.add(c)
                stack.append(c)
    return own


def _game_label(p):
    for a in reversed(p.argv):
        if a.lower().endswith(".exe"):
            return os.path.basename(a.replace("\\", "/"))
    m = _APPID.search(" ".join(p.argv))
    if m:
        return "Steam app %s" % m.group(1)
    return p.comm or p.name


def _is_steam_launch(p):
    joined = " ".join(p.argv)
    return "SteamLaunch" in joined or ("reaper" in {p.comm, p.name} and "AppId=" in joined)


def _steam_appid(p, procs):
    """The AppId of a Steam launch: from its own argv, else from the nearest Steam-launch
    ancestor's (a helper under reaper inherits reaper's). None when no AppId is visible."""
    q, seen = p, set()
    while q is not None and q.pid not in seen and len(seen) < 64:
        seen.add(q.pid)
        if q is p or _is_steam_launch(q):
            m = _APPID.search(" ".join(q.argv))
            if m:
                return int(m.group(1))
        q = procs.get(q.ppid) if q.ppid > 1 else None
    return None


def _wine_like(p):
    """Is this a Wine/Proton process (a Windows program running, or Proton starting one)?"""
    if {p.comm, p.name} & WINE_PROCESS_NAMES or p.comm.lower().endswith(".exe"):
        return True
    return any(os.path.basename(a) == "proton" or _WIN_PATH.match(a) for a in p.argv)


def _wine_in_subtree(procs, children, root):
    """The first Wine/Proton process in root's process subtree (root itself included)."""
    stack, seen = [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        q = procs.get(pid)
        if q is not None and not q.kthread and _wine_like(q):
            return q
        stack.extend(children.get(pid, ()))
    return None


def _shortcut_label(p, wine):
    for q in (p, wine):
        for a in reversed(q.argv):
            if a.lower().endswith(".exe"):
                return os.path.basename(a.replace("\\", "/"))
    return "non-Steam game (%s)" % (wine.comm or wine.name)


def detect_game(procs, exclude=()):
    """Name of a running game, or None. Immediate (no sustain): a Steam launch (`reaper
    SteamLaunch AppId=...`) of a real Steam app (AppId < 2^31, or no AppId visible), Wine/Proton
    processes, Lutris, Heroic's store launchers while launching, and any Windows program.
    A non-Steam shortcut (AppId >= 2^31) is a game only when Wine/Proton runs in its process
    subtree; one running a native Linux program (Firefox, Dolphin, ...) is left to the CPU/GPU
    rule. Steam's own client and web helper never count."""
    children = {}
    for q in procs.values():
        children.setdefault(q.ppid, []).append(q.pid)
    for pid in sorted(procs):
        p = procs[pid]
        if pid in exclude or p.kthread:
            continue
        names = {p.comm, p.name}
        if names & NOT_GAMES:
            continue
        if _is_steam_launch(p):
            appid = _steam_appid(p, procs)
            if appid is None or appid < SHORTCUT_APPID_MIN:
                return _game_label(p)
            wine = _wine_in_subtree(procs, children, pid)
            if wine is not None:
                return _shortcut_label(p, wine)
            continue                  # a native non-Steam shortcut: only the heavy rule judges it
        if names & GAME_PROCESS_NAMES:
            return _game_label(p)
        if any(os.path.basename(a) == "proton" for a in p.argv[:3]):
            return _game_label(p)
        launcher = names | {os.path.basename(a) for a in p.argv[:2]}
        if launcher & GAME_LAUNCHERS and "launch" in p.argv:
            return _game_label(p)
        if p.comm.lower().endswith(".exe") or (p.argv and _WIN_PATH.match(p.argv[0])):
            return _game_label(p)
    return None


class CpuSampler:
    """Others' CPU use, in cores, from /proc/<pid>/stat utime+stime deltas between samples."""

    def __init__(self, proc_root=None, clk_tck=None):
        self.proc_root = proc_root
        self.clk = clk_tck or _clk_tck()
        self.prev = None
        self.prev_t = None

    def _uptime(self):
        if not self.proc_root:
            return None
        u = _read(os.path.join(self.proc_root, "uptime"))
        try:
            return float(u.split()[0]) if u else None
        except (ValueError, IndexError):
            return None

    def sample(self, procs, exclude, now):
        """-> None on the first call (baseline), else (cores, 'name N.N cores' of the top
        consumer). A process seen for the first time counts in full only if it was born
        during the interval (so one unreadable read can't fake a spike)."""
        cur = {pid: (p.start, p.ticks, p.comm) for pid, p in procs.items()
               if pid not in exclude and not p.kthread}
        prev, prev_t = self.prev, self.prev_t
        self.prev, self.prev_t = cur, now
        if prev is None or now <= prev_t:
            return None
        dt = now - prev_t
        uptime = self._uptime()
        by_name = {}
        total = 0
        for pid, (start, ticks, comm) in cur.items():
            old = prev.get(pid)
            if old is not None and old[0] == start:
                d = ticks - old[1]
            elif uptime is not None and uptime - start / self.clk > dt + 1.0:
                continue                      # existed before the interval: baseline only
            else:
                d = ticks
            if d > 0:
                total += d
                by_name[comm] = by_name.get(comm, 0) + d
        cores = total / self.clk / dt
        if not by_name:
            return cores, "nothing"
        top = max(by_name, key=by_name.get)
        return cores, "%s %.1f cores" % (top, by_name[top] / self.clk / dt)


def _best(readings):
    """The highest of [(percent, label)] -> (percent, label); (0.0, "") when none is above 0."""
    best, label = 0.0, ""
    for pct, what in readings:
        if pct > best:
            best, label = pct, what
    return best, label


def _drm_cards(sys_root):
    """[(card name, device id)] for /sys/class/drm/card<N>; the id is the basename of its
    device link (a PCI address such as 0000:03:00.0), which is what fdinfo's drm-pdev names."""
    out = []
    for card in sorted(glob.glob(os.path.join(sys_root, "class", "drm", "card*"))):
        name = os.path.basename(card)
        if re.fullmatch(r"card\d+", name):
            out.append((name, os.path.basename(os.path.realpath(os.path.join(card, "device")))))
    return out


def _drm_device_busy(sys_root, skip=()):
    """[(percent, label)] of the whole device, for the cards not in `skip`: drm
    gpu_busy_percent, which only amdgpu provides (i915, xe and nouveau give no reading)."""
    out = []
    for name, _dev in _drm_cards(sys_root):
        if name in skip:
            continue
        v = _read(os.path.join(sys_root, "class", "drm", name, "device", "gpu_busy_percent"))
        try:
            pct = float(v) if v else None
        except ValueError:
            pct = None
        if pct is not None:
            out.append((pct, "GPU %s %.0f%% busy" % (name, pct)))
    return out


def _nvidia_device_busy(runner):
    """[(percent, label)] of each whole NVIDIA GPU from nvidia-smi; [] when it gives none."""
    r = runner(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], 1.0)
    out = []
    if r and r[0] == 0:
        for i, line in enumerate((r[1] or "").splitlines()):
            try:
                pct = float(line.strip())
            except ValueError:
                continue
            out.append((pct, "NVIDIA GPU %d %.0f%% busy" % (i, pct)))
    return out


def gpu_busy(sys_root, runner):
    """(highest busy percent of any whole GPU, label): drm gpu_busy_percent (amdgpu), and
    nvidia-smi only when the nvidia module is loaded and nvidia-smi exists. Counts everything
    on the GPU, the desktop's own drawing included; GpuSampler falls back to it."""
    readings = _drm_device_busy(sys_root)
    if os.path.isdir(os.path.join(sys_root, "module", "nvidia")):
        readings += _nvidia_device_busy(runner)
    return _best(readings)


def _gpu_excluded(pid, procs, exclude, command=""):
    """Is this process's GPU use left out: ours, the desktop's own drawing, or Steam's client?"""
    if pid in exclude:
        return True
    p = procs.get(pid)
    names = {p.comm, p.name} if p is not None else set()
    if command:
        names.add(os.path.basename(command))
    return any(n in GPU_EXCLUDED_NAMES or (len(n) == _COMM_LEN and n in _GPU_EXCLUDED_CUT)
               for n in names if n)


def parse_drm_fdinfo(text):
    """One /proc/<pid>/fdinfo/<fd> of a DRM fd -> ((device, client id), {engine: ns},
    {engine: capacity}, {engine: (cycles, total cycles)}), or None when it carries no
    per-client engine reading. The device is drm-pdev (else drm-driver). Engine time is
    "drm-engine-<name>: <ns> ns" (amdgpu, i915, ...); Intel xe gives busy cycles instead,
    "drm-cycles-<name>" against the GPU's own clock "drm-total-cycles-<name>" (an engine with
    only one of the two is left out). nouveau gives neither."""
    fields = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            fields[k.strip()] = v.strip()
    client = fields.get("drm-client-id")
    if not client:
        return None
    engines, capacity, cycles, totals = {}, {}, {}, {}
    for k, v in fields.items():
        if k.startswith("drm-engine-capacity-"):
            try:
                capacity[k[len("drm-engine-capacity-"):]] = max(1, int(v))
            except ValueError:
                pass
        elif k.startswith("drm-engine-"):
            tok = v.split()
            if tok and tok[0].isdigit() and tok[1:] in ([], ["ns"]):
                engines[k[len("drm-engine-"):]] = int(tok[0])
        elif k.startswith("drm-cycles-") and v.isdigit():
            cycles[k[len("drm-cycles-"):]] = int(v)
        elif k.startswith("drm-total-cycles-") and v.isdigit():
            totals[k[len("drm-total-cycles-"):]] = int(v)
    clocked = {e: (n, totals[e]) for e, n in cycles.items() if e in totals}
    if not engines and not clocked:
        return None
    key = (fields.get("drm-pdev") or fields.get("drm-driver") or "?", client)
    return key, engines, capacity, clocked


def drm_clients(proc_root, pids):
    """{(device, client id): {"pids", "engines", "capacity", "cycles"}} from the fdinfo of every
    DRM fd (fd -> /dev/dri/*) of `pids` that this user can read. A client open in several fds
    or processes is one client: its time counts once."""
    out = {}
    for pid in pids:
        base = os.path.join(proc_root, str(pid))
        try:
            fds = os.listdir(os.path.join(base, "fdinfo"))
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(os.path.join(base, "fd", fd)).startswith("/dev/dri/"):
                    continue
            except OSError:
                continue
            info = parse_drm_fdinfo(_read(os.path.join(base, "fdinfo", fd)) or "")
            if info is None:
                continue
            key, engines, capacity, cycles = info
            c = out.setdefault(key, {"pids": set(), "engines": engines, "capacity": capacity,
                                     "cycles": cycles})
            c["pids"].add(pid)
    return out


def parse_pmon(text):
    """`nvidia-smi pmon -c 1 -s u` -> {gpu index: {pid: (sm percent, command)}}, or None when
    it holds no GPU row. Columns are read from its header (newer drivers add jpg/ofa); a "-"
    is 0; a GPU with nothing running shows one row of dashes (an empty dict)."""
    cols = ["gpu", "pid", "type", "sm", "mem", "enc", "dec", "command"]
    out = {}
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            head = [c.lower() for c in s[1:].split()]
            if "gpu" in head and "pid" in head and "sm" in head:
                cols = head
            continue
        f = s.split()
        gi, pi, si = cols.index("gpu"), cols.index("pid"), cols.index("sm")
        if len(f) <= max(gi, pi, si):
            continue
        try:
            gpu = int(f[gi])
        except ValueError:
            continue
        procs = out.setdefault(gpu, {})
        try:
            pid = int(f[pi])
        except ValueError:
            continue                              # "-": nothing running on this GPU
        try:
            sm = float(f[si])
        except ValueError:
            sm = 0.0
        ci = cols.index("command") if "command" in cols else len(f)
        command = " ".join(f[ci:])
        if pid not in procs or sm > procs[pid][0]:
            procs[pid] = (sm, command)
    return out or None


class GpuSampler:
    """Others' GPU use, per process: (highest busy percent of any GPU, label). Leaves out our
    own processes and GPU_EXCLUDED_NAMES (the desktop's own drawing, the Steam client).
    NVIDIA (nvidia module loaded): `nvidia-smi pmon -c 1 -s u`, the sm % of the others summed
    per GPU. Any other DRM driver (amdgpu, i915, xe, ...): each client's engine time or busy
    cycles from /proc/<pid>/fdinfo (deduplicated by drm-client-id), as busy % of the GPU's
    busiest engine between two samples -- so it needs a baseline: begin(), or the first
    sample() reads 0 for those GPUs. Where no per-process reading exists (pmon fails, no
    readable client on a card, nouveau), the whole-device reading stands in (gpu_busy's parts;
    none on nouveau)."""

    def __init__(self, sys_root, proc_root, runner):
        self.sys_root, self.proc_root, self.runner = sys_root, proc_root, runner
        self.nvidia = os.path.isdir(os.path.join(sys_root, "module", "nvidia"))
        self.prev = None                     # (t, drm_clients) of the last sample
        self.pmon = None                     # (thread, result box) of a pmon still sampling

    def begin(self, procs, now):
        """Baseline: the DRM clients' engine time now, and a pmon started in the background
        (it samples for ~1 s, so status overlaps it with its CPU sample)."""
        self.prev = (now, self._clients(procs))
        self._start_pmon()

    def _clients(self, procs):
        return drm_clients(self.proc_root, [pid for pid, p in procs.items() if not p.kthread])

    def _start_pmon(self):
        if not self.nvidia or self.pmon is not None:
            return
        box = {}

        def run():
            try:
                box["r"] = self.runner(PMON_ARGV, PMON_TIMEOUT)
            except Exception:
                box["r"] = None
        t = threading.Thread(target=run, name="pmon", daemon=True)
        self.pmon = (t, box)
        t.start()

    def _take_pmon(self):
        if self.pmon is None:
            return self.runner(PMON_ARGV, PMON_TIMEOUT)
        t, box = self.pmon
        t.join(PMON_TIMEOUT)
        if t.is_alive():
            return None                      # still sampling: the whole-device reading this time
        self.pmon = None
        return box.get("r")

    def _drm(self, procs, exclude, now):
        """[(percent, label)] per device with readable clients, and the cards they cover.
        Engine time (ns) is a share of the wall time between the samples. Busy cycles (xe)
        are not: drm-usage-stats defines a client's use of an engine as the growth of its
        drm-cycles over the growth of its OWN drm-total-cycles (the GPU clock read with them, so
        CPU-side timing does not enter), and the clients' shares of one engine add up. A common
        denominator (the largest total-cycles growth of any client) would match that only when
        every client was read at the same instants. A client seen for the first time gives a
        baseline only (its total-cycles is the GPU clock, not its age), and a counter that went
        back keeps its larger reading until it catches up, as the documentation asks."""
        cur = self._clients(procs)
        prev, self.prev = self.prev, (now, cur)
        names = {dev: name for name, dev in _drm_cards(self.sys_root)}
        busy, caps = {}, {}                  # device -> {engine: ns of others}, {engine: capacity}
        shares = {}                          # device -> {engine: engines' worth busy, from cycles}
        for (dev, _client), c in cur.items():
            busy.setdefault(dev, {})
            shares.setdefault(dev, {})
            caps.setdefault(dev, {}).update(c["capacity"])
        dt_ns = (now - prev[0]) * 1e9 if prev is not None else 0.0
        if dt_ns > 0:
            for key, c in cur.items():
                if any(_gpu_excluded(pid, procs, exclude) for pid in c["pids"]):
                    continue
                old = prev[1].get(key)             # None: opened since, its time is all new
                for eng, ns in c["engines"].items():
                    d = ns - old["engines"].get(eng, 0) if old is not None else ns
                    cap = caps[key[0]].get(eng, 1)
                    d = min(max(d, 0), dt_ns * cap)          # one client fills an engine at most
                    busy[key[0]][eng] = busy[key[0]].get(eng, 0) + d
                for eng, (cyc, total) in c["cycles"].items():
                    then = old["cycles"].get(eng) if old is not None else None
                    if then is None or eng in c["engines"]:
                        continue                 # a baseline only; ns time wins where both exist
                    if cyc < then[0] or total < then[1]:
                        c["cycles"][eng] = then  # went back: measure from the larger reading
                        continue
                    if total == then[1]:
                        continue
                    cap = caps[key[0]].get(eng, 1)
                    share = min(float(cyc - then[0]) / (total - then[1]), cap)
                    shares[key[0]][eng] = shares[key[0]].get(eng, 0.0) + share
        out = []
        for dev, engs in busy.items():
            pct = 0.0
            for eng, ns in engs.items():
                pct = max(pct, min(100.0, ns * 100.0 / (dt_ns * caps[dev].get(eng, 1))))
            for eng, share in shares[dev].items():
                pct = max(pct, min(100.0, share * 100.0 / caps[dev].get(eng, 1)))
            out.append((pct, "GPU %s %.0f%% busy" % (names.get(dev, dev), pct)))
        return out, {names[dev] for dev in busy if dev in names}

    def _nvidia(self, procs, exclude):
        r = self._take_pmon()
        got = parse_pmon(r[1]) if r and r[0] == 0 else None
        if not got:
            return _nvidia_device_busy(self.runner)
        out = []
        for gpu in sorted(got):
            pct = sum(sm for pid, (sm, command) in got[gpu].items()
                      if not _gpu_excluded(pid, procs, exclude, command))
            pct = min(100.0, pct)
            out.append((pct, "NVIDIA GPU %d %.0f%% busy" % (gpu, pct)))
        return out

    def sample(self, procs, exclude, now, prefetch=False):
        """-> (percent, label) of the busiest GPU. `prefetch` starts the next pmon at once, so
        the watchdog's next sample does not wait for one."""
        readings, covered = self._drm(procs, exclude, now)
        readings += _drm_device_busy(self.sys_root, covered)
        if self.nvidia:
            readings += self._nvidia(procs, exclude)
            if prefetch:
                self._start_pmon()
        return _best(readings)


class HeavyTracker:
    """'Anything heavy': others' CPU > cpu_limit cores or any GPU > gpu_limit %, in EVERY
    sample for at least `secs`. One quiet sample resets it, so a momentary spike never counts."""

    def __init__(self, secs, cpu_limit=CPU_CORES_LIMIT, gpu_limit=GPU_BUSY_LIMIT):
        self.secs, self.cpu_limit, self.gpu_limit = secs, cpu_limit, gpu_limit
        self.since = None

    def update(self, now, interval_start, cpu=None, gpu=None):
        """cpu / gpu: (value, label) or None. -> label of what is heavy once sustained."""
        over = []
        if cpu is not None and cpu[0] > self.cpu_limit:
            over.append(cpu[1])
        if gpu is not None and gpu[0] > self.gpu_limit:
            over.append(gpu[1])
        if not over:
            self.since = None
            return None
        if self.since is None:
            self.since = interval_start
        if now - self.since >= self.secs - 1e-6:
            return ", ".join(over)
        return None


# ---- jobs: owner markers, liveness, lock ----------------------------------------------------

def pid_alive(pid, start, proc_root):
    """Is `pid` still the process that wrote the marker? Zombies and reused pids are dead."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    st = _read(os.path.join(proc_root, str(pid), "stat"))
    if st:
        try:
            p = parse_stat(pid, st)
        except (ValueError, IndexError):
            return True
        if p.state in ("Z", "X"):
            return False
        if start is not None and p.start != start:
            return False
    return True


def read_marker(jobdir):
    raw = _read(os.path.join(jobdir, OWNER_FILE))
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def write_marker(jobdir, role, proc_root):
    pid = os.getpid()
    m = {"pid": pid, "start": proc_start(proc_root, pid), "role": role, "t": time.time()}
    tmp = os.path.join(jobdir, OWNER_FILE + ".tmp")
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, os.path.join(jobdir, OWNER_FILE))
    return m


def marker_alive(m, proc_root):
    return bool(m) and pid_alive(m.get("pid"), m.get("start"), proc_root)


def job_dirs(cfg):
    try:
        names = sorted(os.listdir(cfg.jobs))
    except OSError:
        return []
    return [(n, os.path.join(cfg.jobs, n)) for n in names
            if JOB_ID_RE.fullmatch(n) and os.path.isdir(os.path.join(cfg.jobs, n))]


def live_encode_jobs(cfg, exclude_job=None):
    out = []
    for name, path in job_dirs(cfg):
        if name == exclude_job:
            continue
        m = read_marker(path)
        if m and m.get("role") == "encode" and marker_alive(m, cfg.proc):
            out.append(name)
    return out


@contextlib.contextmanager
def jobs_lock(cfg, create=True):
    if create:
        os.makedirs(cfg.jobs, mode=0o700, exist_ok=True)
    elif not os.path.isdir(cfg.jobs):
        yield
        return
    fd = os.open(os.path.join(cfg.jobs, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _rmtree(path):
    for _ in range(3):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            return True
        time.sleep(0.05)
    return not os.path.lexists(path)


def _kill_strays(cfg, paths, procs=None):
    """SIGKILL any x265 whose arguments point into one of `paths` (left behind by a vworker
    that was SIGKILLed before it could clean up)."""
    if not paths:
        return []
    procs = list_procs(cfg.proc) if procs is None else procs
    killed = []
    me = os.getpid()
    for p in procs.values():
        if p.pid == me or p.kthread or not ({p.comm, p.name} & {"x265"}):
            continue
        if any(any(a == d or a.startswith(d + os.sep) for d in paths) for a in p.argv):
            try:
                os.kill(p.pid, signal.SIGKILL)
                killed.append(p.pid)
            except OSError:
                pass
    return killed


def _has_part(jobdir):
    return bool(glob.glob(os.path.join(glob.escape(jobdir), ".*.part")))


def sweep(cfg, now=None):
    """Delete job dirs whose owner is gone: an encode whose pid is dead; a put-staged dir
    (its put process has exited by design) once it is STAGE_TTL old, or at once when it still
    holds a .part (that put died mid-transfer); a dir with no marker once it is ORPHAN_TTL old.
    -> list of removed job ids."""
    now = time.time() if now is None else now
    removed = []
    with jobs_lock(cfg, create=False):
        for name, path in job_dirs(cfg):
            m = read_marker(path)
            if m is None:
                try:
                    age = now - os.stat(path).st_mtime
                except OSError:
                    continue
                if age <= cfg.orphan_ttl:
                    continue
            elif marker_alive(m, cfg.proc):
                continue
            elif m.get("role") == "put" and now - float(m.get("t") or 0) < cfg.stage_ttl \
                    and not _has_part(path):
                continue
            _rmtree(path)
            removed.append(name)
    _kill_strays(cfg, [os.path.join(cfg.jobs, n) for n in removed])
    return removed


def kill_all(cfg, wait=4.0, job=None):
    """Stop every live job (SIGTERM so it cleans up, SIGKILL after `wait`), then delete every
    job dir -- or, with `job`, only that job's process and dir. -> list of pids signalled."""
    me = os.getpid()
    procs = list_procs(cfg.proc)
    targets = {}

    def mine():
        return [(n, p) for n, p in job_dirs(cfg) if job is None or n == job]
    for name, path in mine():
        m = read_marker(path)
        if not m or not marker_alive(m, cfg.proc) or m.get("pid") == me:
            continue
        pid = m["pid"]
        p = procs.get(pid)
        if p is not None and not any("vworker" in a for a in p.argv[:3]):
            continue                          # pid reused by something that is not us
        targets[pid] = m.get("start")
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and any(pid_alive(p, s, cfg.proc) for p, s in targets.items()):
        time.sleep(0.05)
    procs = list_procs(cfg.proc)
    for pid, start in targets.items():
        if pid_alive(pid, start, cfg.proc):
            for c in [q.pid for q in procs.values() if q.ppid == pid] + [pid]:
                try:
                    os.kill(c, signal.SIGKILL)
                except OSError:
                    pass
    paths = []
    with jobs_lock(cfg, create=False):
        for name, path in mine():
            _rmtree(path)
            paths.append(path)
    _kill_strays(cfg, paths)
    return sorted(targets)


# ---- status ---------------------------------------------------------------------------------

def free_gb(path):
    p = os.path.abspath(path)
    while not os.path.isdir(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    try:
        st = os.statvfs(p)
    except OSError:
        return 0.0
    return st.f_bavail * st.f_frsize / float(1 << 30)


def x265_version(x265, runner):
    """'4.1+1-1d117be' from `x265 --version` (printed on stderr), or '' when x265 is missing
    or does not run."""
    if not (os.path.isfile(x265) and os.access(x265, os.X_OK)):
        return ""
    r = runner([x265, "--version"], 1.5)
    if not r or r[0] != 0:
        return ""
    m = re.search(r"HEVC encoder version (\S+)", (r[1] or "") + "\n" + (r[2] or ""))
    return m.group(1) if m else ""


def _zstd_ok(cfg):
    return _executable(cfg.zstd)


_SELF_SHA256 = []


def self_sha256():
    """sha256 of this script's own bytes (the file that is running), "" when unreadable."""
    if not _SELF_SHA256:
        try:
            with open(os.path.abspath(__file__), "rb") as f:
                _SELF_SHA256.append(hashlib.sha256(f.read()).hexdigest())
        except (OSError, NameError):
            return ""
    return _SELF_SHA256[0]


def heavy_only(st, cfg):
    """Is a heavy CPU/GPU reading the ONLY thing between this status and ok? (Then an encode
    re-samples instead of refusing at once: one busy second is not "anything heavy".)"""
    return bool(st.get("heavy") and st.get("ac") and st.get("ethernet") and not st.get("game")
                and not st.get("busy") and st.get("x265") and _zstd_ok(cfg)
                and free_gb(cfg.jobs) >= cfg.min_free_gb)


def compute_status(cfg, runner=None, exclude_job=None, ssh_connection=None, self_pid=None,
                   parent_pid=None, clock=time.monotonic, sleep=time.sleep):
    """The status dict. `exclude_job` is the job an encode is about to claim (its own staged
    dir must not read as busy). "sleep_block" is sleep_block()'s answer, for Setup only: it
    never makes the status not ok."""
    runner = runner or default_runner
    self_pid = os.getpid() if self_pid is None else self_pid
    parent_pid = os.getppid() if parent_pid is None else parent_pid
    sleep_answer = _start_sleep_probe(cfg.inhibit)
    ac = ac_online(cfg.sys)
    links = ethernet_links(cfg.sys)
    ethernet, eth_why = bool(links), "no Ethernet"
    if ethernet and ssh_connection:
        via = ssh_arrival_iface(ssh_connection, runner)
        if via and via not in links:
            ethernet, eth_why = False, "no Ethernet: ssh arrives on %s" % via
    procs = list_procs(cfg.proc)
    own = own_tree(procs, self_pid, parent_pid)
    game = detect_game(procs, own)
    cpu = None
    gpus = GpuSampler(cfg.sys, cfg.proc, runner)
    if cfg.status_sample > 0:
        sampler = CpuSampler(cfg.proc)
        t0 = clock()
        sampler.sample(procs, own, t0)
        gpus.begin(procs, t0)
        sleep(cfg.status_sample)
        t1 = clock()
        procs = list_procs(cfg.proc)
        own = own_tree(procs, self_pid, parent_pid)
        game = game or detect_game(procs, own)
        cpu = sampler.sample(procs, own, t1)
        gpu = gpus.sample(procs, own, t1)
    else:
        gpu = gpus.sample(procs, own, clock())
    heavy = None
    if cpu is not None and cpu[0] > CPU_CORES_LIMIT:
        heavy = cpu[1]
    elif gpu[0] > GPU_BUSY_LIMIT:
        heavy = gpu[1]
    free = free_gb(cfg.jobs)
    busy_jobs = live_encode_jobs(cfg, exclude_job)
    ver = x265_version(cfg.x265, runner)
    zstd_ok = _zstd_ok(cfg)
    if not ac:
        code, why = "battery", "on battery"
    elif not ethernet:
        code, why = "no_ethernet", eth_why
    elif game:
        code, why = "game", "game running: %s" % game
    elif busy_jobs:
        code, why = "busy", "busy: encoding job %s" % busy_jobs[0]
    elif heavy:
        code, why = "heavy", "busy: %s" % heavy
    elif free < cfg.min_free_gb:
        code, why = "low_disk", "low disk: %.1f GB free, need %.0f" % (free, cfg.min_free_gb)
    elif not ver:
        code, why = "x265_missing", "x265 missing or not runnable: %s" % cfg.x265
    elif not zstd_ok:
        code, why = "zstd_missing", "zstd missing: %s" % cfg.zstd
    else:
        code, why = "ready", "ready"
    ok = bool(ac and ethernet and not game and not heavy and free >= cfg.min_free_gb
              and ver and zstd_ok and not busy_jobs)
    return {"ok": ok, "why": why, "code": code, "ac": ac, "ethernet": ethernet, "game": game,
            "heavy": heavy, "free_gb": round(free, 1), "busy": bool(busy_jobs),
            "x265": ver, "version": PROTOCOL_VERSION, "sha256": self_sha256(),
            "ipv4": wired_ipv4(links, runner), "sleep_block": sleep_answer()}


# ---- x265 argument allow-list ---------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_VALUE_RULES = {
    "--output": "{out}",
    "--dolby-vision-rpu": "{rpu}",
    "--preset": _WORD,
    "--crf": re.compile(r"\d{1,2}(\.\d{1,4})?"),
    "--output-depth": re.compile(r"\d{1,2}"),
    "--vbv-maxrate": re.compile(r"\d{1,8}"),
    "--vbv-bufsize": re.compile(r"\d{1,8}"),
    "--dolby-vision-profile": re.compile(r"\d{1,2}\.\d{1,2}"),
    "--range": _WORD,
    "--colorprim": _WORD,
    "--transfer": _WORD,
    "--colormatrix": _WORD,
    "--master-display": re.compile(
        r"G\(\d{1,6},\d{1,6}\)B\(\d{1,6},\d{1,6}\)R\(\d{1,6},\d{1,6}\)"
        r"WP\(\d{1,6},\d{1,6}\)L\(\d{1,10},\d{1,10}\)"),
    "--max-cll": re.compile(r"\d{1,5},\d{1,5}"),
}
_BOOL_FLAGS = {"--y4m", "--repeat-headers", "--aud", "--hrd"}


def validate_x265_args(args):
    """Check Visionary's x265 arguments (binary dropped, {rpu}/{out} placeholders) against the
    allow-list. -> a copy of the list; raises ValueError naming the first offence."""
    if not isinstance(args, list) or not args or len(args) > 64:
        raise ValueError("x265_args must be a non-empty list of at most 64 strings")
    if not all(isinstance(a, str) for a in args):
        raise ValueError("x265_args must all be strings")
    seen, stdin_seen, i = set(), False, 0
    while i < len(args):
        a = args[i]
        if a == "-":
            if stdin_seen:
                raise ValueError("'-' given twice")
            stdin_seen = True
            i += 1
            continue
        if a in _BOOL_FLAGS:
            if a in seen:
                raise ValueError("flag given twice: %s" % a)
            seen.add(a)
            i += 1
            continue
        rule = _VALUE_RULES.get(a)
        if rule is None:
            raise ValueError("flag not allowed: %r" % a)
        if a in seen:
            raise ValueError("flag given twice: %s" % a)
        if i + 1 >= len(args):
            raise ValueError("%s needs a value" % a)
        v = args[i + 1]
        if v.startswith("/") or ".." in v or v.startswith("-"):
            raise ValueError("%s value not allowed: %r" % (a, v))
        if isinstance(rule, str):
            if v != rule:
                raise ValueError("%s must be %s, got %r" % (a, rule, v))
        elif not rule.fullmatch(v):
            raise ValueError("%s value not allowed: %r" % (a, v))
        seen.add(a)
        i += 2
    for need in ("--y4m", "--output"):
        if need not in seen:
            raise ValueError("missing %s" % need)
    if not stdin_seen:
        raise ValueError("missing '-' (y4m on stdin)")
    return list(args)


def substitute(args, rpu_path, out_path):
    out = []
    for i, a in enumerate(args):
        prev = args[i - 1] if i else None
        if a == "{rpu}" and prev == "--dolby-vision-rpu":
            out.append(rpu_path)
        elif a == "{out}" and prev == "--output":
            out.append(out_path)
        else:
            out.append(a)
    return out


def decode_spec(b64):
    """base64(JSON {"x265_args": [...], "frames": N}) -> (args, frames). Raises ValueError."""
    if not isinstance(b64, str) or len(b64) > 65536:
        raise ValueError("bad spec")
    raw = None
    for dec in (lambda s: base64.b64decode(s, validate=True),
                lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))):
        try:
            raw = dec(b64)
            break
        except (binascii.Error, ValueError):
            continue
    if raw is None:
        raise ValueError("spec is not base64")
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("spec is not JSON")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    frames = spec.get("frames")
    if not isinstance(frames, int) or isinstance(frames, bool) or not 0 < frames <= 10_000_000:
        raise ValueError("frames must be a positive integer")
    return validate_x265_args(spec.get("x265_args")), frames


# ---- encode ---------------------------------------------------------------------------------

_LIBC = None
if sys.platform.startswith("linux") and ctypes is not None:
    try:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    except OSError:                     # pragma: no cover
        _LIBC = None


_SUMMARY_RE = re.compile(rb"encoded \d+ frames")
_X265_ERROR_RE = re.compile(rb"x265 \[error\]:")
# zstd's messages when READING its input fails ("Read error (39) : premature end", "unexpected
# end of file"); a write to a dead x265 says "Write error ... Broken pipe" or dies of SIGPIPE
_ZSTD_READ_FAIL_RE = re.compile(rb"premature end|unexpected end|Read error", re.I)


def _tail_text(data, lines=2, limit=300):
    """The last `lines` non-empty lines of a stderr tail (its error lines when it has any)."""
    text = data.decode("utf-8", "replace").replace("\r", "\n")
    got = [ln.strip() for ln in text.splitlines() if ln.strip()]
    errs = [ln for ln in got if "[error]" in ln]
    return " / ".join((errs or got)[-lines:])[-limit:]


def _rc_text(rc):
    if rc is not None and rc < 0:
        try:
            return "killed by %s" % signal.Signals(-rc).name
        except ValueError:
            return "killed by signal %d" % -rc
    return "exited %s" % rc


def _set_idle_priority():
    """In the child: SCHED_IDLE (most desktop distros turn sched_autogroup on, so nice alone
    does not let the desktop win), else nice 19. -> "idle" or "nice"."""
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
        return "idle"
    except (AttributeError, OSError, ValueError, TypeError):
        pass
    try:
        os.setpriority(os.PRIO_PROCESS, 0, CHILD_NICE)
    except (OSError, AttributeError):
        pass
    return "nice"


def _make_child_setup(parent):
    def setup():
        _set_idle_priority()
        if _LIBC is not None:
            try:
                _LIBC.prctl(1, int(signal.SIGKILL), 0, 0, 0)   # PR_SET_PDEATHSIG: die with us
            except Exception:
                pass
            if os.getppid() != parent:                         # we died before prctl took
                os._exit(1)
    return setup


class EncodeJob:
    """One `vworker encode`: zstd -d | x265 --output FIFO, FIFO -> stdout, x265 stderr ->
    stderr, a watchdog, and a cleanup that runs on every path."""

    def __init__(self, cfg, job_id, runner=None):
        self.cfg, self.job_id = cfg, job_id
        self.runner = runner or default_runner
        self.jobdir = os.path.join(cfg.jobs, job_id)
        self.fifo = os.path.join(self.jobdir, FIFO_NAME)
        self.rpu = os.path.join(self.jobdir, RPU_NAME)
        self.stop = threading.Event()
        self.state_lock = threading.Lock()
        self.result, self.why = None, ""
        self.code = None                           # REASON_CODES word of a yield or a refusal
        self.owns_dir = False
        self.zstd = self.x265 = None
        self.inhibitors = []                       # systemd-inhibits holding off sleep and idle
        self.zstd_killed = False
        self.rfd = self.dummy_w = None
        self.err_lock = threading.Lock()
        self.err_open = True
        self.err_at_bol = True
        self.err_tail = b""
        self.zstd_err = b""
        self.copied = 0
        self.last_activity = time.monotonic()
        self.ppid0 = os.getppid()
        self.threads = {}
        self.summary_at = self.error_at = None     # when x265 printed its summary / an error
        self.hung = None                           # (what, output_closed) once we killed it

    # -- state
    def abort(self, result, why, code=None):
        with self.state_lock:
            if self.result is None:
                self.result, self.why, self.code = result, why, code
        self.stop.set()

    def install_signals(self):
        def handler(signum, _frame):
            self.abort("disconnected", "terminated by %s" % signal.Signals(signum).name)
        for s in STOP_SIGNALS:
            signal.signal(s, handler)
        if _EARLY:                            # it came while main was still sweeping
            handler(_EARLY[0], None)

    # -- stderr
    def _write_err(self, data):
        """Write to our stderr. A broken stderr means the Mac is gone."""
        with self.err_lock:
            if not self.err_open:
                return
            try:
                mv = memoryview(data)
                while mv:
                    n = os.write(2, mv)
                    mv = mv[n:]
                if data:
                    self.err_at_bol = data.endswith(b"\n")
            except OSError:
                self.err_open = False
                self.abort("disconnected", "stderr broken")

    def _forward_x265_err(self, f):
        fd = f.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.last_activity = time.monotonic()
            self.err_tail = (self.err_tail + chunk)[-16384:]
            if self.summary_at is None and _SUMMARY_RE.search(self.err_tail):
                self.summary_at = time.monotonic()
            if self.error_at is None and _X265_ERROR_RE.search(self.err_tail):
                self.error_at = time.monotonic()
            self._write_err(chunk)

    def _collect_zstd_err(self, f):
        fd = f.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.zstd_err = (self.zstd_err + chunk)[-2048:]

    # -- stdout
    def _copy_out(self):
        try:
            mv = memoryview(STREAM_MAGIC)          # the Mac drops shell noise up to this marker
            while mv:
                mv = mv[os.write(1, mv):]
            self.last_activity = time.monotonic()
            while True:
                data = os.read(self.rfd, 1 << 20)
                if not data:
                    return
                mv = memoryview(data)
                while mv:
                    n = os.write(1, mv)
                    mv = mv[n:]
                self.copied += len(data)
                self.last_activity = time.monotonic()
        except OSError as e:
            if not self.stop.is_set():
                self.abort("disconnected", "stdout broken: %s" % (e.strerror or e))

    # -- watchdog
    def _watchdog(self):
        cfg, runner = self.cfg, self.runner
        me = os.getpid()
        kids = lambda: [p.pid for p in (self.zstd, self.x265) if p is not None]
        try:
            sampler = CpuSampler(cfg.proc)
            gpus = GpuSampler(cfg.sys, cfg.proc, runner)
            heavy = HeavyTracker(cfg.heavy_secs)
            wifi = WifiStream(cfg.sys, cfg.proc)
            reader = self.zstd.pid if self.zstd is not None else None   # reads the ssh stdin
            procs = list_procs(cfg.proc)
            t0 = time.monotonic()
            sampler.sample(procs, own_tree(procs, me, self.ppid0, kids()), t0)
            gpus.begin(procs, t0)
            wifi.update(reader, ethernet_links(cfg.sys))
            last = time.monotonic()
            while not self.stop.wait(cfg.watch_interval):
                now = time.monotonic()
                if os.getppid() != self.ppid0:
                    return self.abort("disconnected", "parent process gone")
                procs = list_procs(cfg.proc)
                own = own_tree(procs, me, self.ppid0, kids())
                game = detect_game(procs, own)
                if game:
                    return self.abort("yielded", "game running: %s" % game, "game")
                links = ethernet_links(cfg.sys)
                if not links:
                    return self.abort("disconnected", "Ethernet down")
                via = wifi.update(reader, links)
                if via:
                    return self.abort("yielded", WIFI_WHY % via, "no_ethernet")
                if not ac_online(cfg.sys):
                    return self.abort("yielded", "on battery", "battery")
                cpu = sampler.sample(procs, own, now)
                what = heavy.update(now, last, cpu, gpus.sample(procs, own, now, prefetch=True))
                if what:
                    return self.abort("yielded", "heavy: %s" % what, "heavy")
                if now - self.last_activity > cfg.stall_secs:
                    return self.abort("disconnected", "stalled: no data for %.0f s" % cfg.stall_secs)
                last = now
        except Exception as e:                # fail closed: no protection -> stop
            self.abort("error", "watchdog failed: %s" % e)

    def _start_thread(self, name, target, *args):
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        self.threads[name] = t
        t.start()
        return t

    # -- children
    def _kill_children(self):
        for p in (self.x265, self.zstd):
            if p is not None and p.poll() is None:
                if p is self.zstd:
                    self.zstd_killed = True
                try:
                    p.kill()
                except OSError:
                    pass
        for p in (self.x265, self.zstd):
            if p is not None:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._close_dummy()
        self._stop_inhibitor()

    def _start_inhibitor(self):
        """Ask logind for a sleep block and, beside it, an idle block for the encode (each a
        `systemd-inhibit ... sleep infinity` in its own process group), so the helper does not
        suspend mid-segment. Two, because polkit judges each kind on its own: over ssh a stock
        setup usually refuses sleep (status reports it as sleep_block) but allows idle, which
        covers the desktops and logind's IdleAction that honour it. Only where systemd-inhibit
        exists; one that cannot start, or exits at once (polkit said no), never fails the job.
        They die with us (PDEATHSIG), and logind lifts a block the moment its systemd-inhibit
        exits."""
        if not self.cfg.inhibit:
            return
        for args in (INHIBIT_ARGS, IDLE_INHIBIT_ARGS):
            try:
                self.inhibitors.append(subprocess.Popen(
                    [self.cfg.inhibit] + args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    preexec_fn=_make_child_setup(os.getpid()), start_new_session=True))
            except (OSError, ValueError, subprocess.SubprocessError):
                pass

    def _stop_inhibitor(self):
        """Release the blocks: kill each systemd-inhibit and its sleep (their process group)."""
        held, self.inhibitors = self.inhibitors, []
        for p in held:
            try:
                os.killpg(p.pid, signal.SIGKILL)   # not reaped yet, so the group is still ours
            except OSError:
                try:
                    p.kill()
                except OSError:
                    pass
        for p in held:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _close_dummy(self):
        if self.dummy_w is not None:
            try:
                os.close(self.dummy_w)
            except OSError:
                pass
            self.dummy_w = None

    # -- the job
    def prepare(self, b64):
        """Validate and claim. -> None to go ahead, or (result, why) to finish now. A stop
        signal at any point here finishes the job at once (its dir deleted when nobody else
        owns it). A heavy reading alone is re-sampled up to HEAVY_SAMPLES times in all and
        refuses only when every sample is heavy; anything else that is not ok refuses at once."""
        if not JOB_ID_RE.fullmatch(self.job_id or ""):
            return ("error", "bad job id")
        if self.stop.is_set():
            self.owns_dir = self._dir_is_unowned()
            return (self.result, self.why)
        try:
            self.args, self.frames = decode_spec(b64)
        except ValueError as e:
            self.owns_dir = self._dir_is_unowned()
            return ("error", "bad spec: %s" % e)
        with jobs_lock(self.cfg):
            m = read_marker(self.jobdir)
            if m and m.get("pid") != os.getpid() and marker_alive(m, self.cfg.proc) \
                    and m.get("role") == "encode":
                self.code = "busy"
                return ("refused", "busy: job %s is already encoding" % self.job_id)
            self.owns_dir = True              # from here on the dir is ours to delete
            for _sample in range(HEAVY_SAMPLES):
                if self.stop.is_set():
                    return (self.result, self.why)
                st = compute_status(self.cfg, self.runner, exclude_job=self.job_id,
                                    ssh_connection=os.environ.get("SSH_CONNECTION"))
                if self.stop.is_set():
                    return (self.result, self.why)
                if st["ok"] or not heavy_only(st, self.cfg):
                    break
            if not st["ok"]:
                self.code = st.get("code")
                return ("refused", st["why"])
            os.makedirs(self.jobdir, mode=0o700, exist_ok=True)
            write_marker(self.jobdir, "encode", self.cfg.proc)
        if "{rpu}" in self.args and not os.path.isfile(self.rpu):
            return ("error", "%s missing: put it before encode" % RPU_NAME)
        return None

    def _dir_is_unowned(self):
        m = read_marker(self.jobdir)
        return not (m and m.get("pid") != os.getpid() and marker_alive(m, self.cfg.proc))

    def run(self):
        """Spawn the pipe and wait. -> (result, why, frames)."""
        cfg = self.cfg
        if self.stop.is_set():                # stopped between the claim and the spawn
            return (self.result, self.why, 0)
        if os.path.lexists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo, 0o600)
        # Reader first (non-blocking open succeeds with no writer), then a dummy writer we hold
        # until x265 has exited: x265's open() never blocks, and EOF only comes after x265 is done.
        self.rfd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.dummy_w = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.set_blocking(self.rfd, True)
        argv = [cfg.x265] + substitute(self.args, self.rpu, self.fifo)
        setup = _make_child_setup(os.getpid())
        self._start_inhibitor()
        try:
            self.zstd = subprocess.Popen([cfg.zstd, "-d", "-q", "-c"], stdin=None,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         preexec_fn=setup)
            self.x265 = subprocess.Popen(argv, stdin=self.zstd.stdout, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, preexec_fn=setup)
        except OSError as e:
            return ("error", "cannot start %s: %s" % ("x265" if self.zstd else "zstd", e), 0)
        finally:
            if self.zstd is not None and self.zstd.stdout is not None:
                self.zstd.stdout.close()
        self.last_activity = time.monotonic()
        self._start_thread("copy", self._copy_out)
        self._start_thread("x265err", self._forward_x265_err, self.x265.stderr)
        self._start_thread("zstderr", self._collect_zstd_err, self.zstd.stderr)
        self._start_thread("watchdog", self._watchdog)

        while not self.stop.wait(0.1):
            if self.x265.poll() is not None:
                break
            if self.summary_at is not None:
                # x265 opened its output long before its summary: from now on EOF on the FIFO
                # means x265 itself closed it
                self._close_dummy()
            seen = [t for t in (self.summary_at, self.error_at) if t is not None]
            if seen and time.monotonic() - min(seen) >= cfg.exit_grace:
                self._kill_hung_x265()
                break
        if self.stop.is_set():
            return self._aborted()
        # x265 is done: let the copier drain the FIFO to EOF
        self._close_dummy()
        while self.threads["copy"].is_alive():
            if self.stop.wait(0.1):
                return self._aborted()
        self.threads["x265err"].join(5)
        try:
            self.zstd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.zstd_killed = True
            self.zstd.kill()
            self.zstd.wait()
        self.threads["zstderr"].join(2)
        if self.stop.is_set():
            return self._aborted()
        return self._classify()

    def _output_closed(self):
        """Has x265 closed its end of the output FIFO (so every byte it wrote is in the FIFO)?
        The dummy writer is gone once the summary is out, so a copier that has reached EOF
        while x265 still lives says yes; otherwise Linux's /proc/<pid>/fd answers exactly.
        When neither can tell: no (the conservative answer)."""
        if self.summary_at is not None and self.dummy_w is None \
                and not self.threads["copy"].is_alive():
            return True
        fd_dir = os.path.join("/proc", str(self.x265.pid), "fd")   # our real child, not cfg.proc
        try:
            want = os.stat(self.fifo)
            fds = os.listdir(fd_dir)
        except OSError:
            return False
        for fd in fds:
            try:
                st = os.stat(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino):
                return False
        return True

    def _kill_hung_x265(self):
        """x265 printed its summary or an error line EXIT_GRACE s ago and is still alive."""
        if self.summary_at is not None and (self.error_at is None or self.summary_at <= self.error_at):
            what = "its summary"
        else:
            what = "an error line"
        closed = self._output_closed()
        try:
            self.x265.kill()
        except OSError:
            pass
        try:
            self.x265.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.hung = (what, closed)

    def _encoded(self):
        found = re.findall(rb"encoded (\d+) frames", self.err_tail)
        return int(found[-1]) if found else None

    def _zstd_read_failed(self):
        """zstd failed READING its input (the Mac's stream broke), not writing to a dead x265."""
        zrc = self.zstd.returncode
        return bool(zrc) and not self.zstd_killed and bool(_ZSTD_READ_FAIL_RE.search(self.zstd_err))

    def _classify(self):
        n = self._encoded()
        xrc, zrc = self.x265.returncode, self.zstd.returncode
        if self.hung is not None:
            what, closed = self.hung
            if n == self.frames and closed:
                return ("ok", "", n)
            why = "x265 did not exit %g s after %s" % (self.cfg.exit_grace, what)
            if n is not None and n != self.frames:
                why += " (encoded %d of %d frames)" % (n, self.frames)
            elif n is not None:
                why += " with its output still open"
            tail = _tail_text(self.err_tail)
            return ("error", why + (": " + tail if tail and n is None else ""), n or 0)
        zstd_broken = ("input stream broken (zstd %s: %s)"
                       % (_rc_text(zrc), _tail_text(self.zstd_err, 1)))
        if xrc != 0:
            if self._zstd_read_failed():          # the input broke first; x265 died of it
                return ("disconnected", zstd_broken, n or 0)
            # x265 failed or crashed; zstd then died on the broken pipe (or was killed by us)
            tail = _tail_text(self.err_tail)
            return ("error", "x265 %s%s" % (_rc_text(xrc), ": " + tail if tail else ""), n or 0)
        if zrc != 0:                              # x265 was fine: zstd's input broke
            return ("disconnected", zstd_broken, n or 0)
        if n is None:
            return ("error", "x265 reported no frame count", 0)
        if n < self.frames:
            return ("disconnected", "input ended early: %d of %d frames" % (n, self.frames), n)
        if n != self.frames:
            return ("error", "x265 encoded %d frames, expected %d" % (n, self.frames), n)
        return ("ok", "", n)

    def _aborted(self):
        self._kill_children()
        return (self.result, self.why, self._encoded() or 0)

    def finish(self, result, why, frames):
        """Cleanup common to every exit: children dead, job dir gone, VWORKER line last."""
        try:
            self._kill_children()
        except Exception:
            pass
        try:
            self._stop_inhibitor()                # already gone unless _kill_children failed
        except Exception:
            pass
        t = self.threads.get("x265err")
        if t is not None:
            t.join(1)
        if self.owns_dir and JOB_ID_RE.fullmatch(self.job_id or ""):
            _rmtree(self.jobdir)
        rec = {"result": result, "why": why, "frames": int(frames or 0)}
        if result in ("yielded", "refused") and self.code in REASON_CODES:
            rec["code"] = self.code           # the Mac's status line reads this, not the why
        line = "VWORKER " + json.dumps(rec, separators=(",", ":"))
        with self.err_lock:                   # one hold: nothing can be written after this line
            if self.err_open:
                data = (b"" if self.err_at_bol else b"\n") + line.encode() + b"\n"
                try:
                    mv = memoryview(data)
                    while mv:
                        mv = mv[os.write(2, mv):]
                except OSError:
                    pass
            self.err_open = False
        return EXIT_FOR.get(result, 1)


def cmd_encode(cfg, job_id, b64, runner=None):
    job = EncodeJob(cfg, job_id, runner)
    job.install_signals()
    try:
        early = job.prepare(b64)
        if early is not None:
            return job.finish(early[0], early[1], 0)
        return job.finish(*job.run())
    except Exception as e:
        job.abort("error", "vworker: %s: %s" % (type(e).__name__, e))
        return job.finish(job.result, job.why, 0)


# ---- put ------------------------------------------------------------------------------------

_SIZE_RE = re.compile(r"\d{1,12}")


def cmd_put(cfg, job_id, name, size, stdin=None):
    """Exactly `size` bytes of stdin -> jobs/<job>/<name>. Prints {"ok":..,"bytes":..,
    "sha256":..} on stdout. A short read (the Mac's stream ended early) or a long one (more
    than `size` before EOF) deletes the job dir and fails; so does SIGHUP/SIGTERM/SIGINT while
    the put is in progress, and so does a put that has not finished cfg.put_deadline seconds
    after it started (SIGALRM: a link that cannot move a few hundred KB in a minute is gone).
    The handlers and the timer are restored on return."""
    if not JOB_ID_RE.fullmatch(job_id or ""):
        return _fail("bad job id")
    if not NAME_RE.fullmatch(name or "") or name.startswith(".") or name in RESERVED_NAMES:
        return _fail("bad name")
    if not isinstance(size, str) or not _SIZE_RE.fullmatch(size) or int(size) > PUT_MAX_BYTES:
        return _fail("bad size (want 0..%d bytes)" % PUT_MAX_BYTES)
    size = int(size)
    jobdir = os.path.join(cfg.jobs, job_id)
    state = {"active": False}

    def on_signal(signum, _frame):
        if state["active"]:
            _rmtree(jobdir)
        if signum == signal.SIGALRM:
            _fail("put timed out after %g s" % cfg.put_deadline)
        else:
            _fail("put stopped by %s" % signal.Signals(signum).name)
        os._exit(1)

    old, timed = {}, False

    def stop_timer():
        if timed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            except (ValueError, OSError):
                pass
    try:
        if threading.current_thread() is threading.main_thread():
            for s in STOP_SIGNALS + (signal.SIGALRM,):
                old[s] = signal.signal(s, on_signal)
            if _EARLY:                            # it came while main was still sweeping
                return _fail("put stopped by %s" % signal.Signals(_EARLY[0]).name)
            if cfg.put_deadline > 0:
                signal.setitimer(signal.ITIMER_REAL, cfg.put_deadline)
                timed = True
        with jobs_lock(cfg):
            m = read_marker(jobdir)
            if m and m.get("role") == "encode" and marker_alive(m, cfg.proc):
                return _fail("job %s is encoding" % job_id)
            state["active"] = True
            os.makedirs(jobdir, mode=0o700, exist_ok=True)
            write_marker(jobdir, "put", cfg.proc)
        src = stdin if stdin is not None else sys.stdin.buffer
        dest = os.path.join(jobdir, name)
        tmp = os.path.join(jobdir, "." + name + ".part")
        h, total = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as f:
                while total < size:
                    chunk = src.read(min(1 << 20, size - total))
                    if not chunk:
                        raise ValueError("short read: got %d of %d bytes" % (total, size))
                    total += len(chunk)
                    h.update(chunk)
                    f.write(chunk)
                if src.read(1):
                    raise ValueError("long read: more than %d bytes" % size)
            stop_timer()                             # read whole: no late alarm past this point
            os.replace(tmp, dest)
            write_marker(jobdir, "put", cfg.proc)   # refresh: the stage TTL runs from now
            print(json.dumps({"ok": True, "bytes": total, "sha256": h.hexdigest()}), flush=True)
        except (OSError, ValueError) as e:
            stop_timer()
            _rmtree(jobdir)
            return _fail("put failed: %s" % e)
        state["active"] = False                  # staged: a late signal leaves it to the sweep
        return 0
    finally:
        stop_timer()
        state["active"] = False
        for s, h_old in old.items():
            try:
                signal.signal(s, h_old)
            except (ValueError, TypeError):
                pass


def _fail(why):
    try:
        print(json.dumps({"ok": False, "why": why}), flush=True)
        sys.stderr.write("vworker: %s\n" % why)
    except OSError:
        pass
    return 1


# ---- main -----------------------------------------------------------------------------------

USAGE = ("usage: vworker status | put <job_id> <name> <bytes> | encode <job_id> <b64> | "
         "kill [<job_id>] | sweep")


_EARLY = []            # stop signals that arrived before a command installed its own handlers


def _note_early_signal(signum, _frame):
    _EARLY.append(signum)


def install_early_signals():
    """main's FIRST act: SIGTERM/SIGHUP/SIGINT are recorded, never fatal, from the very start
    (the sweep included). encode and put install their own handlers next and act on a recorded
    one at once (the encode finishes "disconnected", the put fails); status gives up; kill and
    sweep finish their (bounded) work. -> False when not on the main thread (nothing done)."""
    if threading.current_thread() is not threading.main_thread():
        return False
    for s in STOP_SIGNALS:
        signal.signal(s, _note_early_signal)
    return True


def main(argv=None, cfg=None):
    install_early_signals()
    argv = sys.argv[1:] if argv is None else argv
    cfg = cfg or Config()
    cmd = argv[0] if argv else ""
    try:
        removed = sweep(cfg)
    except Exception as e:
        removed = []
        if cmd != "encode":
            sys.stderr.write("vworker: sweep failed: %s\n" % e)
    if cmd == "status" and len(argv) == 1:
        if _EARLY:
            return 1
        try:
            st = compute_status(cfg, ssh_connection=os.environ.get("SSH_CONNECTION"))
        except Exception as e:
            st = {"ok": False, "why": "status error: %s" % e, "code": "error", "ac": False,
                  "ethernet": False, "game": None, "heavy": None, "free_gb": 0.0, "busy": False,
                  "x265": "", "version": PROTOCOL_VERSION, "sha256": self_sha256(), "ipv4": [],
                  "sleep_block": None}
        if _EARLY:
            return 1
        print(json.dumps(st, separators=(",", ":")), flush=True)
        return 0
    if cmd == "put" and len(argv) == 4:
        return cmd_put(cfg, argv[1], argv[2], argv[3])
    if cmd == "encode" and len(argv) == 3:
        return cmd_encode(cfg, argv[1], argv[2])
    if cmd == "kill" and len(argv) in (1, 2):
        job = argv[1] if len(argv) == 2 else None
        if job is not None and not JOB_ID_RE.fullmatch(job):
            return _fail("bad job id")
        pids = kill_all(cfg, job=job)
        print(json.dumps({"ok": True, "killed": pids}), flush=True)
        return 0
    if cmd == "sweep" and len(argv) == 1:
        print(json.dumps({"ok": True, "removed": removed}), flush=True)
        return 0
    sys.stderr.write(USAGE + "\n")
    return 1


if __name__ == "__main__":
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)       # never wait on a copier thread blocked on a dead ssh channel
