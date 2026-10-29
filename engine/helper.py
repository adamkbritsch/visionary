"""THE HELPER — the Mac's half (2026-10-28).

The capped x265 remux is the pipeline's bottleneck (~2.9 fps on the Mac). An OPTIONAL helper — any
Linux machine on Ethernet running the SAME x265 build as this Mac (measured on a test helper, a
handheld PC on a wired dock: ~8 fps with bit-identical slices) — can take some of an item's
segments while the Mac takes others (dvcap.encode_capped_segmented's helper scheduler). Its SDR->PQ
colour step is NOT bit-identical, so the MAC always decodes + colour-converts and streams raw y4m
(zstd -1, ~2.4x lossless) to the helper, which only runs x265 and streams the HEVC straight back
over the same ssh. Everything the user sees calls it "the helper"; its own host name appears only
in a tooltip.

User rules this side enforces (the helper polices itself too, independently):
  - used only when switched on (settings helper_enabled, default OFF) and set up: an empty host
    or user (the shipped defaults) reads "not set up", with nothing logged; a malformed host,
    user or key is SAVED AS TYPED and switches the helper off with "settings: bad host" (etc.),
    logged once — never a silent fallback to the defaults;
  - only over a WIRED route (not Wi-Fi), checked on EVERY availability check and again right
    before every encode. The Mac's interface is chosen the way nas_link chooses one for the NAS:
    an active, non-Wi-Fi interface on the helper's subnet, in the Mac's service order — so a
    Wi-Fi service ranked above the cable does not matter; only with no such interface does the
    route table decide. The ssh goes to the IPv4 that check used, bound to that interface
    (BindInterface): a pulled cable ends the session instead of moving it to Wi-Fi (measured:
    a helper's .local name resolved to an IPv6 link-local on Wi-Fi at 55 Mbps; its IPv4 over a
    USB Ethernet adapter gives 777 Mbps). Once the helper has reported its own wired IPv4
    addresses (status "ipv4") only those are used — a name that also resolves to the helper's
    Wi-Fi address can no longer pick that one;
  - only when the helper's own `vworker status` says ok (mains, Ethernet, no game, nothing
    heavy, disk, not busy), speaks protocol PROTOCOL_VERSION and runs the SAME x265 build as
    this Mac;
  - a segment the helper drops goes back to the Mac; the helper never parks or fails an item,
    and with it off, not set up or unreachable the pipeline behaves exactly as before;
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

SETUP (Settings > Setup > Helper): setup_status() is GET /api/helper/setup — the login, the wired
route, the installed worker, the x265 build and the helper's own verdict, each in plain words
with the one action that fixes it; it runs only when asked (cached SETUP_TTL seconds). The fixes
are setup_jobs targets whose argv setup_job_argv() builds from the SAVED settings only: a new ssh
key (never over an existing one), helper_worker/install.sh, and build-x265.sh run on the helper
for exactly this Mac's x265 version.
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
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
import nas_link

SSH = "/usr/bin/ssh"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
ZSTD = "/opt/homebrew/bin/zstd"
ROUTE = "/sbin/route"
IFCONFIG = "/sbin/ifconfig"
NETWORKSETUP = "/usr/sbin/networksetup"
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))   # the bundle's engine (the repo's in a checkout)
WORKER_DIR = os.path.join(ENGINE_DIR, "helper_worker")    # vworker.py, install.sh, build-x265.sh
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
SETUP_TTL = 30                  # setup_status's checks are reused this long (fresh=1 runs them again)
LOGIN_TIMEOUT = 15              # setup's login probe: ssh's 8-s connect plus `vworker status`

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


# what an ssh that did not log in printed -> (kind, the words Setup shows, Setup's action). Checked
# in this order: a missing/unusable key file is warned about before the "Permission denied" it
# causes, and a host key problem stops ssh before it tries any key.
_SSH_FAILURES = (
    ("host_key_changed", ("remote host identification has changed",),
     "the helper's host key has changed since this Mac last met it - if it was reinstalled, "
     "run: ssh-keygen -R {host}", ""),
    ("host_key", ("host key verification failed", "host key is known"),
     "this Mac hasn't met the helper yet - run the command below once", "authorize"),
    ("key_missing", ("no such identity", "not accessible"), "no SSH key yet - Create key", ""),
    ("key_perms", ("unprotected private key", "bad permissions"),
     "the SSH key file can be read by others, so ssh ignores it - make it private: "
     "chmod 600 {key}", ""),
    ("key_bad", ("invalid format", "load key"), "the SSH key file can't be used: {last}", ""),
    ("auth", ("permission denied", "too many authentication failures"),
     "the key isn't authorised on the helper - run the command below", "authorize"),
    ("refused", ("connection refused",),
     "{host} answered, but its SSH server is off - turn on sshd there", ""),
    ("network", ("could not resolve", "name or service not known", "nodename nor servname",
                 "no route to host", "network is unreachable", "host is down", "timed out",
                 "host is unreachable"),
     "no answer from {host}", ""),
)
# the same failures in the remux line's tooltip (Setup has the fix)
_SEE_SETUP = " - see Settings > Setup > Helper"
_SSH_TIPS = {"host_key": "this Mac hasn't met the helper yet" + _SEE_SETUP,
             "host_key_changed": "the helper's host key has changed" + _SEE_SETUP,
             "auth": "the key isn't authorised on the helper" + _SEE_SETUP,
             "key_missing": "no SSH key yet" + _SEE_SETUP,
             "refused": "{host} answered, but its SSH server is off"}


def ssh_failure(rc, err: str, host: str, key: str = "") -> tuple:
    """PURE. (kind, detail, action) for an ssh that did NOT log in (rc 255, or None: it could not
    run or timed out), classified from its stderr into plain words; `action` is Setup's fix
    ("authorize" = the ssh-copy-id command, else ""). Anything unrecognised is "no answer from
    <host>" when ssh printed nothing, else "the login failed: <its last line>"."""
    e = (err or "").lower()
    last = _last_line(err)
    for kind, needles, words, action in _SSH_FAILURES:
        if any(n in e for n in needles):
            return kind, words.format(host=host, key=key, last=last), action
    if rc is None or not last:
        return "network", f"no answer from {host}", ""
    return "other", f"the login failed: {last}", ""


