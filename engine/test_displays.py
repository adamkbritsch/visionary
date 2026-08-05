import unittest
from unittest import mock

import displays


class DisplayKey(unittest.TestCase):
    """PURE. The key is what a saved priority list persists, so it must survive replug and
    reboot — which means it must never contain CGDirectDisplayID (reassigned freely; 1 and 5
    on the dev machine today) and must be identical across two reads of the same panel."""

    def test_uuid_wins_when_present(self):
        k = displays.display_key("ABC-123", 1552, 40961, 0, 3456, 2234)
        self.assertEqual(k, "uuid:ABC-123")

    def test_falls_back_to_vendor_hardware_when_there_is_no_uuid(self):
        k = displays.display_key(None, 1552, 40961, 7, 3840, 2160)
        self.assertEqual(k, "vhw:1552:40961:7:3840x2160")

    def test_the_key_never_contains_the_display_id(self):
        # The id is deliberately NOT an input — this test fails loudly if anyone adds it.
        import inspect
        self.assertNotIn("display_id", inspect.signature(displays.display_key).parameters)

    def test_two_identical_panels_are_distinguished_by_uuid_but_not_by_hardware(self):
        a = displays.display_key("UUID-A", 1552, 40961, 0, 3840, 2160)
        b = displays.display_key("UUID-B", 1552, 40961, 0, 3840, 2160)
        self.assertNotEqual(a, b)
        # Documented weakness of the fallback: same vendor/model/serial/size collides.
        self.assertEqual(displays.display_key(None, 1552, 40961, 0, 3840, 2160),
                         displays.display_key(None, 1552, 40961, 0, 3840, 2160))

    def test_missing_hardware_numbers_degrade_to_zeros_not_a_crash(self):
        self.assertEqual(displays.display_key(None, None, None, None, None, None),
                         "vhw:0:0:0:0x0")


class Lookup(unittest.TestCase):
    FAKE = [
        {"key": "uuid:MAIN", "main": True, "id": 1},
        {"key": "uuid:HDMI", "main": False, "id": 5},
    ]

    def test_find_returns_the_matching_display(self):
        with mock.patch.object(displays, "enumerate_displays", return_value=self.FAKE):
            self.assertEqual(displays.find("uuid:HDMI")["id"], 5)

    def test_find_returns_none_when_that_screen_is_unplugged(self):
        # The priority list can name a display that is not attached — that must read as
        # "absent", never as "fall through to whatever is first".
        with mock.patch.object(displays, "enumerate_displays", return_value=self.FAKE):
            self.assertIsNone(displays.find("uuid:GONE"))
            self.assertIsNone(displays.find(""))
            self.assertIsNone(displays.find(None))

    def test_main_display_picks_the_one_flagged_main(self):
        with mock.patch.object(displays, "enumerate_displays", return_value=self.FAKE):
            self.assertEqual(displays.main_display()["key"], "uuid:MAIN")

    def test_main_display_is_none_when_nothing_is_flagged(self):
        with mock.patch.object(displays, "enumerate_displays", return_value=[]):
            self.assertIsNone(displays.main_display())


class LiveEnumeration(unittest.TestCase):
    """Against the real CoreGraphics on this machine. Asserts only invariants that hold for
    ANY Mac, so it is not pinned to the dev rig's two screens."""

    def setUp(self):
        try:
            self.ds = displays.enumerate_displays()
        except Exception as e:                      # headless CI / no window server
            self.skipTest("CoreGraphics unavailable: %s" % e)
        if not self.ds:
            self.skipTest("no active displays")

    def test_exactly_one_display_is_main_and_it_sorts_first(self):
        self.assertEqual(sum(1 for d in self.ds if d["main"]), 1)
        self.assertTrue(self.ds[0]["main"])

    def test_the_main_display_sits_at_the_global_origin(self):
        # Everything downstream (cliclick coordinates, screencapture -R) is in this space,
        # and the whole coordinate-translation change rests on main being (0, 0).
        self.assertEqual(self.ds[0]["origin"], (0.0, 0.0))

    def test_every_display_reports_a_usable_geometry_and_scale(self):
        for d in self.ds:
            self.assertGreater(d["backing"][0], 0, d["key"])
            self.assertGreater(d["size_pt"][0], 0, d["key"])
            self.assertAlmostEqual(d["scale"], d["backing"][0] / d["size_pt"][0], places=6)

    def test_keys_are_unique_and_stable_across_two_reads(self):
        keys = [d["key"] for d in self.ds]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [d["key"] for d in displays.enumerate_displays()])

    def test_eligibility_uses_the_existing_rule_unchanged(self):
        # This module decides NOTHING about eligibility — preflight.match_display stays the
        # single rule and versions.REQUIRED_BACKING_SCALE stays the invariant.
        import preflight, versions
        for d in self.ds:
            got = preflight.match_display(d["backing"][0], d["backing"][1],
                                          d["scale"], d["builtin"])
            if abs(d["scale"] - versions.REQUIRED_BACKING_SCALE) > 0.01:
                self.assertIsNone(got, "a non-2x display must never qualify: %s" % d["key"])


if __name__ == "__main__":
    unittest.main()
