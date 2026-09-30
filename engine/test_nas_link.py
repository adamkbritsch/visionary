"""engine/nas_link.py — picking the wired link to the NAS. Fed the real text of this Mac's
`ifconfig` / `networksetup` output (2026-09-30), where the lane had been on Wi-Fi beside a 2.5 GbE
adapter on the same subnet."""
import unittest
from unittest import mock

import nas_link

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether c2:98:8b:46:98:85
\tinet6 fe80::106e:74e5:88b5:9685%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.117 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: autoselect
\tstatus: active
en9: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.140 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: autoselect (none)
\tstatus: inactive
en12: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 6c:1f:f7:26:b4:3a
\tinet 192.168.1.92 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: autoselect (2500Base-T <full-duplex>)
\tstatus: active
en14: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 172.20.10.2 netmask 0xfffffff0 broadcast 172.20.10.15
\tstatus: active
utun6: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.121.121.2 --> 100.121.121.2 netmask 0xffffffff
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.250 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
"""
PORTS = """Hardware Port: USB 10/100/1G/2.5G/5G LAN
Device: en12
Ethernet Address: 6c:1f:f7:26:b4:3a

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: c2:98:8b:46:98:85

Hardware Port: AX88179A
Device: en9
Ethernet Address: 00:11:22:33:44:55

Hardware Port: iPhone USB
Device: en14
Ethernet Address: n/a
"""
ORDER = """An asterisk (*) denotes that a network service is disabled.
(1) Living Room 5G LAN
(Hardware Port: USB 10/100/1G/2.5G/5G LAN, Device: en12)
(2) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)
(3) Bedroom LAN
(Hardware Port: AX88179A, Device: en9)
(*) Old Dock
(Hardware Port: ThinkPad TBT 3 Dock, Device: en13)
(9) Tailscale
(Hardware Port: io.tailscale.ipn.macos, Device: )
"""


class Parse(unittest.TestCase):
    def test_ifconfig(self):
        i = nas_link.parse_ifconfig(IFCONFIG)
        self.assertEqual(i["en12"], {"inet": "192.168.1.92", "mask": 24, "active": True,
                                     "media": "autoselect (2500Base-T <full-duplex>)"})
        self.assertFalse(i["en9"]["active"])
        self.assertNotIn("awdl0", i)

    def test_ports_and_order(self):
        self.assertEqual(nas_link.parse_hardware_ports(PORTS)["en0"], "Wi-Fi")
        self.assertEqual(nas_link.parse_service_order(ORDER),
                         [("Living Room 5G LAN", "en12"), ("Wi-Fi", "en0"), ("Bedroom LAN", "en9")])

    def test_speed(self):
        self.assertEqual(nas_link.link_speed("autoselect (2500Base-T <full-duplex>)"), "2.5 GbE")
        self.assertEqual(nas_link.link_speed("autoselect (1000baseT <full-duplex>)"), "1 GbE")
        self.assertEqual(nas_link.link_speed("10GBase-T <full-duplex>"), "10 GbE")
        self.assertEqual(nas_link.link_speed("autoselect"), "")


class Pick(unittest.TestCase):
    def setUp(self):
        self.i = nas_link.parse_ifconfig(IFCONFIG)
        self.p = nas_link.parse_hardware_ports(PORTS)
        self.o = nas_link.parse_service_order(ORDER)

    def test_the_active_ethernet_on_the_nas_subnet_wins_over_wifi(self):
        self.assertEqual(nas_link.pick_wired("192.168.1.195", self.i, self.p, self.o), "en12")

    def test_never_wifi_an_inactive_port_a_tunnel_or_the_bridge(self):
        i = {k: v for k, v in self.i.items() if k != "en12"}
        self.assertIsNone(nas_link.pick_wired("192.168.1.195", i, self.p, self.o))

    def test_another_subnet_is_not_a_path(self):
        self.assertIsNone(nas_link.pick_wired("10.0.0.5", self.i, self.p, self.o))

    def test_service_order_breaks_a_tie(self):
        i = dict(self.i)
        i["en9"] = dict(i["en9"], active=True)
        o = [("Bedroom LAN", "en9"), ("Living Room 5G LAN", "en12")]
        self.assertEqual(nas_link.pick_wired("192.168.1.195", i, self.p, o), "en9")

    def test_garbage_ip(self):
        self.assertIsNone(nas_link.pick_wired("adamsnas.local", self.i, self.p, self.o))


class Detect(unittest.TestCase):
    def _run(self, cmd):
        return {"ifconfig": IFCONFIG, "networksetup": PORTS if "-listallhardwareports" in cmd
                else ORDER, "route": "   interface: en0\n"}[cmd[0]]

    def setUp(self):
        nas_link._cache.update(at=0.0, key=None, link=None)

    def test_reports_the_wired_link(self):
        with mock.patch.object(nas_link, "_run", side_effect=self._run), \
             mock.patch.object(nas_link, "nas_ipv4", return_value="192.168.1.195"):
            link = nas_link.detect("adamsnas.local")
        self.assertEqual(link, {"iface": "en12", "wired": True, "kind": "Ethernet",
                                "ip": "192.168.1.195", "name": "Living Room 5G LAN",
                                "speed": "2.5 GbE"})
        self.assertEqual(nas_link.last(), link)

    def test_undocked_falls_back_to_the_route_and_says_it_is_wifi(self):
        no_eth = IFCONFIG.replace("status: active\nen14", "status: inactive\nen14")
        def run(cmd):
            return no_eth if cmd[0] == "ifconfig" else self._run(cmd)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link, "nas_ipv4", return_value="192.168.1.195"):
            link = nas_link.detect("adamsnas.local")
        self.assertEqual((link["iface"], link["wired"], link["kind"]), ("en0", False, "Wi-Fi"))


if __name__ == "__main__":
    unittest.main()
