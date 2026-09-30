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


class Choose(unittest.TestCase):
    def setUp(self):
        self.i = nas_link.parse_ifconfig(IFCONFIG)
        self.p = nas_link.parse_hardware_ports(PORTS)
        self.o = nas_link.parse_service_order(ORDER)

    def test_priorities(self):
        c = lambda pr, i=None: nas_link.choose("192.168.1.195", i or self.i, self.p, self.o, pr)
        self.assertEqual(c("ethernet"), ("en12", False))
        self.assertEqual(c("wifi"), ("en0", False))
        self.assertEqual(c("ethernet_only"), ("en12", False))
        no_eth = {k: v for k, v in self.i.items() if k != "en12"}
        self.assertEqual(c("ethernet", no_eth), ("en0", False))
        self.assertEqual(c("ethernet_only", no_eth), (None, True))
        self.assertEqual(c("bogus"), ("en12", False))                 # junk reads as the default

    def test_valid_priority(self):
        self.assertEqual(nas_link.valid_priority("wifi"), "wifi")
        self.assertEqual(nas_link.valid_priority(None), "ethernet")

    def test_no_system_commands_from_a_test(self):
        self.assertEqual(nas_link._run(["ifconfig"]), "")
        self.assertIsNone(nas_link.nas_ipv4("adamsnas.local"))


class Resolution(unittest.TestCase):
    def setUp(self):
        nas_link._negative.clear()
        nas_link._known.clear()

    def test_an_unknown_address_is_never_blamed_on_the_cable(self):
        i = nas_link.parse_ifconfig(IFCONFIG)
        p = nas_link.parse_hardware_ports(PORTS)
        o = nas_link.parse_service_order(ORDER)
        self.assertEqual(nas_link.choose(None, i, p, o, "ethernet"), (None, False))
        # Ethernet only still pins to the cable — "never over the air" holds without an address
        self.assertEqual(nas_link.choose(None, i, p, o, "ethernet_only"), ("en12", False))
        no_eth = {k: v for k, v in i.items() if k != "en12"}
        # the iPhone USB tether (en14) is active and not Wi-Fi — and still not the Ethernet
        self.assertEqual(nas_link.choose(None, no_eth, p, o, "ethernet_only"), (None, True))

    def test_a_phone_tether_is_never_the_ethernet(self):
        p = nas_link.parse_hardware_ports(PORTS)
        self.assertFalse(nas_link.is_ethernet_like("en14", p))
        self.assertTrue(nas_link.is_ethernet_like("en12", p))
        self.assertTrue(nas_link.is_ethernet_like("en9", p))       # "AX88179A": a USB LAN chip
        self.assertFalse(nas_link.is_ethernet_like("en0", p))

    def _live(self, fn, deadline=0.2):
        return (mock.patch.object(nas_link, "_under_test", return_value=False),
                mock.patch.object(nas_link.socket, "getaddrinfo", side_effect=fn),
                mock.patch.object(nas_link, "RESOLVE_DEADLINE", deadline))

    def test_a_slow_lookup_is_bounded_and_never_remembered_as_a_failure(self):
        import time as _t
        def slow(*a, **k):
            _t.sleep(1.0)
            return [(None, None, None, None, ("192.168.1.195", 22))]
        a, b, c = self._live(slow)
        with a, b, c:
            t0 = _t.monotonic()
            self.assertIsNone(nas_link.nas_ipv4("nas.local"))       # slow, nothing known yet
            self.assertLess(_t.monotonic() - t0, 0.8)
            _t.sleep(1.1)                                           # ...the answer lands
            self.assertEqual(nas_link.nas_ipv4("nas.local"), "192.168.1.195")   # it stands in
        self.assertNotIn("nas.local", nas_link._negative)

    def test_after_a_real_failure_a_kept_answer_from_this_network_stands_in(self):
        home = ("192.168.1.92", "192.168.1.117")
        answers = iter([[(None, None, None, None, ("192.168.1.195", 22))], OSError("mdns hiccup")])
        def look(*a, **k):
            r = next(answers)
            if isinstance(r, Exception):
                raise r
            return r
        a, b, c = self._live(look)
        with a, b, c:
            self.assertEqual(nas_link.nas_ipv4("nas.local", home), "192.168.1.195")
            self.assertTrue(nas_link.last_was_fresh("nas.local"))
            self.assertEqual(nas_link.nas_ipv4("nas.local", home), "192.168.1.195")   # kept
            self.assertFalse(nas_link.last_was_fresh("nas.local"))
            self.assertEqual(nas_link.nas_ipv4("nas.local", home), "192.168.1.195")   # still kept
            # ...but never on another network (the café on the same 192.168.1.0/24)
            self.assertIsNone(nas_link.nas_ipv4("nas.local", ("192.168.1.50",)))

    def test_a_kept_answer_ages_by_the_wall_clock(self):
        nas_link._known["nas.local"] = (nas_link.time.time() - nas_link.KNOWN_SECS - 1,
                                        "192.168.1.195", None)
        nas_link._negative["nas.local"] = nas_link.time.time() + 60
        with mock.patch.object(nas_link, "_under_test", return_value=False):
            self.assertIsNone(nas_link.nas_ipv4("nas.local"))

    def test_a_failed_lookup_is_not_repeated(self):
        calls = []
        def fail(*a, **k):
            calls.append(1)
            raise OSError("no answer")
        a, b, c = self._live(fail)
        with a, b, c:
            self.assertIsNone(nas_link.nas_ipv4("away.local"))
            self.assertIsNone(nas_link.nas_ipv4("away.local"))
        self.assertEqual(len(calls), 1)


