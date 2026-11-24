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
import ast
import base64
import hashlib
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


def add_proc(proc_root, pid, comm, argv, ppid=1, ticks=0, start=1000, flags=0, state="S",
             cgroup=None):
    """Create /proc/<pid> atomically (stat + cmdline, and its cgroup v2 path when given, appear
    together)."""
    tmp = os.path.join(proc_root, ".tmp-%d" % pid)
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "stat"), "w") as f:
        f.write(stat_line(pid, comm, ppid, ticks, start, state=state, flags=flags))
    with open(os.path.join(tmp, "cmdline"), "wb") as f:
        f.write(b"".join(a.encode() + b"\0" for a in argv))
    if cgroup is not None:
        with open(os.path.join(tmp, "cgroup"), "w") as f:
            f.write("0::%s\n" % cgroup)
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


def add_net(root, name, kind=1, state="up", carrier="1", virtual=False, wireless=False,
            lower=(), brif=(), slaves=()):
    """/sys/class/net/<name> laid out as the kernel does it: a link into devices/, under
    devices/virtual for a virtual interface (bridge, bond, VLAN, veth, tun, ...). kind is
    the ARPHRD type (1 Ethernet, 772 loopback, 65534 none: tun, WireGuard, Tailscale)."""
    where = "devices/virtual/net" if virtual else "devices/pci0000:00/%s-nic/net" % name
    d = os.path.join(root, where, name)
    write(os.path.join(d, "type"), "%d\n" % kind)
    write(os.path.join(d, "operstate"), state + "\n")
    if carrier is not None:
        write(os.path.join(d, "carrier"), carrier + "\n")
    if wireless:
        os.makedirs(os.path.join(d, "wireless"))
    for m in lower:
        os.symlink("../" + m, os.path.join(d, "lower_" + m))
    if brif:
        os.makedirs(os.path.join(d, "brif"))
        for m in brif:
            os.symlink("../../" + m, os.path.join(d, "brif", m))
    if slaves:
        write(os.path.join(d, "bonding/slaves"), " ".join(slaves) + "\n")
    link = os.path.join(root, "class/net", name)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(os.path.relpath(d, os.path.dirname(link)), link)
    return d


def add_supply(root, name, kind, **files):
    """/sys/class/power_supply/<name>: type plus any of online, scope, status."""
    write(os.path.join(root, "class/power_supply", name, "type"), kind + "\n")
    for k, v in files.items():
        write(os.path.join(root, "class/power_supply", name, k), "%s\n" % v)


def make_sys(root, ac=True, eth=True, gpu=3, wifi=True):
    add_supply(root, "ACAD", "Mains", online=1 if ac else 0)
    add_supply(root, "BAT0", "Battery", online=1)
    add_net(root, "enp4s0", state="up" if eth else "down", carrier="1" if eth else "0")
    if wifi:
        add_net(root, "wlan0", wireless=True)
    write(os.path.join(root, "class/drm/card0/device/gpu_busy_percent"), "%d\n" % gpu)
    os.makedirs(os.path.join(root, "class/drm/card0-DP-1"), exist_ok=True)


def add_card(root, card, pdev, busy=None):
    """/sys/class/drm/<card> whose device link names the PCI address fdinfo's drm-pdev gives."""
    dev = os.path.join(root, "devices/pci0000:00", pdev)
    os.makedirs(dev, exist_ok=True)
    if busy is not None:
        write(os.path.join(dev, "gpu_busy_percent"), "%d\n" % busy)
    d = os.path.join(root, "class/drm", card)
    os.makedirs(d, exist_ok=True)
    os.symlink(os.path.relpath(dev, d), os.path.join(d, "device"))


def add_drm_fd(proc_root, pid, fd, client, engines, pdev="0000:03:00.0", driver="amdgpu",
               capacity=None, node="/dev/dri/renderD128"):
    """A DRM fd of /proc/<pid> and its fdinfo as amdgpu / i915 write it (engine time in ns)."""
    base = os.path.join(proc_root, str(pid))
    os.makedirs(os.path.join(base, "fd"), exist_ok=True)
    link = os.path.join(base, "fd", str(fd))
    if not os.path.lexists(link):
        os.symlink(node, link)
    lines = ["pos:\t0", "flags:\t02100002", "mnt_id:\t26", "ino:\t1063",
             "drm-driver:\t%s" % driver, "drm-pdev:\t%s" % pdev, "drm-client-id:\t%d" % client]
    lines += ["drm-engine-%s:\t%d ns" % kv for kv in sorted(engines.items())]
    lines += ["drm-engine-capacity-%s:\t%d" % kv for kv in sorted((capacity or {}).items())]
    lines += ["drm-memory-vram:\t1024 KiB"]
    write(os.path.join(base, "fdinfo", str(fd)), "\n".join(lines) + "\n")


XE_ENGINES = ("rcs", "bcs", "vcs", "vecs", "ccs")


def add_xe_fd(proc_root, pid, fd, client, cycles, total, pdev="0000:00:02.0", capacity=None,
              node="/dev/dri/renderD128"):
    """A DRM fd of /proc/<pid> and its fdinfo as Intel xe writes it: busy cycles per engine
    class against the GPU clock (drm-total-cycles, read once for every class), memory lines
    that also start with "drm-total-", and no drm-engine-<class> time at all."""
    base = os.path.join(proc_root, str(pid))
    os.makedirs(os.path.join(base, "fd"), exist_ok=True)
    link = os.path.join(base, "fd", str(fd))
    if not os.path.lexists(link):
        os.symlink(node, link)
    lines = ["pos:\t0", "flags:\t0100002", "mnt_id:\t26", "ino:\t685", "drm-driver:\txe",
             "drm-client-id:\t%d" % client, "drm-pdev:\t%s" % pdev, "drm-total-system:\t0",
             "drm-total-gtt:\t192 KiB", "drm-total-vram0:\t23992 KiB",
             "drm-resident-vram0:\t23992 KiB"]
    for eng in XE_ENGINES:
        lines += ["drm-cycles-%s:\t%d" % (eng, cycles.get(eng, 0)),
                  "drm-total-cycles-%s:\t%d" % (eng, total)]
        if eng in (capacity or {}):
            lines.append("drm-engine-capacity-%s:\t%d" % (eng, capacity[eng]))
    write(os.path.join(base, "fdinfo", str(fd)), "\n".join(lines) + "\n")


def put_counter(path, n):
    """Write a /sys or /proc counter atomically (a reader never sees it half written)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write("%d\n" % n)
    os.replace(path + ".tmp", path)


def set_rx(sys_root, iface, n):
    put_counter(os.path.join(sys_root, "class/net", iface, "statistics/rx_bytes"), n)


def set_rchar(proc_root, pid, n):
    """/proc/<pid>/io as the kernel writes it (rchar counts every read(), pipes included)."""
    d = os.path.join(proc_root, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "io.tmp"), "w") as f:
        f.write("rchar: %d\nwchar: %d\nsyscr: 632687\nsyscw: 632675\nread_bytes: 0\n"
                "write_bytes: 0\ncancelled_write_bytes: 0\n" % (n, n))
    os.replace(os.path.join(d, "io.tmp"), os.path.join(d, "io"))


def set_cgroup(sys_root, path, frozen, populated=1, freeze=None, procs=None):
    """A cgroup v2 directory under <sys>/fs/cgroup as the kernel shows it, each file replaced
    atomically and in the kernel's order: cgroup.freeze (the state asked for; as `frozen`
    unless given -- a frozen game being killed reads freeze 1 while frozen is 0), then
    cgroup.events, and cgroup.procs when given. -> its cgroup.events."""
    d = os.path.join(sys_root, "fs", "cgroup") + path
    os.makedirs(d, exist_ok=True)
    freeze = frozen if freeze is None else freeze
    files = [("cgroup.freeze", "%d\n" % (1 if freeze else 0)),
             ("cgroup.events", "populated %d\nfrozen %d\n" % (populated, 1 if frozen else 0))]
    if procs is not None:
        files.append(("cgroup.procs", "".join("%d\n" % pid for pid in procs)))
    for name, text in files:
        with open(os.path.join(d, name + ".tmp"), "w") as f:
            f.write(text)
        os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
    return os.path.join(d, "cgroup.events")


def cgroup_file(sys_root, path, name):
    return os.path.join(sys_root, "fs", "cgroup") + path + "/" + name


def seed_rest(path, keys, age):
    """The worker's rest-seen file as if these games had rested for `age` s by now (this
    boot's monotonic clock, as the worker's own processes read it)."""
    vworker.RestSeen(path, clock=lambda: time.monotonic() - age).update(keys)


def local_time(t):
    """A rest manager's "waking": the local time, "YYYY-MM-DD HH:MM:SS"."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def write_rest_file(path, **data):
    """A rest manager's file, replaced atomically (the worker only ever reads it)."""
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def remove_proc(proc_root, pid):
    """The process exits: /proc/<pid> disappears in one step (renamed out, then deleted)."""
    gone = os.path.join(os.path.dirname(proc_root.rstrip("/")), ".gone-%d" % pid)
    os.rename(os.path.join(proc_root, str(pid)), gone)
    shutil.rmtree(gone)


def until(pred, timeout=5.0):
    """Wait for pred() to be true -> True, or False after `timeout` s."""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def note_latency(what, seconds):
    """A measured stop latency, appended to $VWORKER_TEST_LATENCY_LOG when that is set (the
    report's numbers); nothing otherwise."""
    log = os.environ.get("VWORKER_TEST_LATENCY_LOG")
    if log:
        with open(log, "a") as f:
            f.write("%s\t%.3f\n" % (what, seconds))


def write_meminfo(proc_root, available_gb):
    """/proc/meminfo with MemAvailable at `available_gb` (2^30 bytes), replaced atomically."""
    path = os.path.join(proc_root, "meminfo")
    with open(path + ".tmp", "w") as f:
        f.write("MemTotal:       15523840 kB\nMemFree:          412044 kB\n"
                "MemAvailable:   %8d kB\nBuffers:            4016 kB\n"
                % int(round(available_gb * (1 << 20))))
    os.replace(path + ".tmp", path)


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
    oom = os.environ.get("FAKE_X265_OOM")     # our oom_score_adj as x265 starts
    if oom:
        with open(oom) as src, open(ran + ".oom", "w") as f:
            f.write(src.read())
    with open(ran + ".ppid", "w") as f:       # who started it, and was that inside a scope?
        f.write("%d %s" % (os.getppid(), os.environ.get("VWORKER_IN_SCOPE", "")))
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


# a stand-in for systemd-inhibit. An encode's block (--what=sleep) notes its pid and arguments
# in <log>.<what>, then holds on like the real one (which runs `sleep infinity` under its
# block); status's probe (`... true`) appends its arguments to <log>.probe and exits like the
# real one would. FAKE_INHIBIT_FAIL: polkit says no to both
FAKE_INHIBIT = r'''#!{python}
import json, os, sys, time
log = os.environ["FAKE_INHIBIT_LOG"]
fail = bool(os.environ.get("FAKE_INHIBIT_FAIL"))
if sys.argv[-1] == "true":
    with open(log + ".probe", "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
    sys.exit(1 if fail else 0)
what = [a for a in sys.argv if a.startswith("--what=")][0][len("--what="):]
path = "%s.%s" % (log, what)
with open(path + ".tmp", "w") as f:
    f.write("\n".join([str(os.getpid())] + sys.argv[1:]))
os.rename(path + ".tmp", path)
if fail:
    sys.exit(1)
time.sleep(600)
'''

# a stand-in for systemd-inhibit as status's probe meets it: notes its arguments and whether
# stdin, stdout and stderr are /dev/null, waits SLEEP s, exits RC
FAKE_PROBE = r'''#!{python}
import json, os, sys, time
null = os.stat(os.devnull)
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:],
                        "devnull": [os.path.samestat(os.fstat(fd), null) for fd in (0, 1, 2)]}})
            + "\n")
time.sleep({sleep})
sys.exit({rc})
'''

# a stand-in for systemd-run: notes its arguments in FAKE_SYSTEMD_RUN_LOG (a JSON line each),
# then -- as the real one does once it has moved itself into the new scope -- execs the command
# after "--" in this very process (same pid, same stdin/stdout/stderr). The new scope is
# written into the fake /proc (self/cgroup) and /sys (its memory.max: MemoryMax) the worker
# reads. FAKE_SYSTEMD_RUN_FAIL: no scope can be started; FAKE_SYSTEMD_RUN_NOCAP: no memory cap
FAKE_SYSTEMD_RUN = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SYSTEMD_RUN_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_SYSTEMD_RUN_FAIL"):
    sys.stderr.write("Failed to start transient scope unit: Access denied\n")
    sys.exit(1)
unit = [a[len("--unit="):] for a in args if a.startswith("--unit=")]
if unit:
    path = "/user.slice/user-1000.slice/user@1000.service/app.slice/%s.scope" % unit[0]
    proc, sysfs = os.environ["VWORKER_PROC"], os.environ["VWORKER_SYS"]
    os.makedirs(os.path.join(proc, "self"), exist_ok=True)
    with open(os.path.join(proc, "self", "cgroup"), "w") as f:
        f.write("0::%s\n" % path)
    cap = [a.split("=", 1)[1] for a in args if a.startswith("MemoryMax=")]
    d = os.path.join(sysfs, "fs", "cgroup") + path
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "memory.max"), "w") as f:
        f.write(("max" if os.environ.get("FAKE_SYSTEMD_RUN_NOCAP") or not cap else cap[0])
                + "\n")
