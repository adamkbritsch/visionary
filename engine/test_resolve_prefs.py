"""engine/resolve_prefs.py — Visionary never leaves DaVinci Resolve opening on the pinned display
(live 2026-10-04: every DV pass saved the dummy plug as Resolve's primary display, and the user's
own Resolve opened there, invisible, and snapped back when moved)."""
import os
import struct
import tempfile
import unittest
from unittest import mock

import resolve_prefs as rp
import stages


def qstr(s):
    b = s.encode("utf-16-be")
    return struct.pack(">I", len(b)) + b


def qint(v):
    return struct.pack(">IB", rp.QT_INT, 0) + struct.pack(">i", v)


def layout_bytes(primary, secondary, projects, decoys=False):
    body = qstr("MainWindow.TopBarButtonStyle") + qint(7)
    if decoys:
        body += (qstr("Viewer.LastSize") + struct.pack(">IB", rp.QT_STRING, 0) + qstr("1920x1080")
                 + qstr(rp.PRIMARY) + struct.pack(">IB", rp.QT_STRING, 0) + qstr("x")   # as a string
                 + struct.pack(">I", 3) + rp.PRIMARY.encode("utf-16-be") + qint(9))      # bad prefix
    body += qstr(rp.PRIMARY) + qint(primary)
    if secondary is not None:
        body += qstr(rp.SECONDARY) + qint(secondary)
    return body + qstr(rp.PROJECTS) + qint(projects)


def blob(layouts):
    """The real file's shape: header, "__CurrentPreset", then LastUsedResolution and one
    QByteArray per layout (engine/resolve_prefs.layouts parses exactly this)."""
    out = struct.pack(">II", 2, 1) + qstr("__CurrentPreset") + struct.pack(">I", len(layouts) + 1)
    out += qstr("LastUsedResolution") + struct.pack(">IB", rp.QT_STRING, 0) + qstr("1728x1117")
    for name, data in layouts:
        out += qstr(name) + struct.pack(">IB", rp.QT_BYTES, 0) + struct.pack(">I", len(data)) + data
    return out


LIVE = [("2560x1440", layout_bytes(0, 1, 0)),             # a normal two-screen layout
        ("1728x1117", layout_bytes(1, 0, 1, decoys=True))]  # what a pass on the dummy saved


class Parse(unittest.TestCase):
    def test_layouts_are_exact_byte_ranges(self):
        b = blob(LIVE)
        got = rp.layouts(b)
        self.assertEqual([g[0] for g in got], ["2560x1440", "1728x1117"])
        self.assertEqual(b[got[1][1]:got[1][2]], LIVE[1][1])

    def test_an_unknown_shape_yields_nothing_to_write(self):
        self.assertIsNone(rp.layouts(blob(LIVE) + b"\x00"))        # trailing bytes
        self.assertIsNone(rp.layouts(b"\x00\x00\x00\x02\x00\x00\x00\x01" + qstr("Other")))
        self.assertEqual(rp.fields(b"garbage"), [])

    def test_keys_belong_to_the_layout_they_sit_in(self):
        # "1920x1080" as a value inside the 1728x1117 block must not claim its keys
        vals = {(lay, key): val for lay, key, _o, val in rp.fields(blob(LIVE))}
        self.assertEqual(vals, {
            ("2560x1440", rp.PRIMARY): 0, ("2560x1440", rp.SECONDARY): 1,
            ("2560x1440", rp.PROJECTS): 0,
            ("1728x1117", rp.PRIMARY): 1, ("1728x1117", rp.SECONDARY): 0,
            ("1728x1117", rp.PROJECTS): 1})


