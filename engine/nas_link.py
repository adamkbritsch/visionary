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
Tailscale address it otherwise tries first (transfer.connect). Since 2026-10-01 the lane moves its
files over that same FTP too (nas_ftp.py — user: "it shouldn't be going over ssh at all"), so the
binding above is the FTP connection's source address, and Ethernet only is
transfer.connect(lan_only=True).

PURE (unit-tested): parse_ifconfig, parse_hardware_ports, parse_service_order, pick_wired,
pick_wifi, choose, link_speed, valid_priority. The rest runs `ifconfig` / `networksetup` locally —
never anything on the NAS.
"""
from __future__ import annotations

import ipaddress
import os
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
        # a proof is only marked for re-checking, never dropped here: forget() runs after any
        # failed LAN transfer, and a running leg must not lose its link to a sample not yet back
        for k, v in list(_ts_seen.items()):
            _ts_seen[k] = (0.0,) + tuple(v[1:])
        _ifc_cache.update(at=0.0, ifaces=None)
        _mac_cache.clear()


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


TAILSCALE_CLIS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale")
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_PONG = re.compile(r"pong from .*?\((?P<ts>[0-9.]+)\) via (?P<ip>\d{1,3}(?:\.\d{1,3}){3}):\d+")
_PONG_ANY = re.compile(r"pong from .*?\((?P<ts>[0-9.]+)\) via ")
# tailscale ip -> (wall time sampled, attachment, LAN ipv4 or None, wall time last PROVEN). The
# attachment of a proof is the Mac's interface(s) on the proven address's subnet — what a pulled
# cable or a move to another network changes, and a Wi-Fi lease renewal elsewhere does not; of a
# negative sample, the Mac's whole address set (review 2026-10-06).
_ts_seen = {}
_ts_refreshing = set()          # single-flight: one background sample per address at a time
_ifc_cache = {"at": 0.0, "ifaces": None}


def _spawn(fn):
    threading.Thread(target=fn, daemon=True, name="nas-tailscale-proof").start()


def _ifaces_now() -> dict:
    """parse_ifconfig, re-read at most every few seconds (a leg asks every 2 s)."""
    now = time.time()
    with _lock:
        if _ifc_cache["ifaces"] is not None and 0 <= now - _ifc_cache["at"] < 5:
            return _ifc_cache["ifaces"]
    ifaces = parse_ifconfig(_run(["ifconfig"]))
    with _lock:
        _ifc_cache.update(at=now, ifaces=ifaces)
    return ifaces


def attachment(ip, ifaces) -> tuple:
    """PURE: the active interfaces whose subnet holds `ip`, as (device, address, prefix)."""
    try:
        nas = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return ()
    out = []
    for dev, i in ifaces.items():
        if i.get("active") is False or not i.get("inet"):
            continue
        try:
            if nas in ipaddress.ip_network(f"{i['inet']}/{i['mask']}", strict=False):
                out.append((dev, i["inet"], i["mask"]))
        except (ValueError, TypeError, KeyError):
            continue
    return tuple(sorted(out))


def _proof_key(ip, ifaces) -> tuple:
    return attachment(ip, ifaces) if ip else net_signature(ifaces)


def local_address_active(addr) -> bool:
    """`addr` is still an address of an ACTIVE interface on this Mac — local only, no I/O to the
    NAS. What a pulled cable changes; what a NAS that stalls a login does not."""
    return any(i.get("inet") == addr and i.get("active") is not False
               for i in _ifaces_now().values())


def is_tailscale(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in _TAILSCALE_NET
    except ValueError:
        return False


def parse_pong(text: str, ts_ip: str):
    """PURE: the private IPv4 a `tailscale ping` reply from `ts_ip` came back over DIRECTLY, or
    None — relayed through DERP, a public address, another peer's reply, or nothing at all."""
    for m in _PONG.finditer(text or ""):
        if m.group("ts") != ts_ip:
            continue
        try:
            ip = ipaddress.ip_address(m.group("ip"))
        except ValueError:
            continue
        if (ip.version == 4 and ip.is_private and ip not in _TAILSCALE_NET
                and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified):
            return str(ip)
    return None


def _ts_sample(cli, ts_ip, ifaces):
    """Ping once (up to five pongs, stopping at the first direct one) and record the result.
    A proof is kept through samples that got NO answer at all (the NAS's tailscaled restarting,
    a lost packet) for KNOWN_SECS on the same attachment — one bad sample must not flip a running
    leg's link — and dropped at once on CONTRARY evidence: the NAS answered, but not directly
    from a LAN address (review 2026-10-06)."""
    out = _run([cli, "ping", "--c", "5", "--timeout", "2s", ts_ip])
    ip = parse_pong(out, ts_ip)
    answered = any(m.group("ts") == ts_ip for m in _PONG_ANY.finditer(out or ""))
    now = time.time()
    with _lock:
        prev = _ts_seen.get(ts_ip)
        if ip:
            _ts_seen[ts_ip] = (now, _proof_key(ip, ifaces), ip, now)
        elif (not answered and prev and prev[2] and prev[3] is not None
              and prev[1] == _proof_key(prev[2], ifaces) and 0 <= now - prev[3] < KNOWN_SECS):
            _ts_seen[ts_ip] = (now, prev[1], prev[2], prev[3])
        else:
            _ts_seen[ts_ip] = (now, _proof_key(None, ifaces), None, None)
        return _ts_seen[ts_ip][2]


