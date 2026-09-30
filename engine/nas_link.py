"""Which of the Mac's network links reaches the NAS — so the Dolby Vision lane moves its terabytes over
the WIRED one (user-dictated 2026-09-30: "they should be able to download/upload on the ethernet
connection that's connected to this laptop to the nas").

Left to itself, ssh took Wi-Fi. The NAS's mDNS name resolves to IPv6 link-local addresses as well
as its IPv4 one, and ssh prefers IPv6, and the link-local address it picked was scoped to the
Wi-Fi interface — so while a 2.5 GbE adapter sat on the same subnet, every conversion's download
and upload crossed the air (live-caught 2026-09-30: one lane session on en0 at ~57 MB/s, another
tool's IPv4 session on the Ethernet adapter at ~156 MB/s).

So the lane's ssh is pinned: IPv4 only, and bound (`ssh -B`) to the first ACTIVE, NON-Wi-Fi
interface that has an address on the NAS's subnet, in the Mac's own network service order. With no
wired link (the laptop undocked) it falls back to ordinary routing, and the panel says which link
the transfers are on.

PURE (unit-tested): parse_ifconfig, parse_hardware_ports, parse_service_order, pick_wired,
link_speed. The rest runs `ifconfig` / `networksetup` locally — never anything on the NAS.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time

CACHE_SECS = 30
# Never a path to the NAS even when they carry an address on its subnet: tunnels, the
# Thunderbolt bridge, Apple's peer-to-peer Wi-Fi links, loopback.
_NOT_A_LINK = ("lo", "utun", "bridge", "awdl", "llw", "anpi", "gif", "stf", "ap", "ipsec")

_lock = threading.Lock()
_cache = {"at": 0.0, "key": None, "link": None}


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
        if dev.startswith(_NOT_A_LINK) or is_wifi(dev, ports) or info.get("active") is False:
            continue
        try:
            net = ipaddress.ip_network(f"{info['inet']}/{info['mask']}", strict=False)
        except (ValueError, TypeError):
            continue
        if nas in net:
            cands.append((rank.get(dev, 999), dev))
    return min(cands)[1] if cands else None


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


def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def nas_ipv4(host: str):
    """The NAS's IPv4 address for a name or address (mDNS names resolve through the system)."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 22, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except OSError:
        return None


def detect(host: str) -> dict:
    """{"iface", "kind", "name", "speed", "wired", "ip"} for the link the lane should use to reach
    `host` — the wired one when there is one, else whatever the routing table picks. Cached."""
    with _lock:
        if _cache["key"] == host and time.monotonic() - _cache["at"] < CACHE_SECS:
            return _cache["link"]
    ip = nas_ipv4(host)
    ifaces = parse_ifconfig(_run(["ifconfig"]))
    ports = parse_hardware_ports(_run(["networksetup", "-listallhardwareports"]))
    order = parse_service_order(_run(["networksetup", "-listnetworkserviceorder"]))
    names = {dev: n for n, dev in order}
    dev = pick_wired(ip, ifaces, ports, order) if ip else None
    wired = dev is not None
    if not dev and ip:
        m = re.search(r"interface:\s*(\S+)", _run(["route", "-n", "get", ip]))
        dev = m.group(1) if m else None
    kind = ("Wi-Fi" if dev and is_wifi(dev, ports)
            else "Ethernet" if wired else (ports.get(dev) or dev or "unknown"))
    link = {"iface": dev, "wired": wired, "kind": kind, "ip": ip,
            "name": names.get(dev) or ports.get(dev) or dev,
            "speed": link_speed((ifaces.get(dev) or {}).get("media", "")) if dev else ""}
    with _lock:
        _cache.update(at=time.monotonic(), key=host, link=link)
    return link


def last() -> dict | None:
    """The most recent detection, WITHOUT detecting — for the state poll."""
    with _lock:
        return _cache["link"]