class Unpinned(unittest.TestCase):
    def test_the_pinned_display_is_swapped_out_of_primary(self):
        vals = {("1728x1117", rp.PRIMARY): 1, ("1728x1117", rp.SECONDARY): 0,
                ("1728x1117", rp.PROJECTS): 1, ("2560x1440", rp.PRIMARY): 0,
                ("2560x1440", rp.SECONDARY): 1, ("2560x1440", rp.PROJECTS): 0}
        self.assertEqual(rp.unpinned(vals, host=1, layout="1728x1117"), {
            ("1728x1117", rp.PRIMARY): 0, ("1728x1117", rp.SECONDARY): 1,
            ("1728x1117", rp.PROJECTS): 0})

    def test_only_the_layout_of_the_screens_attached_now(self):
        # index 1 means the dummy only under the current screens; another layout's 1 may be a
        # real monitor it was saved with
        vals = {("1728x1117", rp.PRIMARY): 1, ("1728x1117", rp.SECONDARY): 0,
                ("1920x1080", rp.PRIMARY): 1, ("1920x1080", rp.SECONDARY): 0}
        self.assertEqual(rp.unpinned(vals, host=1, layout="1920x1080"), {
            ("1920x1080", rp.PRIMARY): 0, ("1920x1080", rp.SECONDARY): 1})

    def test_a_real_second_monitor_is_never_touched(self):
        vals = {("1920x1080", rp.PRIMARY): 2, ("1920x1080", rp.SECONDARY): 0,
                ("1920x1080", rp.PROJECTS): 2}
        self.assertEqual(rp.unpinned(vals, host=1, layout="1920x1080"), {})

    def test_already_on_main_needs_nothing(self):
        vals = {("1728x1117", rp.PRIMARY): 0, ("1728x1117", rp.PROJECTS): -1}
        self.assertEqual(rp.unpinned(vals, host=1, layout="1728x1117"), {})

    def test_a_real_monitor_as_secondary_stays(self):
        self.assertEqual(rp.unpinned({("L", rp.PRIMARY): 1, ("L", rp.SECONDARY): 2}, host=1,
                                     layout="L"), {("L", rp.PRIMARY): 0})

    def test_a_primary_without_a_secondary_key(self):
        self.assertEqual(rp.unpinned({("a", rp.PRIMARY): 1}, host=1, layout="a"),
                         {("a", rp.PRIMARY): 0})


class File(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "UI.preset")
        self.p = mock.patch.object(rp, "PRESET", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _file(self, upper=False, mode=0o666):
        b = blob(LIVE)
        hx = b.hex()
        raw = b"UI_Persistence\n" + (hx.upper() if hx else hx).encode() if upper else \
            b"UI_Persistence\n" + hx.encode()
        with open(self.path, "wb") as fh:
            fh.write(raw)
        os.chmod(self.path, mode)
        return raw, b

    def test_write_changes_only_those_ints_and_keeps_case_and_mode(self):
        old = os.umask(0o022)
        try:
            for upper in (False, True):
                raw, b = self._file(upper=upper)
                done = rp.write({("1728x1117", rp.PRIMARY): 0, ("1728x1117", rp.SECONDARY): 1})
                self.assertEqual(done, [("1728x1117", rp.PRIMARY, 1, 0),
                                        ("1728x1117", rp.SECONDARY, 0, 1)])
                new = open(self.path, "rb").read()
                self.assertEqual(len(new), len(raw))
                self.assertTrue(new.startswith(b"UI_Persistence\n"))
                hx = new.split(b"\n")[1]
                self.assertEqual(hx == hx.upper(), upper)
                got = bytes.fromhex(hx.decode())
                self.assertEqual(len([i for i in range(len(b)) if got[i] != b[i]]), 2)
                self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o666)   # not the umask's
                self.assertFalse(os.path.exists(self.path + ".visionary-tmp"))
        finally:
            os.umask(old)

    def test_nothing_is_written_when_nothing_differs(self):
        self._file()
        os.utime(self.path, (1000, 1000))
        self.assertEqual(rp.write({("2560x1440", rp.PRIMARY): 0}), [])
        self.assertEqual(os.stat(self.path).st_mtime, 1000)

    def test_unpin_points_resolve_back_at_main_while_it_is_closed(self):
        self._file()
        with mock.patch.object(rp, "screens_now", return_value=(1, "1728x1117")), \
             mock.patch.object(rp, "resolve_running", return_value=False):
            done = rp.unpin()
        self.assertEqual(sorted(done), [("1728x1117", rp.PRIMARY, 1, 0),
                                        ("1728x1117", rp.PROJECTS, 1, 0),
                                        ("1728x1117", rp.SECONDARY, 0, 1)])
        self.assertEqual(rp.read()[("1728x1117", rp.PRIMARY)], 0)
        self.assertEqual(rp.read()[("2560x1440", rp.SECONDARY)], 1)          # untouched

    def test_never_while_resolve_runs_or_with_nothing_pinned(self):
        raw, _b = self._file()
        for screens, running in (((1, "1728x1117"), True), ((None, "1728x1117"), False),
                                 ((1, None), False), ((1, "2560x1440"), False)):
            with mock.patch.object(rp, "screens_now", return_value=screens), \
                 mock.patch.object(rp, "resolve_running", return_value=running):
                self.assertEqual(rp.unpin(), [], screens)          # 2560x1440: already on main
        self.assertEqual(open(self.path, "rb").read(), raw)

    def test_an_unreadable_file_is_left_alone(self):
        with open(self.path, "wb") as fh:
            fh.write(b"UI_Persistence\nnot hex\n")
        with mock.patch.object(rp, "screens_now", return_value=(1, "1728x1117")), \
             mock.patch.object(rp, "resolve_running", return_value=False):
            self.assertEqual(rp.unpin(), [])

    def test_a_test_can_never_reach_the_real_preferences(self):
        self.p.stop()
        try:
            with self.assertRaisesRegex(RuntimeError, "real Resolve preferences"):
                rp.read()
            with self.assertRaisesRegex(RuntimeError, "real Resolve preferences"):
                rp.write({})
        finally:
            self.p.start()