def _no_answer_why(host: str, rc, err: str) -> str:
    """availability()'s `why` when a status probe got no answer: "no answer from <host>", or —
    when ssh's stderr says the login itself failed — that, in words (the tooltip shows it). An
    ssh that DID log in (any exit but 255) and still got no status: the worker is missing."""
    if rc is not None and rc != 255:
        return "the helper's worker did not answer" + _SEE_SETUP
    kind, detail, _action = ssh_failure(rc, err, host)
    tip = _SSH_TIPS.get(kind)
    if tip:
        return tip.format(host=host)
    return detail if kind == "other" else f"no answer from {host}"


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


def _mac_links(run) -> tuple:
    """(ifaces, ports, order, ports_ok): this Mac's interfaces, hardware ports and network service
    order through nas_link's PURE parsers (`ifconfig`, `networksetup -listallhardwareports`,
    `networksetup -listnetworkserviceorder`), read once per check. `ports_ok` = the port list was
    read: without it nothing can tell the Wi-Fi from a cable."""
    rc, out, _err = run([IFCONFIG], CMD_TIMEOUT)
    ifaces = nas_link.parse_ifconfig(out or "") if rc == 0 else {}
    rc, out, _err = run([NETWORKSETUP, "-listallhardwareports"], CMD_TIMEOUT)
    ports_ok = rc == 0
    ports = nas_link.parse_hardware_ports(out or "") if ports_ok else {}
    rc, out, _err = run([NETWORKSETUP, "-listnetworkserviceorder"], CMD_TIMEOUT)
    order = nas_link.parse_service_order(out or "") if rc == 0 else []
    return ifaces, ports, order, ports_ok


