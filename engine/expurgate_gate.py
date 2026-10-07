"""Who may hold Visionary's machine: Expurgate, and only while it is OPEN and ACTIVATED.

User rule, 2026-10-06: "expurgate shouldn't have the machine unless it's open and activated. Those
rules shouldn't be possible to bypass."

What it fixes. The yield lease (yield_lease.py) believed whoever asked. Its only credential was the
`holder` string in the request body, so anything on this Mac that could reach loopback held
Visionary off just by saying "discretion". On 2026-10-06 a training script Expurgate had left
behind (~/expurgate-scratch/train/harvest.py, orphaned for ten hours) did exactly that and renewed
it all day: about 10.5 of 15 awake hours idle, while Expurgate itself was not even running.

So the lease takes nothing on the request's word. The lease checks the requester itself on every take
and every renewal, and re-checks every few seconds while a lease is held:

  OPEN       Expurgate's engine — the one process listening on its port — is a child of the
             Expurgate app: the parent's executable path comes from the KERNEL (proc_pidpath), must
             be one of the installed locations (EXPURGATE_APPS), and the running parent must satisfy
             Expurgate's own code signature (EXPURGATE_REQUIREMENT: its bundle id plus the leaf of
             the "Discretion Local Signing" certificate). A look-alike bundle built anywhere else, or
             a renamed process, fails one of those.
  ITSELF     The request comes from that same engine: the program on the other end of THIS
             connection, matched by its exact address pair (both ends, any TCP state — a socket
             half-closed to dodge a lookup is still found, and still is not Expurgate). A script, a
             terminal, another app: refused. Visionary also listens for its phone remote, so a
             request that is not even from this Mac is refused before anything is looked up.
  ACTIVATED  Expurgate's own state says its automation is on — read over a connection whose far
             end has been confirmed to BE that engine, so nothing else can answer in its place.
             (Expurgate reports `running` today, which stays true while a deactivated Expurgate
             finishes the pass in hand; when it publishes `enabled` — armed — that is used instead.)

Every check FAILS CLOSED. A requester that cannot be identified, an Expurgate that does not answer, a
tool that errors: no new lease. A lease already held is revoked the moment Expurgate is closed,
replaced or deactivated, and after two misses in a row when a check merely could not complete (a
busy Expurgate, a slow lsof). Refusal costs Expurgate nothing it cannot live with: its side treats
every refusal as benign and simply works without the lease.

A running Expurgate is verified once per process (pid + start time): macOS kills a process whose
signed code changes under it, so what passed stays what runs — and `codesign` on a running pid also
compares the bundle on DISK, which an Expurgate rebuilt while open no longer matches (live
2026-10-06). One rebuilt before Visionary ever saw it is refused with "relaunch Expurgate".
A forked child that has not exec'd yet briefly shares the engine's sockets; it is recognised as the
engine's own (its parent IS the engine), and any other ambiguity is a check that could not complete.

If Expurgate is ever re-signed with a NEW certificate, update EXPURGATE_REQUIREMENT
(`codesign -d -r- Expurgate.app`) — until then its leases are refused, which is the safe direction.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time

EXPURGATE_PORT = 8766
EXPURGATE_APPS = (os.path.expanduser("~/discretion/Expurgate.app/Contents/MacOS/Expurgate"),
                  "/Applications/Expurgate.app/Contents/MacOS/Expurgate")
EXPURGATE_REQUIREMENT = ('=identifier "com.adambritsch.discretion" and certificate leaf = '
                         'H"3981f56e805ec9717227de1df80a82aa189659e8"')
STATE_TIMEOUT = 3.0
ACCEPT_WAIT = 2.0            # how long Expurgate may take to accept our state connection
LSOF = "/usr/sbin/lsof"
LSOF_FLAGS = ["-b", "-w", "-nP"]     # -b: never block in the kernel (a sick NAS mount can stall a
                                     # plain lsof for its whole timeout); -w: no warnings
PS = "/bin/ps"
CODESIGN = "/usr/bin/codesign"


class _Failed(object):
    """A check that could not COMPLETE (a tool failed or timed out) — soft, unlike "no"."""
    def __repr__(self):
        return "TOOL_FAILED"


TOOL_FAILED = _Failed()
MANY = "MANY"                # more than one process, none the parent of the rest
REBUILT = "REBUILT"          # the running app no longer matches its bundle on disk
_VERIFIED = {}               # (pid, start time) of a running Expurgate app that passed the check


def _under_test() -> bool:
    return "unittest" in sys.modules


def _run(cmd, timeout=5):
    """(returncode, stdout, stderr), or None when the command could not run or timed out."""
    if _under_test():
        raise RuntimeError("a test reached the real process table — mock expurgate_gate")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return None


_libproc = None


def exe_path(pid):
    """The executable a process is running, from the KERNEL (proc_pidpath), or None. Not argv[0]
    and not the process name: neither is evidence of anything."""
    global _libproc
    if not isinstance(pid, int) or pid <= 0 or _under_test():
        return None
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        n = _libproc.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(4096))
        return buf.value.decode("utf-8", "replace") if n > 0 else None
    except (OSError, AttributeError, ValueError):
        return None


def parent_pid(pid):
    r = _run([PS, "-o", "ppid=", "-p", str(int(pid))])
    if r is None:
        return TOOL_FAILED
    out = r[1].strip()
    return int(out) if out.isdigit() else None


def one_process(pids, ppid=None):
    """The one process among `pids`, counting a forked child that has not exec'd yet (its parent
    is another of them) as its parent's; None for none, MANY when that does not settle it."""
    pids = set(pids or ())
    if len(pids) <= 1:
        return pids.pop() if pids else None
    ppid = ppid or parent_pid
    for cand in pids:
        if all(ppid(p) == cand for p in pids - {cand}):
            return cand
    return MANY