class LanRoute(unittest.TestCase):
    def test_an_ip_literal_is_never_taken_as_the_home_lan(self):
        with mock.patch.object(nas_link, "detect", side_effect=AssertionError("not asked")):
            self.assertIsNone(nas_link.lan_route(["192.168.1.195", "100.101.182.68"]))

    def test_the_first_host_the_link_reaches_directly(self):
        links = {"adamsnas.local": {"bound": True, "ip": "192.168.1.195", "src": "192.168.1.92",
                                    "fresh": True}}
        with mock.patch.object(nas_link, "detect", side_effect=lambda h: links[h]):
            self.assertEqual(nas_link.lan_route(["100.101.182.68", "adamsnas.local"]),
                             ("192.168.1.195", "192.168.1.92"))

    def test_a_kept_address_never_carries_the_ftp_login(self):
        stale = {"bound": True, "ip": "192.168.1.195", "src": "192.168.1.117", "fresh": False}
        with mock.patch.object(nas_link, "detect", return_value=stale):
            self.assertIsNone(nas_link.lan_route(["adamsnas.local"]))

    def test_nothing_direct_means_none(self):
        with mock.patch.object(nas_link, "detect", return_value={"bound": False, "unavailable": True}):
            self.assertIsNone(nas_link.lan_route(["a", "b"]))


class Detect(unittest.TestCase):
    def _run(self, cmd):
        return {"ifconfig": IFCONFIG, "networksetup": PORTS if "-listallhardwareports" in cmd
                else ORDER, "route": "   interface: en0\n"}[cmd[0]]

    def setUp(self):
        nas_link._cache.clear()

    def test_reports_the_wired_link(self):
        with mock.patch.object(nas_link, "_run", side_effect=self._run), \
             mock.patch.object(nas_link, "nas_ipv4", return_value="192.168.1.195"):
            link = nas_link.detect("adamsnas.local", "ethernet")
        self.assertEqual(link, {"iface": "en12", "bound": True, "wired": True, "kind": "Ethernet",
                                "ip": "192.168.1.195", "unresolved": False, "fresh": False,
                                "src": "192.168.1.92",
                                "priority": "ethernet", "unavailable": False,
                                "name": "Living Room 5G LAN", "speed": "2.5 GbE"})

    def _detect(self, priority, text=IFCONFIG):
        def run(cmd):
            return text if cmd[0] == "ifconfig" else self._run(cmd)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link, "nas_ipv4", return_value="192.168.1.195"):
            return nas_link.detect("adamsnas.local", priority)

    NO_ETH = IFCONFIG.replace("status: active\nen14", "status: inactive\nen14")

    def test_undocked_ethernet_first_falls_back_to_wifi_and_says_so(self):
        link = self._detect("ethernet", self.NO_ETH)
        self.assertEqual((link["iface"], link["bound"], link["wired"], link["kind"]),
                         ("en0", True, False, "Wi-Fi"))

    def test_wifi_first_binds_the_wifi_even_with_a_cable(self):
        link = self._detect("wifi")
        self.assertEqual((link["iface"], link["bound"], link["src"]), ("en0", True, "192.168.1.117"))

    def test_ethernet_only_without_a_cable_is_unavailable_not_wifi(self):
        link = self._detect("ethernet_only", self.NO_ETH)
        self.assertEqual((link["iface"], link["bound"], link["unavailable"]), (None, False, True))

    def test_each_priority_is_cached_on_its_own(self):
        self.assertEqual(self._detect("ethernet")["iface"], "en12")
        self.assertEqual(self._detect("wifi")["iface"], "en0")
        self.assertEqual(self._detect("ethernet")["iface"], "en12")

    def test_unknown_address_under_ethernet_only_uses_the_port_that_last_reached_the_nas(self):
        nas_link._last_wired.clear()
        self._detect("ethernet_only")                          # address known: en12 remembered
        nas_link._cache.clear()
        def run(cmd):
            return IFCONFIG if cmd[0] == "ifconfig" else self._run(cmd)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link, "nas_ipv4", return_value=None):
            link = nas_link.detect("adamsnas.local", "ethernet_only")
        self.assertEqual((link["iface"], link["bound"], link["unavailable"]), ("en12", True, False))

    def test_the_lane_link_is_remembered_for_the_poll(self):
        link = self._detect("ethernet")
        nas_link.remember(link)
        got = dict(nas_link.last())
        self.assertLess(abs(got.pop("at") - __import__("time").time()), 5)   # stamped, so age is known
        self.assertEqual(got, link)


if __name__ == "__main__":
    unittest.main()