def _route_verdict(host: str, ip: str, run, links) -> tuple:
    """One address's verdict. A wired interface on the helper's subnet is chosen exactly as
    nas_link chooses one for the NAS (pick_wired: active, not Wi-Fi, not a tunnel/phone, in the
    Mac's service order) and bound to — so Wi-Fi ranked above the cable is no refusal. Only
    with no such interface does the route table decide: Wi-Fi there is "the Mac is on Wi-Fi"."""
    ifaces, ports, order, ports_ok = links
    dev = nas_link.pick_wired(ip, ifaces, ports, order) if ports_ok else None
    if dev:
        return True, f"wired via {dev} ({ip})", ip, dev, "wired"
    rc, out, _err = run([ROUTE, "-n", "get", ip], CMD_TIMEOUT)
    iface = parse_route_interface(out) if rc == 0 else None
    if not iface:
        return False, f"no route to {host} ({ip})", ip, None, "unreachable"
    if not ports_ok:
        return False, "cannot list this Mac's network ports", ip, iface, "route_check"
    if nas_link.is_wifi(iface, ports):
        return False, f"the route to {host} is Wi-Fi ({iface})", ip, iface, "wifi"
    # a helper on another subnet, reached through a router over the cable: the route's own
    # interface, when it is a wired one (never a tethered phone or a tunnel)
    wired = iface.startswith("bridge") or (iface.startswith("en")
                                            and nas_link.is_ethernet_like(iface, ports))
    if not wired:
        return False, f"the route to {host} is not wired ({iface})", ip, iface, "not_wired"
    return True, f"wired via {iface} ({ip})", ip, iface, "wired"


def wired_route_ok(host: str, *, run=None, resolve=None, prefer=None) -> tuple:
    """(ok, why, ip, iface, code): the Mac reaches `host`'s IPv4 `ip` over a wired interface
    `iface` (_route_verdict). `prefer` = the IPv4s the helper itself reported as wired: when it
    holds any, ONLY those are tried (in order, the first wired one wins) and the name is not
    resolved at all; otherwise the name's first IPv4. `code` is the verdict as one word
    ("wired", "wifi", "not_wired", "unreachable", "route_check") — what status_summary
    classifies on; `why` (which names the host) is for logs and the tooltip only. Never cached:
    it is a few milliseconds, and a cached verdict could outlive a pulled cable. The ssh then
    goes to exactly this `ip` over exactly this `iface` (ssh_argv). Never raises."""
    try:
        run = run or _run
        ips = [ip for ip in (prefer or ()) if isinstance(ip, str) and ip]
        if not ips:
            ip = (resolve or _resolve_ipv4)(host)
            if not ip:
                return False, f"cannot resolve {host} to an IPv4 address", None, None, "unreachable"
            ips = [ip]
        links = _mac_links(run)
        first = None
        for ip in ips:
            verdict = _route_verdict(host, ip, run, links)
            if verdict[0]:
                return verdict
            first = first or verdict
        return first
    except Exception as e:  # noqa: BLE001
        return False, f"route check failed: {e}", None, None, "route_check"


# ---- the helper's own wired addresses --------------------------------------------------------