class ScreensNow(unittest.TestCase):
    def _now(self, screens, pinned=("uuid:DUMMY",), on=True):
        with mock.patch("settings.get_display_priority", return_value=list(pinned)), \
             mock.patch("settings.get_settings", return_value={"resolve_host_pinning": on}), \
             mock.patch("displays.enumerate_displays", return_value=screens):
            return rp.screens_now()

    MAIN = {"key": "uuid:BUILTIN", "main": True, "size_pt": [1728, 1117]}
    DUMMY = {"key": "uuid:DUMMY", "main": False, "size_pt": [1920, 1080]}

    def test_the_dummy_beside_the_built_in_is_screen_1_in_the_built_ins_layout(self):
        self.assertEqual(self._now([self.DUMMY, self.MAIN]), (1, "1728x1117"))   # main first

    def test_the_pinning_switch_does_not_matter(self):
        # off: passes run on the main screen — a Resolve left on the dummy breaks those too
        self.assertEqual(self._now([self.MAIN, self.DUMMY], on=False), (1, "1728x1117"))

    def test_nothing_pinned_or_unplugged(self):
        self.assertEqual(self._now([self.MAIN, self.DUMMY], pinned=()), (None, "1728x1117"))
        self.assertEqual(self._now([self.MAIN]), (None, "1728x1117"))

    def test_the_pinned_display_being_main_needs_nothing(self):
        self.assertEqual(self._now([dict(self.DUMMY, main=True)]), (None, "1920x1080"))

    def test_mirrored_displays_are_not_screens(self):
        mirror = {"key": "uuid:M", "main": False, "mirror_slave": True}
        self.assertEqual(self._now([self.MAIN, mirror, self.DUMMY])[0], 1)


