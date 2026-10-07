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
    def setUp(self):
        nas_link._ts_seen.clear()

    tearDown = setUp

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

    # ---- mDNS dead, Tailscale proves the address (live 2026-10-06: a whole day over Tailscale)
    LAN = {"bound": True, "ip": "192.168.1.195", "src": "192.168.1.92", "fresh": True,
           "iface": "en12"}
    SILENT = {"bound": False, "ip": None, "src": None, "fresh": False, "unresolved": True}

    def test_a_dead_mdns_name_falls_back_to_the_address_tailscale_proves(self):
        links = {"adamsnas.local": self.SILENT, "192.168.1.195": self.LAN}
        with mock.patch.object(nas_link, "detect", side_effect=lambda h: links[h]), \
             mock.patch.object(nas_link, "tailscale_lan_ip",
                               side_effect=lambda h, *a: "192.168.1.195" if h == "100.101.182.68" else None):
            self.assertEqual(nas_link.lan_route(["100.101.182.68", "adamsnas.local"]),
                             ("192.168.1.195", "192.168.1.92"))

    def test_a_fresh_mdns_answer_still_comes_first(self):
        with mock.patch.object(nas_link, "detect", return_value=self.LAN), \
             mock.patch.object(nas_link, "tailscale_lan_ip",
                               side_effect=AssertionError("not asked")):
            self.assertEqual(nas_link.lan_route(["100.101.182.68", "adamsnas.local"]),
                             ("192.168.1.195", "192.168.1.92"))

    def test_a_proven_address_no_link_reaches_directly_is_not_a_route(self):
        links = {"adamsnas.local": self.SILENT,
                 "192.168.1.195": {"bound": False, "ip": "192.168.1.195", "src": None}}
        with mock.patch.object(nas_link, "detect", side_effect=lambda h: links[h]), \
             mock.patch.object(nas_link, "tailscale_lan_ip", return_value="192.168.1.195"):
            self.assertIsNone(nas_link.lan_route(["100.101.182.68", "adamsnas.local"]))

    def test_no_tailscale_proof_means_no_route(self):
        with mock.patch.object(nas_link, "detect", return_value=self.SILENT), \
             mock.patch.object(nas_link, "tailscale_lan_ip", return_value=None):
            self.assertIsNone(nas_link.lan_route(["100.101.182.68", "adamsnas.local"]))