def reported_ipv4(s):
    """PURE. The IPv4 addresses a status answer says the helper's WIRED interfaces carry
    ("ipv4", in its order, duplicates and junk dropped), or None when it sends no such list (an
    older vworker). [] = it reported none."""
    v = s.get("ipv4") if isinstance(s, dict) else None
    if not isinstance(v, list):
        return None
    out = []
    for a in v[:16]:
        if not (isinstance(a, str) and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", a)):
            continue
        try:
            ip = ipaddress.IPv4Address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or a in out:
            continue
        out.append(a)
    return out


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
_WIRED_IPS: dict = {}            # (host, user, key) -> the IPv4s the helper last reported as wired
                                 #   (status "ipv4"); the route check uses only these once known
_SETUP: dict = {"t": 0.0, "res": None, "jobs": None}   # setup_status's last result
_SETUP_LOCK = threading.Lock()   # one setup check at a time (it runs an ssh: never under _STATE_LOCK)


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
        _WIRED_IPS.clear()
        _SETUP.update(t=0.0, res=None, jobs=None)


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


def _known_ipv4(hid) -> list:
    """The helper's last reported wired IPv4s ([] while none are known)."""
    with _STATE_LOCK:
        return list(_WIRED_IPS.get(hid) or ())


def _learn_ipv4(hid, s) -> None:
    """Remember the wired IPv4s a status answer reports (an answer without the list changes
    nothing: an older vworker)."""
    ips = reported_ipv4(s)
    if ips is not None:
        with _STATE_LOCK:
            _WIRED_IPS[hid] = ips


def _forget_ipv4(hid) -> None:
    """No answer over the remembered addresses (a new DHCP lease, say): the next check starts
    from the resolved name again, and learns the list afresh from that first answer."""
    with _STATE_LOCK:
        _WIRED_IPS.pop(hid, None)


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
        self._last_err = (None, "")              # (rc, ssh's stderr) of the last status probe

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
        known = _known_ipv4(self._id)            # the helper's own wired IPv4s, once reported
        try:
            if known and dvcap._takes_kw(self._route_ok, "prefer"):
                r = tuple(self._route_ok(self.host, prefer=known))
            else:
                r = tuple(self._route_ok(self.host))
            ok, why = bool(r[0]), str(r[1])
            ip, iface = (r[2], r[3]) if len(r) >= 4 else (None, None)
            code = r[4] if len(r) >= 5 and isinstance(r[4], str) else ""
        except Exception as e:  # noqa: BLE001
            ok, why, ip, iface, code = False, f"route check failed: {e}", None, None, "route_check"
        if ok and not (ip and iface):
            ok, why = False, f"the route check named no address and interface for {self.host}"
            code = "route_check"
        if ok and known and str(ip) not in known:
            # never an address the helper does not list as wired, once it has listed any
            ok, why = False, f"{ip} is not one of the helper's wired addresses ({', '.join(known)})"
            code = "not_wired"
        code = "wired" if ok else (code if code and code != "wired" else "unreachable")
        self._route = (str(ip), str(iface)) if ok else None
        with _STATE_LOCK:                       # status_summary's tooltip: "<host> · <ip> over Ethernet"
            if ok:
                _LAST_ROUTE[self._id] = self._route
            else:
                _LAST_ROUTE.pop(self._id, None)
        return ok, why, code

    def status(self):
        """The helper's `vworker status` JSON (its last JSON line), or None (ssh's stderr kept
        in _last_err for the tooltip). The wired IPv4s it reports are remembered (_learn_ipv4).
        Never raises."""
        self._last_err = (None, "")
        cmd = self.argv("status")
        if cmd is None:
            return None
        rc, out, err = _run(cmd, STATUS_TIMEOUT)
        self._last_err = (rc, err or "")
        if rc is None:
            return None
        s = _last_json(out)
        if isinstance(s, dict):
            _learn_ipv4(self._id, s)
        return s

    def _off_list(self) -> bool:
        """The last checked route's address is not one the helper reports as wired — the first
        contact's resolved address (the helper's Wi-Fi, say), before its list was known."""
        known = _known_ipv4(self._id)
        return bool(known) and self._route is not None and self._route[0] not in known

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
            if s is not None and self._off_list():
                # the answer came over an address the helper does not list as wired: check the
                # route again over one it does list, and ask once more (nothing cached before)
                ok, why, code = self._check_route()
                if not ok:
                    return False, why, code
                s = self.status()
            if s is None:
                _forget_ipv4(self._id)
                res, keep = (False, _no_answer_why(self.host, *self._last_err), "unreachable"), True
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
NOT_SET_UP = "not set up"        # _configured's problem while the host or the user is still empty


def _configured() -> tuple:
    """(enabled, {field: value}, problem) from the settings, read once. `problem` is "",
    NOT_SET_UP (no host or no user yet — the shipped defaults; judged even while switched off,
    and never logged: nothing was typed wrong) or what status_summary shows after "settings:"
    ("bad host", "bad user, key", ...); a bad value is logged once (per distinct one) and never
    replaced by a default. May raise (unreadable)."""
    st = _settings()
    s = st.get_settings()
    vals = {k: s.get(k, st.DEFAULT_SETTINGS[k]) for k in _FIELD_ORDER}
    enabled = st._valid_helper_enabled(s.get("helper_enabled", False))
    if not st.helper_set_up(vals):
        return enabled, vals, NOT_SET_UP
    if not enabled:
        return False, vals, ""
    bad = [k for k in _FIELD_ORDER if not st.helper_field_ok(k, vals[k])]
    if bad:
        _log_once("Helper not used: invalid " + ", ".join(f"{k}={vals[k]!r}" for k in bad)
                  + " in settings — fix it in Settings > Setup > Helper")
        return True, vals, "bad " + ", ".join(st.HELPER_FIELDS[k][0] for k in bad)
    return True, vals, ""


def for_remux():
    """The configured helper for one remux, or None: helper_enabled off, not set up (no host
    or no user yet), settings unreadable, or a malformed helper_host / helper_user / helper_key —
    which is logged once (per distinct problem) and never silently replaced by a default. Never
    raises."""
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
    the helper is switched off, its settings go bad or empty, or `stop`. available() itself never asks
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
    (or "<host> · <the technical reason>")}. "connected" while a remote segment is in flight;
    "off · not set up" while the host or the user is empty, whatever the switch says. Never
    raises."""
    try:
        enabled, vals, problem = _configured()
    except Exception:  # noqa: BLE001
        enabled, vals, problem = False, None, ""
    host = str((vals or {}).get("helper_host") or "")

    def line(state, detail, tooltip=None):
        return {"enabled": bool(enabled), "state": state, "detail": detail, "host": host,
                "tooltip": tooltip if tooltip is not None else host}
    try:
        if problem == NOT_SET_UP:
            return line("off", NOT_SET_UP)
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


# ---- SETUP: Settings > Setup > Helper ----------------------------------------------------------
# setup_status() answers GET /api/helper/setup; setup_job_argv() builds the argv of the three
# setup_jobs targets that fix what it finds. Both read the SAVED settings only, judged by
# settings.helper_field_ok, so nothing typed reaches a shell — and neither ever raises.

HELPER_JOBS = ("helper_key", "helper_worker", "helper_x265")   # setup_jobs targets (argv here)
X265_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")     # `build-x265.sh --version` takes it
_CHECK_IDS = ("login", "wired", "worker", "x265", "helper")
_STATE_WORDS = {"connected": "connected", "ready": "ready", "paused": "paused", "off": "off",
                "not_connected": "not connected"}


def login_argv(host: str, user: str, key: str, *remote) -> list:
    """PURE. Setup's login probe, `vworker <remote...>` with the configured key only, over the
    Mac's NORMAL route — no -4, no BindInterface: this proves the login (key, host key, sshd);
    wired_route_ok judges the route on its own. The host is the destination itself, so its key
    is looked up under exactly the name the remux's HostKeyAlias uses. Never asks anything
    (BatchMode), and nothing from the user's ssh config can multiplex, jump or allocate a tty."""
    return [SSH, "-i", os.path.expanduser(key),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ProxyJump=none",
            "-o", "RequestTTY=no", "--", f"{user}@{host}", VWORKER] + \
        [shlex.quote(str(a)) for a in remote]