def listener_pid(port=EXPURGATE_PORT):
    """The one process LISTENING on `port`; None (none), MANY, or TOOL_FAILED."""
    r = _run([LSOF] + LSOF_FLAGS + ["-iTCP:%d" % port, "-sTCP:LISTEN", "-Fp"])
    if r is None:
        return TOOL_FAILED
    return one_process({int(line[1:]) for line in r[1].splitlines()
                        if line[:1] == "p" and line[1:].isdigit()})


def proc_start(pid):
    """When `pid` started (ps lstart) — with the pid, one process incarnation; None if unknown."""
    r = _run([PS, "-o", "lstart=", "-p", str(int(pid))])
    out = r[1].strip() if r else ""
    return out or None


def signed_as_expurgate(pid):
    """True when the RUNNING process satisfies Expurgate's designated requirement; False when it
    does not; REBUILT when its bundle changed on disk before it was ever verified; TOOL_FAILED
    when codesign could not say. A pass is remembered for that process's lifetime."""
    start = proc_start(pid)
    if start is None:
        return TOOL_FAILED          # the process exists (its kernel path just answered): ps failed
    if _VERIFIED.get((pid, start)):
        return True
    r = _run([CODESIGN, "--verify", "-R", EXPURGATE_REQUIREMENT, str(int(pid))], timeout=10)
    if r is None:
        return TOOL_FAILED
    if r[0] == 0:
        if len(_VERIFIED) > 32:
            _VERIFIED.clear()
        _VERIFIED[(pid, start)] = True
        return True
    err = (r[2] or "").lower()
    if "does not match what is running" in err or "sealed resource" in err:
        return REBUILT
    return False


def _addr(ip, port) -> str:
    ip = str(ip).split("%", 1)[0]
    return ("[%s]:%d" if ":" in ip else "%s:%d") % (ip, int(port))


def parse_owners(lsof_fpn: str, name: str, own_pid: int) -> set:
    """PURE: the pids (other than ours) owning a socket whose lsof name is exactly `name`
    ("local->remote" from that socket's side)."""
    pid, found = None, set()
    for line in (lsof_fpn or "").splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            pid = int(line[1:])
        elif line[:1] == "n" and pid is not None and pid != own_pid and line[1:] == name:
            found.add(pid)
    return found


def owner_of(local, remote):
    """The process owning the socket (local -> remote), by its exact address pair and in any TCP
    state: a pid, None (nobody), MANY (not one process) or TOOL_FAILED (lsof could not say)."""
    r = _run([LSOF] + LSOF_FLAGS + ["-iTCP@%s" % _addr(*local), "-Fpn"])
    if r is None:
        return TOOL_FAILED
    return one_process(parse_owners(r[1], "%s->%s" % (_addr(*local), _addr(*remote)), os.getpid()))


def is_loopback(ip) -> bool:
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_loopback


