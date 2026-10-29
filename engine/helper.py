"""THE HELPER — the Mac's half (2026-10-28).

The capped x265 remux is the pipeline's bottleneck (~2.9 fps on the Mac). An OPTIONAL helper — any
Linux machine on Ethernet running the SAME x265 build (4.1, commit 1d117be; measured: a docked ROG
Ally under Bazzite at ~8 fps with bit-identical slices) — can take some of an item's segments while
the Mac takes others (dvcap.encode_capped_segmented's helper scheduler). Its SDR->PQ colour step is
NOT bit-identical, so the MAC always decodes + colour-converts and streams raw y4m (zstd -1, ~2.4x
lossless) to the helper, which only runs x265 and streams the HEVC straight back over the same ssh.
Everything the user sees calls it "the helper"; its own host name appears only in a tooltip.

User rules this side enforces (the helper polices itself too, independently):
  - used only when switched on (settings helper_enabled, default OFF; a malformed host, user or
    key is SAVED AS TYPED and switches the helper off with "settings: bad host" (etc.), logged
    once — never a silent fallback to the defaults);
  - only over a WIRED route (not Wi-Fi), checked on EVERY availability check and again right
    before every encode. The ssh goes to the IPv4 that check resolved, bound to its wired
    interface (BindInterface): a pulled cable ends the session instead of moving it to Wi-Fi
    (measured: bazzite.local resolved to an IPv6 link-local on Wi-Fi at 55 Mbps; the IPv4 over
    en12 gives 777 Mbps);
  - only when the helper's own `vworker status` says ok (mains, Ethernet, no game, nothing
    heavy, disk, not busy), speaks protocol PROTOCOL_VERSION and runs the SAME x265 build as
    this Mac;
  - a segment the helper drops goes back to the Mac; the helper never parks or fails an item,
    and with it off or unreachable the pipeline behaves exactly as before;
  - after the helper YIELDS or REFUSES (a game or a heavy app appeared) it sits out for COOLDOWN
    seconds;
  - another stage taking the machine (dvcap.suspend_encoders: AI outpainting, a sibling app's
    lease) KILLS the in-flight remote segment — "suspended": requeued, not counted, no cooldown —
    instead of freezing it.

Wire protocol v1 (both halves implement exactly this): ssh_argv + `vworker status | put <job>
<name> <bytes> | encode <job> <b64> | kill [<job>]`; see the docstrings below. The remote command
runs through the helper user's login shell, so anything its rc files print lands on stdout first:
`encode` writes STREAM_MAGIC (b"VWHEVC1\n") before the bitstream, and this side discards
everything before it, fails as "error" when it never comes, and checks that the first NAL after it
is an AUD or a VPS (_StreamFramer). `status` and a yield's or refusal's VWORKER line carry a
one-word reason "code" (HELPER_CODES) next to "why"; the UI line is classified on codes only
(an older vworker without them: status's structured fields, else "busy"). After a successful
put, ANY failure on this side before the encode's VWORKER line arrives is followed by a
best-effort `vworker kill <job>`. Nothing here raises: every failure is a (False, why) or a kind.

status_summary() is the UI's one line ("Helper connected · on seg 9 · 7.9 fps · did 10–11"); it
never blocks — a background refresher (started lazily while helper_enabled) keeps its verdict fresh.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import uuid

import dvcap

SSH = "/usr/bin/ssh"
ZSTD = "/opt/homebrew/bin/zstd"
ROUTE = "/sbin/route"
NETWORKSETUP = "/usr/sbin/networksetup"
VWORKER = "~/visionary-worker/bin/vworker"   # on the helper; its login shell expands the ~
PROTOCOL_VERSION = 1                          # the `vworker status` "version" this side speaks
RPU_NAME = "rpu.bin"

STATUS_TIMEOUT = 8          # `vworker status` answers in < 3 s; the rest is ssh's connect
PUT_TIMEOUT = 15            # one segment's RPU slice is a few hundred KB over gigabit
KILL_TIMEOUT = 5            # best effort after a failure — never hold anything up for long
CMD_TIMEOUT = 5             # route / networksetup / x265 --version
RESOLVE_TIMEOUT = 5         # getaddrinfo has no timeout of its own (mDNS can hang)
AVAIL_TTL = 60              # the ssh status probe is cached this long (the route never is)
COOLDOWN = 300              # after a "yielded"/"refused" the helper is left alone this long
PIPE_WAIT = 30              # after the stream ends, how long processes get to exit before a kill
STALL_SECS = 600            # no x265 progress line AND no HEVC growth this long -> killed, "disconnected"

STREAM_MAGIC = b"VWHEVC1\n"     # `vworker encode` writes this on stdout before the HEVC
JUNK_LIMIT = 1 << 20            # shell noise tolerated before STREAM_MAGIC; more is an error
REFRESH_SECS = 60               # status_summary's background refresher calls available() this often
NOTE_TTL = 30                   # a note_segments record older than this no longer reads as live

KINDS = ("ok", "yielded", "disconnected", "refused", "error", "aborted", "suspended")
_EXIT_KIND = {75: "yielded", 76: "disconnected", 255: "disconnected", 77: "refused"}
_VWORKER_RE = re.compile(r"^VWORKER\s+(\{.*\})\s*$")
_SUSPENDED_WHY = "suspended: another stage has the machine — the segment goes back in the queue"
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")

# REASON CODES: every availability verdict carries one word next to its `why`, and the UI line
# (summarize_verdict) is classified on that word ONLY — the `why` text names the configured host
# ("cannot resolve busybox.local ...") and the helper's apps, so matching words in it would
# misread a box called "wi-fi-bridge" or "gamebox". This side's own: "ready", "switched_off",
# "wifi" / "not_wired" / "unreachable" / "route_check" (wired_route_ok), "cooling", "mismatch"
# (protocol or x265 build), "error", "not_ready". The helper's (vworker REASON_CODES, sent as
# its status "code" and on a yield's or refusal's VWORKER line):
HELPER_CODES = frozenset({"battery", "no_ethernet", "game", "busy", "heavy", "low_disk",
                          "x265_missing", "zstd_missing", "error"})


# ---- ssh -------------------------------------------------------------------------------------

def ssh_argv(host: str, user: str, key: str, ip: str, iface: str, *remote) -> list:
    """PURE. The protocol's ssh command line running `vworker <remote...>` on the helper.
    It connects to `ip` — the IPv4 the wired-route check resolved `host` to (-4: never an IPv6
    link-local that rides Wi-Fi) — bound to `iface`, that route's wired interface, so pulling
    the cable kills the session instead of letting it fall back to Wi-Fi. `host` still names the
    helper's known_hosts entry (HostKeyAlias). Only the configured key is offered, and nothing
    from the user's ssh config can multiplex, jump or allocate a tty. The remote arguments are
    shell-quoted (the remote side joins them into one command line); every one the protocol
    uses (job ids, names, byte counts, base64) is shell-safe already, so quoting is a no-op for
    them. VWORKER stays unquoted so the remote shell expands its ~."""
    return [SSH, "-4", "-i", os.path.expanduser(key),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", f"HostKeyAlias={host}", "-o", f"BindInterface={iface}",
            "-o", "IdentitiesOnly=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ProxyJump=none", "-o", "RequestTTY=no",
            f"{user}@{ip}", VWORKER] + [shlex.quote(str(a)) for a in remote]


def _run(cmd: list, timeout: float, stdin=None) -> tuple:
    """(returncode|None, stdout, stderr). None = could not run or timed out. Never raises."""
    try:
        r = subprocess.run(cmd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, "", str(e)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _last_json(text: str):
    """The last line of `text` that parses as a JSON object, or None. The object may follow
    other text on its line: shell noise printed without a newline (an rc file's echo -n) lands
    in front of vworker's answer."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        at = line.find("{")
        while at >= 0:
            try:
                d = json.loads(line[at:])
            except ValueError:
                d = None
            if isinstance(d, dict):
                return d
            at = line.find("{", at + 1)
    return None


# ---- the HEVC stream's framing ---------------------------------------------------------------

_FIRST_NAL_TYPES = (32, 35)     # VPS, AUD (x265 runs with --aud: an AUD comes first)


def first_nal_verdict(data: bytes):
    """PURE. Does `data` (what followed STREAM_MAGIC) open with an Annex-B start code and an AUD
    or VPS NAL? True / False, or None while too few bytes have arrived to tell."""
    for code in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        if data.startswith(code):
            at = len(code)
            break
    else:
        if len(data) < 4 and any(c.startswith(data) for c in (b"\x00\x00\x00\x01", b"\x00\x00\x01")):
            return None
        return False
    if len(data) < at + 2:                     # the NAL header is two bytes
        return None
    head = data[at]
    return not head & 0x80 and (head >> 1) & 0x3F in _FIRST_NAL_TYPES


class _StreamFramer:
    """What `vworker encode` sends on stdout -> the HEVC part file. Everything before
    STREAM_MAGIC is shell noise (the helper user's rc files print through the same channel) and
    is discarded — up to JUNK_LIMIT bytes; the first bytes after it must open with an AUD or VPS
    NAL (first_nal_verdict). `feed` returns False once the stream cannot be trusted (`error`
    says why); `finish` judges a stream that ended. Only verified bytes reach `out`."""

    def __init__(self, out):
        self.out = out
        self.state = "junk"                    # junk -> head -> body, or bad
        self.buf = b""
        self.junk = 0
        self.error = None

    def feed(self, data: bytes) -> bool:
        if self.state == "body":
            self.out.write(data)
            self.out.flush()
            return True
        if self.state == "bad":
            return False
        self.buf += data
        if self.state == "junk":
            k = self.buf.find(STREAM_MAGIC)
            if k < 0:
                keep = len(STREAM_MAGIC) - 1   # a marker split across two reads
                if len(self.buf) > keep:
                    self.junk += len(self.buf) - keep
                    self.buf = self.buf[-keep:]
                if self.junk > JUNK_LIMIT:
                    return self._bad(f"the helper sent over {JUNK_LIMIT} bytes without its "
                                     f"stream marker")
                return True
            self.junk += k
            if self.junk > JUNK_LIMIT:             # the same bound when the marker came in this read
                return self._bad(f"the helper sent over {JUNK_LIMIT} bytes without its "
                                 f"stream marker")
            self.buf = self.buf[k + len(STREAM_MAGIC):]
            self.state = "head"
        verdict = first_nal_verdict(self.buf)
        if verdict is None:
            return True
        if not verdict:
            return self._bad("the helper's stream does not start with an AUD or VPS NAL")
        self.out.write(self.buf)
        self.out.flush()
        self.buf, self.state = b"", "body"
        return True

    def finish(self) -> None:
        """The stream ended: anything short of a verified body is an error (`error` set)."""
        if self.state == "junk" and self.error is None:
            self.error = "the helper's stream never started (no stream marker)"
        elif self.state == "head":
            if self.buf:
                self._bad("the helper's stream does not start with an AUD or VPS NAL")
            # magic and nothing after it: the frame/size checks call that what it is

    def _bad(self, why: str) -> bool:
        self.state, self.error = "bad", why
        self.buf = b""
        return False


# ---- is the Mac's route to the helper wired? ---------------------------------------------------

_STATE_LOCK = threading.Lock()


def _resolve_ipv4(host: str, timeout: float = RESOLVE_TIMEOUT):
    """The host's first IPv4 address, or None. Bounded: getaddrinfo runs on a daemon thread."""
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host or ""):
        return host
    box = {}

    def go():
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            box["ip"] = infos[0][4][0] if infos else None
        except Exception:  # noqa: BLE001
            box["ip"] = None
    t = threading.Thread(target=go, name="helper-resolve", daemon=True)
    t.start()
    t.join(timeout)
    return box.get("ip")


