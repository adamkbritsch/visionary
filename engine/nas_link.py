"""Which of the Mac's network links reaches the NAS — so the Dolby Vision lane moves its terabytes over
the WIRED one (user-dictated 2026-09-30: "they should be able to download/upload on the ethernet
connection that's connected to this laptop to the nas").

Left to itself, ssh took Wi-Fi. The NAS's mDNS name resolves to IPv6 link-local addresses as well
as its IPv4 one, and ssh prefers IPv6, and the link-local address it picked was scoped to the
Wi-Fi interface — so while a 2.5 GbE adapter sat on the same subnet, every conversion's download
and upload crossed the air (live-caught 2026-09-30: one lane session on en0 at ~57 MB/s, another
tool's IPv4 session on the Ethernet adapter at ~156 MB/s).

So the lane's ssh is pinned: IPv4 only, and bound (`ssh -B`) to the chosen interface on the NAS's
subnet. WHICH one is the "NAS network" setting (Settings, main section — user-dictated 2026-09-30):
  ethernet       Ethernet first (the default): the first ACTIVE, NON-Wi-Fi interface on the NAS's
                 subnet, in the Mac's network service order; else Wi-Fi; else ordinary routing
  wifi           Wi-Fi first, then Ethernet, then ordinary routing
  ethernet_only  Ethernet or nothing: with no cable the Dolby Vision lane WAITS rather than move
                 terabytes over the air. The upscale pipeline's FTP never waits on this — it keeps
                 its configured host order instead (a stalled transfer there fails episodes)
The same choice puts the pipeline's FTP on the NAS's LAN address, bound to that link, ahead of the
Tailscale address it otherwise tries first (transfer.connect).

PURE (unit-tested): parse_ifconfig, parse_hardware_ports, parse_service_order, pick_wired,
pick_wifi, choose, link_speed, valid_priority. The rest runs `ifconfig` / `networksetup` locally —
never anything on the NAS.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import threading
import time

CACHE_SECS = 30
RESOLVE_DEADLINE = 1.0          # an mDNS name that has not answered by now is treated as absent
NEGATIVE_SECS = 300             # ...and not asked again for this long: away from home the lookup
                                # can never succeed, and each try used to cost ~5 s per connect
PRIORITIES = ("ethernet", "wifi", "ethernet_only")
DEFAULT_PRIORITY = "ethernet"
# Never a path to the NAS even when they carry an address on its subnet: tunnels, the
# Thunderbolt bridge, Apple's peer-to-peer Wi-Fi links, loopback.
_NOT_A_LINK = ("lo", "utun", "bridge", "awdl", "llw", "anpi", "gif", "stf", "ap", "ipsec")

_lock = threading.Lock()
_cache = {}                     # (host, priority) -> (monotonic time, link): FTP asks per host
_last = {"link": None}          # the Dolby Vision lane's most recent link, for the state poll
_last_wired = {}                # host -> the wired port that last reached it (address known)


def parse_ifconfig(text: str) -> dict:
    """{iface: {"inet": "a.b.c.d", "mask": prefixlen, "active": bool, "media": str}} from `ifconfig`.
    Only interfaces with an IPv4 address are kept."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z0-9]+):\s", line)
        if m:
            cur = m.group(1)
            out[cur] = {"inet": None, "mask": None, "active": None, "media": ""}
            continue
        if cur is None:
            continue
        s = line.strip()
        m = re.match(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", s)
        if m and out[cur]["inet"] is None:
            out[cur]["inet"] = m.group(1)
            out[cur]["mask"] = bin(int(m.group(2), 16)).count("1")
        elif s.startswith("status:"):
            out[cur]["active"] = s.split(":", 1)[1].strip() == "active"
        elif s.startswith("media:"):
            out[cur]["media"] = s.split(":", 1)[1].strip()
    return {k: v for k, v in out.items() if v["inet"]}


def parse_hardware_ports(text: str) -> dict:
    """{device: hardware port name} from `networksetup -listallhardwareports`."""
    out, port = {}, None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port is not None:
            dev = line.split(":", 1)[1].strip()
            if dev:
                out[dev] = port
            port = None
    return out


def parse_service_order(text: str) -> list:
    """[(service name, device)] in the Mac's network service order, from
    `networksetup -listnetworkserviceorder`. Disabled services (marked `(*)`) are skipped."""
    out, name = [], None
    for line in text.splitlines():
        m = re.match(r"^\((\d+|\*)\)\s+(.*)$", line.strip())
        if m:
            name = None if m.group(1) == "*" else m.group(2).strip()
            continue
        m = re.search(r"Device:\s*([^)\s]+)\)", line)
        if m and name:
            out.append((name, m.group(1)))
            name = None
    return out


# Not the Ethernet, though none of them is Wi-Fi either: a tethered phone or iPad, Bluetooth PAN.
# The test that caught it: with the NAS's address unknown, "Ethernet only" pinned to "iPhone USB".
_NOT_ETHERNET = ("iphone", "ipad", "bluetooth", "modem", "tether")


def is_ethernet_like(device: str, ports: dict) -> bool:
    """A wired LAN port: not a tunnel/bridge, not Wi-Fi, not a phone or Bluetooth link."""
    port = (ports.get(device) or "").lower()
    return (not device.startswith(_NOT_A_LINK) and not is_wifi(device, ports)
            and not any(w in port for w in _NOT_ETHERNET))


def is_wifi(device: str, ports: dict) -> bool:
    return "wi-fi" in (ports.get(device) or "").lower() or "airport" in (ports.get(device) or "").lower()


def pick_wired(nas_ip: str, ifaces: dict, ports: dict, order: list):
    """PURE: the interface to bind the NAS transfers to, or None. It must be up (status active, or no
    status line at all), carry an IPv4 address on the NAS's subnet, and not be Wi-Fi or a tunnel.
    Ties go to the Mac's network service order, then to the device name."""
    try:
        nas = ipaddress.ip_address(nas_ip)
    except ValueError:
        return None
    rank = {dev: i for i, (_n, dev) in enumerate(order)}
    cands = []
    for dev, info in ifaces.items():
        if not is_ethernet_like(dev, ports) or info.get("active") is False:
            continue
        try:
            net = ipaddress.ip_network(f"{info['inet']}/{info['mask']}", strict=False)
        except (ValueError, TypeError):
            continue
        if nas in net:
            cands.append((rank.get(dev, 999), dev))
    return min(cands)[1] if cands else None


def pick_wifi(nas_ip: str, ifaces: dict, ports: dict):
    """PURE: the active Wi-Fi interface with an address on the NAS's subnet, or None."""
    try:
        nas = ipaddress.ip_address(nas_ip)
    except ValueError:
        return None
    for dev, info in sorted(ifaces.items()):
        if not is_wifi(dev, ports) or info.get("active") is False:
            continue
        try:
            if nas in ipaddress.ip_network(f"{info['inet']}/{info['mask']}", strict=False):
                return dev
        except (ValueError, TypeError):
            continue
    return None


def order_rank(order, dev) -> int:
    for i, (_n, d) in enumerate(order):
        if d == dev:
            return i
    return 999


def valid_priority(v) -> str:
    return v if v in PRIORITIES else DEFAULT_PRIORITY


def choose(nas_ip, ifaces, ports, order, priority):
    """PURE: (interface to bind or None, unavailable) for a priority. `unavailable` is True only for
    ethernet_only with no wired link to a KNOWN NAS address — the caller must not fall back to
    anything. An unknown address is not proof the cable is missing (the NAS may be rebooting), so
    it is never "unavailable" — though Ethernet only still pins to an active wired port, since the
    promise is "never over the air", not "only to a known subnet" (review 2026-09-30)."""
    if not nas_ip:
        if valid_priority(priority) == "ethernet_only":
            wired = sorted((order_rank(order, d), d) for d, i in ifaces.items()
                           if is_ethernet_like(d, ports) and i.get("active") is not False)
            return (wired[0][1], False) if wired else (None, True)
        return None, False
    wired = pick_wired(nas_ip, ifaces, ports, order) if nas_ip else None
    wifi = pick_wifi(nas_ip, ifaces, ports) if nas_ip else None
    seq = {"ethernet": [wired, wifi], "wifi": [wifi, wired],
           "ethernet_only": [wired]}[valid_priority(priority)]
    for dev in seq:
        if dev:
            return dev, False
    return None, valid_priority(priority) == "ethernet_only"


def priority_setting() -> str:
    """The user's choice from Settings (read at use time, so a change applies within CACHE_SECS)."""
    try:
        import settings
        return valid_priority(settings.get_settings().get("nas_network"))
    except Exception:  # noqa: BLE001
        return DEFAULT_PRIORITY


def link_speed(media: str) -> str:
    """PURE: "autoselect (2500Base-T <full-duplex>)" -> "2.5 GbE"; "" when unknown."""
    m = re.search(r"(\d+)(G)?[Bb]ase", media or "")
    if not m:
        return ""
    mbps = int(m.group(1)) * (1000 if m.group(2) else 1)
    if mbps >= 1000:
        g = mbps / 1000
        return f"{g:g} GbE"
    return f"{mbps} Mb/s"


def _under_test() -> bool:
    return "unittest" in sys.modules


def _run(cmd) -> str:
    if _under_test():                      # a test sees no links unless it feeds the text itself
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


_negative = {}                  # host -> wall time until which a FAILED lookup is not repeated
_known = {}                     # host -> (wall time, ip, network signature) of the last ANSWER
_fresh = {}                     # host -> did the last nas_ipv4 get a fresh answer (not a stand-in)
KNOWN_SECS = 600                # a recent answer stands in for one that is slow or failing


def is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def net_signature(ifaces: dict) -> tuple:
    """PURE: the Mac's active local addresses — a saved NAS address is only good on the network it
    was learned on."""
    return tuple(sorted(i["inet"] for i in ifaces.values()
                        if i.get("inet") and i.get("active") is not False))


def nas_ipv4(host: str, sig=None):
    """The NAS's IPv4 address for a name or address. A name is resolved through the system (mDNS
    for .local) and waited on for RESOLVE_DEADLINE; the lookup itself runs on. An ANSWER is kept
    (KNOWN_SECS, with the network it came from); a FAILED lookup is not repeated for NEGATIVE_SECS
    — away from home each try used to cost ~5 s per FTP connect. A slow or failed lookup is answered
    with the kept address when there is one from THIS network (`sig`), so one lost multicast never
    unpins the lane. Times are wall-clock: a monotonic clock stops while the lid is closed, which let
    a home address survive into a café (review 2026-09-30). last_was_fresh() says whether the answer
    was fresh — the FTP LAN route trusts nothing else."""
    if is_literal(host):
        with _lock:
            _fresh[host] = True
        return host
    if _under_test():                      # no mDNS lookups from a test (they can take seconds)
        return None
    now = time.time()

    def kept():
        with _lock:
            k = _known.get(host)
        if k and now - k[0] < KNOWN_SECS and (sig is None or k[2] == sig):
            return k[1]
        return None

    with _lock:
        failed_recently = _negative.get(host, 0) > now
        _fresh[host] = False
    if failed_recently:
        return kept()
    box = []

    def look():
        try:
            infos = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)
            ip = infos[0][4][0] if infos else None
        except OSError:
            ip = None
        with _lock:
            if ip:
                _known[host] = (time.time(), ip, sig)
                _negative.pop(host, None)
            else:
                _negative[host] = time.time() + NEGATIVE_SECS
        box.append(ip)
    t = threading.Thread(target=look, daemon=True, name="nas-resolve")
    t.start()
    t.join(RESOLVE_DEADLINE)
    if box and box[0]:
        with _lock:
            _fresh[host] = True
        return box[0]
    return kept()