def tailscale_lan_ip(ts_ip: str, wait: bool = True):
    """The NAS's LAN address as Tailscale proves it, or None. A `tailscale ping` is answered
    by the peer's own tailscaled and sealed with its key, and says which path the answer took:
    "pong from adamsnas-tailscale (100.101.182.68) via 192.168.1.195:41691" means the NAS itself
    answered from that address, on a network this Mac reaches directly, a moment ago. That is as
    strong as a fresh mDNS answer — stronger: a stranger on a café's 192.168.1.x cannot forge it
    — and it still works when the NAS's mDNS responder has died (live 2026-10-06: avahi silent,
    so every transfer of the day went over Tailscale at ~7 MB/s instead of the 2.5 GbE cable).

    A proof is used only on the attachment it was made on (the Mac's interface on that subnet)
    and only within KNOWN_SECS of its last confirmation, by the wall clock — a monotonic clock
    stops while the lid is closed, which would carry a home proof into a café (nas_ipv4's rule).
    It is re-sampled every CACHE_SECS OFF the caller's thread: a leg asks every 2 s and must never
    stall on a ping (review 2026-10-06). The one blocking sample is a cold one, and only when
    `wait` — a connect choosing its route; a leg's checks pass wait=False. Never from a test."""
    if not is_tailscale(ts_ip):
        return None
    cli = next((c for c in TAILSCALE_CLIS if os.path.exists(c)), None)
    if not cli:
        return None
    ifaces = _ifaces_now()
    now = time.time()
    with _lock:
        hit = _ts_seen.get(ts_ip)
    usable = (hit is not None and hit[1] == _proof_key(hit[2], ifaces)
              and (hit[2] is None or (hit[3] is not None and 0 <= now - hit[3] < KNOWN_SECS)))
    if usable and 0 <= now - hit[0] < CACHE_SECS:
        return hit[2]
    if not usable and wait:
        return _ts_sample(cli, ts_ip, ifaces)    # cold here: the first answer, now
    with _lock:
        start = ts_ip not in _ts_refreshing
        if start:
            _ts_refreshing.add(ts_ip)
    if start:
        def refresh():
            try:
                _ts_sample(cli, ts_ip, _ifaces_now())
            finally:
                with _lock:
                    _ts_refreshing.discard(ts_ip)
        _spawn(refresh)
    return hit[2] if usable else None


# ---- the third proof: the NAS's own hardware address -------------------------------------------
# Expurgate added this on 2026-10-07 (~/discretion/engine/naslink.py): with mDNS silent and Tailscale
# sometimes reaching the NAS through the house's PUBLIC address (a router hairpin) instead of
# directly, neither proof above can bind the cable. ARP still knows who answers at the LAN address.
#
# Visionary's rule is stricter about LEARNING than Expurgate's. Expurgate records the hardware
# address after any successful login to a configured literal, and lists the literal first in its
# plain host order — so at a café whose 192.168.1.195 runs an FTP server that accepts any login, it
# would first send the NAS credentials there and then learn the stranger. Here an address is learned
# only after a login over a LAN route that was ALREADY PROVEN (a fresh mDNS answer or a direct
# Tailscale pong): the hardware address then belongs to the box those proofs vouched for. Later the
# literal is proven again when ARP shows exactly that one address and an active interface is on its
# subnet — a café's 192.168.1.195 is a different box with a different address. No literal needs to be
# added to the configured hosts for any of this.
ARP = "/usr/sbin/arp"
MAC_BOOK = os.path.expanduser("~/.topaz-pipeline/nas_macs.json")
_mac_cache = {}                 # (ip, attachment) -> (wall time, proven)


def parse_arp(text: str, ip: str, iface=None) -> set:
    """PURE: the hardware addresses `arp -n <ip>` reports for `ip`, normalised (zero-padded, lower)
    — only those seen on `iface` when one is given (the interface a login would bind to)."""
    out = set()
    for line in (text or "").splitlines():
        if "(%s)" % ip not in line:
            continue
        if iface is not None:
            on = re.search(r" on (\S+)", line)
            if not on or on.group(1) != iface:
                continue
        m = re.search(r" at ((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})\b", line)
        if m:
            out.add(":".join("%02x" % int(b, 16) for b in m.group(1).split(":")))
    return out


def private_literal(host) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        return False
    return (ip.version == 4 and ip.is_private and ip not in _TAILSCALE_NET and not ip.is_loopback
            and not ip.is_link_local)