def parse_route_interface(text: str):
    """PURE. The `interface:` of `route -n get <ip>`, or None."""
    m = re.search(r"^\s*interface:\s*(\S+)", text or "", re.M)
    return m.group(1) if m else None


def parse_wifi_device(text: str):
    """PURE. The device of the port named Wi-Fi (AirPort on old macOS) in
    `networksetup -listallhardwareports`, or None when the Mac has no such port."""
    port = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port in ("Wi-Fi", "AirPort"):
            dev = line.split(":", 1)[1].strip()
            return dev or None
    return None


def _route_verdict(host: str, run, resolve) -> tuple:
    ip = resolve(host)
    if not ip:
        return False, f"cannot resolve {host} to an IPv4 address", None, None, "unreachable"
    rc, out, _err = run([ROUTE, "-n", "get", ip], CMD_TIMEOUT)
    iface = parse_route_interface(out) if rc == 0 else None
    if not iface:
        return False, f"no route to {host} ({ip})", ip, None, "unreachable"
    rc, out, _err = run([NETWORKSETUP, "-listallhardwareports"], CMD_TIMEOUT)
    if rc != 0:
        return False, "cannot list this Mac's network ports", ip, iface, "route_check"
    wifi = parse_wifi_device(out)
    if wifi and iface == wifi:
        return False, f"the route to {host} is Wi-Fi ({iface})", ip, iface, "wifi"
    if not (iface.startswith("en") or iface.startswith("bridge")):
        return False, f"the route to {host} is not wired ({iface})", ip, iface, "not_wired"
    return True, f"wired via {iface} ({ip})", ip, iface, "wired"


