#!/usr/bin/env python3
"""vworker -- Visionary's optional x265 helper, running on a Linux machine on Ethernet ("the
helper"; first measured on a docked ROG Ally under Bazzite).

The Mac decodes + colour-converts a segment itself (its SDR->PQ step is the only bit-exact one)
and streams the raw y4m to us, zstd-compressed, over ssh. We only run x265 on it and stream the
HEVC back on stdout as it is produced. Nothing we make is kept on the helper: the only file a job
ever owns is its RPU slice, and the job directory is deleted on every exit path.

Wire protocol v1 (the Mac half lives in engine/helper.py):
    vworker status                 one JSON line on stdout, exit 0 always, answers in < 3 s;
                                   "x265" is the full version token (e.g. "4.1+1-1d117be");
                                   "code" is the reason as one word (REASON_CODES) next to "why"
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
(> 1.5 cores) or GPU (> 30 %) for 10 s, when the machine leaves mains power or Ethernet, or when
the Mac goes away (parent gone, stdout broken, input truncated, no data for STALL_SECS). Ordinary apps (browser, file manager, Steam's own
menus) are below those thresholds and do not stop it. A Steam launch is a game when its AppId
is a real Steam app (< 2^31) or Wine/Proton runs under it; a non-Steam shortcut (AppId >= 2^31)
running a native Linux program (Firefox, Dolphin, ...) is judged by the CPU/GPU rule only.
zstd and x265 run under SCHED_IDLE (nice 19 where that is unavailable): sched_autogroup is on,
so nice alone would not let the desktop win.

Stdlib only. Runs on Python 3.8+ (the measured helper has 3.14; the Mac's tests run it too).
Paths and timings can be overridden through VWORKER_* environment variables (tests use them).
"""
from __future__ import annotations

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
PF_KTHREAD = 0x00200000

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


# ---- configuration --------------------------------------------------------------------------

def _env_float(env, name, default):
    try:
        return float(env.get(name, ""))
    except ValueError:
        return default