def _mac_book() -> dict:
    try:
        import json
        with open(MAC_BOOK) as f:
            d = json.load(f)
        macs = d.get("macs") if isinstance(d, dict) else None
        return dict(macs) if isinstance(macs, dict) else {}
    except (OSError, ValueError):
        return {}


def _arp_macs(ip, populate=True, iface=None) -> set:
    macs = parse_arp(_run([ARP, "-n", ip]), ip, iface)
    if not macs and populate and not _under_test():
        try:                       # no entry yet: one SYN fills it — no login, no credentials
            socket.create_connection((ip, 21), timeout=1.0).close()
        except OSError:
            pass
        macs = parse_arp(_run([ARP, "-n", ip]), ip, iface)
    return macs


def learn_mac(ip, src=None):
    """After a SUCCESSFUL login over a PROVEN LAN route to `ip` bound to local address `src`
    (transfer.connect calls this only then): remember the one hardware address answering there, as
    seen on that route's interface. Two would mean ARP cannot say."""
    if _under_test() or not private_literal(ip):
        return
    iface = next((d for d, i in _ifaces_now().items() if src and i.get("inet") == src), None)
    if src and iface is None:
        return                       # the bound interface is gone already: learn nothing
    macs = _arp_macs(ip, populate=False, iface=iface)
    if len(macs) != 1:
        return
    mac = next(iter(macs))
    book = _mac_book()
    if book.get(ip) == mac:
        return
    book[ip] = mac
    try:
        import json
        os.makedirs(os.path.dirname(MAC_BOOK), exist_ok=True)
        tmp = MAC_BOOK + ".part"
        with open(tmp, "w") as f:
            json.dump({"macs": book, "learned": time.time()}, f, indent=1, sort_keys=True)
        os.replace(tmp, MAC_BOOK)
    except OSError:
        pass
    with _lock:
        _mac_cache.clear()


def mac_proven(ip, wait: bool = True, iface=None) -> bool:
    """`ip` (a private literal whose hardware address was learned) answers with that same single
    address — on `iface`, the interface the login would bind to, when given — on a subnet an active
    interface of this Mac is on. Cached CACHE_SECS per attachment and interface. `wait=False` never
    sends the SYN that fills an empty ARP entry (a leg's 2 s check), and an answer it could only
    give WITHOUT that fill is not cached, so the next connect still fills and asks (review
    2026-10-07)."""
    if not private_literal(ip):
        return False
    known = _mac_book().get(ip)
    if not known:
        return False
    ifaces = _ifaces_now()
    att = attachment(ip, ifaces)
    if not att:
        return False
    key = (ip, att, iface)
    now = time.time()
    with _lock:
        hit = _mac_cache.get(key)
    if hit and 0 <= now - hit[0] < CACHE_SECS:
        return hit[1]
    macs = _arp_macs(ip, populate=wait, iface=iface)
    proven = macs == {known}
    if proven or wait or macs:
        with _lock:
            _mac_cache[key] = (time.time(), proven)
    return proven


def lan_link(hosts, wait: bool = True):
    """The detect() link for the NAS's LAN address, when the chosen link reaches it DIRECTLY and
    the address is PROVEN to be the NAS's, else None. Never None because of ethernet_only: that
    choice only makes the Dolby Vision lane wait.

    Proof is one of two things, never a bare address: a bare 192.168.1.x matches half the networks
    in the world, and trying it first on someone else's would send the FTP login to whatever
    answered there (review 2026-09-30).
      1. a configured NAME that resolves FRESHLY on the local link (mDNS) — a kept answer could be
         a stranger's on another network (review 2026-09-30);
      2. a configured TAILSCALE address whose peer answers a tailscale ping directly from a LAN
         address (tailscale_lan_ip) — so a dead mDNS responder no longer costs the LAN route;
      3. a LAN address whose hardware address was learned over a route proven by 1 or 2 and still
         answers there alone (mac_proven) — so a router hairpin no longer costs it either."""
    for h in hosts or ():
        if is_literal(h):
            continue
        ln = detect(h)
        if ln.get("bound") and ln.get("src") and ln.get("ip") and ln.get("fresh"):
            return ln
    for h in hosts or ():
        ip = tailscale_lan_ip(h, wait)
        if not ip:
            continue
        ln = detect(ip)
        if ln.get("bound") and ln.get("src") and ln.get("ip"):
            return ln
    for ip in sorted(_mac_book()):
        if not private_literal(ip):
            continue
        ln = detect(ip)                 # where a login would BIND — the proof must hold there
        if (ln.get("bound") and ln.get("src") and ln.get("ip")
                and mac_proven(ip, wait, iface=ln.get("iface"))):
            return ln
    return None


def lan_route(hosts, wait: bool = True):
    """(NAS LAN address, local address to bind) for lan_link(hosts), or None — the pipeline's FTP
    then keeps its configured order. `wait=False` never pings on the caller's thread."""
    ln = lan_link(hosts, wait)
    return (ln["ip"], ln["src"]) if ln else None


def last() -> dict | None:
    """The most recent detection, WITHOUT detecting — for the state poll."""
    with _lock:
        return _last["link"]