def wired_route_ok(host: str, *, run=None, resolve=None) -> tuple:
    """(ok, why, ip, iface, code): the Mac reaches `host`'s IPv4 `ip` over a wired interface
    `iface` — the route's interface is an en*/bridge* device that is NOT the Wi-Fi port's.
    `code` is the verdict as one word ("wired", "wifi", "not_wired", "unreachable",
    "route_check") — what status_summary classifies on; `why` (which names the host) is for
    logs and the tooltip only. Never cached: it is a few milliseconds, and a cached verdict
    could outlive a pulled cable. The ssh then goes to exactly this `ip` over exactly this
    `iface` (ssh_argv). Never raises."""
    try:
        return _route_verdict(host, run or _run, resolve or _resolve_ipv4)
    except Exception as e:  # noqa: BLE001
        return False, f"route check failed: {e}", None, None, "route_check"


# ---- this Mac's x265 -------------------------------------------------------------------------

_LOCAL_X265: dict = {}           # x265 path -> version token, once read (per process)


def parse_x265_version(text: str) -> str:
    """PURE. '4.1+1-1d117be' from `x265 --version`'s FIRST line ("x265 [info]: HEVC encoder
    version 4.1+1-1d117be"), or '' when that line names no version."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    m = re.search(r"\bversion\s+(\S+)", lines[0]) if lines else None
    return m.group(1) if m else ""


def local_x265_version(x265: str | None = None, *, run=None) -> str:
    """This Mac's x265 version token (the helper must run the SAME build), or '' when it cannot be
    read. A token, once read, is cached for the process. Never raises."""
    x265 = x265 or dvcap.X265
    with _STATE_LOCK:
        hit = _LOCAL_X265.get(x265)
    if hit:
        return hit
    try:
        rc, out, err = (run or _run)([x265, "--version"], CMD_TIMEOUT)
    except Exception:  # noqa: BLE001
        return ""
    # x265 logs on stderr; stdout is empty, so the first line of the two is stderr's first
    tok = parse_x265_version((out or "") + "\n" + (err or "")) if rc == 0 else ""
    if tok:
        with _STATE_LOCK:
            _LOCAL_X265[x265] = tok
    return tok


# ---- one `vworker status` answer, structurally --------------------------------------------------

def status_code(s: dict) -> str:
    """PURE. The reason code of a status answer that is not ok: its own "code" when it sends one
    (a word of HELPER_CODES), else derived from its structured fields in vworker's own order —
    battery, Ethernet, a game, another job, a heavy app — and "not_ready" for anything else (low
    disk, a missing x265 or zstd on an older vworker). Never reads the "why" text."""
    c = s.get("code")
    if isinstance(c, str) and c in HELPER_CODES:
        return c
    if s.get("ac") is False:
        return "battery"
    if s.get("ethernet") is False:
        return "no_ethernet"
    if s.get("game"):
        return "game"
    if s.get("busy") is True:
        return "busy"
    if s.get("heavy"):
        return "heavy"
    return "not_ready"


def heavy_only(s: dict) -> bool:
    """PURE. Is a heavy CPU/GPU reading the ONLY problem this status answer names? vworker
    decides "heavy" from ONE 1-s sample, so such an answer is never cached (available()): the
    next check asks again instead of sitting the helper out for AVAIL_TTL seconds."""
    if s.get("ok") is True or not s.get("heavy"):
        return False
    if s.get("ac") is not True or s.get("ethernet") is not True or s.get("game") \
            or s.get("busy") is True:
        return False
    c = s.get("code")
    return c is None or c == "heavy"


# ---- the helper ------------------------------------------------------------------------------

_AVAIL: dict = {}                # (host, user, key) -> {"t", "res": (ok, why, code), "cool",
                                 #   "cool_why", "cool_code"}
_SLOTS: dict = {}                # (host, user, key) -> Lock: ONE remote job per helper at a time
_LOGGED: set = set()             # messages for_remux / status_summary already logged (once each)
_LIVE: dict = {}                 # (host, user, key) -> how many encode_segment calls are in flight
_NOTES: dict = {}                # (host, user, key) -> (monotonic, the scheduler's last segments info)
_VERDICT: dict = {}              # (host, user, key) -> the refresher's last {"ok", "why", "code", "t"}
_LAST_ROUTE: dict = {}           # (host, user, key) -> (ip, iface) of the last wired-route verdict


def reset_state() -> None:
    """Forget every cached verdict, cooldown, slot, x265 token, logged message and status
    record, and stop the status refresher (tests; a settings change)."""
    _stop_refresher()
    with _STATE_LOCK:
        _AVAIL.clear()
        _SLOTS.clear()
        _LOCAL_X265.clear()
        _LOGGED.clear()
        _LIVE.clear()
        _NOTES.clear()
        _VERDICT.clear()
        _LAST_ROUTE.clear()


def _settings():
    import settings
    return settings


def _log(msg: str) -> None:
    try:
        import logbook
        logbook.event(msg)
    except Exception:  # noqa: BLE001 — logging never takes the remux down
        pass


def _log_once(msg: str) -> None:
    with _STATE_LOCK:
        if msg in _LOGGED:
            return
        _LOGGED.add(msg)
    _log(msg)


def _enabled_setting() -> bool:
    try:
        st = _settings()
        return st._valid_helper_enabled(st.get_settings().get("helper_enabled", False))
    except Exception:  # noqa: BLE001
        return False


class _RemotePipe:
    """One in-flight remote segment's three local processes (decoder, zstd, ssh), registered
    with dvcap's suspension machinery (dvcap.register_remote). Calling it — dvcap.suspend_encoders
    does, under its lock — KILLS them and marks the segment "suspended". A remote pipeline is
    never SIGSTOPped: a frozen stream would only read on the helper as a stalled link (it stops
    after its stall timer and deletes the job anyway) while the Mac's decoder held its memory."""

    def __init__(self, procs):
        self.procs = tuple(procs)
        self.suspended = threading.Event()

    def __call__(self) -> None:
        self.suspended.set()
        self.kill()

    def kill(self) -> None:
        for p in self.procs:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass


class Helper:
    """One configured helper. `argv(*remote)` builds the command that runs `vworker <remote...>`
    there over the last CHECKED wired route (ssh_argv by default; tests inject a local fake) —
    None while there is none, so nothing is ever sent over an unchecked route. `enabled()` is the
    live switch, re-read at every availability check so turning it off stops new remote work at
    the next segment boundary. `route_ok(host)` -> (ok, why, ip, iface[, code]) is
    wired_route_ok; `local_x265()` -> this Mac's x265 version token."""

    def __init__(self, host: str, user: str, key: str, *, argv=None, enabled=None,
                 route_ok=None, clock=None, local_x265=None):
        self.host, self.user, self.key = host, user, key
        self._argv = argv
        self._enabled = enabled or _enabled_setting
        self._route_ok = route_ok or wired_route_ok
        self._local_x265 = local_x265 or local_x265_version
        self._clock = clock or time.monotonic
        self._id = (host, user, key)
        self._route = None                       # (ip, iface) of the last wired verdict
        self._codes = {}                         # job -> the "code" of its VWORKER line

    def argv(self, *remote):
        if self._route is None:
            return None
        if self._argv is not None:
            return self._argv(*remote)
        ip, iface = self._route
        return ssh_argv(self.host, self.user, self.key, ip, iface, *remote)

    # -- availability --------------------------------------------------------------------------

    def check_route(self) -> tuple:
        """(ok, why): the wired-route check, run NOW (never cached). Its (ip, iface) is what
        argv() connects over; a failed check leaves no route, so nothing can be sent."""
        return self._check_route()[:2]

    def _check_route(self) -> tuple:
        """check_route's (ok, why) plus the verdict's reason code (wired_route_ok's; a route_ok
        that names none reads "wired" / "unreachable")."""
        try:
            r = tuple(self._route_ok(self.host))
            ok, why = bool(r[0]), str(r[1])
            ip, iface = (r[2], r[3]) if len(r) >= 4 else (None, None)
            code = r[4] if len(r) >= 5 and isinstance(r[4], str) else ""
        except Exception as e:  # noqa: BLE001
            ok, why, ip, iface, code = False, f"route check failed: {e}", None, None, "route_check"
        if ok and not (ip and iface):
            ok, why = False, f"the route check named no address and interface for {self.host}"
            code = "route_check"
        code = "wired" if ok else (code if code and code != "wired" else "unreachable")
        self._route = (str(ip), str(iface)) if ok else None
        with _STATE_LOCK:                       # status_summary's tooltip: "<host> · <ip> over Ethernet"
            if ok:
                _LAST_ROUTE[self._id] = self._route
            else:
                _LAST_ROUTE.pop(self._id, None)
        return ok, why, code

    def status(self):
        """The helper's `vworker status` JSON (its last JSON line), or None. Never raises."""
        cmd = self.argv("status")
        if cmd is None:
            return None
        rc, out, _err = _run(cmd, STATUS_TIMEOUT)
        if rc is None:
            return None
        return _last_json(out)

    def _judge(self, s: dict) -> tuple:
        """(ok, why, code) of one status answer: protocol PROTOCOL_VERSION, the helper's own ok
        (code: status_code), and the SAME x265 build as this Mac's."""
        v = s.get("version")
        if type(v) is not int or v != PROTOCOL_VERSION:
            return (False, f"the helper's vworker speaks protocol {v!r}, this Mac {PROTOCOL_VERSION}",
                    "mismatch")
        if s.get("ok") is not True:
            return False, str(s.get("why") or "not ready"), status_code(s)
        mine = self._local_x265()
        if not mine:
            return False, "cannot read this Mac's x265 version", "error"
        theirs = s.get("x265")
        if theirs != mine:
            return (False, f"x265 builds differ: the helper has {theirs!r}, this Mac {mine!r}",
                    "mismatch")
        return True, "ready", "ready"

    def available(self) -> tuple:
        """(ok, why): switched on, a wired route (checked on EVERY call), not cooling down, no
        other lane of this process holding the helper, and the helper's status passes _judge. Only
        the ssh status probe is cached (AVAIL_TTL seconds) — and never a "busy" answer, which
        changes the moment a job ends, nor a refusal whose ONLY problem is a heavy app (one
        1-s sample: a brief blip must not sideline the helper for a minute). Never raises."""
        return self.availability()[:2]

    def availability(self) -> tuple:
        """available()'s (ok, why) plus its reason code (see HELPER_CODES) — what the status
        refresher records and summarize_verdict classifies on. Never raises."""
        try:
            if not self._enabled():
                return False, "helper is off", "switched_off"
            ok, why, code = self._check_route()
            if not ok:
                return False, why, code
            now = self._clock()
            with _STATE_LOCK:
                st = _AVAIL.setdefault(self._id, {})
                if now < st.get("cool", 0.0):
                    return False, "cooling down", "cooling"
                slot = _SLOTS.get(self._id)
                if slot is not None and slot.locked():
                    # another lane of THIS process has the helper: its own job would only read as
                    # busy, and that answer must not be probed for or cached for anyone
                    return False, "busy: another remux is using the helper", "busy"
                if st.get("res") is not None and now - st.get("t", 0.0) < AVAIL_TTL:
                    return st["res"]
            s = self.status()
            if s is None:
                res, keep = (False, f"no answer from {self.host}", "unreachable"), True
            else:
                res = self._judge(s)
                keep = not (s.get("busy") is True or heavy_only(s))
            if keep:
                with _STATE_LOCK:
                    st = _AVAIL.setdefault(self._id, {})
                    st["res"], st["t"] = res, self._clock()
            return res
        except Exception as e:  # noqa: BLE001
            return False, f"helper check failed: {e}", "error"

    def _forget(self) -> None:
        """Drop the cached status verdict: the next check asks the helper again. No cooldown."""
        with _STATE_LOCK:
            _AVAIL.setdefault(self._id, {})["res"] = None

    def _note(self, kind: str, why: str = "", code=None) -> None:
        """A remote result's effect on availability: a yield or a refusal THE HELPER sent starts
        the cooldown (its `why` and reason `code` — its VWORKER line's, "busy" when it sent none —
        are kept for status_summary); a lost link or an error drops the cached verdict so the next
        check asks the helper again; ok, an abort and a suspension change nothing. A route check
        failing on this side is no refusal: encode_segment only _forget()s."""
        if kind in ("ok", "aborted", "suspended"):
            return
        with _STATE_LOCK:
            st = _AVAIL.setdefault(self._id, {})
            st["res"] = None
            if kind in ("yielded", "refused"):
                st["cool"] = self._clock() + COOLDOWN
                st["cool_why"] = str(why or kind)
                st["cool_code"] = code if code in HELPER_CODES else "busy"

    def note_segments(self, info) -> None:
        """dvcap's scheduler hands every segments update here too (the same dict as its
        on_segments): status_summary's "on seg 9 · 7.9 fps · did 10–11" is read from it."""
        with _STATE_LOCK:
            _NOTES[self._id] = (time.monotonic(), info)

    def claim(self) -> bool:
        """Take this helper's single job slot (non-blocking). Two finisher lanes share one helper:
        the second waits instead of being refused as busy."""
        with _STATE_LOCK:
            lock = _SLOTS.setdefault(self._id, threading.Lock())
        return lock.acquire(blocking=False)

    def release(self) -> None:
        with _STATE_LOCK:
            lock = _SLOTS.get(self._id)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass

    def kill_job(self, job=None) -> None:
        """Best effort, bounded: `vworker kill <job>` (that job's processes stopped, its dir
        deleted); with no job, `vworker kill` (every job). Never raises."""
        try:
            cmd = self.argv("kill", job) if job else self.argv("kill")
            if cmd is not None:
                _run(cmd, KILL_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass

    # -- one segment ---------------------------------------------------------------------------

    def encode_segment(self, dec_cmd: list, x265_args: list, rpu_path, frames: int,
                       out_part: str, abort=None, on_frame=None) -> tuple:
        """Encode ONE segment on the helper: dec_cmd (the Mac's decode + colour step, y4m on
        stdout) | zstd -1 | ssh `vworker encode` -> HEVC into `out_part`. `x265_args` carry the
        "{rpu}"/"{out}" placeholders; `rpu_path` (None for SDR) is uploaded first as rpu.bin.
        The wired route is checked again right here (not sent at all over anything else), and
        nothing starts while dvcap.encoders_suspended(). Returns (got|None, reason, kind), kind in
        KINDS. The pipeline is registered with dvcap.register_remote: a suspension kills it as
        "suspended". Once the helper holds anything of the job, any failure before its VWORKER line
        is followed by `vworker kill <job>`. Nothing is left running, and `out_part` is removed
        unless ok. Only a yield or refusal the HELPER sends starts the cooldown; a route check
        failing here (nothing sent) is "refused" to the scheduler but only drops the cached
        verdict."""
        frames = int(frames)
        if abort is not None and abort.is_set():
            return None, "aborted", "aborted"
        if dvcap.encoders_suspended():
            return None, _SUSPENDED_WHY, "suspended"
        ok, why = self.check_route()
        if not ok:
            # a Mac-side check (often a transient mDNS or networksetup timeout right after
            # available() passed the same check), not the helper refusing: no cooldown — the
            # cached verdict goes, so the next availability check asks again
            self._forget()
            _wake_refresher()
            return None, f"not sent: {why}", "refused"
        job = uuid.uuid4().hex
        with _STATE_LOCK:                       # status_summary: "connected" while this runs
            _LIVE[self._id] = _LIVE.get(self._id, 0) + 1
        try:
            got, reason, kind = self._send(job, dec_cmd, x265_args, rpu_path, frames, out_part,
                                           abort, on_frame)
        except Exception as e:  # noqa: BLE001 — nothing here raises; the Mac takes the segment
            self.kill_job(job)                  # whatever the helper may hold of it goes too
            got, reason, kind = None, f"helper failed: {e}", "error"
        finally:
            with _STATE_LOCK:
                n = _LIVE.get(self._id, 0) - 1
                if n > 0:
                    _LIVE[self._id] = n
                else:
                    _LIVE.pop(self._id, None)
        code = self._codes.pop(job, None)
        if kind == "unsent":                    # the checked route was gone: nothing was sent
            self._forget()
            kind = "refused"
        else:
            self._note(kind, reason, code)
        if kind not in ("ok", "aborted", "suspended"):
            _wake_refresher()                   # status_summary: say why without waiting a minute
        if kind != "ok":
            dvcap._rm(out_part)
        return got, reason, kind

    def _send(self, job, dec_cmd, x265_args, rpu_path, frames, out_part, abort, on_frame):
        """encode_segment's body once the job has an id: the RPU put, then the stream, then the
        best-effort `vworker kill <job>` when the helper may still hold anything of the job."""
        handed = False                          # the helper holds this job's RPU
        if rpu_path:
            why, kind, staged = self._put(job, rpu_path, abort)
            if kind != "ok":
                if staged:
                    self.kill_job(job)          # it answered, but not with the whole slice
                return None, why, kind
            handed = True
        payload = base64.b64encode(json.dumps({"x265_args": [str(a) for a in x265_args],
                                               "frames": frames},
                                              separators=(",", ":")).encode()).decode("ascii")
        got, reason, kind, started, answered = self._stream(dec_cmd, payload, job, frames,
                                                            out_part, abort, on_frame)
        if kind != "ok" and not answered and (handed or started):
            self.kill_job(job)                  # the helper may still hold the job: never leave it
        return got, reason, kind

    def _put(self, job: str, path: str, abort) -> tuple:
        """`vworker put <job> rpu.bin <bytes>` with the file on stdin -> (why, kind, staged);
        `staged` = the put exited 0, so the helper may hold the file even when this side rejects
        the answer. Killed on an abort (the caller's, or the scheduler stopping this call), on a
        suspension, and after PUT_TIMEOUT seconds (a link that cannot move a few hundred KB is
        a lost one); a killed put leaves nothing behind — vworker deletes a short read."""
        try:
            f = open(path, "rb")
        except OSError as e:
            return f"rpu upload: {e}", "error", False
        with f:
            try:
                size = os.fstat(f.fileno()).st_size
            except OSError as e:
                return f"rpu upload: {e}", "error", False
            cmd = self.argv("put", job, RPU_NAME, size)
            if cmd is None:
                return "rpu upload: no checked wired route", "refused", False
            try:
                p = subprocess.Popen(cmd, stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, errors="replace")
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                return f"rpu upload could not start: {e}", "error", False
            deadline = time.monotonic() + PUT_TIMEOUT
            stop, out, err = None, "", ""
            while True:
                try:
                    out, err = p.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if abort is not None and abort.is_set():
                        stop = "aborted"
                    elif dvcap.encoders_suspended():
                        stop = "suspended"
                    elif time.monotonic() >= deadline:
                        stop = "timeout"
                except (OSError, ValueError) as e:
                    stop = f"error: {e}"
                if stop:
                    try:
                        p.kill()
                    except OSError:
                        pass
                    try:
                        p.communicate(timeout=KILL_TIMEOUT)
                    except (subprocess.SubprocessError, OSError, ValueError):
                        pass
                    break
        if stop == "aborted":
            return "aborted", "aborted", False
        if stop == "suspended":
            return _SUSPENDED_WHY, "suspended", False
        if stop == "timeout":
            return f"rpu upload timed out after {PUT_TIMEOUT} s", "disconnected", False
        if stop:
            return f"rpu upload {stop}", "error", False
        rc = p.returncode
        if rc == 0:
            # the protocol's answer is {"ok": true, "bytes": <size>, ...}: anything else (no
            # answer, a refusal, another byte count) is not a staged slice this side can trust
            reply = _last_json(out) or {}
            n = reply.get("bytes")
            if reply.get("ok") is not True or type(n) is not int or n != size:
                return f"rpu upload: the helper stored {n!r} of {size} bytes", "error", True
            return "ok", "ok", True
        kind = "disconnected" if rc == 255 else "error"
        return f"rpu upload failed (rc={rc}): {_last_line(err)}", kind, False

    def _stream(self, dec_cmd, payload, job, frames, out_part, abort, on_frame) -> tuple:
        """-> (got|None, reason, kind, started, answered): `started` = the ssh ran, `answered`
        = its VWORKER line arrived. ssh's stdout goes through a _StreamFramer (shell noise
        before STREAM_MAGIC dropped, the first NAL checked) into `out_part`. A stream that
        completed and passed every check is "ok" even when an abort, a suspension or the stall
        timer lands after it ended — that segment is done and is kept."""
        procs, out, pipe = [], None, None
        aborted, stalled, done = threading.Event(), threading.Event(), threading.Event()
        tail, final, registered = [], None, False
        seen = [time.monotonic(), -1]        # the last sign of progress: [when, out_part's size]
        framer, pump = None, None
        pass_fps = on_frame is not None and dvcap._takes_kw(on_frame, "fps")
        cmd = self.argv("encode", job, payload)
        if cmd is None:                      # "unsent": encode_segment reads it as "refused"
            return None, "not sent: no checked wired route", "unsent", False, False
        try:
            try:
                out = open(out_part, "wb")
                framer = _StreamFramer(out)
                dec = subprocess.Popen(dec_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
                procs.append(dec)
                zst = subprocess.Popen([ZSTD, "-1", "-T4", "-q", "-c"], stdin=dec.stdout,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                procs.append(zst)
                dec.stdout.close()
                ssh = subprocess.Popen(cmd, stdin=zst.stdout, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
                procs.append(ssh)
                zst.stdout.close()
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                return None, f"could not start the helper pipe: {e}", "error", False, False
            pipe = _RemotePipe((dec, zst, ssh))
            registered = dvcap.register_remote(pipe)
            if not registered:
                pipe()                       # a suspension landed between the check and the spawn

            def pump_out():
                # ssh's stdout -> the framer -> out_part; an untrustworthy stream is killed
                try:
                    while True:
                        data = ssh.stdout.read1(1 << 20)
                        if not data:
                            return
                        if not framer.feed(data):
                            pipe.kill()
                            return
                except Exception as e:  # noqa: BLE001
                    if framer.error is None:
                        framer._bad(f"reading the helper's stream failed: {e}")
                    pipe.kill()
            pump = threading.Thread(target=pump_out, name="helper-hevc", daemon=True)
            pump.start()

            def watch():
                # an abort must land even while nothing arrives (a stalled link); and a stream
                # with no progress line and no HEVC growth for STALL_SECS is a dead link — the
                # helper's own stall timer is the other half of this safety net
                while not done.wait(0.2):
                    if abort is not None and abort.is_set():
                        aborted.set()
                        pipe.kill()
                        return
                    try:
                        size = os.fstat(out.fileno()).st_size
                    except (OSError, ValueError):
                        size = seen[1]
                    now = time.monotonic()
                    if size != seen[1]:
                        seen[0], seen[1] = now, size
                    elif now - seen[0] > STALL_SECS:
                        stalled.set()
                        pipe.kill()
                        return
            threading.Thread(target=watch, name="helper-watch", daemon=True).start()
            try:
                err = io.TextIOWrapper(ssh.stderr, encoding="utf-8", errors="replace")
                for line in err:
                    for chunk in line.replace("\r", "\n").splitlines():
                        m = _VWORKER_RE.match(chunk.strip())
                        if m:
                            try:
                                final = json.loads(m.group(1))
                            except ValueError:
                                pass
                            if isinstance(final, dict):     # a yield's/refusal's reason code
                                self._codes[job] = final.get("code")
                            continue
                        tail.append(chunk)
                        if len(tail) > 20:
                            tail.pop(0)
                        n = dvcap.parse_x265_progress(chunk)
                        if n is not None:
                            seen[0] = time.monotonic()
                            if on_frame:
                                if pass_fps:
                                    fm = _FPS_RE.search(chunk)
                                    on_frame(n, fps=float(fm.group(1)) if fm else None)
                                else:
                                    on_frame(n)
                for p in (ssh, zst, dec):
                    if p is not ssh and ssh.returncode != 0 and p.poll() is None:
                        p.kill()                   # the stream is discarded: don't let the decoder
                                                   # run on until it hits the broken pipe
                    try:
                        p.wait(timeout=PIPE_WAIT)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                pump.join(PIPE_WAIT)
            except Exception as e:  # noqa: BLE001 — a callback or pipe failure is an error result
                answered = final is not None
                if aborted.is_set():
                    return None, "aborted", "aborted", True, answered
                if pipe.suspended.is_set():
                    return None, _SUSPENDED_WHY, "suspended", True, answered
                return None, f"helper pipe error: {e}", "error", True, answered
        finally:
            done.set()
            if registered:
                dvcap.unregister_remote(pipe)
            for p in procs:                                # nothing leaves here running
                try:
                    if p.poll() is None:
                        p.kill()
                    p.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            if pump is not None:
                pump.join(10)
            for p in procs:
                for stream in (p.stdout, p.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:  # noqa: BLE001
                        pass
            if out is not None:
                try:
                    out.close()
                except OSError:
                    pass
        answered = final is not None
        rc = ssh.returncode
        if pump.is_alive():
            framer._bad("the helper's stream did not end with the ssh session")
        framer.finish()
        if framer.state == "bad" or (rc == 0 and framer.error):
            # garbage on the channel (or no stream at all from an "ok" exit): never trusted
            return None, f"error: {framer.error}", "error", True, answered
        res = self._result(rc, final, tail, frames, out_part, dec.returncode, zst.returncode)
        if res[2] == "ok":
            return res + (True, answered)    # finished and verified before any stop landed
        if aborted.is_set():
            return None, "aborted", "aborted", True, answered
        if rc != 0 and pipe.suspended.is_set():
            return None, _SUSPENDED_WHY, "suspended", True, answered
        if rc != 0 and stalled.is_set():
            return (None, f"disconnected: no progress from the helper for {STALL_SECS:g} s",
                    "disconnected", True, answered)
        return res + (True, answered)

    @staticmethod
    def _result(rc, final, tail, frames, out_part, dec_rc, zst_rc) -> tuple:
        """PURE-ish. (got|None, reason, kind) from ssh's exit code and the VWORKER line."""
        final = final if isinstance(final, dict) else {}
        why = str(final.get("why") or "") or " / ".join(tail[-3:])
        fr = final.get("frames")
        got = fr if isinstance(fr, int) and not isinstance(fr, bool) else \
            dvcap.parse_x265_encoded("\n".join(tail))
        if rc == 0:
            size = os.path.getsize(out_part) if os.path.exists(out_part) else 0
            if got == frames and size > 0:
                return got, "ok", "ok"
            return None, f"the helper said ok but sent {got} of {frames} frames ({size} bytes)", "error"
        kind = _EXIT_KIND.get(rc, "error")
        # The local exit codes go in the REASON only: a local decode failure truncates the
        # stream (the helper reads it as a lost link), but ffmpeg also exits non-zero on the broken
        # pipe left when the helper stops reading — the code cannot tell the two apart. Either way
        # the segment goes back to the Mac, whose own decode reports a real failure properly.
        local = "".join(f" · Mac {name} rc={lrc}" for name, lrc in (("decoder", dec_rc), ("zstd", zst_rc))
                        if isinstance(lrc, int) and lrc > 0)   # < 0: our own kill
        return None, f"{kind} (rc={rc}): {why}".rstrip(": ") + local, kind


_FIELD_ORDER = ("helper_host", "helper_user", "helper_key")


def _configured() -> tuple:
    """(enabled, {field: value}, problem) from the settings, read once. `problem` is "" or what
    status_summary shows after "settings:" ("bad host", "bad user, key", ...); a problem is
    logged once (per distinct one) and never replaced by a default. May raise (unreadable)."""
    st = _settings()
    s = st.get_settings()
    vals = {k: s.get(k, st.DEFAULT_SETTINGS[k]) for k in _FIELD_ORDER}
    if not st._valid_helper_enabled(s.get("helper_enabled", False)):
        return False, vals, ""
    bad = [k for k in _FIELD_ORDER if not st.helper_field_ok(k, vals[k])]
    if bad:
        _log_once("Helper not used: invalid " + ", ".join(f"{k}={vals[k]!r}" for k in bad)
                  + " in settings — fix it in Settings")
        return True, vals, "bad " + ", ".join(st.HELPER_FIELDS[k][0] for k in bad)
    return True, vals, ""


def for_remux():
    """The configured helper for one remux, or None: helper_enabled off, settings
    unreadable, or a malformed helper_host / helper_user / helper_key — which is logged once
    (per distinct problem) and never silently replaced by a default. Never raises."""
    try:
        enabled, vals, problem = _configured()
        if not enabled or problem:
            return None
        return Helper(vals["helper_host"], vals["helper_user"], vals["helper_key"])
    except Exception:  # noqa: BLE001
        return None


# ---- status_summary: the UI's one line ---------------------------------------------------------

_REFRESH: dict = {"thread": None, "stop": None, "wake": None}


def _summary_helper(vals: dict) -> "Helper":
    """The Helper the status refresher asks (tests replace it: nothing may reach a host)."""
    return Helper(vals["helper_host"], vals["helper_user"], vals["helper_key"])


def _slot_held(hid) -> bool:
    with _STATE_LOCK:
        lock = _SLOTS.get(hid)
    return lock is not None and lock.locked()


def _refresh_loop(stop: threading.Event, wake: threading.Event) -> None:
    """Keeps _VERDICT fresh: available() now, then every REFRESH_SECS (sooner on a wake), until
    the helper is switched off, its settings go bad, or `stop`. available() itself never asks
    the helper while it is cooling down or this process holds its job slot."""
    try:
        while not stop.is_set():
            try:
                enabled, vals, problem = _configured()
            except Exception:  # noqa: BLE001
                enabled, vals, problem = False, None, ""
            if not enabled or problem:
                return
            h = _summary_helper(vals)
            if _slot_held(h._id):
                ok, why, code = True, "ready", "ready"   # this process is encoding on it right now
            else:
                try:
                    ok, why, code = h.availability()
                except Exception as e:  # noqa: BLE001
                    ok, why, code = False, f"helper check failed: {e}", "error"
            if stop.is_set():
                return
            with _STATE_LOCK:
                _VERDICT[h._id] = {"ok": bool(ok), "why": str(why), "code": str(code or ""),
                                   "t": time.monotonic()}
            wake.wait(REFRESH_SECS)
            wake.clear()
    finally:
        with _STATE_LOCK:
            if _REFRESH.get("stop") is stop:
                _REFRESH["thread"] = None


def _ensure_refresher() -> None:
    """Start the status refresher unless one is registered and alive. The thread is started
    UNDER the lock: a second caller can never see a registered-but-not-yet-started thread as
    dead and start another, which _stop_refresher (stopping only the registered one) would leave
    probing. Safe: start() returns once the thread runs, before the loop takes the lock."""
    with _STATE_LOCK:
        t = _REFRESH.get("thread")
        if t is not None and t.is_alive():
            return
        stop, wake = threading.Event(), threading.Event()
        t = threading.Thread(target=_refresh_loop, args=(stop, wake), name="helper-status",
                             daemon=True)
        _REFRESH.update(thread=t, stop=stop, wake=wake)
        t.start()


def _wake_refresher() -> None:
    with _STATE_LOCK:
        wake = _REFRESH.get("wake")
    if wake is not None:
        wake.set()


def _stop_refresher() -> None:
    with _STATE_LOCK:
        stop, wake = _REFRESH.get("stop"), _REFRESH.get("wake")
        _REFRESH.update(thread=None, stop=None, wake=None)
    for ev in (stop, wake):
        if ev is not None:
            ev.set()


def _ranges(nums) -> str:
    """PURE. [8, 10, 11] -> "8, 10–11"."""
    out, run = [], []
    for n in sorted(set(int(x) for x in nums)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(str(run[0]) if len(run) == 1 else f"{run[0]}–{run[-1]}")
        run = [n]
    if run:
        out.append(str(run[0]) if len(run) == 1 else f"{run[0]}–{run[-1]}")
    return ", ".join(out)


def connected_detail(info) -> str:
    """PURE. "on seg 9 · 7.9 fps · did 10–11" from a dvcap on_segments dict (segment numbers
    shown from 1); "" when nothing is known yet."""
    info = info if isinstance(info, dict) else {}
    parts = []
    cur = info.get("helper")
    if isinstance(cur, (list, tuple)) and cur:
        try:
            parts.append(f"on seg {int(cur[0]) + 1}")
            fps = float(cur[2]) if len(cur) > 2 and cur[2] is not None else 0.0
            if fps > 0:
                parts.append(f"{fps:.1f} fps")
        except (TypeError, ValueError):
            pass
    try:
        did = _ranges(int(i) + 1 for i in (info.get("helper_done") or ()))
    except (TypeError, ValueError):
        did = ""
    if did:
        parts.append(f"did {did}")
    return " · ".join(parts)


_LINES = {                       # reason code -> (state, the words after the dot)
    "game": ("paused", "a game is running on it"),
    "busy": ("paused", "it's busy"),
    "heavy": ("paused", "it's busy"),
    "low_disk": ("paused", "it's low on disk space"),
    "wifi": ("off", "the Mac is on Wi-Fi"),
    "no_ethernet": ("off", "it's not on Ethernet"),
    "battery": ("off", "it's on battery"),
    "switched_off": ("off", "switched off"),
}


def summarize_verdict(ok: bool, code: str, cool_code: str = "") -> tuple:
    """PURE. (state, detail) for a not-in-flight helper from its last availability verdict's
    REASON CODE (never its `why` text, which names the host): "ready"/"paused"/"off"/
    "not_connected" and the words after the dot. "cooling" reads as what started the cooldown
    (`cool_code`), paused when that is no paused/off line. Anything it cannot place reads
    "not_connected" with no detail (the tooltip carries the technical reason)."""
    if ok:
        return "ready", "waiting for the next segment"
    if code == "cooling":
        state, detail = summarize_verdict(False, cool_code or "busy")
        return (state, detail) if state in ("paused", "off") else ("paused", "it's busy")
    return _LINES.get(str(code or ""), ("not_connected", ""))


def status_summary() -> dict:
    """The helper's one status line for the UI — NEVER blocks: no ssh and no route check here;
    it reads what the background refresher (started here, lazily, while helper_enabled; it
    polls available() every REFRESH_SECS and stops when switched off) and the live remux last
    recorded. {"enabled", "state": connected|ready|paused|off|not_connected, "detail": the words
    after the dot, "host": the configured host name, "tooltip": "<host> · <IPv4> over Ethernet"
    (or "<host> · <the technical reason>")}. "connected" while a remote segment is in flight.
    Never raises."""
    try:
        enabled, vals, problem = _configured()
    except Exception:  # noqa: BLE001
        enabled, vals, problem = False, None, ""
    host = str((vals or {}).get("helper_host") or "")

    def line(state, detail, tooltip=None):
        return {"enabled": bool(enabled), "state": state, "detail": detail, "host": host,
                "tooltip": tooltip if tooltip is not None else host}
    try:
        if not enabled:
            return line("off", "switched off")
        if problem:
            return line("off", f"settings: {problem}")
        hid = (vals["helper_host"], vals["helper_user"], vals["helper_key"])
        _ensure_refresher()
        now = time.monotonic()
        with _STATE_LOCK:
            live = _LIVE.get(hid, 0) > 0
            note = _NOTES.get(hid)
            verdict = dict(_VERDICT.get(hid) or {})
            av = dict(_AVAIL.get(hid) or {})
            route = _LAST_ROUTE.get(hid)
        wired = f"{host} · {route[0]} over Ethernet" if route else host
        if live:
            info = note[1] if note and now - note[0] < NOTE_TTL else None
            return line("connected", connected_detail(info), wired)
        if now < av.get("cool", 0.0):
            state, detail = summarize_verdict(False, "cooling", av.get("cool_code", ""))
            return line(state, detail, wired if route else f"{host} · {av.get('cool_why', '')}")
        if not verdict:
            return line("not_connected", "", f"{host} · checking")
        state, detail = summarize_verdict(verdict.get("ok"), verdict.get("code", ""),
                                          av.get("cool_code", ""))
        if state in ("ready", "paused"):
            return line(state, detail, wired)
        why = verdict.get("why", "")
        return line(state, detail, f"{host} · {why}" if why else host)
    except Exception:  # noqa: BLE001
        return line("not_connected", "")