class TailscaleProof(unittest.TestCase):
    PONG = "pong from adamsnas-tailscale (100.101.182.68) via 192.168.1.195:41691 in 1ms\n"
    DERP = "pong from adamsnas-tailscale (100.101.182.68) via DERP(den) in 31ms\n"

    def setUp(self):
        nas_link._ts_seen.clear()
        nas_link._ts_refreshing.clear()
        nas_link._ifc_cache.update(at=0.0, ifaces=None)
        p = mock.patch.object(nas_link, "_spawn", side_effect=lambda fn: fn())   # inline, in tests
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        nas_link._ts_seen.clear()
        nas_link._ts_refreshing.clear()
        nas_link._ifc_cache.update(at=0.0, ifaces=None)

    def test_a_direct_pong_names_the_lan_address(self):
        self.assertEqual(nas_link.parse_pong(self.PONG, "100.101.182.68"), "192.168.1.195")

    def test_anything_but_a_direct_private_answer_from_that_peer_proves_nothing(self):
        for text in ("pong from adamsnas-tailscale (100.101.182.68) via DERP(den) in 31ms",
                     "pong from adamsnas-tailscale (100.101.182.68) via 73.4.5.6:41641 in 9ms",
                     "pong from other (100.70.1.2) via 192.168.1.50:41641 in 1ms",
                     "pong from adamsnas-tailscale (100.101.182.68) via 100.99.1.1:41641 in 1ms",
                     "pong from adamsnas-tailscale (100.101.182.68) via 127.0.0.1:41641 in 1ms",
                     "pong from adamsnas-tailscale (100.101.182.68) via 169.254.3.4:41641 in 1ms",
                     "ping \"100.101.182.68\" timed out", "", None):
            self.assertIsNone(nas_link.parse_pong(text, "100.101.182.68"), text)

    def test_only_a_tailscale_address_is_ever_pinged(self):
        with mock.patch.object(nas_link, "_run", side_effect=AssertionError("not run")):
            for h in ("adamsnas.local", "192.168.1.195", "8.8.8.8", "garbage"):
                self.assertIsNone(nas_link.tailscale_lan_ip(h))

    def _runner(self, pong, ifconfig=IFCONFIG, pings=None):
        pings = [] if pings is None else pings
        def run(cmd):
            if cmd[0] == "ifconfig":
                return ifconfig
            pings.append(cmd)
            return pong
        return run, pings

    def test_the_proof_is_cached_both_ways(self):
        run, pings = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68"), "192.168.1.195")
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68"), "192.168.1.195")
        self.assertEqual(len(pings), 1)
        self.assertEqual(pings[0][1:], ["ping", "--c", "5", "--timeout", "2s", "100.101.182.68"])
        nas_link._ts_seen.clear()
        run, pings = self._runner("")
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))
        self.assertEqual(len(pings), 1)

    def test_a_derp_first_pong_then_a_direct_one_is_proof(self):
        text = ("pong from adamsnas-tailscale (100.101.182.68) via DERP(den) in 31ms\n" + self.PONG)
        self.assertEqual(nas_link.parse_pong(text, "100.101.182.68"), "192.168.1.195")

    def test_a_proof_from_another_network_is_never_reused(self):
        run, pings = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
        nas_link._ifc_cache.update(at=0.0, ifaces=None)     # (re-read every few seconds in life)
        cafe = IFCONFIG.replace("192.168.1.92", "10.0.0.42").replace("192.168.1.117", "10.0.0.43")
        run2, pings2 = self._runner("pong from adamsnas-tailscale (100.101.182.68) via DERP(den) in 40ms",
                                    ifconfig=cafe)
        with mock.patch.object(nas_link, "_run", side_effect=run2), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))   # asked again, here
        self.assertEqual(len(pings2), 1)

    def test_the_proof_ages_by_the_wall_clock(self):
        run, pings = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
            t, sig, ip, proven = nas_link._ts_seen["100.101.182.68"]
            nas_link._ts_seen["100.101.182.68"] = (t - nas_link.CACHE_SECS - 1, sig, ip, proven)
            nas_link.tailscale_lan_ip("100.101.182.68")      # stale: re-sampled (behind the caller)
        self.assertEqual(len(pings), 2)

    def _proven(self):
        run, _ = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68"), "192.168.1.195")

    def test_forget_only_marks_the_proof_for_rechecking(self):
        self._proven()
        nas_link.forget()                               # a failed LAN transfer elsewhere
        spawned = []
        run, pings = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True), \
             mock.patch.object(nas_link, "_spawn", side_effect=lambda fn: spawned.append(fn)):
            # a running leg still sees its link at once; the re-check happens behind it
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68", wait=False), "192.168.1.195")
        self.assertEqual((len(pings), len(spawned)), (0, 1))

    def test_a_lease_change_on_another_interface_keeps_the_proof(self):
        self._proven()
        nas_link._ifc_cache.update(at=0.0, ifaces=None)
        other = IFCONFIG.replace("172.20.10.2", "172.20.10.9")     # the phone tether renewed
        run, pings = self._runner(self.PONG, ifconfig=other)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68", wait=False), "192.168.1.195")
        self.assertEqual(pings, [])

    def test_an_old_proof_is_never_used_for_a_login_without_a_fresh_one(self):
        self._proven()
        t, key, ip, proven = nas_link._ts_seen["100.101.182.68"]
        hours = 8 * 3600                                # the lid closed overnight
        nas_link._ts_seen["100.101.182.68"] = (t - hours, key, ip, proven - hours)
        run, pings = self._runner(self.DERP * 5)        # a café: the NAS only via a relay
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))   # re-proven first
        self.assertEqual(len(pings), 1)

    def test_attachment_is_the_interface_on_the_nas_subnet(self):
        ifaces = nas_link.parse_ifconfig(IFCONFIG)
        att = nas_link.attachment("192.168.1.195", ifaces)
        self.assertTrue(att and all(a[1].startswith("192.168.1.") for a in att))
        self.assertEqual(nas_link.attachment("10.9.9.9", ifaces), ())
        self.assertEqual(nas_link.attachment("garbage", ifaces), ())

    def test_a_local_address_is_active_only_while_its_interface_is(self):
        nas_link._ifc_cache.update(at=0.0, ifaces=None)
        with mock.patch.object(nas_link, "_run", return_value=IFCONFIG):
            self.assertTrue(nas_link.local_address_active("192.168.1.92"))
            self.assertFalse(nas_link.local_address_active("10.1.2.3"))

    def _age(self):
        t, sig, ip, proven = nas_link._ts_seen["100.101.182.68"]
        nas_link._ts_seen["100.101.182.68"] = (t - nas_link.CACHE_SECS - 1, sig, ip, proven)

    def test_a_sample_with_no_answer_keeps_the_proof(self):
        run, _ = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
        self._age()
        run, _ = self._runner('ping "100.101.182.68" timed out')      # nothing came back at all
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
            self.assertEqual(nas_link.tailscale_lan_ip("100.101.182.68"), "192.168.1.195")

    def test_contrary_evidence_drops_the_proof_at_once(self):
        run, _ = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
        self._age()
        run, _ = self._runner(self.DERP * 5)                 # it answered — only through a relay
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))

    def test_a_kept_proof_expires_without_a_fresh_one(self):
        run, _ = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
        t, sig, ip, proven = nas_link._ts_seen["100.101.182.68"]
        nas_link._ts_seen["100.101.182.68"] = (t - nas_link.CACHE_SECS - 1, sig, ip,
                                               proven - nas_link.KNOWN_SECS - 1)
        run, _ = self._runner("")
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True):
            nas_link.tailscale_lan_ip("100.101.182.68")
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))

    def test_a_leg_never_pings_on_its_own_thread(self):
        spawned = []
        run, pings = self._runner(self.PONG)
        with mock.patch.object(nas_link, "_run", side_effect=run), \
             mock.patch.object(nas_link.os.path, "exists", return_value=True), \
             mock.patch.object(nas_link, "_spawn", side_effect=lambda fn: spawned.append(fn)):
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68", wait=False))   # cold
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68", wait=False))
        self.assertEqual((len(pings), len(spawned)), (0, 1))  # one background sample, single-flight

    def test_no_tailscale_installed_is_simply_no_proof(self):
        with mock.patch.object(nas_link.os.path, "exists", return_value=False), \
             mock.patch.object(nas_link, "_run", side_effect=AssertionError("not run")):
            self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))

    def test_never_from_a_test_unless_fed(self):
        self.assertIsNone(nas_link.tailscale_lan_ip("100.101.182.68"))   # _run is silent in tests


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