def automation_state(engine_pid):
    """Expurgate's answer to "is your automation on", over a connection whose far end has been
    CONFIRMED to be `engine_pid`: True/False; None when it gave no answer (soft); MANY when
    something else answered on its port (hard)."""
    if _under_test():
        raise RuntimeError("a test reached Expurgate — mock expurgate_gate")
    try:
        s = socket.create_connection(("127.0.0.1", EXPURGATE_PORT), timeout=STATE_TIMEOUT)
    except OSError:
        return None
    try:
        mine = s.getsockname()[:2]
        theirs = ("127.0.0.1", EXPURGATE_PORT)
        deadline = time.time() + ACCEPT_WAIT
        while True:                                 # the engine's end exists once it accepts
            owner = owner_of(theirs, mine)
            if owner is TOOL_FAILED or owner is MANY:
                return None
            if owner is not None or time.time() >= deadline:
                break
            time.sleep(0.05)
        if owner is None:
            return None
        if owner != engine_pid:
            return MANY                             # someone ELSE answered on its port
        s.sendall(b"GET /api/state HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        raw = b"".join(chunks)
        head, _sep, body = raw.partition(b"\r\n\r\n")
        if not head.startswith(b"HTTP/") or b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
            return None
        auto = (json.loads(body.decode("utf-8", "replace")).get("automation") or {})
        # `enabled` = armed, if Expurgate publishes it: `running` stays true until the pass in
        # hand finishes after a Deactivate (review 2026-10-06), so armed is the better answer
        on = auto.get("enabled", auto.get("running"))
        return on if isinstance(on, bool) else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def check_engine(pid):
    """(ok, why, hard) for `pid` as Expurgate's engine, right now. `hard` = a fact that waiting
    will not change (closed, deactivated, an impostor, someone else asking); soft = a check that
    could not complete."""
    soft = (False, "could not check Expurgate just now", False)
    listener = listener_pid()
    if listener is TOOL_FAILED:
        return soft
    if listener is None:
        return False, "Expurgate is not open", True
    if listener is MANY:      # a fork mid-exec settles in milliseconds; a second listener does not
        return False, "more than one program is listening on Expurgate's port", False
    parent = parent_pid(listener)
    if parent is TOOL_FAILED:
        return soft
    path = exe_path(parent) if isinstance(parent, int) else None
    allowed = {os.path.realpath(p) for p in EXPURGATE_APPS}
    if not path or os.path.realpath(path) not in allowed:
        return False, "Expurgate is not open (its port is not Expurgate's own engine)", True
    signed = signed_as_expurgate(parent)
    if signed is TOOL_FAILED:
        return soft
    if signed is REBUILT:
        return False, "Expurgate was rebuilt since it opened — relaunch Expurgate", True
    if not signed:
        return False, "the app on Expurgate's port is not the real Expurgate (signature)", True
    if pid != listener:
        return False, "only Expurgate itself can ask for the machine — this came from another program", True
    state = automation_state(listener)
    if state is MANY:
        return False, "something besides Expurgate answered on its port", True
    if state is None:
        return False, "Expurgate did not say whether it is activated", False
    if not state:
        return False, "Expurgate is open but not activated", True
    return True, "", False


def verify_request(peer):
    """(ok, why, owner pid, hard) for a lease REQUEST. `peer` = (client address, our address) of
    the request's connection."""
    try:
        client, local = peer
        ip = client[0]
    except (TypeError, ValueError, IndexError):
        return False, "only Expurgate, on this Mac, can ask for the machine", None, True
    if not is_loopback(ip):
        return False, "only Expurgate, on this Mac, can ask for the machine", None, True
    pid = owner_of(client[:2], local[:2])
    if pid is TOOL_FAILED or pid is MANY:
        return False, "could not identify the program asking for the machine just now", None, False
    if pid is None:
        return False, "the program asking for the machine could not be identified", None, True
    ok, why, hard = check_engine(pid)
    return ok, why, pid, hard


def identify(peer):
    """(pid or None, sure) — who sent a request, for a RELEASE: only the lease's own engine may
    give it back. `sure` is False when the lookup could not complete (the caller then errs toward
    releasing: a wrongly refused release costs Visionary its machine for a whole lease)."""
    try:
        client, local = peer
        if not is_loopback(client[0]):
            return None, True
        pid = owner_of(client[:2], local[:2])
    except Exception:  # noqa: BLE001
        return None, False
    if pid is TOOL_FAILED or pid is MANY:
        return None, False
    return pid, True