def worker_sha256(path=None) -> str:
    """sha256 hex of this app's own helper_worker/vworker.py — what an up-to-date worker
    reports as its status "sha256" — or '' when it cannot be read."""
    try:
        with open(path or os.path.join(WORKER_DIR, "vworker.py"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def authorize_command(host: str, user: str, key: str) -> str:
    """PURE. The one command the user runs in Terminal so the helper takes the key: ssh-copy-id
    asks for the helper's password once, and on first contact records its host key under the
    host name as typed — the name the remux looks it up by. The key path stays as typed (a
    leading ~ still expands) unless it needs quoting."""
    pub = f"{key}.pub"
    word = pub if re.fullmatch(r"[A-Za-z0-9_./~+-]+", pub) else shlex.quote(os.path.expanduser(pub))
    return f"ssh-copy-id -i {word} {user}@{host}"


def _check(cid: str, ok, detail: str = "", action: str = "") -> dict:
    return {"id": cid, "ok": ok, "detail": detail, "action": action}


def _wired_check(host: str, r) -> dict:
    """The "wired" check's words from a wired_route_ok verdict."""
    ok, _why, ip, iface, code = (tuple(r) + (None,) * 5)[:5]
    if ok:
        return _check("wired", True, f"over Ethernet ({iface})")
    if code == "wifi":
        return _check("wired", False, "the Mac reaches it over Wi-Fi - plug both into Ethernet")
    if code == "not_wired":
        return _check("wired", False, f"the Mac reaches it over {iface}, not Ethernet")
    if code == "route_check":
        return _check("wired", False, "couldn't read this Mac's network ports - try again")
    if not ip:
        return _check("wired", False, f"can't find {host} on the network - check the host name")
    return _check("wired", False, f"no route to {host} ({ip})")


def _login(host: str, user: str, key: str, run) -> tuple:
    """(check, status|None): setup's login probe runs `vworker status`. Logged in = ssh exited
    with anything but 255 (a missing vworker is the shell's 127 — the login itself worked);
    `status` is its answer when one came back."""
    if not os.path.isfile(os.path.expanduser(key)):
        return _check("login", False, "no SSH key yet - Create key"), None
    rc, out, err = run(login_argv(host, user, key, "status"), LOGIN_TIMEOUT)
    if rc is None or rc == 255:
        _kind, detail, action = ssh_failure(rc, err, host, key)
        return _check("login", False, detail, action), None
    s = _last_json(out)
    return _check("login", True, f"logged in as {user}"), (s if isinstance(s, dict) and "version" in s
                                                          else None)


def _worker_check(s) -> dict:
    mine = worker_sha256()
    if s is None:
        return _check("worker", False, "not installed", "helper_worker")
    if not mine:
        return _check("worker", None, "this app's own copy of the worker is missing")
    if s.get("sha256") == mine:
        return _check("worker", True, "installed")
    return _check("worker", False, "an older worker - Install updates it", "helper_worker")


def _x265_check(s, mac: str) -> dict:
    theirs = str((s or {}).get("x265") or "")
    if not mac:
        return _check("x265", None, "install the command-line tools first")
    if s is None:
        return _check("x265", None, "install the worker first")
    if not theirs:
        return _check("x265", False, "not built yet", "helper_x265")
    if theirs == mac:
        return _check("x265", True, f"x265 {mac}, the same as this Mac")
    return _check("x265", False, f"x265 {theirs} on the helper, {mac} on this Mac - Build replaces it",
                  "helper_x265")


def _helper_check(vals: dict, enabled: bool) -> dict:
    """The status line's own words for the helper, from a verdict taken NOW: the refresher's
    Helper asked afresh (a cooldown and this process's job slot still count), then
    status_summary() read exactly as the remux line reads it. Switched off: not applicable."""
    if not enabled:
        return _check("helper", None, "off - switched off")
    h = _summary_helper(vals)
    if not _slot_held(h._id):
        with _STATE_LOCK:
            _AVAIL.setdefault(h._id, {})["res"] = None
        try:
            ok, why, code = h.availability()
        except Exception as e:  # noqa: BLE001
            ok, why, code = False, f"helper check failed: {e}", "error"
        with _STATE_LOCK:
            _VERDICT[h._id] = {"ok": bool(ok), "why": str(why), "code": str(code or ""),
                               "t": time.monotonic()}
    st = status_summary()
    state, detail = str(st.get("state") or ""), str(st.get("detail") or "")
    words = _STATE_WORDS.get(state, state) + (f" - {detail}" if detail else "")
    if state == "not_connected" and not detail:
        with _STATE_LOCK:
            why = str((_VERDICT.get(h._id) or {}).get("why") or "").replace(_SEE_SETUP, "")
        words += f" - {why}" if why else ""
    return _check("helper", state in ("ready", "connected"), words)


def _setup_compute(run, mac_x265) -> dict:
    st = _settings()
    s = st.get_settings()
    vals = {k: s.get(k, st.DEFAULT_SETTINGS[k]) for k in _FIELD_ORDER}
    enabled = st._valid_helper_enabled(s.get("helper_enabled", False))
    host, user, key = (vals[k] for k in _FIELD_ORDER)
    as_str = {k: (v if isinstance(v, str) else "" if v is None else str(v)) for k, v in vals.items()}
    set_up = st.helper_set_up(vals)
    bad = [st.HELPER_FIELDS[k][0] for k in _FIELD_ORDER if not st.helper_field_ok(k, vals[k])]
    configured = set_up and not bad
    key_ok = st.helper_field_ok("helper_key", key)
    path = os.path.expanduser(key) if key_ok else ""
    pub = os.path.abspath(path + ".pub") if key_ok and os.path.isfile(path + ".pub") else ""
    mac = mac_x265() or ""
    out = {"configured": configured, "host": as_str["helper_host"], "user": as_str["helper_user"],
           "key": as_str["helper_key"], "key_exists": bool(key_ok and os.path.isfile(path)),
           "pubkey": pub, "authorize_cmd": authorize_command(host, user, key) if configured else "",
           "mac_x265": mac, "helper_x265": "", "checks": [], "checked_at": time.time()}
    if not configured:
        why = NOT_SET_UP if not set_up else "settings: bad " + ", ".join(bad)
        out["checks"] = [_check(c, None, why if c == "login" else "") for c in _CHECK_IDS]
        return out
    hid = (host, user, key)
    login, status = _login(host, user, key, run)
    if status is not None:
        _learn_ipv4(hid, status)        # the first contact may tell the remux its wired address
        out["helper_x265"] = str(status.get("x265") or "")
    checks = [login, _wired_check(host, wired_route_ok(host, run=run, prefer=_known_ipv4(hid)))]
    if login["ok"]:
        checks += [_worker_check(status), _x265_check(status, mac), _helper_check(vals, enabled)]
    else:
        checks += [_check("worker", None),               # not checked: nothing logged in
                   _check("x265", None, "" if mac else "install the command-line tools first"),
                   _check("helper", None)]
    out["checks"] = checks
    return out


def _helper_jobs_sig():
    """What setup_jobs says about the helper's three jobs (the running one, each one's state and
    finish time): a different answer than the cached result's = a job started or finished."""
    try:
        import setup_jobs
        st = setup_jobs.status()
    except Exception:  # noqa: BLE001
        return None
    jobs = st.get("jobs") or {}
    active = st.get("active")
    return (active if active in HELPER_JOBS else None,
            tuple((w, (jobs.get(w) or {}).get("state"), (jobs.get(w) or {}).get("finished"))
                  for w in HELPER_JOBS))


def invalidate_setup() -> None:
    """A helper job started or finished: setup_status's next call checks again, and the status
    line's cached verdicts go too (the refresher asks at once), so neither keeps saying what the
    job just changed. Never blocks on a running setup check."""
    with _STATE_LOCK:
        _SETUP["res"] = None
        for st in _AVAIL.values():
            st["res"] = None
    _wake_refresher()


def _setup_unavailable(why: str) -> dict:
    return {"configured": False, "host": "", "user": "", "key": "", "key_exists": False,
            "pubkey": "", "authorize_cmd": "", "mac_x265": "", "helper_x265": "",
            "checks": [_check(c, None, f"the check failed: {why}" if c == "login" else "")
                       for c in _CHECK_IDS],
            "checked_at": 0}


def setup_status(fresh: bool = False, *, run=None, local_x265=None) -> dict:
    """GET /api/helper/setup: {"configured", "host", "user", "key" (as saved), "key_exists",
    "pubkey" (absolute path of <key>.pub, "" if missing), "authorize_cmd" (the ssh-copy-id line,
    "" until set up), "mac_x265", "helper_x265" ("" if unknown), "checks": [{"id", "ok"
    (True/False, None = not checked / not applicable), "detail" (plain words), "action" (""|
    "authorize"|"helper_worker"|"helper_x265")} for login, wired, worker, x265, helper — in that
    order], "checked_at" (epoch, 0 = never)}.

    Runs only when asked (the app: Setup opening, Test, after a job): one login ssh over the
    normal route, the wired-route check, and the helper's verdict afresh. The result is reused
    for SETUP_TTL seconds unless `fresh`, and never once a helper job has started or finished
    since (invalidate_setup). It never touches /api/state's path (status_summary reads caches
    only) and never raises. `run`/`local_x265` are for tests."""
    try:
        asked = time.monotonic()
        jobs = _helper_jobs_sig()
        with _SETUP_LOCK:
            with _STATE_LOCK:
                hit, t, sig = _SETUP.get("res"), _SETUP.get("t", 0.0), _SETUP.get("jobs")
            if hit is not None and sig == jobs and (t >= asked or (not fresh and
                                                                    asked - t < SETUP_TTL)):
                return json.loads(json.dumps(hit))
            if hit is not None and sig != jobs:
                invalidate_setup()
            t0 = time.monotonic()
            res = _setup_compute(run or _run, local_x265 or local_x265_version)
            with _STATE_LOCK:
                _SETUP.update(t=t0, res=res, jobs=jobs)
            return json.loads(json.dumps(res))
    except Exception as e:  # noqa: BLE001
        return _setup_unavailable(str(e))


def setup_job_argv(what: str, *, local_x265=None) -> tuple:
    """(argv, "") for one of Setup's helper jobs, or (None, why in plain words). Every value
    comes from the SAVED settings, judged by settings.helper_field_ok, and reaches the job as
    its own argv word (never through a shell unquoted):
      helper_key     ssh-keygen -t ed25519 -N "" -C visionary-helper -f <key, expanded>; refused
                     when the key (or its .pub) already exists — never overwritten. The key's
                     folder (~/.ssh) is made, mode 700, when missing.
      helper_worker  /bin/bash <engine>/helper_worker/install.sh --host H --user U --key K
      helper_x265    <engine>/helper_worker/build-x265.sh run ON THE HELPER over ssh (the script
                     is ssh's stdin) with "--version <this Mac's x265 token>", the token checked
                     against X265_TOKEN_RE first.
    The worker and x265 jobs need a host, a user and an existing key. Never raises."""
    try:
        st = _settings()
        s = st.get_settings()
        vals = {k: s.get(k, st.DEFAULT_SETTINGS[k]) for k in _FIELD_ORDER}
        return _job_argv(what, vals, st, local_x265 or local_x265_version)
    except Exception as e:  # noqa: BLE001
        return None, f"cannot prepare {what}: {e}"


def _job_argv(what: str, vals: dict, st, mac_x265) -> tuple:
    if what not in HELPER_JOBS:
        return None, f"no such helper job {what!r}"
    host, user, key = (vals[k] for k in _FIELD_ORDER)
    if not st.helper_field_ok("helper_key", key):
        return None, "the SSH key path in Settings > Setup > Helper is not usable - fix it first"
    path = os.path.expanduser(key)
    if what == "helper_key":
        if os.path.lexists(path) or os.path.lexists(path + ".pub"):
            return None, f"{key} already exists - Visionary never replaces a key"
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, mode=0o700)
            os.chmod(folder, 0o700)
        return [SSH_KEYGEN, "-t", "ed25519", "-N", "", "-C", "visionary-helper", "-f", path], ""
    if not st.helper_set_up(vals):
        return None, "the helper isn't set up yet - fill in its host and user first"
    bad = [st.HELPER_FIELDS[k][0] for k in ("helper_host", "helper_user")
           if not st.helper_field_ok(k, vals[k])]
    if bad:
        return None, "settings: bad " + ", ".join(bad) + " - fix it first"
    if not os.path.isfile(path):
        return None, "no SSH key yet - Create key first"
    if what == "helper_worker":
        script = os.path.join(WORKER_DIR, "install.sh")
        if not os.path.isfile(script):
            return None, "this app's helper installer is missing"
        return ["/bin/bash", script, "--host", host, "--user", user, "--key", path], ""
    script = os.path.join(WORKER_DIR, "build-x265.sh")
    if not os.path.isfile(script):
        return None, "this app's x265 build script is missing"
    token = mac_x265() or ""
    if not token:
        return None, "this Mac has no x265 yet - install the command-line tools first"
    if not X265_TOKEN_RE.fullmatch(token):
        return None, f"this Mac's x265 version {token!r} is not one the build can make"
    remote = (f'exec {SSH} -i "$1" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=8 '
              f'-o ServerAliveInterval=15 -- "$2@$3" "bash -s -- --version $5" < "$4"')
    return ["/bin/bash", "-c", remote, "_", path, user, host, script, token], ""