def last_was_fresh(host: str) -> bool:
    with _lock:
        return bool(_fresh.get(host))


def forget():
    """Drop every cached choice — a transfer on the chosen link just failed, so look again."""
    with _lock:
        _cache.clear()


def detect(host: str, priority: str = None) -> dict:
    """{"iface", "bound", "wired", "kind", "name", "speed", "ip", "src", "priority", "unavailable"}
    for the link to use to reach `host` under `priority` (default: the setting). `bound` = the
    transfers are pinned to `iface`; otherwise ordinary routing picks and `iface` is only what it
    picked. Cached per host and priority."""
    priority = valid_priority(priority or priority_setting())
    key = (host, priority)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECS:
            return hit[1]
    ifaces = parse_ifconfig(_run(["ifconfig"]))
    ip = nas_ipv4(host, net_signature(ifaces))
    fresh = last_was_fresh(host)
    ports = parse_hardware_ports(_run(["networksetup", "-listallhardwareports"]))
    order = parse_service_order(_run(["networksetup", "-listnetworkserviceorder"]))
    names = {dev: n for n, dev in order}
    dev, unavailable = choose(ip, ifaces, ports, order, priority)
    if ip is None and priority == "ethernet_only":
        with _lock:
            last = _last_wired.get(host)
        if last and (ifaces.get(last) or {}).get("active") is not False and last in ifaces:
            dev, unavailable = last, False          # the port that last reached the NAS
    bound = dev is not None
    if not dev and ip and not unavailable:
        m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", ip]))
        dev = m.group(1) if m else None
    wifi = bool(dev) and is_wifi(dev, ports)
    wired = bound and not wifi
    kind = ("Wi-Fi" if wifi else "Ethernet" if wired
            else (ports.get(dev) or dev or ("none" if unavailable else "unknown")))
    link = {"iface": dev, "bound": bound, "wired": wired, "kind": kind, "ip": ip,
            "unresolved": ip is None, "fresh": fresh,
            "src": (ifaces.get(dev) or {}).get("inet") if bound else None,
            "priority": priority, "unavailable": unavailable,
            "name": names.get(dev) or ports.get(dev) or dev,
            "speed": link_speed((ifaces.get(dev) or {}).get("media", "")) if dev else ""}
    with _lock:
        _cache[key] = (time.monotonic(), link)
        if ip and wired:
            _last_wired[host] = dev
    return link