class Config:
    """Paths and timings. Everything is overridable from the environment (VWORKER_*)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = os.path.expanduser("~")
        self.x265 = env.get("VWORKER_X265") or os.path.join(home, "visionary-worker", "bin", "x265")
        self.zstd = env.get("VWORKER_ZSTD") or "/usr/bin/zstd"
        self.jobs = env.get("VWORKER_JOBS") or os.path.join(home, "visionary-scratch", "jobs")
        self.proc = env.get("VWORKER_PROC") or "/proc"
        self.sys = env.get("VWORKER_SYS") or "/sys"
        self.watch_interval = _env_float(env, "VWORKER_WATCH_INTERVAL", 2.0)
        self.heavy_secs = _env_float(env, "VWORKER_HEAVY_SECS", 10.0)
        self.status_sample = _env_float(env, "VWORKER_STATUS_SAMPLE", 1.0)
        self.stall_secs = _env_float(env, "VWORKER_STALL_SECS", 300.0)
        self.min_free_gb = _env_float(env, "VWORKER_MIN_FREE_GB", 30.0)
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

def ac_online(sys_root):
    """Docked on mains: any power_supply of type Mains that is online."""
    for d in sorted(glob.glob(os.path.join(sys_root, "class", "power_supply", "*"))):
        if _read(os.path.join(d, "type")) == "Mains" and _read(os.path.join(d, "online")) == "1":
            return True
    return False


def ethernet_links(sys_root):
    """Names of en* interfaces whose link is up with carrier."""
    up = []
    for d in sorted(glob.glob(os.path.join(sys_root, "class", "net", "en*"))):
        if _read(os.path.join(d, "operstate")) == "up" and _read(os.path.join(d, "carrier")) == "1":
            up.append(os.path.basename(d))
    return up


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


def gpu_busy(sys_root, runner):
    """(highest busy percent of any GPU, label). AMD/Intel: drm gpu_busy_percent. NVIDIA:
    nvidia-smi, only when the nvidia module is loaded and nvidia-smi exists."""
    best, label = 0.0, ""
    for card in sorted(glob.glob(os.path.join(sys_root, "class", "drm", "card*"))):
        name = os.path.basename(card)
        if not re.fullmatch(r"card\d+", name):
            continue
        v = _read(os.path.join(card, "device", "gpu_busy_percent"))
        try:
            pct = float(v) if v else None
        except ValueError:
            pct = None
        if pct is not None and pct > best:
            best, label = pct, "GPU %s %.0f%% busy" % (name, pct)
    if os.path.isdir(os.path.join(sys_root, "module", "nvidia")):
        r = runner(["nvidia-smi", "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits"], 1.0)
        if r and r[0] == 0:
            for i, line in enumerate(r[1].splitlines()):
                try:
                    pct = float(line.strip())
                except ValueError:
                    continue
                if pct > best:
                    best, label = pct, "NVIDIA GPU %d %.0f%% busy" % (i, pct)
    return best, label


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
    return os.path.isfile(cfg.zstd) and os.access(cfg.zstd, os.X_OK)


def heavy_only(st, cfg):
    """Is a heavy CPU/GPU reading the ONLY thing between this status and ok? (Then an encode
    re-samples instead of refusing at once: one busy second is not "anything heavy".)"""
    return bool(st.get("heavy") and st.get("ac") and st.get("ethernet") and not st.get("game")
                and not st.get("busy") and st.get("x265") and _zstd_ok(cfg)
                and free_gb(cfg.jobs) >= cfg.min_free_gb)


def compute_status(cfg, runner=None, exclude_job=None, ssh_connection=None, self_pid=None,
                   parent_pid=None, clock=time.monotonic, sleep=time.sleep):
    """The status dict. `exclude_job` is the job an encode is about to claim (its own staged
    dir must not read as busy)."""
    runner = runner or default_runner
    self_pid = os.getpid() if self_pid is None else self_pid
    parent_pid = os.getppid() if parent_pid is None else parent_pid
    ac = ac_online(cfg.sys)
    links = ethernet_links(cfg.sys)
    ethernet, eth_why = bool(links), "no Ethernet"
    if ethernet and ssh_connection:
        via = ssh_arrival_iface(ssh_connection, runner)
        if via and not via.startswith("en"):
            ethernet, eth_why = False, "no Ethernet: ssh arrives on %s" % via
    procs = list_procs(cfg.proc)
    own = own_tree(procs, self_pid, parent_pid)
    game = detect_game(procs, own)
    cpu = None
    if cfg.status_sample > 0:
        sampler = CpuSampler(cfg.proc)
        sampler.sample(procs, own, clock())
        sleep(cfg.status_sample)
        procs = list_procs(cfg.proc)
        own = own_tree(procs, self_pid, parent_pid)
        game = game or detect_game(procs, own)
        cpu = sampler.sample(procs, own, clock())
    gpu = gpu_busy(cfg.sys, runner)
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
            "x265": ver, "version": PROTOCOL_VERSION}


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
    """In the child: SCHED_IDLE (Bazzite runs sched_autogroup, so nice alone does not let the
    desktop win), else nice 19. -> "idle" or "nice"."""
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
            heavy = HeavyTracker(cfg.heavy_secs)
            procs = list_procs(cfg.proc)
            sampler.sample(procs, own_tree(procs, me, self.ppid0, kids()), time.monotonic())
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
                if not ethernet_links(cfg.sys):
                    return self.abort("disconnected", "Ethernet down")
                if not ac_online(cfg.sys):
                    return self.abort("yielded", "on battery", "battery")
                cpu = sampler.sample(procs, own, now)
                what = heavy.update(now, last, cpu, gpu_busy(cfg.sys, runner))
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
                  "x265": "", "version": PROTOCOL_VERSION}
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