class StageHooks(unittest.TestCase):
    def test_under_tests_nothing_real_is_touched(self):
        with mock.patch.object(rp, "unpin") as up, \
             mock.patch.object(rp, "resolve_running") as rr, \
             mock.patch.object(stages.threading, "Thread") as th:
            self.assertEqual(stages.unpin_resolve_display(wait=5), [])
            stages._kill_resolve()
            self.assertFalse(stages.start_resolve_display_watch())
        up.assert_not_called()
        rr.assert_not_called()
        th.assert_not_called()

    def test_kill_unpins_only_after_resolve_is_gone(self):
        order = []
        alive = [True, True, False]
        def running():
            order.append("running?")
            return alive.pop(0)
        with mock.patch.object(stages, "_UNDER_TEST", False), \
             mock.patch.object(stages.subprocess, "run",
                               side_effect=lambda cmd, **kw: order.append(cmd[0])), \
             mock.patch.object(stages.time, "sleep"), \
             mock.patch.object(rp, "resolve_running", side_effect=running), \
             mock.patch.object(rp, "unpin", side_effect=lambda: order.append("unpin") or
                               [("1728x1117", rp.PRIMARY, 1, 0)]), \
             mock.patch.object(stages.logbook, "event") as ev:
            stages._kill_resolve()
        self.assertEqual(order, ["pkill", "running?", "running?", "running?", "unpin"])
        self.assertIn("1728x1117 PrimaryScreenIdx 1->0", ev.call_args.args[0])

    def test_a_resolve_that_outlives_the_wait_is_left_to_unpin_itself(self):
        # past the deadline unpin is still asked — and it refuses while Resolve runs
        t = [100.0]
        def sleep(secs):
            t[0] += secs
        with mock.patch.object(stages, "_UNDER_TEST", False), \
             mock.patch.object(stages.subprocess, "run"), \
             mock.patch.object(stages.time, "time", lambda: t[0]), \
             mock.patch.object(stages.time, "sleep", side_effect=sleep), \
             mock.patch.object(rp, "resolve_running", return_value=True), \
             mock.patch.object(rp, "unpin", return_value=[]) as up:
            stages._kill_resolve()
        up.assert_called_once()
        self.assertGreaterEqual(t[0], 105.0)                  # it did wait the 5 s first

    def test_a_kept_resolve_records_whose_it_is(self):
        stages._RESOLVE_KEPT.update(open=False, mode=None, passes=0, pids=())
        try:
            with mock.patch.object(stages, "_UNDER_TEST", False), \
                 mock.patch.object(stages, "_resolve_pids", return_value=(111,)), \
                 mock.patch.object(stages, "_refocus_app"):
                self.assertTrue(stages._keep_resolve_open("dv1000"))
            self.assertEqual(stages._RESOLVE_KEPT["pids"], (111,))
        finally:
            stages._RESOLVE_KEPT.update(open=False, mode=None, passes=0, pids=())

    def test_the_watcher_looks_again_when_the_screens_change(self):
        screens = [(None, "1728x1117")]                     # dummy unplugged when Resolve saved
        calls = []
        with mock.patch.object(stages.os, "stat", return_value=mock.Mock(st_mtime_ns=5)), \
             mock.patch.object(rp, "screens_now", side_effect=lambda: screens[0]), \
             mock.patch.object(rp, "resolve_running", return_value=False), \
             mock.patch.object(stages, "unpin_resolve_display",
                               side_effect=lambda: calls.append(screens[0])):
            seen = stages._display_watch_tick(None)
            seen = stages._display_watch_tick(seen)           # nothing changed: no second look
            screens[0] = (1, "1728x1117")                     # plugged back in
            seen = stages._display_watch_tick(seen)
        self.assertEqual(calls, [(None, "1728x1117"), (1, "1728x1117")])

    def test_the_watcher_waits_for_resolve_to_quit(self):
        with mock.patch.object(stages.os, "stat", return_value=mock.Mock(st_mtime_ns=5)), \
             mock.patch.object(rp, "screens_now", return_value=(1, "1728x1117")), \
             mock.patch.object(rp, "resolve_running", return_value=True), \
             mock.patch.object(stages, "unpin_resolve_display") as up:
            self.assertIsNone(stages._display_watch_tick(None))  # unchanged: looked at after
        up.assert_not_called()

    def test_a_kept_resolve_the_user_already_quit_is_not_killed(self):
        stages._RESOLVE_KEPT.update(open=True, mode="dv1000", passes=1, pids=(111,))
        try:
            with mock.patch.object(stages, "_UNDER_TEST", False), \
                 mock.patch.object(stages, "_resolve_pids", return_value=(222,)), \
                 mock.patch.object(stages, "_kill_resolve") as kill, \
                 mock.patch.object(stages, "unpin_resolve_display") as up:
                self.assertFalse(stages.close_kept_resolve("done"))   # 222 is the user's own
            kill.assert_not_called()
            up.assert_called_once()
            self.assertFalse(stages.resolve_kept_open())
            stages._RESOLVE_KEPT.update(open=True, mode="dv1000", passes=1, pids=(111,))
            with mock.patch.object(stages, "_UNDER_TEST", False), \
                 mock.patch.object(stages, "_resolve_pids", return_value=(111,)), \
                 mock.patch.object(stages, "_kill_resolve") as kill, \
                 mock.patch.object(stages.logbook, "event"):
                self.assertTrue(stages.close_kept_resolve("done"))    # still the kept one
            kill.assert_called_once()
        finally:
            stages._RESOLVE_KEPT.update(open=False, mode=None, passes=0, pids=())

    def test_enable_unpins_and_still_arms_if_that_fails(self):
        import orchestrator as orch
        for effect in (None, RuntimeError("boom")):
            o = orch.Orchestrator()
            with mock.patch.object(orch.settings, "get_settings", return_value={}), \
                 mock.patch.object(orch.logbook, "event"), \
                 mock.patch.object(o, "_start_caffeinate"), \
                 mock.patch.object(o, "_ensure"), \
                 mock.patch.object(stages, "unpin_resolve_display", side_effect=effect) as up:
                o.enable()
            up.assert_called_once()
            self.assertTrue(o._enabled)


if __name__ == "__main__":
    unittest.main()