def remember(link):
    """Record the link the Dolby Vision lane is using, for last(), stamped so a reader can tell a
    current answer from one the stopped lane left behind."""
    with _lock:
        _last["link"] = dict(link or {}, at=time.time()) if link else None
    return link


def lan_route(hosts):
    """(NAS LAN address, local address to bind) for the first configured host the chosen link
    reaches DIRECTLY, or None — the pipeline's FTP then keeps its configured order. Never None
    because of ethernet_only: that choice only makes the Dolby Vision lane wait.

    Only NAMES count, never an IP literal: a name that resolves on the local link (mDNS) is proof
    the Mac is on the NAS's network, while a bare 192.168.1.x matches half the networks in the
    world, and trying it first on someone else's would send the FTP login to whatever answered
    there (review 2026-09-30)."""
    for h in hosts or ():
        if is_literal(h):
            continue
        ln = detect(h)
        # only a FRESH answer: a kept address is fine for the lane's ssh (a stranger fails its host
        # key check) but must never carry an FTP login (review 2026-09-30)
        if ln.get("bound") and ln.get("src") and ln.get("ip") and ln.get("fresh"):
            return ln["ip"], ln["src"]
    return None


def last() -> dict | None:
    """The most recent detection, WITHOUT detecting — for the state poll."""
    with _lock:
        return _last["link"]