cmd = args[args.index("--") + 1:]
os.execvp(cmd[0], cmd)
'''

# a stand-in for ionice: notes its arguments in FAKE_IONICE_LOG, then execs the command after
# "-c 3" in this very process, as the real one does once it set the class; FAKE_IONICE_FAIL:
# the class can't be set, and like the real one it then runs nothing
FAKE_IONICE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_IONICE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_IONICE_FAIL") or args[:2] != ["-c", "3"]:
    sys.stderr.write("ionice: ioprio_set failed: Operation not permitted\n")
    sys.exit(1)
os.execvp(args[2], args[2:])
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
    """smi: `nvidia-smi --query-gpu` output; pmon: `nvidia-smi pmon` output, or (rc, output)."""

    def __init__(self, x265=None, ip_out="", smi=None, pmon=None):
        self.x265, self.ip_out, self.smi, self.pmon, self.calls = x265, ip_out, smi, pmon, []

    def __call__(self, argv, timeout=2.0):
        self.calls.append(argv)
        if self.x265 and argv == [self.x265, "--version"]:
            return (0, "", "x265 [info]: HEVC encoder version 4.1+1-1d117be\n")
        if argv[0] == "ip":
            return (0, self.ip_out, "")
        if argv[:2] == ["nvidia-smi", "pmon"]:
            if isinstance(self.pmon, tuple):
                return (self.pmon[0], self.pmon[1], "")
            return None if self.pmon is None else (0, self.pmon, "")
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
        self.rest_file = os.path.join(self.tmp, "ally-rest.json")
        self.rest_seen = os.path.join(self.tmp, "rest-seen.json")
        vworker._SCOPE_PROBE.clear()
        vworker._IONICE_PROBE.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    IP_OUT = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
              "2: enp4s0    inet 192.0.2.20/24 brd 192.0.2.255 scope global enp4s0\\  x\n"
              "3: wlan0    inet 192.0.2.30/24 brd 192.0.2.255 scope global wlan0\\  x\n"
              "3: wlan0    inet6 fe80::1234/64 scope link \\       valid_lft forever\n")

    def status(self, cfg=None, sleep=None, **kw):
        """compute_status over the fake trees: a counting clock, an instant sample."""
        clock = iter(range(0, 1000, 1))
        kw.setdefault("runner", FakeRunner(self.x265, self.IP_OUT))
        kw.setdefault("self_pid", 4000)
        kw.setdefault("parent_pid", 3999)
        return vworker.compute_status(cfg or self.cfg(), clock=lambda: float(next(clock)),
                                      sleep=sleep or (lambda s: None), **kw)

    def cfg(self, **over):
        # a rest manager's file, the rest ages and the tools of a scope and of I/O priority are
        # this test's own (none, unless a test makes them)
        env = {"VWORKER_X265": self.x265, "VWORKER_ZSTD": ZSTD or "/nonexistent",
               "VWORKER_JOBS": self.jobs, "VWORKER_PROC": self.proc, "VWORKER_SYS": self.sys,
               "VWORKER_MIN_FREE_GB": "0", "VWORKER_STATUS_SAMPLE": "1",
               "VWORKER_REST_FILE": self.rest_file, "VWORKER_REST_SEEN": self.rest_seen,
               "VWORKER_SYSTEMD_RUN": "", "VWORKER_IONICE": ""}
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
        self.reject(swap("--output", "/home/user/out.hevc"))
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


STEAM_DIR = "/home/user/.local/share/Steam"


def shortcut_tree(app, base=50):
    """Steam (gamepadui) launching a non-Steam shortcut, as `ps` shows it on a handheld:
    steam -> reaper SteamLaunch AppId=<2^31 + n> -> steam-launch-wrapper -> the program.
    app: "firefox" / "dolphin" (native programs) or "proton" (a Windows game via Proton)."""
    wrap = STEAM_DIR + "/ubuntu12_32/steam-launch-wrapper"
    cmd = {"firefox": ["/usr/bin/flatpak", "run", "org.mozilla.firefox"],
           "dolphin": ["/usr/bin/dolphin"],
           "proton": [STEAM_DIR + "/ubuntu12_32/steam-runtime/..sniper/_v2-entry-point",
                      "--verb=waitforexitandrun", "--",
                      STEAM_DIR + "/steamapps/common/Proton 9.0 (Beta)/proton",
                      "waitforexitandrun", "/home/user/Games/Celeste/Celeste.exe"]}[app]
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
                 P(base + 5, "Celeste.exe", ["Z:\\home\\user\\Games\\Celeste\\Celeste.exe"],
                   ppid=base + 3)]
    return tree


# A Steam game as read on a handheld (GTA V Enhanced, AppId 3240220; argv shortened as read):
# Steam's launch wrapper lives in Steam's OWN cgroup, which nothing freezes, with a lower pid
# than the game; reaper, wineserver and the game (a Windows .exe under Proton, 5.2 GB) live in
# the game's app scope, which a rest mode freezes.
APP_SLICE = "/user.slice/user-1000.slice/user@1000.service/app.slice"
STEAM_CGROUP = APP_SLICE + "/app-steam@autostart.service"
GTA_SCOPE = APP_SLICE + "/app-steam-app3240220-1829784.scope"
STEAMRT = STEAM_DIR + "/steamapps/common/SteamLinuxRuntime_4/steamrt64"
GTA = "Steam app 3240220"                     # its label: the wrapper's, the first in pid order
GTA_WRAPPER, GTA_REAPER, GTA_WINESERVER, GTA_EXE = 1829775, 1829784, 1831150, 1831209


def add_gta(proc_root, sys_root, frozen=True, state="S", flags=0, start=1000, freeze=None):
    """The game above in a fake /proc and /sys. frozen: its scope's freezer (a rest mode);
    freeze: its scope's cgroup.freeze when it differs (1 while it is killed); state, flags,
    start: its scope's processes' /proc stat ("T": SIGSTOPped, "t": traced, "Z": a zombie;
    PF_EXITING; another start time: the pid was reused)."""
    launch = [STEAMRT + "/reaper", "SteamLaunch", "AppId=3240220", "--",
              STEAMRT + "/pv-adverb", "--"]
    add_proc(proc_root, 1828000, "steam", [STEAM_DIR + "/ubuntu12_32/steam", "-gamepadui"],
             cgroup=STEAM_CGROUP)
    add_proc(proc_root, GTA_WRAPPER, "steam-runtime-l",
             ["steam-runtime-launch-client", "--alongside-steam", "--forward-fd=132", "--"]
             + launch, ppid=1828000, cgroup=STEAM_CGROUP)
    scope = dict(state=state, flags=flags, start=start, cgroup=GTA_SCOPE)
    add_proc(proc_root, GTA_REAPER, "reaper", launch, ppid=1828000, **scope)
    add_proc(proc_root, GTA_WINESERVER, "wineserver", [STEAM_DIR + "/files/bin/wineserver"],
             ppid=GTA_REAPER, **scope)
    add_proc(proc_root, GTA_EXE, "GTA5_Enhanced.e",
             ["Z:\\home\\user\\Games\\GTAV Enhanced\\GTA5_Enhanced.exe"], ppid=GTA_REAPER,
             **scope)
    set_cgroup(sys_root, STEAM_CGROUP, frozen=False, procs=[1828000, GTA_WRAPPER])
    return set_cgroup(sys_root, GTA_SCOPE, frozen=frozen, freeze=freeze,
                      procs=[GTA_REAPER, GTA_WINESERVER, GTA_EXE])


GTA_KEY = "scope:" + GTA_SCOPE                # its rest key: its scope, read as a whole
GTA_SUPERVISOR = 1829780


def add_supervisor(proc_root, sys_root, where, launch, state="S"):
    """steam-runtime-supervisor beside the game above, in Steam's own cgroup or in the game's
    scope (where; nobody knows which on a given system), itself a Steam launch (its argv runs
    reaper) or not (it inherits the AppId from the wrapper above it)."""
    argv = ["steam-runtime-supervisor", "--subreaper", "--terminate-timeout=10"]
    if launch:
        argv += ["--", STEAMRT + "/reaper", "SteamLaunch", "AppId=3240220", "--"]
    add_proc(proc_root, GTA_SUPERVISOR, "steam-runtime-s", argv, ppid=GTA_WRAPPER, state=state,
             cgroup=where)
    path = os.path.join(sys_root, "fs", "cgroup") + where + "/cgroup.procs"
    with open(path) as f:
        pids = [int(x) for x in f.read().split()]
    write(path, "".join("%d\n" % pid for pid in sorted(set(pids) | {GTA_SUPERVISOR})))


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


# -- resting games: frozen by a rest mode (the cgroup v2 freezer) or stopped (SIGSTOP)

class RestingGames(Tmp):
    """The real topology of a Steam game on a handheld (add_gta) in a fake /proc and /sys, and
    the rules that keep a running game from ever looking at rest (A1, A3)."""

    def scan(self, exclude=(), **kw):
        return vworker.scan_games(vworker.list_procs(self.proc), exclude, self.proc, self.sys,
                                  **kw)

    def cgroup_files(self, text):
        """Every scope process's /proc/<pid>/cgroup as `text` (None: no such file)."""
        for pid in (GTA_REAPER, GTA_WINESERVER, GTA_EXE):
            path = os.path.join(self.proc, str(pid), "cgroup")
            if text is None:
                os.unlink(path)
            else:
                write(path, text)

    def test_a_frozen_game_is_no_running_game(self):
        add_gta(self.proc, self.sys, frozen=True)
        ps = vworker.list_procs(self.proc)
        # the launch wrapper in Steam's own cgroup comes first and is never frozen: a freezer
        # check of each process alone would always find a running game in it
        self.assertEqual(vworker.detect_game(ps), GTA)          # without the roots: can't tell
        self.assertIsNone(vworker.detect_game(ps, (), self.proc, self.sys))
        s = self.scan()
        self.assertEqual((s.running, s.resting), (None, GTA))
        [r] = s.rests                                            # the game: its scope, whole
        self.assertEqual((r.key, r.label, r.scope, r.game), (GTA_KEY, GTA, GTA_SCOPE, 3240220))
        self.assertEqual(r.remnant, {(GTA_WRAPPER, 1000)})      # Steam's wrapper, outside it

    def test_the_same_game_thawed_is_a_game(self):
        add_gta(self.proc, self.sys, frozen=False)
        s = self.scan()
        self.assertEqual((s.running, s.resting, s.rests), (GTA, None, []))
        self.assertEqual(vworker.detect_game(vworker.list_procs(self.proc), (), self.proc,
                                             self.sys), GTA)

    def test_a_sigstopped_game_rests_too(self):
        add_gta(self.proc, self.sys, frozen=False, state="T")
        s = self.scan()
        self.assertEqual((s.running, s.resting), (None, GTA))
        self.assertEqual([r.key for r in s.rests], [GTA_KEY])  # every process in the scope: T
        self.assertIsNone(vworker.detect_game(vworker.list_procs(self.proc)))   # no roots needed

    def test_a_traced_game_is_running(self):
        add_gta(self.proc, self.sys, frozen=False, state="t")      # a debugger's stop is no rest
        self.assertEqual(self.scan().running, GTA)

    # -- A1: one side process at rest never puts a running game to rest
    def native(self, scope=True, reaper="S", game="S", child=None):
        """A native Linux Steam game (cs2-style, AppId 730): reaper -> cs2 (and a helper child
        of it in state `child`), in the game's own scope, or (scope=False, as on other
        distributions) in a session cgroup that no rest mode freezes."""
        where = (APP_SLICE + "/app-steam-app730-1700000.scope" if scope
                 else "/user.slice/user-1000.slice/session-2.scope")
        common = STEAM_DIR + "/steamapps/common/Counter-Strike Global Offensive"
        add_proc(self.proc, 1700000, "reaper", [STEAM_DIR + "/ubuntu12_32/reaper",
                                                "SteamLaunch", "AppId=730", "--",
                                                common + "/cs2.sh", "-steam"],
                 ppid=1828000, state=reaper, cgroup=where)
        add_proc(self.proc, 1700010, "cs2", [common + "/game/bin/linuxsteamrt64/cs2", "-steam"],
                 ppid=1700000, state=game, cgroup=where)
        pids = [1700000, 1700010]
        if child is not None:
            add_proc(self.proc, 1700020, "cs2", [common + "/game/bin/linuxsteamrt64/cs2",
                                                 "-steam", "-crashhandler"],
                     ppid=1700010, state=child, cgroup=where)
            pids.append(1700020)
        else:
            shutil.rmtree(os.path.join(self.proc, "1700020"), ignore_errors=True)
        set_cgroup(self.sys, where, frozen=False, procs=pids)
        return where

    def test_a_running_native_game_with_one_stopped_side_process_is_running(self):
        for scope in (True, False):
            self.native(scope=scope, child="T")               # its crash handler, stopped
            s = self.scan()
            self.assertEqual((s.running, s.resting, s.rests), ("Steam app 730", None, []), scope)
        where = self.native(scope=True, child="S")           # frozen as a whole: it rests
        set_cgroup(self.sys, where, frozen=True)
        self.assertEqual(self.scan().resting, "Steam app 730")

    def test_reaper_left_stopped_while_the_game_runs_is_running(self):
        for scope in (True, False):
            self.native(scope=scope, reaper="T")
            self.assertEqual(self.scan().running, "Steam app 730", scope)

    def test_a_whole_tree_stopped_without_a_scope_rests(self):
        self.native(scope=False, reaper="T", game="T", child="T")
        for roots in ((self.proc, self.sys), (None, None)):
            s = vworker.scan_games(vworker.list_procs(self.proc), (), *roots)
            self.assertEqual((s.running, s.resting), (None, "Steam app 730"), roots)
            [r] = s.rests
            self.assertEqual((r.key, r.scope), ("app:730", None))
            self.assertEqual([m[:2] for m in r.members], [(1700010, 1000), (1700020, 1000)])
            self.assertEqual(r.remnant, {(1700000, 1000)})     # reaper: a launch
        self.native(scope=False, reaper="T", game="T", child="S")  # not all of it
        self.assertEqual(self.scan().running, "Steam app 730")
        self.native(scope=True, reaper="T", game="T", child="T")   # a whole scope stopped
        self.assertEqual([r.key for r in self.scan().rests],
                         ["scope:" + APP_SLICE + "/app-steam-app730-1700000.scope"])

    def test_the_supervisor_anywhere_gives_the_same_verdicts(self):
        for where in (STEAM_CGROUP, GTA_SCOPE):
            for launch in (True, False):
                case = (where, launch)
                shutil.rmtree(self.proc)
                shutil.rmtree(os.path.join(self.sys, "fs"), ignore_errors=True)
                os.makedirs(self.proc)
                add_gta(self.proc, self.sys, frozen=True)
                add_supervisor(self.proc, self.sys, where, launch)
                s = self.scan()
                self.assertEqual((s.running, s.resting), (None, GTA), case)
                self.assertEqual([r.key for r in s.rests], [GTA_KEY], case)
                set_cgroup(self.sys, GTA_SCOPE, frozen=False)
                self.assertEqual(self.scan().running, GTA, case)
                # SIGSTOPped instead, everything in its scope: the supervisor too when it is
                # there; in Steam's cgroup it runs on beside the game, and the scope decides
                add_gta(self.proc, self.sys, frozen=False, state="T")
                add_supervisor(self.proc, self.sys, where, launch,
                               state="T" if where == GTA_SCOPE else "S")
                s = self.scan()
                self.assertEqual((s.running, s.resting), (None, GTA), case)
                add_gta(self.proc, self.sys, frozen=False)           # continued
                add_supervisor(self.proc, self.sys, where, launch)
                self.assertEqual(self.scan().running, GTA, case)

    # -- A3: a frozen game being killed or quit is no wake-up, and an exit is no game
    def test_a_frozen_game_being_killed_still_rests(self):
        add_gta(self.proc, self.sys, frozen=False, freeze=True)   # freeze 1, frozen 0: it dies
        s = self.scan()
        self.assertEqual((s.running, s.resting), (None, GTA))
        add_gta(self.proc, self.sys, frozen=False, freeze=True, flags=vworker.PF_EXITING)
        self.assertEqual(self.scan().running, None)

    def test_dead_zombie_and_exiting_processes_are_no_candidates(self):
        for left in (dict(state="Z"), dict(state="X"), dict(flags=vworker.PF_EXITING)):
            add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"], **left)
            self.assertIsNone(self.scan().running, left)
        add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"])
        self.assertEqual(self.scan().running, "wineserver")

    def test_an_emptied_scope_is_an_exit(self):
        # its processes are zombies now and the scope says it is empty: Steam's launch
        # wrapper, still there, is no game
        add_gta(self.proc, self.sys, frozen=False, freeze=True, state="Z")
        set_cgroup(self.sys, GTA_SCOPE, frozen=False, freeze=True, populated=0)
        s = self.scan()
        self.assertEqual((s.running, s.resting, s.rests), (None, None, []))
        set_cgroup(self.sys, GTA_SCOPE, frozen=False, populated=1)   # only zombies listed
        s = self.scan()
        self.assertEqual((s.running, s.resting, s.rests), (None, None, []))
        shutil.rmtree(os.path.join(self.sys, "fs", "cgroup") + GTA_SCOPE)   # removed
        s = self.scan()
        self.assertEqual((s.running, s.resting, s.rests), (None, None, []))

    def test_the_leftovers_of_an_exit_are_no_game(self):
        add_gta(self.proc, self.sys, frozen=True)
        rests = self.scan().rests
        # quit while it rested: the scope's processes and the scope are gone; Steam's launch
        # wrapper is still there for a moment
        for pid in (GTA_REAPER, GTA_WINESERVER, GTA_EXE):
            remove_proc(self.proc, pid)
        shutil.rmtree(os.path.join(self.sys, "fs", "cgroup") + GTA_SCOPE)
        self.assertEqual(self.scan().running, GTA)                # on its own: a launch starting
        s = self.scan(known=rests)                                # seen at rest: what's left
        self.assertEqual((s.running, s.resting, s.rests), (None, None, []))
        # a new launch of the same game is a game at once
        add_proc(self.proc, 1950000, "steam-runtime-l",
                 ["steam-runtime-launch-client", "--", "reaper", "SteamLaunch", "AppId=3240220"],
                 ppid=1828000, cgroup=STEAM_CGROUP)
        self.assertEqual(self.scan(known=rests).running, GTA)

    def test_whatever_cannot_be_told_counts_as_running(self):
        events = cgroup_file(self.sys, GTA_SCOPE, "cgroup.events")
        freeze = cgroup_file(self.sys, GTA_SCOPE, "cgroup.freeze")
        procs_file = cgroup_file(self.sys, GTA_SCOPE, "cgroup.procs")
        spoil = {
            "no cgroup file": lambda: self.cgroup_files(None),
            "an empty cgroup file": lambda: self.cgroup_files(""),
            "cgroup v1 only": lambda: self.cgroup_files("12:freezer:%s\n1:name=systemd:%s\n"
                                                        % (GTA_SCOPE, GTA_SCOPE)),
            "cgroup v1 beside v2": lambda: self.cgroup_files("12:freezer:%s\n0::%s\n"
                                                             % (GTA_SCOPE, GTA_SCOPE)),
            "no 0:: path": lambda: self.cgroup_files("0::\n"),
            "neither cgroup.events nor cgroup.freeze": lambda: (os.unlink(events),
                                                                os.unlink(freeze)),
            "no frozen line, freeze 0": lambda: (write(events, "populated 1\n"),
                                                 write(freeze, "0\n")),
            "a frozen value it does not know": lambda: (write(events, "populated 1\nfrozen 2\n"),
                                                        write(freeze, "0\n")),
            "a freeze value it does not know": lambda: (write(events, "populated 1\nfrozen 0\n"),
                                                        write(freeze, "2\n")),
        }
        for name, how in spoil.items():
            add_gta(self.proc, self.sys, frozen=True)
            self.assertIsNone(self.scan().running, name)
            how()
            self.assertEqual(self.scan().running, GTA, name)
        # stopped, but who is in its scope can't be read
        add_gta(self.proc, self.sys, frozen=False, state="T")
        self.assertIsNone(self.scan().running)
        os.unlink(procs_file)
        self.assertEqual(self.scan().running, GTA)
        # either freezer file alone saying so is enough
        for gone in (events, freeze):
            add_gta(self.proc, self.sys, frozen=True)
            os.unlink(gone)
            self.assertIsNone(self.scan().running, gone)

    def test_two_games_one_resting_one_running(self):
        lutris = APP_SLICE + "/app-lutris-game.scope"
        add_gta(self.proc, self.sys, frozen=True)
        add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"], cgroup=lutris)
        set_cgroup(self.sys, lutris, frozen=False, procs=[1900000])
        s = self.scan()
        self.assertEqual((s.running, s.resting), ("wineserver", GTA))   # the running one
        add_gta(self.proc, self.sys, frozen=False)
        set_cgroup(self.sys, lutris, frozen=True)
        s = self.scan()
        self.assertEqual((s.running, s.resting), (GTA, "wineserver"))
        self.assertEqual([(r.key, r.label) for r in s.rests],
                         [("proc:1900000:1000", "wineserver")])

    def test_wine_and_windows_programs_rest_only_by_themselves(self):
        # only the game's .exe stopped: wineserver beside it still runs, so the game runs --
        # and a game with anything running is no resting game
        add_gta(self.proc, self.sys, frozen=False)
        add_proc(self.proc, GTA_EXE, "GTA5_Enhanced.e", ["Z:\\Games\\GTA5_Enhanced.exe"],
                 ppid=GTA_REAPER, state="T", cgroup=GTA_SCOPE)
        s = self.scan()
        self.assertEqual((s.running, s.resting, s.rests), (GTA, None, []))
        # a Lutris game (no AppId): each Wine process is judged by itself
        lutris = APP_SLICE + "/app-lutris-game.scope"
        native = os.path.join(self.tmp, "lutris-proc")
        add_proc(native, 90, "wineserver", ["/usr/bin/wineserver"], cgroup=lutris)
        add_proc(native, 91, "Game.exe", ["C:\\games\\Game.exe"], ppid=1, state="T",
                 cgroup=lutris)
        s = vworker.scan_games(vworker.list_procs(native), (), native, self.sys)
        self.assertEqual((s.running, s.resting), ("wineserver", "Game.exe"))
        add_proc(native, 90, "wineserver", ["/usr/bin/wineserver"], state="T", cgroup=lutris)
        s = vworker.scan_games(vworker.list_procs(native), (), native, self.sys)
        self.assertEqual((s.running, s.resting), (None, "wineserver"))
        self.assertEqual([r.key for r in s.rests], ["proc:90:1000", "proc:91:1000"])

    def test_our_own_processes_are_left_out(self):
        add_gta(self.proc, self.sys, frozen=True)
        s = self.scan(exclude={GTA_WRAPPER, GTA_REAPER})
        self.assertEqual((s.running, s.resting), (None, "wineserver"))
        self.assertEqual([r.key for r in s.rests], [GTA_KEY])
        add_gta(self.proc, self.sys, frozen=False)
        self.assertEqual(self.scan(exclude={GTA_WRAPPER, GTA_REAPER}).running, "wineserver")

    def test_cgroups_are_read_once_and_only_for_candidates(self):
        add_gta(self.proc, self.sys, frozen=True)
        konsole = APP_SLICE + "/app-org.kde.konsole.scope"
        add_proc(self.proc, 1900001, "bash", ["/bin/bash"], cgroup=konsole)
        set_cgroup(self.sys, konsole, frozen=True)          # frozen too, but no game: not read
        ps = vworker.list_procs(self.proc)
        seen, real = [], vworker._read

        def spy(path):
            seen.append(path)
            return real(path)
        with mock.patch.object(vworker, "_read", spy):
            s = vworker.scan_games(ps, (), self.proc, self.sys)
        self.assertEqual((s.running, s.resting), (None, GTA))
        cgroups = sorted(int(p.split(os.sep)[-2]) for p in seen if p.endswith(os.sep + "cgroup"))
        self.assertEqual(cgroups, [GTA_WRAPPER, GTA_REAPER, GTA_WINESERVER, GTA_EXE])  # its AppId's
        files = [p[len(self.sys) + len("/fs/cgroup"):] for p in seen if "/fs/cgroup/" in p]
        self.assertEqual(files, [GTA_SCOPE + "/cgroup.events"])  # frozen: nothing more to read

    def test_cgroup_path_and_events(self):
        f = os.path.join(self.proc, "77", "cgroup")
        for text, want in (("0::/user.slice/app.scope\n", "/user.slice/app.scope"),
                           ("0::/\n", "/"), ("0::/a/../../etc\n", None), ("0::\n", None),
                           ("", None), ("12:freezer:/a\n0::/a\n", None),
                           ("1:name=systemd:/a\n", None)):
            write(f, text)
            self.assertEqual(vworker.cgroup_path(self.proc, 77), want, text)
        os.unlink(f)
        self.assertIsNone(vworker.cgroup_path(self.proc, 77))
        self.assertEqual(vworker.cgroup_events_file("/sys", "/a/b.scope"),
                         "/sys/fs/cgroup/a/b.scope/cgroup.events")
        self.assertEqual(vworker.cgroup_file("/sys", "/a/b.scope", "cgroup.freeze"),
                         "/sys/fs/cgroup/a/b.scope/cgroup.freeze")
        ev = set_cgroup(self.sys, "/a.scope", frozen=True)
        self.assertIs(vworker.cgroup_frozen(ev), True)
        set_cgroup(self.sys, "/a.scope", frozen=False)
        self.assertIs(vworker.cgroup_frozen(ev), False)
        for text in ("populated 1\n", "frozen yes\n", "", "frozen\n"):
            write(ev, text)
            self.assertIsNone(vworker.cgroup_frozen(ev), text)
        self.assertIsNone(vworker.cgroup_frozen(ev + ".missing"))

    def test_scope_appid(self):
        self.assertEqual(vworker.scope_appid(GTA_SCOPE), 3240220)
        self.assertEqual(vworker.scope_appid(GTA_SCOPE + "/"), 3240220)
        for path in (STEAM_CGROUP, GTA_SCOPE + "/sub", APP_SLICE + "/app-steam-app-12.scope",
                     APP_SLICE + "/app-steam-app12x-5.scope", APP_SLICE + "/app-steam-app12.scope",
                     APP_SLICE + "/xapp-steam-app12-5.scope", "/", "", None):
            self.assertIsNone(vworker.scope_appid(path), path)

    def test_a_label_is_one_readable_line(self):
        p = P(1, "Bad.exe", ["Z:\\games\\Bad\nName\t\x1b[31m\x85.exe"])
        self.assertEqual(vworker.detect_game(procs(p)), "Bad Name [31m .exe")
        long = "A" * 200 + ".exe"
        label = vworker.detect_game(procs(P(3, "AAAA.exe", ["Z:\\" + long])))   # pid 2: kthreadd
        self.assertEqual((len(label), label[-3:]), (vworker.LABEL_MAX, "..."))


class FastWatchRests(Tmp):
    """FastWatch's look at the resting games: a stop within a quarter second of one waking."""

    def rests(self, **kw):
        add_gta(self.proc, self.sys, **kw)
        s = vworker.scan_games(vworker.list_procs(self.proc), (), self.proc, self.sys)
        self.assertIsNone(s.running)
        return s.rests

    def looker(self, rests):
        """A FastWatch for one look at a time: check() only, no thread."""
        w = vworker.FastWatch(self.cfg(), threading.Event(), None, None)
        w.watch(rests)
        return w

    def watcher(self, rests, interval=vworker.THAW_INTERVAL):
        got = {"stops": [], "failed": []}
        stop = threading.Event()
        w = vworker.FastWatch(self.cfg(), stop, lambda why, code: got["stops"].append((why, code)),
                              got["failed"].append, interval=interval)
        self.addCleanup(w.close)
        w.watch(rests)
        return w, stop, got

    def test_a_thaw_is_a_wake_up(self):
        w = self.looker(self.rests(frozen=True))
        self.assertIsNone(w.check())
        set_cgroup(self.sys, GTA_SCOPE, frozen=False)
        self.assertEqual(w.check(), GTA)

    def test_a_continued_sigstop_is_a_wake_up(self):
        w = self.looker(self.rests(frozen=False, state="T"))
        self.assertIsNone(w.check())
        add_gta(self.proc, self.sys, frozen=False)               # SIGCONT: "S" again
        self.assertEqual(w.check(), GTA)

    def test_stopped_and_frozen_wakes_only_when_both_are_gone(self):
        w = self.looker(self.rests(frozen=True, state="T"))
        add_gta(self.proc, self.sys, frozen=False, state="T")     # thawed, still stopped
        self.assertIsNone(w.check())
        add_gta(self.proc, self.sys, frozen=True)                 # continued, still frozen
        self.assertIsNone(w.check())
        add_gta(self.proc, self.sys, frozen=False)
        self.assertEqual(w.check(), GTA)

    def test_a_frozen_game_being_killed_or_quit_is_no_wake_up(self):
        rests = self.rests(frozen=True)
        # killed while frozen: its tasks leave the freezer to die ("frozen 0"), but the scope
        # is still asked to freeze -- whatever their state reads meanwhile
        add_gta(self.proc, self.sys, frozen=False, freeze=True)
        self.assertIsNone(self.looker(rests).check())
        for left in (dict(state="Z"), dict(state="X"), dict(flags=vworker.PF_EXITING)):
            add_gta(self.proc, self.sys, frozen=False, **left)    # thawed as it dies
            self.assertIsNone(self.looker(rests).check(), left)
        add_gta(self.proc, self.sys, frozen=False)
        set_cgroup(self.sys, GTA_SCOPE, frozen=False, populated=0)   # emptied: an exit
        self.assertIsNone(self.looker(rests).check())
        for pid in (GTA_REAPER, GTA_WINESERVER, GTA_EXE):
            remove_proc(self.proc, pid)
        shutil.rmtree(os.path.join(self.sys, "fs", "cgroup") + GTA_SCOPE)   # removed: an exit
        self.assertIsNone(self.looker(rests).check())

    def test_a_process_at_rest_by_itself_exits_or_wakes(self):
        lutris = APP_SLICE + "/app-lutris-game.scope"
        add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"], state="T",
                 cgroup=lutris)
        set_cgroup(self.sys, lutris, frozen=False, procs=[1900000])
        [r] = vworker.scan_games(vworker.list_procs(self.proc), (), self.proc, self.sys).rests
        self.assertEqual((r.key, r.scope), ("proc:1900000:1000", None))
        for left in (dict(state="Z"), dict(state="X"), dict(flags=vworker.PF_EXITING),
                     dict(start=2000)):                           # 2000: the pid was reused
            add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"], cgroup=lutris,
                     **left)
            self.assertIsNone(self.looker([r]).check(), left)
        remove_proc(self.proc, 1900000)
        self.assertIsNone(self.looker([r]).check())               # gone
        add_proc(self.proc, 1900000, "wineserver", ["/usr/bin/wineserver"], cgroup=lutris)
        self.assertEqual(self.looker([r]).check(), "wineserver")  # continued

    def test_a_live_scope_that_cannot_be_read_counts(self):
        w = self.looker(self.rests(frozen=True))
        for name in ("cgroup.events", "cgroup.freeze"):
            os.unlink(cgroup_file(self.sys, GTA_SCOPE, name))
        self.assertEqual(w.check(), GTA)                          # its live members: running
        os.unlink(cgroup_file(self.sys, GTA_SCOPE, "cgroup.procs"))
        self.assertEqual(w.check(), GTA)                          # there, but can't be read

    def test_a_look_is_light(self):
        def reads(w):
            seen, real = [], vworker._read
            with mock.patch.object(vworker, "_read", lambda p: seen.append(p) or real(p)):
                self.assertIsNone(w.check())
            return seen
        files = [cgroup_file(self.sys, GTA_SCOPE, n)
                 for n in ("cgroup.events", "cgroup.freeze", "cgroup.procs")]
        # frozen: its scope's cgroup.events once, and nothing else
        self.assertEqual(reads(self.looker(self.rests(frozen=True))), files[:1])
        # stopped: cgroup.events and cgroup.freeze, who is in it, and each one's state
        self.assertEqual(reads(self.looker(self.rests(frozen=False, state="T"))),
                         files + [os.path.join(self.proc, str(pid), "stat")
                                  for pid in (GTA_REAPER, GTA_WINESERVER, GTA_EXE)])

    def test_the_thread_yields_within_a_quarter_second_and_ends(self):
        self.assertEqual(vworker.THAW_INTERVAL, 0.25)
        w, _stop, got = self.watcher(self.rests(frozen=True))
        w.start(vworker.list_procs(self.proc))
        time.sleep(0.6)                                            # two looks: still resting
        self.assertEqual(got, {"stops": [], "failed": []})
        set_cgroup(self.sys, GTA_SCOPE, frozen=False)
        t0 = time.monotonic()
        self.assertTrue(until(lambda: got["stops"], 2.0))
        elapsed = time.monotonic() - t0
        note_latency("thaw, FastWatch alone", elapsed)
        self.assertLess(elapsed, 0.45)
        w.thread.join(2.0)
        self.assertFalse(w.thread.is_alive())
        self.assertEqual(got, {"stops": [("a game woke up: " + GTA, "game")], "failed": []})

    def test_no_thread_outlives_its_job(self):
        def alive():
            return [t for t in threading.enumerate() if t.name == "fast-watch"]
        w, stop, got = self.watcher([])
        self.assertIsNone(w.thread)                               # not started: no thread
        w.start({})
        self.assertTrue(w.thread.is_alive())
        stop.set()                                                # the job stopped
        w.thread.join(2.0)
        self.assertFalse(w.thread.is_alive())
        w.close()
        w2, _stop2, _got2 = self.watcher(self.rests(frozen=True))
        w2.start({})
        w2.close()                                                # the job finished
        self.assertFalse(w2.thread.is_alive())
        w3, _stop3, _got3 = self.watcher([])
        w3.close()
        w3.start({})                                              # a late start: none
        self.assertIsNone(w3.thread)
        self.assertEqual(alive(), [])
        self.assertEqual(got, {"stops": [], "failed": []})

    def test_a_thread_that_cannot_start_is_not_kept(self):
        w, _stop, _got = self.watcher([])
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                w.start({})
        self.assertIsNone(w.thread)
        w.close()                                                 # nothing to join: no error

    def test_a_failure_fails_closed(self):
        rests = self.rests(frozen=False, state="T")              # stopped: its stat is read
        w, _stop, got = self.watcher(rests, interval=0.02)
        with mock.patch.object(vworker, "read_stat", side_effect=RuntimeError("boom")):
            w.start({})
            self.assertTrue(until(lambda: got["failed"], 2.0))
        self.assertEqual([str(e) for e in got["failed"]], ["boom"])
        w.thread.join(2.0)
        self.assertFalse(w.thread.is_alive())


class FastWatchNewGames(Tmp):
    """FastWatch's look at new processes (B5): /proc is listed, only a new pid is read, and one
    that could be a game sends the job's whole game check."""

    def setUp(self):
        super().setUp()
        add_proc(self.proc, 1828000, "steam", [STEAM_DIR + "/ubuntu12_32/steam", "-gamepadui"])
        add_proc(self.proc, 2000, "kwin_wayland", ["/usr/bin/kwin_wayland"])
        self.scans = []

    def looker(self):
        def scan(procs):
            self.scans.append(sorted(procs))
            return vworker.scan_games(procs, (), self.proc, self.sys)
        w = vworker.FastWatch(self.cfg(), threading.Event(), None, None, scan)
        w.cache = dict(vworker.list_procs(self.proc))            # what start() hands it
        return w

    def reads(self, w):
        seen, real, real_bytes = [], vworker._read, vworker._read_bytes
        with mock.patch.object(vworker, "_read", lambda p: seen.append(p) or real(p)), \
                mock.patch.object(vworker, "_read_bytes",
                                  lambda p: seen.append(p) or real_bytes(p)):
            got = w.check_new()
        return got, sorted({int(p.split(os.sep)[-2]) for p in seen
                            if p.startswith(self.proc + os.sep)})

    def test_only_new_pids_are_read(self):
        w = self.looker()
        self.assertEqual(self.reads(w), (None, []))               # nothing new: nothing read
        add_proc(self.proc, 3000, "firefox", ["/usr/lib64/firefox/firefox"])
        self.assertEqual(self.reads(w), (None, [3000]))           # no game: no whole check
        self.assertEqual(self.reads(w), (None, [3000]))           # once more, then never again
        self.assertEqual(self.reads(w), (None, []))
        self.assertEqual(self.scans, [])
        add_proc(self.proc, 3001, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                             "proton", "waitforexitandrun", "Hades.exe"])
        got, read = self.reads(w)
        self.assertEqual(got, "Hades.exe")
        self.assertIn(3001, read)
        self.assertEqual(len(self.scans), 1)                      # one whole check, over all
        self.assertEqual(self.scans[0], [2000, 3000, 3001, 1828000])

    def test_a_pid_read_before_its_exec_is_read_again(self):
        w = self.looker()
        add_proc(self.proc, 3002, "steam", [STEAM_DIR + "/ubuntu12_32/steam", "-gamepadui"],
                 ppid=1828000)                                    # forked, not exec'd yet
        self.assertIsNone(w.check_new())
        add_proc(self.proc, 3002, "reaper", ["reaper", "SteamLaunch", "AppId=570", "--",
                                             "dota.sh"], ppid=1828000)
        self.assertEqual(w.check_new(), "Steam app 570")

    def test_gone_pids_are_dropped(self):
        w = self.looker()
        add_proc(self.proc, 3003, "bash", ["/bin/bash"])
        w.check_new()
        self.assertIn(3003, w.cache)
        remove_proc(self.proc, 3003)
        w.check_new()
        self.assertNotIn(3003, w.cache)
        self.assertEqual(w.cache.keys(), {2000, 1828000})

    def test_a_new_process_of_a_resting_game_is_no_new_game(self):
        add_gta(self.proc, self.sys, frozen=True)
        w = self.looker()
        add_proc(self.proc, 1831300, "wine64-preload", [STEAM_DIR + "/files/bin/wine64-preloader",
                                                        "Z:\\GTA5_Enhanced.exe"],
                 ppid=GTA_REAPER, cgroup=GTA_SCOPE)               # in its frozen scope
        self.assertIsNone(w.check_new())
        self.assertEqual(len(self.scans), 1)                      # judged by every rule
        add_proc(self.proc, 3004, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                             "proton", "waitforexitandrun", "Hades.exe"])
        self.assertEqual(w.check_new(), "Hades.exe")              # a new launch beside it

    def test_kernel_threads_dead_and_unreadable_pids_are_no_candidates(self):
        w = self.looker()
        add_proc(self.proc, 3005, "wineserver", ["wineserver"], ppid=2)        # a kthread
        add_proc(self.proc, 3006, "wineserver", ["wineserver"], state="Z")
        os.makedirs(os.path.join(self.proc, "3007"))                           # no stat yet
        self.assertIsNone(w.check_new())
        self.assertEqual(self.scans, [])
        self.assertNotIn(3007, w.cache)                           # read again as new next time


class RestAges(Tmp):
    """B3: a game rests only after a minute at rest in a row (RestSeen), so a quick pause in
    its menu is still a running game."""

    def store(self, now, path=None, boot=""):
        return vworker.RestSeen(path or self.rest_seen, clock=lambda: now[0], boot=boot)

    def test_first_seen_times_age_and_are_forgotten_once_awake(self):
        now = [100.0]
        s = self.store(now)
        self.assertEqual(s.update(["a"]), {"a": 0.0})
        now[0] = 130.0
        self.assertEqual(s.update(["a", "b"]), {"a": 30.0, "b": 0.0})
        now[0] = 160.0
        self.assertEqual(s.update(["b"]), {"b": 30.0})            # "a" woke: forgotten
        self.assertEqual(s.update(["a", "b"]), {"a": 0.0, "b": 30.0})   # at rest again: anew
        self.assertEqual(self.store(now).update(["a", "b"]), {"a": 0.0, "b": 30.0})  # kept

    def test_the_file(self):
        now = [100.0]
        self.assertEqual(self.store(now).update([]), {})
        self.assertFalse(os.path.exists(self.rest_seen))          # nothing rests: nothing written
        self.store(now).update([GTA_KEY])
        with open(self.rest_seen) as f:
            self.assertEqual(json.load(f), {"boot": "", "seen": {GTA_KEY: 100.0}})
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])  # atomic
        now[0] = 50.0                                             # the clock went back
        self.assertEqual(self.store(now).update([GTA_KEY]), {GTA_KEY: 0.0})
        now[0] = 80.0
        self.assertEqual(self.store(now, boot="other-boot").update([GTA_KEY]), {GTA_KEY: 0.0})
        for junk in ("{", "[]", '{"boot": "", "seen": [1]}', '{"boot": "", "seen": {"a": true}}'):
            write(self.rest_seen, junk)
            self.assertEqual(self.store(now).update(["a"]), {"a": 0.0}, junk)
        now[0] = 90.0
        self.assertEqual(self.store(now).update(["a"]), {"a": 10.0})
        self.store(now).update([])                                # every one awake: pruned
        with open(self.rest_seen) as f:
            self.assertEqual(json.load(f), {"boot": "", "seen": {}})
        # a scratch dir that can't be written: every rest reads as just begun
        if os.geteuid() == 0:
            return                                                # root writes anywhere
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro, mode=0o500)
        try:
            path = os.path.join(ro, "rest-seen.json")
            self.assertEqual(self.store(now, path).update(["a"]), {"a": 0.0})
            now[0] = 200.0
            self.assertEqual(self.store(now, path).update(["a"]), {"a": 0.0})
        finally:
            os.chmod(ro, 0o700)

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_status_counts_a_rest_only_after_a_minute(self):
        self.assertEqual(vworker.REST_MIN_AGE_S, 60.0)
        self.assertEqual(vworker.Config({"VWORKER_REST_MIN_AGE": "5"}).rest_min_age, 5.0)
        write_meminfo(self.proc, 8.0)
        add_gta(self.proc, self.sys, frozen=True)
        now = [1000.0]

        def status():
            st = self.status(rest_clock=lambda: now[0])
            return st["ok"], st["code"], st["game"], st["resting_game"]
        game = (False, "game", GTA, None)
        self.assertEqual(status(), game)                          # frozen just now: a pause
        self.assertEqual(self.status(rest_clock=lambda: now[0])["why"],
                         "game running: " + GTA)
        now[0] = 1059.9
        self.assertEqual(status(), game)
        now[0] = 1060.0
        self.assertEqual(status(), (True, "ready", None, GTA))    # a minute at rest
        # its menu opened for a moment: it woke, so the minute starts over
        add_gta(self.proc, self.sys, frozen=False)
        now[0] = 1070.0
        self.assertEqual(status(), game)
        add_gta(self.proc, self.sys, frozen=True)
        now[0] = 1080.0
        self.assertEqual(status(), game)
        now[0] = 1139.0
        self.assertEqual(status(), game)
        now[0] = 1140.0
        self.assertEqual(status(), (True, "ready", None, GTA))


class WakeMarker(Tmp):
    """B4: a rest manager's file says a game is waking up ("waking", fresh); it is only read."""

    def marker(self, data=None, raw=None, mtime=None):
        write(self.rest_file, raw if raw is not None else json.dumps(data))
        if mtime is not None:
            os.utime(self.rest_file, (mtime, mtime))
        return self.rest_file

    def test_the_marker(self):
        now = time.time()
        wall = lambda: now                                                    # noqa: E731
        m = vworker.rest_marker
        self.assertIsNone(m(self.rest_file, wall))                           # no rest manager
        self.assertIs(m(self.marker({"waking": local_time(now - 5)}), wall), True)
        self.assertIs(m(self.marker({"waking": local_time(now - 119)}), wall), True)
        self.assertIs(m(self.marker({"waking": local_time(now - 125)}, mtime=now), wall),
                      False)                                                 # stale
        self.assertIs(m(self.marker({"state": "resting"}, mtime=now), wall), False)   # no wake
        self.assertIs(m(self.marker({"waking": None}, mtime=now), wall), False)
        # a "waking" that can't be read as a local time: the file's own time
        self.assertIs(m(self.marker({"waking": "soon"}, mtime=now - 10), wall), True)
        self.assertIs(m(self.marker({"waking": 12}, mtime=now - 200), wall), False)
        self.assertIs(m(self.marker({"waking": local_time(now + 3600)}, mtime=now - 1), wall),
                      True)                                                  # far ahead
        self.assertIs(m(self.marker({"waking": local_time(now + 3600)}, mtime=now - 500), wall),
                      False)
        # half written, not JSON, not an object: nothing this time
        for raw in ('{"waking": "20', "", "[1, 2]", "null", "\x00\x01"):
            self.assertIs(m(self.marker(raw=raw, mtime=now), wall), False, raw)

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_status_refuses_while_a_game_wakes_and_never_writes_the_file(self):
        write_rest_file(self.rest_file, state="resting", waking=local_time(time.time()))
        before = (os.stat(self.rest_file).st_mtime_ns, open(self.rest_file).read())
        st = self.status()
        self.assertEqual((st["ok"], st["code"], st["why"], st["game"]),
                         (False, "game", "a game is waking up", None))
        self.assertFalse(vworker.heavy_only(dict(st, heavy="x 2.0 cores"), self.cfg()))
        self.assertEqual((os.stat(self.rest_file).st_mtime_ns, open(self.rest_file).read()),
                         before)
        write_rest_file(self.rest_file, state="resting", waking=local_time(time.time() - 600))
        self.assertEqual(self.status()["code"], "ready")   # stale: ignored
        write_rest_file(self.rest_file, state="idle")
        self.assertEqual(self.status()["code"], "ready")

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_where_a_rest_manager_runs_the_sleep_probe_is_not_even_asked(self):
        with mock.patch.object(vworker, "_start_sleep_probe",
                               return_value=lambda: True) as probe:
            self.assertIs(self.status()["sleep_block"], True)
            self.assertEqual(probe.call_count, 1)
            write_rest_file(self.rest_file, state="idle")
            st = self.status()
            self.assertEqual((st["ok"], st["sleep_block"]), (True, None))
            self.assertEqual(probe.call_count, 1)

    def test_its_path(self):
        p = vworker.rest_file_path
        self.assertEqual(p({"VWORKER_REST_FILE": "/x/r.json", "XDG_RUNTIME_DIR": "/run/u"}),
                         "/x/r.json")
        self.assertEqual(p({"XDG_RUNTIME_DIR": "/run/user/1000"}),
                         "/run/user/1000/ally-rest.json")
        self.assertEqual(p({}), "/run/user/%d/ally-rest.json" % os.getuid())
        self.assertEqual(vworker.Config({"XDG_RUNTIME_DIR": "/r"}).rest_file, "/r/ally-rest.json")


class WatcherThreads(Tmp):
    """A2: a watcher thread that can't start never keeps finish() from deleting the job's
    files and printing its VWORKER line, and no watcher thread outlives finish()."""

    def job(self, name):
        job = vworker.EncodeJob(self.cfg(), name)
        os.makedirs(job.jobdir)
        write(os.path.join(job.jobdir, "rpu.bin"), "rpu")
        job.owns_dir = True
        return job

    def finish(self, job, *args):
        """job.finish(*args) with fd 2 caught -> (exit code, what it wrote there)."""
        r, w = os.pipe()
        saved = os.dup(2)
        os.dup2(w, 2)
        os.close(w)
        try:
            rc = job.finish(*args)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        out = os.read(r, 65536)
        os.close(r)
        return rc, out.decode()

    def test_a_thread_that_cannot_start(self):
        job = self.job("seg_nothread")
        real = threading.Thread.start

        def start(t):
            if t.name in ("fast-watch", "watchdog"):
                raise RuntimeError("can't start new thread")
            return real(t)
        with mock.patch.object(threading.Thread, "start", start):
            with self.assertRaises(RuntimeError):
                job._start_thread("watchdog", job._watchdog)
            self.assertNotIn("watchdog", job.threads)
            job._watchdog()                       # its first look done, the fast watch fails
        self.assertIsNone(job.fast.thread)
        self.assertEqual((job.result, job.why),
                         ("error", "fast watch failed to start: can't start new thread"))
        with mock.patch.object(job.fast, "close", side_effect=RuntimeError("boom")):
            rc, err = self.finish(job, job.result, job.why, 0)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(job.jobdir))
        self.assertEqual(json.loads(err.splitlines()[-1][len("VWORKER "):]),
                         {"result": "error", "frames": 0,
                          "why": "fast watch failed to start: can't start new thread"})

    def test_no_watcher_thread_outlives_finish(self):
        job = self.job("seg_threads")
        job.fast.start(vworker.list_procs(self.proc))
        job._start_thread("watchdog", job.stop.wait, 30)
        self.assertTrue(job.fast.thread.is_alive() and job.threads["watchdog"].is_alive())
        rc, err = self.finish(job, "ok", "", 0)
        self.assertEqual(rc, 0)
        self.assertFalse(job.fast.thread.is_alive())
        self.assertFalse(job.threads["watchdog"].is_alive())
        self.assertEqual([t.name for t in threading.enumerate()
                          if t.name in ("fast-watch", "watchdog")], [])
        self.assertFalse(os.path.exists(job.jobdir))
        self.assertTrue(err.endswith('VWORKER {"result":"ok","why":"","frames":0}\n'), err)


class OwnTree(unittest.TestCase):
    def test_us_children_and_session_ancestors_only(self):
        ps = procs(P(1, "systemd", ["/sbin/init"], ppid=0),
                   P(100, "sshd", ["sshd: /usr/sbin/sshd -D"]),
                   P(200, "sshd-session", ["sshd-session: user [priv]"], ppid=100),
                   P(201, "sshd-session", ["sshd-session: user@notty"], ppid=200),
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


# `nvidia-smi pmon -c 1 -s u` on a helper with nothing but its desktop and Steam open: the
# compositor, Xwayland and Steam's web helper keep the GPU 33-38 % busy (as measured on a test
# helper; kwin_wayland 12-15, Xwayland 20-26, steamwebhelper 0-9), and none of it is heavy
PMON_IDLE = (
    "# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command \n"
    "# Idx           #    C/G      %      %      %      %      %      %    name \n"
    "    0       1520     G     14      3      -      -      -      -    kwin_wayland   \n"
    "    0       1588     G     24      5      -      -      -      -    Xwayland       \n"
    "    0       2310     G      0      0      -      -      -      -    steamwebhelper \n"
    "    0       2311     G      -      -      -      -      -      -    xwaylandvideobr\n")
PMON_GAME = PMON_IDLE + (
    "    0       3001     C+G   45     20      -      -      -      -    game.x86_64    \n"
    "    1          -     -      -      -      -      -      -      -    -              \n")
DESKTOP = procs(P(1520, "kwin_wayland", ["/usr/bin/kwin_wayland", "--xwayland"]),
                P(1588, "Xwayland", ["/usr/bin/Xwayland", ":0"], ppid=1520),
                P(2310, "steamwebhelper", ["./steamwebhelper", "--type=gpu-process"]),
                P(3001, "game.x86_64", ["./game.x86_64"]))


class GpuPerProcess(unittest.TestCase):
    """The GPU share leaves out the desktop's own drawing and the Steam client."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vworker-gpu-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sys = os.path.join(self.tmp, "sys")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)

    def nvidia(self):
        os.makedirs(os.path.join(self.sys, "module/nvidia"))

    def test_an_idle_desktop_on_nvidia_is_not_heavy(self):
        self.nvidia()
        r = FakeRunner(smi="38\n", pmon=PMON_IDLE)
        self.assertEqual(vworker.gpu_busy(self.sys, r), (38.0, "NVIDIA GPU 0 38% busy"))  # whole
        s = vworker.GpuSampler(self.sys, self.proc, r)
        self.assertEqual(s.sample(DESKTOP, set(), 1.0), (0.0, ""))
        self.assertNotIn(["nvidia-smi", "--query-gpu=utilization.gpu",
                          "--format=csv,noheader,nounits"], r.calls[1:])

    def test_a_game_on_nvidia_counts(self):
        self.nvidia()
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner(smi="83\n", pmon=PMON_GAME))
        self.assertEqual(s.sample(DESKTOP, set(), 1.0), (45.0, "NVIDIA GPU 0 45% busy"))
        self.assertEqual(s.sample(DESKTOP, {3001}, 2.0), (0.0, ""))       # our own: never
        # a process we cannot see in /proc is judged by pmon's own name for it
        self.assertEqual(s.sample({}, set(), 3.0), (45.0, "NVIDIA GPU 0 45% busy"))
        old = ("# gpu        pid  type    sm   mem   enc   dec   command\n"
               "# Idx          #   C/G     %     %     %     %   name\n"
               "    0       1564     G    20     5     -     -   Xorg\n"
               "    0       4100     C    61     9     -     -   blender\n")
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner(pmon=old))
        self.assertEqual(s.sample({}, set(), 1.0), (61.0, "NVIDIA GPU 0 61% busy"))

    def test_pmon_parsing(self):
        got = vworker.parse_pmon(PMON_GAME)
        self.assertEqual(sorted(got), [0, 1])
        self.assertEqual(got[1], {})                                     # nothing running on it
        self.assertEqual(got[0][1588], (24.0, "Xwayland"))
        self.assertEqual(got[0][2311], (0.0, "xwaylandvideobr"))         # "-" is 0
        self.assertIsNone(vworker.parse_pmon(""))
        self.assertIsNone(vworker.parse_pmon("Failed to initialize NVML: Driver Not Loaded\n"))

    def test_without_pmon_the_whole_device_stands_in(self):
        self.nvidia()
        for pmon in (None, (6, "Not supported\n"), (0, "garbage\n")):
            s = vworker.GpuSampler(self.sys, self.proc, FakeRunner(smi="38\n", pmon=pmon))
            self.assertEqual(s.sample(DESKTOP, set(), 1.0), (38.0, "NVIDIA GPU 0 38% busy"), pmon)

    def test_pmon_runs_in_the_background_between_samples(self):
        self.nvidia()
        r = FakeRunner(smi="38\n", pmon=PMON_GAME)
        s = vworker.GpuSampler(self.sys, self.proc, r)
        s.begin(DESKTOP, 0.0)                   # status: started before its 1 s CPU sample
        self.assertEqual(s.sample(DESKTOP, set(), 1.0, prefetch=True)[0], 45.0)
        self.assertEqual(s.sample(DESKTOP, set(), 2.0)[0], 45.0)
        self.assertEqual([c[:2] for c in r.calls], [["nvidia-smi", "pmon"]] * 2)
        self.assertIsNone(s.pmon)

    def drm_desktop(self, card_busy=40):
        """amdgpu card1 at 0000:03:00.0: kwin, Xwayland, Steam's web helper and a game, each
        with its own DRM client (the game's open in two fds)."""
        add_card(self.sys, "card1", "0000:03:00.0", busy=card_busy)
        os.makedirs(os.path.join(self.sys, "class/drm/card1-eDP-1"))
        self.set_drm(0, 0, 0, 0)

    def set_drm(self, kwin, xwayland, steam, game):
        add_drm_fd(self.proc, 1520, 12, 1, {"gfx": kwin, "compute": 0})
        add_drm_fd(self.proc, 1588, 7, 2, {"gfx": xwayland})
        add_drm_fd(self.proc, 2310, 30, 3, {"gfx": steam})
        for fd in (5, 6):                       # one client open twice counts once
            add_drm_fd(self.proc, 3001, fd, 4, {"gfx": game, "dec": 0})

    def test_an_idle_desktop_on_amd_is_not_heavy(self):
        self.drm_desktop(card_busy=40)
        r = FakeRunner()
        self.assertEqual(vworker.gpu_busy(self.sys, r), (40.0, "GPU card1 40% busy"))   # whole
        s = vworker.GpuSampler(self.sys, self.proc, r)
        s.begin(DESKTOP, 10.0)
        self.set_drm(int(0.15e9), int(0.25e9), int(0.05e9), 0)
        self.assertEqual(s.sample(DESKTOP, set(), 11.0), (0.0, ""))
        self.set_drm(int(0.30e9), int(0.50e9), int(0.10e9), int(0.45e9))   # the game: 0.45 s
        self.assertEqual(s.sample(DESKTOP, set(), 12.0), (45.0, "GPU card1 45% busy"))
        self.set_drm(int(0.45e9), int(0.75e9), int(0.15e9), int(0.90e9))
        self.assertEqual(s.sample(DESKTOP, {3001}, 13.0), (0.0, ""))       # our own: never
        self.assertEqual(r.calls, [])                                     # no nvidia: no smi

    def test_the_first_drm_sample_only_seeds(self):
        self.drm_desktop(card_busy=90)
        self.set_drm(0, 0, 0, int(5e9))
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        self.assertEqual(s.sample(DESKTOP, set(), 1.0), (0.0, ""))         # not the card's 90
        self.set_drm(0, 0, 0, int(5.6e9))
        self.assertEqual(s.sample(DESKTOP, set(), 2.0), (60.0, "GPU card1 60% busy"))

    def test_clients_opened_mid_interval_and_runaway_counters(self):
        self.drm_desktop()
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(DESKTOP, 0.0)
        add_drm_fd(self.proc, 3001, 9, 5, {"gfx": int(0.3e9)})            # opened since begin
        self.assertEqual(s.sample(DESKTOP, set(), 1.0)[0], 30.0)
        add_drm_fd(self.proc, 3001, 9, 5, {"gfx": int(50e9)})             # one client: <= 100 %
        self.assertEqual(s.sample(DESKTOP, set(), 2.0)[0], 100.0)

    def test_engine_capacity_and_intel_cards(self):
        add_card(self.sys, "card0", "0000:00:02.0")         # i915: no gpu_busy_percent
        game = procs(P(4100, "blender", ["blender"]))
        add_drm_fd(self.proc, 4100, 4, 1, {"render": 0, "video": 0}, pdev="0000:00:02.0",
                   driver="i915", capacity={"video": 2}, node="/dev/dri/card0")
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(game, 0.0)
        add_drm_fd(self.proc, 4100, 4, 1, {"render": int(0.2e9), "video": int(1.0e9)},
                   pdev="0000:00:02.0", driver="i915", capacity={"video": 2})
        self.assertEqual(s.sample(game, set(), 1.0), (50.0, "GPU card0 50% busy"))   # 2 engines

    def test_no_readable_clients_falls_back_to_the_device(self):
        add_card(self.sys, "card1", "0000:03:00.0", busy=40)
        add_drm_fd(self.proc, 1520, 3, 1, {}, node="/dev/dri/card1")       # no engine time
        os.makedirs(os.path.join(self.proc, "1588", "fdinfo"))             # no fd/ to read
        write(os.path.join(self.proc, "1588", "fdinfo", "4"), "drm-client-id:\t2\n")
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(DESKTOP, 0.0)
        self.assertEqual(s.sample(DESKTOP, set(), 1.0), (40.0, "GPU card1 40% busy"))

    def test_non_drm_fds_are_not_read(self):
        add_card(self.sys, "card1", "0000:03:00.0", busy=40)
        add_drm_fd(self.proc, 3001, 5, 4, {"gfx": 0}, node="socket:[12345]")
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(DESKTOP, 0.0)
        add_drm_fd(self.proc, 3001, 5, 4, {"gfx": int(0.9e9)}, node="socket:[12345]")
        self.assertEqual(s.sample(DESKTOP, set(), 1.0), (40.0, "GPU card1 40% busy"))

    def test_fdinfo_parsing(self):
        key, engines, cap, cycles = vworker.parse_drm_fdinfo(
            "drm-driver:\ti915\ndrm-pdev:\t0000:00:02.0\ndrm-client-id:\t7\n"
            "drm-engine-render:\t12 ns\ndrm-engine-video:\t3 ns\n"
            "drm-engine-capacity-video:\t2\ndrm-engine-bogus:\tlots\n")
        self.assertEqual((key, engines, cap, cycles), (("0000:00:02.0", "7"),
                                                       {"render": 12, "video": 3}, {"video": 2}, {}))
        self.assertIsNone(vworker.parse_drm_fdinfo("pos:\t0\nflags:\t0\n"))
        self.assertIsNone(vworker.parse_drm_fdinfo("drm-client-id:\t1\ndrm-memory-vram:\t1 KiB\n"))

    def test_xe_fdinfo_parsing(self):
        add_xe_fd(self.proc, 4100, 4, 40, {"rcs": 28257900}, 7655183225, capacity={"ccs": 4})
        with open(os.path.join(self.proc, "4100/fdinfo/4")) as f:
            key, engines, cap, cycles = vworker.parse_drm_fdinfo(f.read())
        self.assertEqual((key, engines, cap), (("0000:00:02.0", "40"), {}, {"ccs": 4}))
        self.assertEqual(sorted(cycles), sorted(XE_ENGINES))     # drm-total-vram0 is no engine
        self.assertEqual(cycles["rcs"], (28257900, 7655183225))
        # cycles without the GPU clock beside them (panthor gives drm-maxfreq instead) are left
        # out; where a driver gives both, both are kept and engine time wins in the sampler
        text = ("drm-client-id:\t3\ndrm-engine-gfx:\t100 ns\ndrm-cycles-gfx:\t5\n"
                "drm-total-cycles-gfx:\t10\ndrm-cycles-frag:\t9\ndrm-maxfreq-frag:\t800 MHz\n")
        self.assertEqual(vworker.parse_drm_fdinfo(text)[1:], ({"gfx": 100}, {}, {"gfx": (5, 10)}))
        self.assertIsNone(vworker.parse_drm_fdinfo("drm-client-id:\t3\ndrm-cycles-frag:\t9\n"))

    # Intel xe (Lunar Lake, Arc B-series): busy cycles against the GPU clock, no engine time and
    # no gpu_busy_percent. TICK is one second of a 19.2 MHz GPU timestamp
    TICK = 19200000
    T0 = 7655183225

    def xe_desktop(self, busy=None):
        add_card(self.sys, "card0", "0000:00:02.0", busy=busy)
        self.xe = procs(P(1520, "kwin_wayland", ["/usr/bin/kwin_wayland"]),
                        P(4100, "blender", ["blender"]), P(4200, "darktable", ["darktable"]))
        self.set_xe(0, {}, {}, {})

    def set_xe(self, t, kwin, blender, darktable, dark_total=None):
        """Every client's counters at GPU second `t`; busy cycles in hundredths of a TICK."""
        total = self.T0 + t * self.TICK
        cyc = lambda d: {e: self.TICK * n // 100 for e, n in d.items()}
        add_xe_fd(self.proc, 1520, 12, 1, cyc(kwin), total)
        add_xe_fd(self.proc, 4100, 4, 2, cyc(blender), total, capacity={"ccs": 4})
        add_xe_fd(self.proc, 4200, 7, 3, cyc(darktable),
                  total if dark_total is None else dark_total, capacity={"ccs": 4})

    @staticmethod
    def busy(s, *args):
        pct, label = s.sample(*args)
        return round(pct, 6), label             # shares of different clocks add up in floats

    def test_intel_xe_busy_cycles(self):
        self.xe_desktop()
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(self.xe, 0.0)
        self.set_xe(1, {"rcs": 90}, {"rcs": 30}, {"rcs": 15})
        # the clients' shares of rcs add up; the compositor's 90 % is left out
        self.assertEqual(self.busy(s, self.xe, set(), 1.0), (45.0, "GPU card0 45% busy"))
        self.set_xe(2, {"rcs": 180}, {"rcs": 30, "ccs": 200}, {"rcs": 15})
        self.assertEqual(self.busy(s, self.xe, set(), 2.0), (50.0, "GPU card0 50% busy"))  # 2 of 4
        self.set_xe(3, {}, {"rcs": 80, "ccs": 200}, {"rcs": 25})
        self.assertEqual(self.busy(s, self.xe, {4100}, 3.0), (10.0, "GPU card0 10% busy"))  # ours
        self.set_xe(4, {}, {"rcs": 580, "ccs": 200}, {"rcs": 25})
        self.assertEqual(self.busy(s, self.xe, set(), 4.0)[0], 100.0)   # one client fills it, at most

    def test_xe_shares_are_per_client_clocks(self):
        # darktable's baseline was read half a GPU second after blender's: its share is its
        # cycles over ITS clock's advance (half a second), not over blender's whole second
        self.xe_desktop()
        self.set_xe(0, {}, {}, {}, dark_total=self.T0 + self.TICK // 2)
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(self.xe, 0.0)
        self.set_xe(1, {}, {"rcs": 20}, {"rcs": 12.5})
        self.assertEqual(self.busy(s, self.xe, set(), 1.0), (45.0, "GPU card0 45% busy"))  # 20+25

    def test_xe_new_clients_and_counters_that_go_back(self):
        self.xe_desktop()
        tick = self.TICK
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(self.xe, 0.0)
        # opened since: its total-cycles is the GPU clock, not its age, so a baseline only
        add_xe_fd(self.proc, 4100, 9, 5, {"rcs": 50 * tick}, self.T0 + tick)
        self.set_xe(1, {}, {}, {"rcs": 50})
        self.assertEqual(self.busy(s, self.xe, set(), 1.0)[0], 50.0)
        add_xe_fd(self.proc, 4100, 9, 5, {"rcs": 50 * tick + tick * 3 // 10}, self.T0 + 2 * tick)
        self.set_xe(2, {}, {}, {"rcs": 20})                           # darktable's went back
        self.assertEqual(self.busy(s, self.xe, set(), 2.0)[0], 30.0)  # the new client counts now
        os.unlink(os.path.join(self.proc, "4100/fdinfo/9"))
        self.set_xe(3, {}, {}, {"rcs": 90})                           # caught up: from 50
        self.assertEqual(self.busy(s, self.xe, set(), 3.0)[0], 20.0)  # 0.4 over two seconds

    def test_xe_cards_are_covered(self):
        # a card with readable xe clients is read per client, never as the whole device
        self.xe_desktop(busy=90)
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(self.xe, 0.0)
        self.set_xe(1, {"rcs": 100}, {}, {})                          # only the compositor
        self.assertEqual(s.sample(self.xe, set(), 1.0), (0.0, ""))

    def test_nouveau_has_no_reading(self):
        # nouveau writes no per-client engine time (memory only) and no gpu_busy_percent: the
        # GPU rule cannot see it (LIMITATIONS lists it)
        add_card(self.sys, "card0", "0000:01:00.0")
        base = os.path.join(self.proc, "4100")
        os.makedirs(os.path.join(base, "fd"))
        os.symlink("/dev/dri/renderD128", os.path.join(base, "fd", "5"))
        write(os.path.join(base, "fdinfo", "5"),
              "drm-driver:\tnouveau\ndrm-pdev:\t0000:01:00.0\ndrm-client-id:\t4\n"
              "drm-total-vram:\t65536 KiB\n")
        s = vworker.GpuSampler(self.sys, self.proc, FakeRunner())
        s.begin(procs(P(4100, "blender", ["blender"])), 0.0)
        self.assertEqual(s.sample(procs(P(4100, "blender", ["blender"])), set(), 1.0), (0.0, ""))

    def test_the_desktop_names(self):
        for name in ("kwin_wayland", "kwin_x11", "kwin", "Xwayland", "Xorg", "X", "gnome-shell",
                     "mutter", "mutter-x11-frames", "plasmashell", "xwaylandvideobridge", "sway",
                     "Hyprland", "weston", "labwc", "wayfire", "gamescope", "gamescope-wl",
                     "cinnamon", "muffin", "cosmic-comp", "gala", "budgie-wm", "xfwm4", "picom",
                     "compton", "marco", "metacity", "openbox", "compiz", "enlightenment", "niri",
                     "river", "steam", "steamwebhelper"):
            self.assertIn(name, vworker.GPU_EXCLUDED_NAMES)
            self.assertTrue(vworker._gpu_excluded(9, procs(P(9, name[:15], [name])), set()), name)
            self.assertTrue(vworker._gpu_excluded(9, procs(P(9, name[:15], [])), set()), name)
            self.assertTrue(vworker._gpu_excluded(9, {}, set(), name[:15]), name)
        for name in ("game.x86_64", "blender", "firefox", "Xwaylandish", "kwinner",
                     "cinnamon-session", "mutter-x11-fr", "rivertuner"):
            self.assertFalse(vworker._gpu_excluded(9, procs(P(9, name[:15], [name])), set()), name)


class Power(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vworker-power-")
        self.addCleanup(shutil.rmtree, self.root)

    def test_a_machine_without_a_system_battery_is_on_mains(self):
        self.assertTrue(vworker.ac_online(self.root))                      # no power_supply at all
        os.makedirs(os.path.join(self.root, "class/power_supply"))
        self.assertTrue(vworker.ac_online(self.root))                      # empty: a desktop or VM
        add_supply(self.root, "hid-00:11:22:33:44:55-battery", "Battery", scope="Device",
                   status="Discharging")                                   # a wireless controller
        add_supply(self.root, "ucsi-source-psy-USBC000:001", "USB", online=0)
        self.assertTrue(vworker.ac_online(self.root))
        add_supply(self.root, "ADP0", "Mains", online=0)    # odd firmware, no battery
        self.assertTrue(vworker.ac_online(self.root))

    def test_mains_decides_when_there_is_one(self):
        add_supply(self.root, "BAT0", "Battery", status="Charging")
        add_supply(self.root, "AC0", "Mains", online=1)
        self.assertTrue(vworker.ac_online(self.root))
        add_supply(self.root, "AC0", "Mains", online=0)
        self.assertFalse(vworker.ac_online(self.root))                     # a USB-C source alone

    def test_a_battery_without_a_mains_supply_tells_by_its_status(self):
        add_supply(self.root, "BAT1", "Battery", scope="System")
        for status, ac in (("Charging", True), ("Full", True), ("Not charging", True),
                           ("Discharging", False), ("Unknown", False)):
            write(os.path.join(self.root, "class/power_supply/BAT1/status"), status + "\n")
            self.assertEqual(vworker.ac_online(self.root), ac, status)
        os.unlink(os.path.join(self.root, "class/power_supply/BAT1/status"))
        self.assertFalse(vworker.ac_online(self.root))                     # nothing known
        write(os.path.join(self.root, "class/power_supply/BAT1/status"), "Not charging\n")
        add_supply(self.root, "BAT2", "Battery", status="Discharging")     # two batteries
        self.assertFalse(vworker.ac_online(self.root))


class Ethernet(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vworker-net-")
        self.addCleanup(shutil.rmtree, self.root)

    def links(self):
        return vworker.ethernet_links(self.root)

    def test_any_name_counts(self):
        for name in ("eth0", "eno1", "enp3s0", "enx00e04c680001", "ens18"):
            add_net(self.root, name)
        self.assertEqual(self.links(), ["eno1", "enp3s0", "ens18", "enx00e04c680001", "eth0"])

    def test_not_ethernet(self):
        add_net(self.root, "lo", kind=772, state="unknown", virtual=True)
        add_net(self.root, "wlan0", wireless=True)
        add_net(self.root, "wlp6s0", carrier=None)
        os.makedirs(os.path.join(self.root, "class/net/wlp6s0/phy80211"))
        add_net(self.root, "tailscale0", kind=65534, state="unknown", virtual=True)
        add_net(self.root, "wg0", kind=65534, state="unknown", virtual=True)
        add_net(self.root, "tun0", kind=65534, state="unknown", virtual=True)
        add_net(self.root, "veth1a2b3c", virtual=True)
        add_net(self.root, "vnet0", state="unknown", virtual=True)          # a VM's tap
        add_net(self.root, "docker0", virtual=True, brif=["veth1a2b3c"], lower=["veth1a2b3c"])
        add_net(self.root, "virbr0", virtual=True, brif=["vnet0"], lower=["vnet0"])
        add_net(self.root, "br-wifi", virtual=True, brif=["wlan0"], lower=["wlan0"])
        add_net(self.root, "enp9s0", state="down", carrier="0")             # unplugged
        add_net(self.root, "enp8s0", state="dormant")                       # 802.1X not done
        add_net(self.root, "enp7s0", state="up", carrier="0")
        os.makedirs(os.path.join(self.root, "class/net/bonding_masters"))   # not an interface
        self.assertEqual(self.links(), [])
        self.assertFalse(vworker.is_wired(self.root, "../net/eth0"))
        self.assertFalse(vworker.is_wired(self.root, "nope"))

    def test_a_driver_that_does_not_track_operstate(self):
        add_net(self.root, "eth0", state="unknown", carrier="1")
        add_net(self.root, "eth1", state="unknown", carrier="0")
        self.assertEqual(self.links(), ["eth0"])

    def test_bridges_bonds_and_vlans_over_a_wired_link(self):
        add_net(self.root, "enp3s0")
        add_net(self.root, "br0", virtual=True, brif=["enp3s0", "vnet0"], lower=["enp3s0", "vnet0"])
        add_net(self.root, "vnet0", state="unknown", virtual=True)
        add_net(self.root, "enp1s0", state="down", carrier="0")
        add_net(self.root, "enp2s0")
        add_net(self.root, "bond0", virtual=True, slaves=["enp1s0", "enp2s0"])
        add_net(self.root, "enp3s0.10", virtual=True, lower=["enp3s0"])
        add_net(self.root, "vmbr0", virtual=True, brif=["bond0"])          # a bond in a bridge
        self.assertEqual(self.links(), ["bond0", "br0", "enp2s0", "enp3s0", "enp3s0.10", "vmbr0"])
        # the wired member goes down: the bridge is not Ethernet any more
        write(os.path.join(self.root, "class/net/enp3s0/carrier"), "0\n")
        write(os.path.join(self.root, "class/net/enp3s0/operstate"), "down\n")
        self.assertEqual(self.links(), ["bond0", "enp2s0", "vmbr0"])

    def test_a_bridge_must_be_up_itself(self):
        add_net(self.root, "enp3s0")
        add_net(self.root, "br0", state="down", carrier="0", virtual=True, brif=["enp3s0"])
        self.assertEqual(self.links(), ["enp3s0"])

    def test_wired_ipv4(self):
        out = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
               "3: wlan0    inet 192.0.2.30/24 brd 192.0.2.255 scope global wlan0\\  x\n"
               "5: br0    inet 192.0.2.20/24 brd 192.0.2.255 scope global br0\\  x\n"
               "5: br0    inet 192.0.2.21/24 scope global secondary br0:1\\  x\n"
               "6: eth1@if7    inet 198.51.100.4/24 scope global eth1\\  x\n")
        r = FakeRunner(ip_out=out)
        self.assertEqual(vworker.wired_ipv4(["br0", "enp3s0", "eth1"], r),
                         ["192.0.2.20", "192.0.2.21", "198.51.100.4"])
        self.assertEqual(r.calls, [["ip", "-4", "-o", "addr", "show"]])
        self.assertEqual(vworker.wired_ipv4([], r), [])
        self.assertEqual(len(r.calls), 1)                                  # no links: no ip run
        self.assertEqual(vworker.wired_ipv4(["br0"], lambda argv, t: None), [])
        self.assertEqual(vworker.wired_ipv4(["br0"], lambda argv, t: (1, out, "")), [])

        def boom(argv, t):
            raise RuntimeError("no")
        self.assertEqual(vworker.wired_ipv4(["br0"], boom), [])

    def test_wireless_links_and_counters(self):
        add_net(self.root, "enp3s0")
        add_net(self.root, "wlan0", wireless=True)
        add_net(self.root, "wlp6s0", carrier=None)
        os.makedirs(os.path.join(self.root, "class/net/wlp6s0/phy80211"))
        add_net(self.root, "wlan1", wireless=True, state="down", carrier="0")
        self.assertEqual(vworker.wireless_links(self.root), ["wlan0", "wlp6s0"])
        set_rx(self.root, "wlan0", 123456789)
        self.assertEqual(vworker.rx_bytes(self.root, "wlan0"), 123456789)
        self.assertIsNone(vworker.rx_bytes(self.root, "wlp6s0"))           # no statistics/
        proc = os.path.join(self.root, "proc")
        set_rchar(proc, 77, 323934931)
        self.assertEqual(vworker.read_rchar(proc, 77), 323934931)
        self.assertIsNone(vworker.read_rchar(proc, 78))                     # gone


class WifiStreamCheck(unittest.TestCase):
    """The stream itself arriving over the helper's Wi-Fi stops the job (ARP can send it there
    though ssh dialled a wired address); Wi-Fi traffic beside the stream never does."""
    MB = 1 << 20
    READER = 5150                              # the zstd that reads the ssh stdin

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vworker-wifi-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.sys = os.path.join(self.tmp, "sys")
        self.proc = os.path.join(self.tmp, "proc")
        add_net(self.sys, "enp4s0")
        add_net(self.sys, "wlan0", wireless=True)
        self.n = {"job": 0, "enp4s0": 0, "wlan0": 0}
        self.paths = {"job": os.path.join(self.proc, str(self.READER), "io"),
                      "enp4s0": os.path.join(self.sys, "class/net/enp4s0/statistics/rx_bytes"),
                      "wlan0": os.path.join(self.sys, "class/net/wlan0/statistics/rx_bytes")}
        self.w = vworker.WifiStream(self.sys, self.proc)
        self.assertIsNone(self.tick())                                     # the baseline

    def tick(self, job=0.0, enp4s0=0.0, wlan0=0.0):
        """One watchdog tick: the job read `job` MB of its stream and each link received its MB
        (None: that counter cannot be read this tick). The links count first, as on the wire."""
        for k, mb in (("enp4s0", enp4s0), ("wlan0", wlan0), ("job", job)):
            if mb is None:
                if os.path.exists(self.paths[k]):
                    os.unlink(self.paths[k])
                continue
            self.n[k] += int(mb * self.MB)
            if k == "job":
                set_rchar(self.proc, self.READER, self.n[k])
            else:
                set_rx(self.sys, k, self.n[k])
        return self.w.update(self.READER, ["enp4s0"])

    def test_the_stream_over_wifi_is_caught_on_the_second_tick(self):
        self.assertIsNone(self.tick(job=20, enp4s0=0.1, wlan0=21))
        self.assertEqual(self.tick(job=20, enp4s0=0.1, wlan0=21), "wlan0")

    def test_a_download_beside_an_ethernet_stream_never_counts(self):
        for _ in range(6):
            self.assertIsNone(self.tick(job=20, enp4s0=21, wlan0=60))
        for _ in range(6):                    # the cable can lag a tick: ssh's window, at most
            self.assertIsNone(self.tick(job=20, enp4s0=16, wlan0=60))
            self.assertIsNone(self.tick(job=20, enp4s0=26, wlan0=60))

    def test_it_takes_two_ticks_in_a_row(self):
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.assertIsNone(self.tick(job=20, enp4s0=21, wlan0=21))         # the cable had this one
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.assertIsNone(self.tick(job=4, wlan0=4))                      # too little to tell
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.assertEqual(self.tick(job=20, wlan0=21), "wlan0")

    def test_thresholds(self):
        for _ in range(3):
            self.assertIsNone(self.tick(job=20, wlan0=9.9))               # under half of it
        for _ in range(3):
            self.assertIsNone(self.tick(job=7.9, wlan0=8))                # under 8 MB a tick
        for _ in range(3):
            self.assertIsNone(self.tick(job=20, enp4s0=10, wlan0=21))     # the cable had half
        self.assertIsNone(self.tick(job=8, wlan0=4))
        self.assertEqual(self.tick(job=8, wlan0=4), "wlan0")              # 8 MB, half on Wi-Fi

    def test_counters_that_cannot_be_read_tell_nothing(self):
        self.assertIsNone(self.tick(job=20, enp4s0=1, wlan0=21))
        self.assertIsNone(self.tick(job=20, enp4s0=None, wlan0=21))      # cable unreadable
        self.assertIsNone(self.tick(job=20, enp4s0=1, wlan0=21))         # ... and its baseline
        self.n["enp4s0"] = 0
        self.assertIsNone(self.tick(job=20, enp4s0=0.5, wlan0=21))       # cable counter reset
        self.assertIsNone(self.tick(job=20, wlan0=None))                  # Wi-Fi unreadable
        self.assertIsNone(self.tick(job=None, wlan0=21))                  # zstd gone
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.assertEqual(self.tick(job=20, wlan0=21), "wlan0")
        self.assertIsNone(vworker.WifiStream(self.sys, self.proc).update(None, ["enp4s0"]))

    def test_a_wifi_link_that_is_down_or_was_reset_is_not_read(self):
        self.assertIsNone(self.tick(job=20, wlan0=21))
        self.n["wlan0"] = 0                                               # the link was reset
        self.assertIsNone(self.tick(job=20, wlan0=1))                     # went back: skipped
        write(os.path.join(self.sys, "class/net/wlan0/operstate"), "down\n")
        write(os.path.join(self.sys, "class/net/wlan0/carrier"), "0\n")
        for _ in range(3):
            self.assertIsNone(self.tick(job=20, wlan0=21))
        add_net(self.sys, "wlp2s0", wireless=True)                        # another Wi-Fi has it
        set_rx(self.sys, "wlp2s0", 0)
        self.assertIsNone(self.tick(job=20))
        for want in (None, "wlp2s0"):
            self.n["wlp2s0"] = self.n.get("wlp2s0", 0) + 21 * self.MB
            set_rx(self.sys, "wlp2s0", self.n["wlp2s0"])
            self.assertEqual(self.tick(job=20), want)


# ---- status ---------------------------------------------------------------------------------

class Status(Tmp):

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_ready(self):
        st = self.status()
        self.assertEqual(set(st), {"ok", "why", "code", "ac", "ethernet", "game", "resting_game",
                                   "heavy", "free_gb", "mem_available_gb", "busy", "x265",
                                   "version", "sha256", "ipv4", "sleep_block"})
        self.assertEqual(st, dict(st, ok=True, why="ready", code="ready", ac=True, ethernet=True,
                                  game=None, resting_game=None, heavy=None,
                                  mem_available_gb=None, busy=False, x265="4.1+1-1d117be",
                                  version=1))
        self.assertIsInstance(st["free_gb"], float)
        json.dumps(st)

    def test_sha256_and_wired_ipv4(self):
        st = self.status()
        with open(VWORKER, "rb") as f:
            self.assertEqual(st["sha256"], hashlib.sha256(f.read()).hexdigest())
        self.assertEqual(st["ipv4"], ["192.0.2.20"])                      # not wlan0's
        self.assertEqual(self.status(runner=FakeRunner(self.x265))["ipv4"], [])   # ip says nothing
        write(os.path.join(self.sys, "class/net/enp4s0/carrier"), "0\n")
        self.assertEqual(self.status()["ipv4"], [])                         # no wired link up
        add_net(self.sys, "br0", virtual=True, brif=["enp4s0", "enp5s0"])
        add_net(self.sys, "enp5s0")
        out = self.IP_OUT + "6: br0    inet 192.0.2.40/24 scope global br0\\  x\n"
        self.assertEqual(self.status(runner=FakeRunner(self.x265, out))["ipv4"], ["192.0.2.40"])

    def test_the_status_cli_prints_sha256_and_ipv4(self):
        env = dict(os.environ, VWORKER_X265=self.x265, VWORKER_JOBS=self.jobs,
                   VWORKER_PROC=self.proc, VWORKER_SYS=self.sys, VWORKER_STATUS_SAMPLE="0",
                   VWORKER_ZSTD=ZSTD or "/nonexistent")
        r = subprocess.run([sys.executable, VWORKER, "status"], env=env, capture_output=True,
                           timeout=10)
        st = json.loads(r.stdout)
        with open(VWORKER, "rb") as f:
            self.assertEqual(st["sha256"], hashlib.sha256(f.read()).hexdigest())
        self.assertIsInstance(st["ipv4"], list)
        self.assertIn("sleep_block", st)
        probe = os.path.join(self.tmp, "systemd-inhibit")
        write(probe, "#!/bin/sh\nexit 1\n")                    # polkit said no
        os.chmod(probe, 0o755)
        r = subprocess.run([sys.executable, VWORKER, "status"],
                           env=dict(env, VWORKER_INHIBIT=probe), capture_output=True, timeout=10)
        self.assertIs(json.loads(r.stdout)["sleep_block"], False)

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_sleep_block_is_reported_and_never_makes_it_not_ok(self):
        vworker._SLEEP_PROBE.clear()
        self.addCleanup(vworker._SLEEP_PROBE.clear)
        for rc, want in ((0, True), (1, False)):
            path = os.path.join(self.tmp, "systemd-inhibit-%d" % rc)
            write(path, "#!/bin/sh\nexit %d\n" % rc)
            os.chmod(path, 0o755)
            st = self.status(self.cfg(inhibit=path))
            self.assertEqual((st["ok"], st["code"], st["sleep_block"]), (True, "ready", want))
        st = self.status(self.cfg(inhibit=os.path.join(self.tmp, "no-systemd-inhibit")))
        self.assertEqual((st["ok"], st["sleep_block"]), (True, None))
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        st = self.status(self.cfg(inhibit=path))
        self.assertEqual((st["ok"], st["sleep_block"]), (False, False))   # not ok: still told

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_a_desktop_with_no_battery_and_eth0_is_ready(self):
        root = os.path.join(self.tmp, "desktop-sys")
        os.makedirs(os.path.join(root, "class/power_supply"))
        add_supply(root, "hidpp_battery_0", "Battery", scope="Device", status="Discharging")
        add_net(root, "eth0")
        add_net(root, "docker0", virtual=True)
        out = ("2: eth0    inet 192.0.2.7/24 brd 192.0.2.255 scope global eth0\\  x\n"
               "4: docker0    inet 172.17.0.1/16 scope global docker0\\  x\n")
        st = self.status(self.cfg(sys=root), runner=FakeRunner(self.x265, out),
                         ssh_connection="192.0.2.50 52000 192.0.2.7 22")
        self.assertEqual((st["ok"], st["why"], st["ac"], st["ethernet"], st["ipv4"]),
                         (True, "ready", True, True, ["192.0.2.7"]))
        st = self.status(self.cfg(sys=root), runner=FakeRunner(self.x265, out),
                         ssh_connection="172.17.0.2 52000 172.17.0.1 22")
        self.assertEqual(st["why"], "no Ethernet: ssh arrives on docker0")

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_ssh_over_a_bridge_on_a_wired_link_is_ethernet(self):
        add_net(self.sys, "br0", virtual=True, brif=["enp4s0"], lower=["enp4s0"])
        out = self.IP_OUT + "5: br0    inet 192.0.2.40/24 scope global br0\\  x\n"
        st = self.status(runner=FakeRunner(self.x265, out),
                         ssh_connection="192.0.2.5 50000 192.0.2.40 22")
        self.assertEqual((st["ok"], st["ethernet"]), (True, True), st)

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_an_idle_desktop_gpu_is_not_heavy(self):
        # NVIDIA: the whole GPU reads 38 %, all of it the desktop's own drawing and Steam
        os.makedirs(os.path.join(self.sys, "module/nvidia"))
        for pid, p in DESKTOP.items():
            add_proc(self.proc, pid, p.comm, p.argv, ppid=p.ppid)
        st = self.status(runner=FakeRunner(self.x265, self.IP_OUT, smi="38\n", pmon=PMON_IDLE))
        self.assertEqual((st["ok"], st["heavy"]), (True, None), st)
        st = self.status(runner=FakeRunner(self.x265, self.IP_OUT, smi="83\n", pmon=PMON_GAME))
        self.assertEqual((st["ok"], st["heavy"], st["code"]),
                         (False, "NVIDIA GPU 0 45% busy", "heavy"))

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_an_idle_desktop_gpu_is_not_heavy_on_amd(self):
        add_card(self.sys, "card1", "0000:03:00.0", busy=85)
        add_proc(self.proc, 1520, "kwin_wayland", ["/usr/bin/kwin_wayland"])
        add_proc(self.proc, 4100, "blender", ["blender"])
        add_drm_fd(self.proc, 1520, 12, 1, {"gfx": 0})
        add_drm_fd(self.proc, 4100, 5, 2, {"gfx": 0})
        st = self.status(sleep=lambda _: add_drm_fd(self.proc, 1520, 12, 1, {"gfx": int(0.85e9)}))
        self.assertEqual((st["ok"], st["heavy"]), (True, None), st)       # the card says 85 %
        st = self.status(sleep=lambda _: add_drm_fd(self.proc, 4100, 5, 2, {"gfx": int(0.5e9)}))
        self.assertEqual((st["ok"], st["heavy"]), (False, "GPU card1 50% busy"))

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_a_gpu_heavy_app_on_intel_xe_is_heavy(self):
        add_card(self.sys, "card1", "0000:00:02.0")         # xe: no gpu_busy_percent
        add_proc(self.proc, 1520, "cosmic-comp", ["/usr/bin/cosmic-comp"])
        add_proc(self.proc, 4100, "blender", ["blender"])
        t0, tick = GpuPerProcess.T0, GpuPerProcess.TICK
        add_xe_fd(self.proc, 1520, 12, 1, {}, t0)
        add_xe_fd(self.proc, 4100, 5, 2, {}, t0)
        st = self.status(sleep=lambda _: add_xe_fd(self.proc, 1520, 12, 1, {"rcs": tick},
                                                   t0 + tick))
        self.assertEqual((st["ok"], st["heavy"]), (True, None), st)       # the desktop's own
        st = self.status(sleep=lambda _: add_xe_fd(self.proc, 4100, 5, 2, {"rcs": tick // 2},
                                                   t0 + tick))
        self.assertEqual((st["ok"], st["heavy"]), (False, "GPU card1 50% busy"))

    def test_disk_floor_is_headroom(self):
        self.assertEqual(vworker.MIN_FREE_GB, 5.0)
        self.assertEqual(vworker.Config({}).min_free_gb, 5.0)
        self.assertEqual(vworker.Config({"VWORKER_MIN_FREE_GB": "12"}).min_free_gb, 12.0)
        with mock.patch.object(vworker, "free_gb", return_value=4.9):
            st = self.status(self.cfg(min_free_gb=5))
        self.assertEqual((st["code"], st["why"]), ("low_disk", "low disk: 4.9 GB free, need 5"))
        with mock.patch.object(vworker, "free_gb", return_value=5.1):
            self.assertNotEqual(self.status(self.cfg(min_free_gb=5))["code"], "low_disk")

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
        st = self.status(ssh_connection="192.0.2.5 50000 192.0.2.30 22")
        self.assertEqual((st["ok"], st["ethernet"]), (False, False))
        self.assertIn("wlan0", st["why"])
        self.assertEqual(st["code"], "no_ethernet")
        self.assertTrue(self.status(ssh_connection="192.0.2.5 50000 192.0.2.20 22")["ok"])
        self.assertFalse(self.status(ssh_connection="fe80::9 50000 fe80::1234 22")["ok"])
        self.assertTrue(self.status(ssh_connection="10.9.9.9 1 10.9.9.8 22")["ok"])  # unknown

    # a docked handheld's /sys as measured on one: AC0 Mains online=1, the dock's USB Ethernet
    # enp11s0f3u1u2, Wi-Fi wlp6s0, tailscale0
    HANDHELD_IP_OUT = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "2: enp11s0f3u1u2    inet 192.0.2.133/24 brd 192.0.2.255 scope global dynamic "
        "noprefixroute enp11s0f3u1u2\\       valid_lft 85000sec\n"
        "3: wlp6s0    inet 192.0.2.134/24 brd 192.0.2.255 scope global dynamic wlp6s0\\  x\n"
        "3: wlp6s0    inet6 fe80::aa:bb/64 scope link noprefixroute \\       valid_lft forever\n"
        "4: tailscale0    inet 100.64.0.7/32 scope global tailscale0\\       x\n")

    def handheld_sys(self):
        root = os.path.join(self.tmp, "handheld-sys")
        add_supply(root, "AC0", "Mains", online=1)
        add_supply(root, "BAT0", "Battery", online=1, scope="System", status="Not charging")
        add_net(root, "enp11s0f3u1u2")
        add_net(root, "wlp6s0", wireless=True)
        add_net(root, "tailscale0", kind=65534, state="unknown", virtual=True)
        add_net(root, "lo", kind=772, state="unknown", virtual=True)
        write(os.path.join(root, "class/drm/card1/device/gpu_busy_percent"), "2\n")
        return root

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_measured_handheld_layout(self):
        root = self.handheld_sys()
        cfg = self.cfg(sys=root)
        run = FakeRunner(self.x265, self.HANDHELD_IP_OUT)
        self.assertEqual(vworker.ethernet_links(root), ["enp11s0f3u1u2"])
        st = self.status(cfg, runner=run, ssh_connection="192.0.2.50 52000 192.0.2.133 22")
        self.assertEqual((st["ok"], st["why"], st["ac"], st["ethernet"], st["x265"],
                          st["version"], st["ipv4"]),
                         (True, "ready", True, True, "4.1+1-1d117be", 1, ["192.0.2.133"]))
        for addr, iface in (("192.0.2.134", "wlp6s0"), ("fe80::aa:bb", "wlp6s0"),
                            ("100.64.0.7", "tailscale0")):
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

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_a_resting_game_is_named_and_does_not_count(self):
        write_meminfo(self.proc, 8.0)                    # room beside it (MemoryAndOom)
        cfg = self.cfg(rest_min_age=0)                   # at rest long enough (RestAges)
        add_gta(self.proc, self.sys, frozen=True)
        st = self.status(cfg)
        self.assertEqual((st["ok"], st["code"], st["game"], st["resting_game"]),
                         (True, "ready", None, GTA))
        add_gta(self.proc, self.sys, frozen=True, state="T")        # SIGSTOPped as well
        self.assertEqual(self.status(cfg)["resting_game"], GTA)
        add_gta(self.proc, self.sys, frozen=False)
        st = self.status(cfg)
        self.assertEqual((st["ok"], st["code"], st["game"], st["resting_game"], st["why"]),
                         (False, "game", GTA, None, "game running: %s" % GTA))

    def test_a_game_waking_during_the_sample_counts(self):
        cfg = self.cfg(rest_min_age=0)
        add_gta(self.proc, self.sys, frozen=True)
        st = self.status(cfg, sleep=lambda _: set_cgroup(self.sys, GTA_SCOPE, frozen=False))
        self.assertEqual((st["ok"], st["code"], st["game"], st["resting_game"]),
                         (False, "game", GTA, None))
        add_gta(self.proc, self.sys, frozen=False)                  # and one that goes to rest
        st = self.status(cfg, sleep=lambda _: set_cgroup(self.sys, GTA_SCOPE, frozen=True))
        self.assertEqual((st["code"], st["game"], st["resting_game"]), ("game", GTA, GTA))

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_low_memory(self):
        self.assertEqual((vworker.ENCODE_MEM_GB, vworker.START_MEM_GB), (2.5, 3.5))
        self.assertEqual(vworker.Config({}).min_mem_gb, 3.5)
        self.assertEqual(vworker.Config({"VWORKER_MIN_MEM_GB": "5"}).min_mem_gb, 5.0)
        write_meminfo(self.proc, 2.0)                    # a big game resting on 14.8 GiB
        st = self.status()
        self.assertEqual((st["ok"], st["code"], st["why"], st["mem_available_gb"]),
                         (False, "low_memory", "low on memory: 2.0 GB available, needs 3.5", 2.0))
        write_meminfo(self.proc, 3.49)                   # rounded down: never more than there is
        st = self.status()
        self.assertEqual((st["ok"], st["why"], st["mem_available_gb"]),
                         (False, "low on memory: 3.4 GB available, needs 3.5", 3.4))
        write_meminfo(self.proc, 3.5)
        st = self.status()
        self.assertEqual((st["ok"], st["code"], st["mem_available_gb"]), (True, "ready", 3.5))
        write_meminfo(self.proc, 2.0)
        self.assertTrue(self.status(self.cfg(min_mem_gb=1.5))["ok"])
        os.unlink(os.path.join(self.proc, "meminfo"))    # unreadable: refuses nothing
        st = self.status()
        self.assertEqual((st["ok"], st["mem_available_gb"]), (True, None))

    def test_low_memory_in_the_order_of_reasons(self):
        write_meminfo(self.proc, 1.0)
        write(os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent"), "85\n")
        self.assertEqual(self.status()["code"], "heavy")              # heavy names the app
        write(os.path.join(self.sys, "class/drm/card0/device/gpu_busy_percent"), "3\n")
        with mock.patch.object(vworker, "free_gb", return_value=1.0):
            self.assertEqual(self.status(self.cfg(min_free_gb=5))["code"], "low_memory")
        add_gta(self.proc, self.sys, frozen=False)
        self.assertEqual(self.status()["code"], "game")

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


class MemoryAndOom(Tmp):
    def test_mem_available(self):
        self.assertIsNone(vworker.mem_available_gb(self.proc))      # no meminfo: not Linux
        write_meminfo(self.proc, 2.0)
        self.assertEqual(vworker.mem_available_gb(self.proc), 2.0)
        path = os.path.join(self.proc, "meminfo")
        for text in ("MemTotal: 15523840 kB\n",                    # a kernel before 3.14
                     "MemAvailable: lots kB\n", "MemAvailable: 2097152 MB\n", "MemAvailable:\n",
                     "MemAvailable: -5 kB\n"):
            write(path, text)
            self.assertIsNone(vworker.mem_available_gb(self.proc), text)
        write(path, "MemAvailable: 786432\n")                     # no unit: kB, as always
        self.assertEqual(vworker.mem_available_gb(self.proc), 0.75)

    def test_low_memory_takes_two_checks_in_a_row(self):
        self.assertEqual((vworker.STOP_MEM_GB, vworker.LOW_MEM_TICKS), (0.75, 2))
        m = vworker.MemoryWatch()
        self.assertEqual([m.update(gb) for gb in (0.5, 2.0, 0.5, None, 0.5, 0.75, 0.74, 0.5,
                                                  0.1)],
                         [False, False, False, False, False, False, False, True, True])

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_heavy_with_low_memory_is_not_resampled(self):
        cfg = self.cfg()
        heavy = {"heavy": "x 2.0 cores", "ac": True, "ethernet": True, "game": None,
                 "busy": False, "x265": "4.1+1-1d117be"}
        self.assertTrue(vworker.heavy_only(heavy, cfg))
        write_meminfo(self.proc, 2.0)
        self.assertFalse(vworker.heavy_only(heavy, cfg))
        write_meminfo(self.proc, 8.0)
        self.assertTrue(vworker.heavy_only(heavy, cfg))

    @unittest.skipUnless(ZSTD, "zstd not installed")
    def test_the_owners_rule_beside_a_resting_game(self):
        # MemAvailable stays >= 3 GiB the whole time a game rests: a job starts only with a
        # budget of MemAvailable - 3.5 GiB >= 3.0 GiB (so 6.5 GiB), and yields under 3.0 GiB
        self.assertEqual((vworker.REST_KEEP_GB, vworker.REST_RESERVE_GB,
                          vworker.REST_MIN_BUDGET_GB, vworker.GIB), (3.0, 3.5, 3.0, 1 << 30))
        cfg = self.cfg(rest_min_age=0)
        add_gta(self.proc, self.sys, frozen=True)
        for gb, want in ((6.49, (False, "low_memory",
                                 "low on memory: 6.4 GB available, needs 6.5 while a game rests")),
                         (6.5, (True, "ready", "ready")), (16.0, (True, "ready", "ready"))):
            write_meminfo(self.proc, gb)
            st = self.status(cfg)
            self.assertEqual((st["ok"], st["code"], st["why"]), want, gb)
            self.assertEqual(st["resting_game"], GTA)
        # measured: a big game's rest held 9.0 GiB and left 2.7-2.8 GiB available -- no job
        write_meminfo(self.proc, 2.75)
        st = self.status(cfg)
        self.assertEqual((st["ok"], st["code"], st["why"]),
                         (False, "low_memory",
                          "low on memory: 2.7 GB available, needs 6.5 while a game rests"))
        os.unlink(os.path.join(self.proc, "meminfo"))   # its memory can't be kept unseen
        st = self.status(cfg)
        self.assertEqual((st["ok"], st["code"], st["why"]),
                         (False, "low_memory", "low on memory: it can't be read while a game rests"))
        write_meminfo(self.proc, 4.0)                    # no game at rest: round 1's 3.5 GB
        add_gta(self.proc, self.sys, frozen=False)
        self.assertEqual(self.status(cfg)["code"], "game")
        shutil.rmtree(self.proc)
        os.makedirs(self.proc)
        write_meminfo(self.proc, 4.0)
        self.assertEqual(self.status(cfg)["code"], "ready")
        # heavy beside a resting game is re-sampled only when its memory rule holds too
        heavy = {"heavy": "x 2.0 cores", "code": "heavy", "ac": True, "ethernet": True,
                 "game": None, "busy": False, "x265": "4.1+1-1d117be", "resting_game": GTA}
        self.assertFalse(vworker.heavy_only(heavy, cfg))
        write_meminfo(self.proc, 7.0)
        self.assertTrue(vworker.heavy_only(heavy, cfg))

    def test_the_memory_knobs(self):
        c = vworker.Config({})
        self.assertEqual((c.min_mem_gb, c.stop_mem_gb, c.rest_keep_gb, c.rest_reserve_gb,
                          c.rest_budget_gb), (3.5, 0.75, 3.0, 3.5, 3.0))
        c = vworker.Config({"VWORKER_STOP_MEM_GB": "0.5", "VWORKER_REST_KEEP_GB": "2",
                            "VWORKER_REST_RESERVE_GB": "1.5", "VWORKER_REST_BUDGET_GB": "1"})
        self.assertEqual((c.stop_mem_gb, c.rest_keep_gb, c.rest_reserve_gb, c.rest_budget_gb),
                         (0.5, 2.0, 1.5, 1.0))
        v = vworker.memory_verdict
        self.assertEqual(v(c, 2.4, GTA), (False, "low on memory: 2.4 GB available, needs 2.5 "
                                                 "while a game rests"))
        self.assertEqual(v(c, 2.5, GTA), (True, ""))
        self.assertEqual(v(c, None, None), (True, ""))
        self.assertEqual(v(c, 3.4, None), (False, "low on memory: 3.4 GB available, needs 3.5"))
        m = vworker.MemoryWatch(3.0)                     # the floor beside a resting game
        self.assertEqual([m.update(gb) for gb in (2.9, 3.1, 2.9, 3.0, 2.99, 2.5)],
                         [False, False, False, False, False, True])

    def test_mem_available_bytes(self):
        write_meminfo(self.proc, 8.0)
        self.assertEqual(vworker.mem_available_bytes(self.proc), 8 << 30)
        os.unlink(os.path.join(self.proc, "meminfo"))
        self.assertIsNone(vworker.mem_available_bytes(self.proc))

    def test_the_oom_killer_takes_the_encode_first(self):
        adj = os.path.join(self.proc, "self", "oom_score_adj")
        self.assertFalse(vworker.raise_oom_score(self.proc))       # none here: never an error
        write(adj, "0\n")
        self.assertTrue(vworker.raise_oom_score(self.proc))
        with open(adj) as f:
            self.assertEqual(f.read(), "1000\n")
        os.unlink(adj)
        os.makedirs(adj)                                          # a write that is refused
        self.assertFalse(vworker.raise_oom_score(self.proc))
        self.assertEqual(vworker.OOM_SCORE_ADJ, 1000)


class ConfigLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vworker-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        self.empty = os.path.join(self.tmp, "empty")
        os.makedirs(self.empty)

    def tool(self, *parts):
        path = os.path.join(self.tmp, *parts)
        write(path, "#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_zstd_on_path_then_the_usual_places(self):
        self.assertEqual(vworker.ZSTD_PATHS, ("/usr/bin/zstd", "/usr/local/bin/zstd"))
        on_path = self.tool("bin", "zstd")
        self.assertEqual(vworker.Config({"PATH": self.bin}).zstd, on_path)
        self.assertEqual(vworker.Config({"PATH": self.bin, "VWORKER_ZSTD": "/x/zstd"}).zstd,
                         "/x/zstd")
        missing, local = os.path.join(self.tmp, "no-zstd"), self.tool("local", "zstd")
        with mock.patch.object(vworker, "ZSTD_PATHS", (missing, local)):
            self.assertEqual(vworker.Config({"PATH": self.empty}).zstd, local)
        with mock.patch.object(vworker, "ZSTD_PATHS", (missing, missing + "2")):
            self.assertEqual(vworker.Config({"PATH": self.empty}).zstd, missing)   # named, missing

    def test_systemd_inhibit_only_where_it_exists(self):
        found = self.tool("bin", "systemd-inhibit")
        self.assertEqual(vworker.Config({"PATH": self.bin}).inhibit, found)
        self.assertEqual(vworker.Config({"VWORKER_INHIBIT": "/x/inhibit"}).inhibit, "/x/inhibit")
        if not os.path.exists("/usr/bin/systemd-inhibit"):
            self.assertEqual(vworker.Config({"PATH": self.empty}).inhibit, "")
        # the sleep block alone: no idle block, which would keep a rest manager from acting
        self.assertEqual(vworker.INHIBIT_ARGS,
                         ["--what=sleep", "--who=visionary-helper",
                          "--why=encoding a segment", "--mode=block", "sleep", "infinity"])
        self.assertFalse(hasattr(vworker, "IDLE_INHIBIT_ARGS"))
        self.assertEqual(vworker.SLEEP_PROBE_ARGS,
                         ["--what=sleep", "--mode=block", "--who=visionary-helper",
                          "--why=probe", "true"])

    def test_systemd_run_and_ionice(self):
        for var, name, attr in (("VWORKER_SYSTEMD_RUN", "systemd-run", "systemd_run"),
                                ("VWORKER_IONICE", "ionice", "ionice")):
            found = self.tool("bin", name)
            self.assertEqual(getattr(vworker.Config({"PATH": self.bin}), attr), found, name)
            self.assertEqual(getattr(vworker.Config({var: "/x/" + name}), attr), "/x/" + name)
            self.assertEqual(getattr(vworker.Config({var: "", "PATH": self.bin}), attr), "")
            if not os.path.exists("/usr/bin/" + name):
                self.assertEqual(getattr(vworker.Config({"PATH": self.empty}), attr), "", name)
        c = vworker.Config({"VWORKER_JOBS": "/home/u/visionary-scratch/jobs"})
        self.assertEqual(c.rest_seen, "/home/u/visionary-scratch/rest-seen.json")   # its own
        self.assertEqual((c.in_scope, c.scope_max), (False, None))
        c = vworker.Config({"VWORKER_IN_SCOPE": "1", "VWORKER_SCOPE_MAX": "5100273664"})
        self.assertEqual((c.in_scope, c.scope_max), (True, 5100273664))


class SleepProbe(unittest.TestCase):
    """status's "sleep_block": may this session hold off sleep (K1)?"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vworker-sleep-")
        self.addCleanup(shutil.rmtree, self.tmp)
        vworker._SLEEP_PROBE.clear()
        self.addCleanup(vworker._SLEEP_PROBE.clear)
        self.log = os.path.join(self.tmp, "probe.log")

    def fake(self, rc=0, sleep=0):
        path = os.path.join(self.tmp, "systemd-inhibit-%d-%s" % (rc, sleep))
        write(path, FAKE_PROBE.format(python=sys.executable, log=self.log, sleep=sleep, rc=rc))
        os.chmod(path, 0o755)
        return path

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def test_allowed_refused_and_absent(self):
        self.assertTrue(vworker.sleep_block(self.fake(rc=0)))
        self.assertEqual(self.calls(), [{"argv": vworker.SLEEP_PROBE_ARGS,
                                         "devnull": [True, True, True]}])   # no polkit agent
        self.assertIs(vworker.sleep_block(self.fake(rc=1)), False)          # "Access denied"
        self.assertIsNone(vworker.sleep_block(""))                          # no systemd-inhibit
        self.assertIsNone(vworker.sleep_block(os.path.join(self.tmp, "gone")))
        unrunnable = os.path.join(self.tmp, "not-executable")
        write(unrunnable, "#!/bin/sh\n")
        self.assertIs(vworker.sleep_block(unrunnable), False)

    def test_a_probe_that_hangs_is_a_no_within_its_timeout(self):
        t0 = time.monotonic()
        self.assertIs(vworker.sleep_block(self.fake(sleep=5)), False)
        self.assertLess(time.monotonic() - t0, vworker.SLEEP_PROBE_TIMEOUT + 1.5)

    def test_one_probe_a_minute_per_process(self):
        path, now = self.fake(rc=0), [100.0]
        clock = lambda: now[0]
        self.assertTrue(vworker.sleep_block(path, clock))
        now[0] += vworker.SLEEP_PROBE_TTL - 1
        self.assertTrue(vworker.sleep_block(path, clock))
        self.assertEqual(len(self.calls()), 1)                              # cached
        now[0] += 2
        self.assertTrue(vworker.sleep_block(path, clock))
        self.assertEqual(len(self.calls()), 2)


class PythonVersion(unittest.TestCase):
    def test_runs_on_python_3_8(self):
        with open(VWORKER) as f:
            src = f.read()
        ast.parse(src, feature_version=(3, 8))
        self.assertNotIn("from __future__", src)
        self.assertIn("Python 3.8 or newer", src)


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
    """zstd and x265 run under SCHED_IDLE (most desktop distros turn sched_autogroup on, so nice
    alone does not let the desktop win), with nice 19 only where SCHED_IDLE is unavailable."""

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
                   VWORKER_REST_FILE=self.rest_file, VWORKER_REST_SEEN=self.rest_seen,
                   VWORKER_SYSTEMD_RUN="", VWORKER_IONICE="", FAKE_X265_RAN=self.ran)
        for k in ("SSH_CONNECTION", "VWORKER_IN_SCOPE", "VWORKER_SCOPE_MAX"):
            env.pop(k, None)
        env.update({k: str(v) for k, v in over.items()})
        return env

    def scope_env(self, **extra):
        """The fake systemd-run (FAKE_SYSTEMD_RUN) -> (env, its log)."""
        path = os.path.join(self.tmp, "systemd-run")
        if not os.path.exists(path):
            write(path, FAKE_SYSTEMD_RUN.replace("{python}", sys.executable))
            os.chmod(path, 0o755)
        log = os.path.join(self.tmp, "systemd-run-%d.log" % len(os.listdir(self.tmp)))
        env = {"VWORKER_SYSTEMD_RUN": path, "FAKE_SYSTEMD_RUN_LOG": log}
        env.update(extra)
        return env, log

    @staticmethod
    def calls(log):
        """Every invocation a fake tool logged (its arguments), in order."""
        if not os.path.exists(log):
            return []
        with open(log) as f:
            return [json.loads(line) for line in f]

    def resting_gta(self, age=120, **kw):
        """The handheld's game, resting for `age` s (two minutes) beside 8 GiB of
        MemAvailable."""
        write_meminfo(self.proc, 8.0)
        add_gta(self.proc, self.sys, **dict(dict(frozen=True), **kw))
        if os.path.exists(self.rest_seen):
            os.unlink(self.rest_seen)
        seed_rest(self.rest_seen, [GTA_KEY], age)

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

    # -- the sleep block: held while x265 runs, gone on every way out
    def inhibitor(self, fail=False):
        path = os.path.join(self.tmp, "systemd-inhibit")
        with open(path, "w") as f:
            f.write(FAKE_INHIBIT.replace("{python}", sys.executable))
        os.chmod(path, 0o755)
        log = os.path.join(self.tmp, "inhibit.log")
        env = {"VWORKER_INHIBIT": path, "FAKE_INHIBIT_LOG": log}
        if fail:
            env["FAKE_INHIBIT_FAIL"] = "1"
        return env, log

    def held(self, log, what, timeout=10):
        """-> (pid, argv) of the fake systemd-inhibit holding the `what` block once it started."""
        path = "%s.%s" % (log, what)
        deadline = time.monotonic() + timeout
        while not os.path.exists(path):
            if time.monotonic() > deadline:
                self.fail("systemd-inhibit --what=%s never started" % what)
            time.sleep(0.02)
        with open(path) as f:
            pid, *argv = f.read().split("\n")
        return int(pid), argv

    def probes(self, log):
        """The arguments of every sleep probe status ran through the fake."""
        if not os.path.exists(log + ".probe"):
            return []
        with open(log + ".probe") as f:
            return [json.loads(line) for line in f]

    def assert_released(self, pid):
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() > deadline:
                self.fail("systemd-inhibit still running after vworker exited")
            time.sleep(0.02)

    def test_sleep_is_blocked_while_encoding(self):
        env, log = self.inhibitor()
        run = self.start("seg_awake", 6, dv=False, **env)
        pid, argv = self.held(log, "sleep")
        self.assertEqual(argv, vworker.INHIBIT_ARGS)          # sleep alone: no idle block
        os.kill(pid, 0)                                       # held while x265 waits for input
        self.assertEqual(self.probes(log), [vworker.SLEEP_PROBE_ARGS])   # status asked once
        run.feed(zstd_compress(y4m(6)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(6))
        self.assertFalse(os.path.exists(log + ".idle"))
        self.assert_released(pid)
        self.assert_cleaned("seg_awake")

    def test_the_sleep_block_goes_on_a_yield_and_an_error(self):
        env, log = self.inhibitor()
        run = self.start("seg_awake_y", 50, dv=False, **env)
        pid = self.held(log, "sleep")[0]
        self.wait_started()
        add_proc(self.proc, FAKE_PID, "reaper", ["reaper", "SteamLaunch", "AppId=1145360"])
        self.assertEqual(run.wait(), 75, run.stderr[-800:])
        self.assert_released(pid)
        shutil.rmtree(os.path.join(self.proc, str(FAKE_PID)))
        os.unlink(log + ".sleep")
        run = self.start("seg_awake_e", 3, dv=False, FAKE_X265_RC=4, **env)
        pid = self.held(log, "sleep")[0]
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 1)
        self.assert_released(pid)
        self.assert_cleaned("seg_awake_e")

    def test_no_sleep_block_never_fails_a_job(self):
        env, log = self.inhibitor(fail=True)                  # polkit said no: it exits at once
        run = self.start("seg_noblock", 4, dv=False, **env)
        self.held(log, "sleep")                               # it ran (and gave up) first
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])    # status's probe said no, too
        self.assertEqual(run.stdout, expected_hevc(4))
        self.assertEqual(self.probes(log), [vworker.SLEEP_PROBE_ARGS])
        run = self.start("seg_noblock2", 4, dv=False,
                         VWORKER_INHIBIT=os.path.join(self.tmp, "no-such-inhibit"))
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])

    def test_no_sleep_block_beside_a_resting_game_or_a_rest_manager(self):
        env, log = self.inhibitor()
        # a game at rest: no block (its rest mode decides about sleep)
        self.resting_gta()
        scope, _calls = self.scope_env()
        run = self.start("seg_rest_awake", 4, dv=False, **dict(env, **scope))
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertFalse(os.path.exists(log + ".sleep"))
        # a rest manager here (its file), no game: no block, and status does not even probe
        shutil.rmtree(self.proc)
        os.makedirs(self.proc)
        os.unlink(log + ".probe")
        write_rest_file(self.rest_file, state="idle")
        run = self.start("seg_manager", 4, dv=False, **env)
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(4))
        self.assertFalse(os.path.exists(log + ".sleep"))
        self.assertEqual(self.probes(log), [])
        # one that appears mid-job: the block it took goes, and the job carries on
        os.unlink(self.rest_file)
        os.unlink(self.ran)
        run = self.start("seg_manager_late", 4, dv=False, VWORKER_WATCH_INTERVAL=30, **env)
        pid = self.held(log, "sleep")[0]
        self.wait_started()
        write_rest_file(self.rest_file, state="idle")
        self.assert_released(pid)
        self.assertIsNone(run.p.poll())
        run.feed(zstd_compress(y4m(4)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(4))

    def test_a_refusal_takes_no_sleep_block(self):
        env, log = self.inhibitor()
        write(os.path.join(self.sys, "class/power_supply/ACAD/online"), "0\n")
        run = self.start("seg_refused", 4, dv=False, **env)
        run.feed(b"")
        self.assertEqual(run.wait(), 77)
        self.assertFalse(os.path.exists(log + ".sleep"))
        self.assertFalse(os.path.exists(log + ".idle"))

    # -- the stream arriving over the helper's Wi-Fi (ARP sent it there): it stops at once
    def stream_reader(self, run, timeout=10):
        """The pid of the zstd that reads the encode's stdin (vworker's child)."""
        deadline = time.monotonic() + timeout
        while True:
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines():
                f = line.split(None, 2)
                if len(f) == 3 and f[1] == str(run.p.pid) and os.path.basename(f[2]) == "zstd":
                    return int(f[0])
            if time.monotonic() > deadline:
                self.fail("no zstd under vworker")
            time.sleep(0.05)

    def traffic(self, reader, wifi_mb, cable_mb):
        """A thread that, every 50 ms until halted, has wlan0 and enp4s0 receive the MB given
        and then the job read 8 MB of its stream (the links count first, as on the wire).
        -> halt(), which stops it and waits for it."""
        stop, n = threading.Event(), {"job": 0, "wlan0": 0, "enp4s0": 0}

        def run():
            while not stop.is_set():
                for iface, mb in (("wlan0", wifi_mb), ("enp4s0", cable_mb)):
                    n[iface] += int(mb * (1 << 20))
                    set_rx(self.sys, iface, n[iface])
                n["job"] += 8 << 20
                set_rchar(self.proc, reader, n["job"])
                time.sleep(0.05)
        set_rx(self.sys, "wlan0", 0)
        set_rx(self.sys, "enp4s0", 0)
        set_rchar(self.proc, reader, 0)
        t = threading.Thread(target=run, daemon=True)
        t.start()

        def halt():
            stop.set()
            t.join(5)
        self.addCleanup(halt)
        return halt

    def test_the_stream_over_wifi_stops_it(self):
        run = self.start("seg_wifi", 50, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        pid = self.wait_started()
        halt = self.traffic(self.stream_reader(run), wifi_mb=8.4, cable_mb=0.05)
        t0 = time.monotonic()
        self.assertEqual(run.wait(), 75, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 5.0)
        halt()
        self.assertEqual(run.vline(), {"result": "yielded", "frames": 0, "code": "no_ethernet",
                                       "why": "the stream is arriving over its Wi-Fi (wlan0) "
                                              "· turn its Wi-Fi off"})
        self.assert_cleaned("seg_wifi", pid)                  # dir gone AND x265 dead

    def test_a_download_over_wifi_beside_an_ethernet_stream_does_not_stop_it(self):
        run = self.start("seg_wifi_dl", 8, dv=False, VWORKER_WATCH_INTERVAL=0.2)
        self.wait_started()
        halt = self.traffic(self.stream_reader(run), wifi_mb=30, cable_mb=8.4)
        time.sleep(1.2)                                       # six watchdog checks
        halt()
        run.feed(zstd_compress(y4m(8)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(8))
        self.assertEqual(run.vline()["result"], "ok")

    # -- yields
    def test_a_game_started_mid_encode_yields_within_a_quarter_second(self):
        self.put_rpu("seg_game")
        run = self.start("seg_game", 50, VWORKER_WATCH_INTERVAL=30)   # only the fast watch
        pid = self.wait_started()
        time.sleep(0.3)
        add_proc(self.proc, FAKE_PID, "reaper", ["reaper", "SteamLaunch", "AppId=1145360", "--",
                                                 "proton", "waitforexitandrun", "Hades.exe"])
        t0 = time.monotonic()
        rc = run.wait()
        elapsed = time.monotonic() - t0
        note_latency("new game, end to end", elapsed)
        self.assertLess(elapsed, 0.75)
        self.assertEqual(rc, 75, run.stderr[-800:])
        v = run.vline()
        self.assertEqual(v["result"], "yielded")
        self.assertEqual((v["why"], v["code"]), ("a game started: Hades.exe", "game"))
        self.assert_cleaned("seg_game", pid)

    def test_the_watchdog_still_catches_what_the_fast_watch_does_not_read_again(self):
        # the backstop: a process that was already there exec's a game later on. The fast
        # watch reads a pid on its first two looks only, so the watchdog's full check finds it
        add_proc(self.proc, FAKE_PID, "bash", ["/bin/bash", "./start.sh"])
        run = self.start("seg_game_wd", 50, dv=False)        # the default 2 s watchdog
        pid = self.wait_started()
        time.sleep(0.8)                                       # its two quick looks are over
        d = os.path.join(self.proc, str(FAKE_PID))
        with open(os.path.join(d, "cmdline"), "wb") as f:     # exec'd in place: same pid
            f.write(b"Z:\\games\\Hades.exe\0")
        write(os.path.join(d, "stat"), stat_line(FAKE_PID, "Hades.exe", 1))
        t0 = time.monotonic()
        rc = run.wait()
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(rc, 75, run.stderr[-800:])
        self.assertEqual((run.vline()["why"], run.vline()["code"]),
                         ("game running: Hades.exe", "game"))
        self.assert_cleaned("seg_game_wd", pid)

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
        run = self.start("seg_proton", 50, dv=False, VWORKER_WATCH_INTERVAL=30)
        pid = self.wait_started()
        self.add_tree(shortcut_tree("proton", base=FAKE_PID))
        t0 = time.monotonic()
        self.assertEqual(run.wait(), 75, run.stderr[-800:])
        self.assertLess(time.monotonic() - t0, 0.75)
        self.assertEqual(run.vline()["why"], "a game started: Celeste.exe")
        self.assert_cleaned("seg_proton", pid)

    # -- a resting game: it encodes beside one, and steps aside the moment it wakes
    def test_a_resting_game_lets_it_encode_and_a_thaw_yields_at_once(self):
        self.resting_gta()
        scope, _log = self.scope_env()
        # a 30 s watchdog: only the fast watch can be this quick
        run = self.start("seg_rest", 50, dv=False, VWORKER_WATCH_INTERVAL=30, **scope)
        pid = self.wait_started()
        time.sleep(0.5)
        self.assertIsNone(run.p.poll(), run.stderr[-800:])        # resting: it encodes
        set_cgroup(self.sys, GTA_SCOPE, frozen=False)             # a button press: thawed
        t0 = time.monotonic()
        rc = run.wait()
        elapsed = time.monotonic() - t0
        note_latency("thaw, end to end", elapsed)
        self.assertEqual(rc, 75, run.stderr[-800:])
        v = run.vline()
        self.assertEqual((v["result"], v["why"], v["code"]),
                         ("yielded", "a game woke up: " + GTA, "game"))
        self.assertLess(elapsed, 0.75)
        self.assert_cleaned("seg_rest", pid)

    def test_a_game_at_rest_for_less_than_a_minute_counts_as_running(self):
        self.resting_gta(age=20)                                  # its quick menu, 20 s ago
        scope, log = self.scope_env()
        run = self.start("seg_young", 4, dv=False, **scope)
        run.feed(b"")
        self.assertEqual(run.wait(), 77, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "refused", "why": "game running: " + GTA,
                                       "frames": 0, "code": "game"})
        self.assertEqual(self.calls(log), [])                     # nothing started a scope
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_young")

    def test_a_game_waking_up_mid_job_yields_within_a_quarter_second(self):
        self.resting_gta()
        write_rest_file(self.rest_file, state="resting")          # the rest manager, idle
        scope, _log = self.scope_env()
        run = self.start("seg_waking", 50, dv=False, VWORKER_WATCH_INTERVAL=30, **scope)
        pid = self.wait_started()
        time.sleep(0.5)
        self.assertIsNone(run.p.poll(), run.stderr[-800:])
        write_rest_file(self.rest_file, state="resting", waking=local_time(time.time()))
        t0 = time.monotonic()
        rc = run.wait()
        elapsed = time.monotonic() - t0
        note_latency("wake marker, end to end", elapsed)
        self.assertEqual(rc, 75, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "yielded", "why": "a game is waking up",
                                       "frames": 0, "code": "game"})
        self.assertLess(elapsed, 0.75)
        self.assert_cleaned("seg_waking", pid)
        # and a job asked for meanwhile is refused as a game
        run = self.start("seg_waking2", 4, dv=False, **scope)
        run.feed(b"")
        self.assertEqual(run.wait(), 77, run.stderr[-800:])
        self.assertEqual((run.vline()["why"], run.vline()["code"]), ("a game is waking up", "game"))

    def test_a_sigstopped_game_continued_mid_job_yields_at_once(self):
        self.resting_gta(frozen=False, state="T")
        scope, _log = self.scope_env()
        run = self.start("seg_stopped", 50, dv=False, VWORKER_WATCH_INTERVAL=30, **scope)
        pid = self.wait_started()
        time.sleep(0.5)
        add_gta(self.proc, self.sys, frozen=False)                # SIGCONT
        t0 = time.monotonic()
        self.assertEqual(run.wait(), 75, run.stderr[-800:])
        elapsed = time.monotonic() - t0
        note_latency("SIGCONT, end to end", elapsed)
        self.assertLess(elapsed, 0.75)
        self.assertEqual((run.vline()["why"], run.vline()["code"]),
                         ("a game woke up: " + GTA, "game"))
        self.assert_cleaned("seg_stopped", pid)

    def test_a_resting_game_that_exits_mid_job_is_no_wake_up(self):
        self.resting_gta()
        scope, _log = self.scope_env()
        run = self.start("seg_rest_exit", 8, dv=False, VWORKER_WATCH_INTERVAL=0.2, **scope)
        self.wait_started()
        time.sleep(0.5)
        # quit while it rests, in the order it really goes: killed in its freezer (its tasks
        # leave it to die while the scope is still asked to freeze), its processes gone, the
        # scope emptied and removed -- and Steam's launch wrapper outside it still there for
        # a while, which on its own would read as a game being launched
        add_gta(self.proc, self.sys, frozen=False, freeze=True, flags=vworker.PF_EXITING)
        time.sleep(0.3)
        for gone in (GTA_REAPER, GTA_WINESERVER, GTA_EXE):
            remove_proc(self.proc, gone)
        set_cgroup(self.sys, GTA_SCOPE, frozen=False, populated=0)
        time.sleep(0.6)                                           # two looks, three checks
        shutil.rmtree(os.path.join(self.sys, "fs", "cgroup") + GTA_SCOPE)
        time.sleep(0.6)
        remove_proc(self.proc, GTA_WRAPPER)
        time.sleep(0.3)
        self.assertIsNone(run.p.poll(), run.stderr[-800:])
        run.feed(zstd_compress(y4m(8)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(8))
        self.assertEqual(run.vline()["result"], "ok")

    # -- memory: a resting game keeps its RAM
    def test_low_memory_refuses_at_once(self):
        log = os.path.join(self.tmp, "samples.log")
        write_meminfo(self.proc, 2.0)
        write(self.gpu_file(), "90\n")                 # heavy too: no re-sample with low memory
        run = self.start("seg_mem_heavy", 4, dv=False, FAKE_X265_VERSION_LOG=log)
        run.feed(b"")
        self.assertEqual(run.wait(), 77, run.stderr[-800:])
        self.assertEqual(run.vline()["code"], "heavy")
        self.assertEqual(self.samples(log), 1)
        write(self.gpu_file(), "3\n")
        os.unlink(log)
        run = self.start("seg_mem", 4, dv=False, FAKE_X265_VERSION_LOG=log)
        run.feed(b"")
        self.assertEqual(run.wait(), 77, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "refused", "frames": 0, "code": "low_memory",
                                       "why": "low on memory: 2.0 GB available, needs 3.5"})
        self.assertEqual(self.samples(log), 1)                   # at once
        self.assertFalse(os.path.exists(self.ran))
        self.assert_cleaned("seg_mem")

    def test_low_memory_mid_job_yields_after_two_checks(self):
        write_meminfo(self.proc, 8.0)
        run = self.start("seg_lowmem", 50, dv=False, VWORKER_WATCH_INTERVAL=0.5)
        pid = self.wait_started()
        write_meminfo(self.proc, 0.5)
        t0 = time.monotonic()
        rc = run.wait()
        self.assertGreater(time.monotonic() - t0, 0.45)          # not on the first low check
        self.assertEqual(rc, 75, run.stderr[-800:])
        v = run.vline()
        self.assertEqual((v["result"], v["why"], v["code"]),
                         ("yielded", "low on memory: 0.5 GB available", "low_memory"))
        self.assert_cleaned("seg_lowmem", pid)

    def test_beside_a_resting_game_it_keeps_three_gib_free(self):
        self.resting_gta()
        scope, _log = self.scope_env()
        run = self.start("seg_rest_mem", 50, dv=False, VWORKER_WATCH_INTERVAL=0.3, **scope)
        pid = self.wait_started()
        write_meminfo(self.proc, 3.0)                             # at the floor: fine
        time.sleep(1.0)
        self.assertIsNone(run.p.poll(), run.stderr[-800:])
        write_meminfo(self.proc, 2.5)
        t0 = time.monotonic()
        rc = run.wait()
        self.assertGreater(time.monotonic() - t0, 0.25)           # not on the first low check
        self.assertEqual(rc, 75, run.stderr[-800:])
        self.assertEqual(run.vline(), {"result": "yielded", "frames": 0, "code": "low_memory",
                                       "why": "low on memory: 2.5 GB available while a game rests"})
        self.assert_cleaned("seg_rest_mem", pid)
        write_meminfo(self.proc, 6.0)                             # and too little to start
        run = self.start("seg_rest_mem2", 4, dv=False, **scope)
        run.feed(b"")
        self.assertEqual(run.wait(), 77)
        self.assertEqual(run.vline()["why"],
                         "low on memory: 6.0 GB available, needs 6.5 while a game rests")

    # -- beside a resting game: inside a scope of its own, capped at its memory budget
    def test_beside_a_resting_game_it_encodes_in_a_capped_scope(self):
        self.resting_gta()
        scope, log = self.scope_env()
        self.put_rpu("seg_scope", b"RPU-IN-SCOPE")
        spec = b64spec(visionary_args(True), 6)
        run = Running([sys.executable, VWORKER, "encode", "seg_scope", spec], self.env(**scope))
        run.feed(zstd_compress(y4m(6)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(6, b"RPU-IN-SCOPE"))   # the stream, untouched
        self.assertEqual(run.vline(), {"result": "ok", "why": "", "frames": 6})
        probe, rexec = self.calls(log)
        self.assertEqual(probe, vworker.SCOPE_PROBE_ARGS)        # can it start a scope at all?
        budget = (8 << 30) - int(3.5 * (1 << 30))                 # MemAvailable - 3.5 GiB
        self.assertEqual(rexec, ["--user", "--scope", "--quiet", "--collect",
                                 "--unit=visionary-x265-seg_scope",
                                 "-p", "MemoryHigh=%d" % budget,
                                 "-p", "MemoryMax=%d" % (budget + (256 << 20)),
                                 "--", sys.executable, VWORKER, "encode", "seg_scope", spec])
        self.assertFalse(any(a.startswith("--unit=app-steam-app") for a in rexec))
        with open(self.ran + ".ppid") as f:
            ppid, in_scope = f.read().split(" ")
        self.assertEqual((int(ppid), in_scope), (run.p.pid, "1"))   # the same process, in it
        self.assert_cleaned("seg_scope")
        # with no game at rest it runs where it is: no scope
        shutil.rmtree(self.proc)
        os.makedirs(self.proc)
        scope2, log2 = self.scope_env()
        run = self.start("seg_noscope", 3, dv=False, **scope2)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(self.calls(log2), [])
        with open(self.ran + ".ppid") as f:
            self.assertEqual(f.read().split(" ")[1], "")

    def test_a_lost_connection_still_ends_the_job_inside_its_scope(self):
        # outside the ssh session's scope, logind no longer ends it with the session: it must
        # see the Mac go by itself -- its stdin ending, its stdout breaking
        self.resting_gta()
        scope, _log = self.scope_env()
        blob = zstd_compress(y4m(30))
        run = self.start("seg_scope_cut", 30, dv=False, **scope)
        pid = self.wait_started()
        run.feed(blob[:len(blob) // 2])                           # the link drops mid-stream
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        self.assertEqual(run.vline()["result"], "disconnected")
        self.assert_cleaned("seg_scope_cut", pid)
        os.unlink(self.ran)
        run = self.start("seg_scope_pipe", 20, dv=False, close_stdout=True, **scope)
        run.feed(zstd_compress(y4m(20)), close=False)
        self.assertEqual(run.wait(), 76, run.stderr[-800:])
        self.assertTrue(run.vline()["why"].startswith("stdout broken"), run.vline())
        pid = None
        if os.path.exists(self.ran):
            with open(self.ran) as f:
                pid = int(f.read())
        self.assert_cleaned("seg_scope_pipe", pid)

    def test_a_resting_game_without_a_capped_scope_refuses(self):
        self.resting_gta()
        for name, extra in (("no systemd-run", {}),
                            ("it can't start a scope", self.scope_env(FAKE_SYSTEMD_RUN_FAIL=1)[0]),
                            ("a scope without a cap", self.scope_env(FAKE_SYSTEMD_RUN_NOCAP=1)[0])):
            run = self.start("seg_nocap", 4, dv=False, **extra)
            run.feed(b"")
            self.assertEqual(run.wait(), 77, name + ": " + run.stderr[-800:].decode())
            self.assertEqual(run.vline(), {"result": "refused", "frames": 0, "code": "low_memory",
                                           "why": "a game is resting and its memory can't be "
                                                  "capped"}, name)
            self.assertFalse(os.path.exists(self.ran), name)
            self.assert_cleaned("seg_nocap")

    # -- zstd and x265 at idle I/O priority
    def test_zstd_and_x265_run_at_idle_io_priority(self):
        path = os.path.join(self.tmp, "ionice")
        write(path, FAKE_IONICE.replace("{python}", sys.executable))
        os.chmod(path, 0o755)
        log = os.path.join(self.tmp, "ionice.log")
        run = self.start("seg_io", 3, dv=False, VWORKER_IONICE=path, FAKE_IONICE_LOG=log)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(3))
        calls = self.calls(log)
        self.assertEqual(calls[0], ["-c", "3", "true"])          # does it work here?
        by_tool = {c[2]: c for c in calls[1:]}                    # zstd's and x265's: either first
        self.assertEqual(sorted(by_tool), sorted([ZSTD, self.x265]))
        self.assertEqual(by_tool[ZSTD], ["-c", "3", ZSTD, "-d", "-q", "-c"])
        self.assertEqual(len(calls), 3)
        with open(self.ran + ".ppid") as f:                       # ionice exec'd: x265 is ours
            self.assertEqual(int(f.read().split(" ")[0]), run.p.pid)
        log2 = os.path.join(self.tmp, "ionice-fail.log")          # it can't: they run without
        run = self.start("seg_io2", 3, dv=False, VWORKER_IONICE=path, FAKE_IONICE_LOG=log2,
                         FAKE_IONICE_FAIL=1)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(self.calls(log2), [["-c", "3", "true"]])
        self.assertEqual(vworker.IONICE_ARGS, ["-c", "3"])

    def test_the_encode_raises_its_oom_score_before_x265_starts(self):
        adj = os.path.join(self.proc, "self", "oom_score_adj")
        write(adj, "0\n")
        run = self.start("seg_oom", 3, dv=False, FAKE_X265_OOM=adj)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        with open(self.ran + ".oom") as f:
            self.assertEqual(f.read(), "1000\n")                 # x265 started after the write
        os.unlink(adj)
        os.makedirs(adj)                                         # refused: the job runs anyway
        run = self.start("seg_oom_refused", 3, dv=False)
        run.feed(zstd_compress(y4m(3)))
        self.assertEqual(run.wait(), 0, run.stderr[-800:])
        self.assertEqual(run.stdout, expected_hevc(3))

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

    def test_build_script_keeps_the_multilib_recipe(self):
        # The version is no longer pinned here: the script builds whatever the Mac's x265
        # reports (`--version <token>`; test_helper_scripts covers the token and the
        # x265Version.txt it writes). The 8/10/12-bit recipe and the install path stay.
        with open(os.path.join(WORKER_DIR, "build-x265.sh")) as f:
            text = f.read()
        for needle in ("BOX=visionary-worker", 'rm -rf "$src/.git"', "-DLINKED_10BIT=ON",
                       "-DLINKED_12BIT=ON", "-DMAIN12=ON", "8bit+10bit+12bit",
                       '"$ROOT/bin/x265"', 'main "$@"'):
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
        key = os.path.join(tmp, "id_helper")
        with open(key, "w") as f:
            f.write("not a real key\n")
        log = os.path.join(tmp, "ssh.log")
        env = {"PATH": fakebin + ":/usr/bin:/bin", "FAKE_HOME": home, "FAKE_SSH_LOG": log,
               "VWORKER_STATUS_SAMPLE": "0", "HOME": tmp}
        for _ in range(2):
            r = subprocess.run(["bash", os.path.join(WORKER_DIR, "install.sh"), "--host",
                                "helper.local", "--user", "user", "--key", key],
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
                      "-o ServerAliveCountMax=3 user@helper.local", calls)
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
