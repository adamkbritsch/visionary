"""Setup's Helper group (rows, dots, the Ready line, the job buttons, which saves re-check),
COMPILED AND RUN.

macapp/Models.swift holds those decisions in HelperSetupDTO's extension with no SwiftUI import
(the MovieRoutes pattern), so this test builds it with the Swift toolchain the app is built with,
decodes answers through the app's own DTOs (one of them the engine's real setup_status for a
saved address it can't use) and asserts on what Setup would show.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import helper
import settings

MACAPP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macapp")
SOURCES = [os.path.join(MACAPP, "Models.swift")]

MAIN = r'''
import Foundation
var failures: [String] = []
func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

func c(_ id: String, _ ok: Bool?, _ detail: String = "") -> HelperCheckDTO {
    HelperCheckDTO(id: id, ok: ok, detail: detail, action: "")
}
func answer(_ checks: [HelperCheckDTO], configured: Bool = true) -> HelperSetupDTO {
    var h = HelperSetupDTO()
    h.configured = configured; h.checks = checks; h.checked_at = 1
    return h
}
func ids(_ rows: [HelperCheckDTO]) -> [String] { rows.map { $0.id ?? "?" } }

// 1. the engine's own answer for a saved address it can't use (user@host typed into Address)
let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
let bad = try! JSONDecoder().decode(HelperSetupDTO.self, from: data)
check(bad.configured == false, "the engine calls a bad address not set up")
let badRows = HelperSetupDTO.rows(bad, appConfigured: true)
check(ids(badRows).first == "login", "a bad address still shows its rows: \(ids(badRows))")
check(badRows.first?.detail == "settings: bad host", "the sign-in row says which field: \(badRows.first?.detail ?? "nil")")
check(!HelperSetupDTO.ready(badRows, switchedOn: false), "a bad address is never Ready")
check(!HelperSetupDTO.ready(badRows, switchedOn: true), "a bad address is never Ready, switched on")
check(HelperSetupDTO.rows(bad, appConfigured: false).isEmpty, "no rows before an address and login are saved")
check(HelperSetupDTO.rows(nil, appConfigured: true).isEmpty, "no rows before any answer")

// 2. the check itself failed: its reason shows too, never Ready
let failed = answer([c("login", nil, "the check failed: boom"), c("wired", nil), c("worker", nil),
                     c("x265", nil), c("sleep", nil), c("helper", nil)], configured: false)
let failedRows = HelperSetupDTO.rows(failed, appConfigured: true)
check(failedRows.first?.detail == "the check failed: boom", "a failed check shows why")
check(!HelperSetupDTO.ready(failedRows, switchedOn: true), "a failed check is never Ready")

// 3. the sleep row: between x265 and Status, titled Sleep, grey and never blocking when nil
check(HelperSetupDTO.rowOrder == ["login", "wired", "worker", "x265", "sleep", "helper"], "the row order")
check(HelperSetupDTO.rowTitles == ["login": "Signs in", "wired": "Ethernet", "worker": "Worker",
                                   "x265": "x265", "sleep": "Sleep", "helper": "Status"], "the row titles")
func good(sleep: Bool?, _ sleepWords: String = "", helperOk: Bool? = true) -> HelperSetupDTO {
    // shuffled on purpose: the rows follow rowOrder, not the payload's order
    answer([c("helper", helperOk, "ready"), c("sleep", sleep, sleepWords), c("login", true),
            c("wired", true), c("worker", true), c("x265", true)])
}
let noBlock = HelperSetupDTO.rows(good(sleep: nil, "it can't keep itself awake over ssh · turn off automatic sleep while it's plugged in"),
                                  appConfigured: true)
check(ids(noBlock) == HelperSetupDTO.rowOrder, "rows in order: \(ids(noBlock))")
check(HelperSetupDTO.ready(noBlock, switchedOn: true), "sleep nil never blocks Ready")
let sleepRow = noBlock.first { $0.id == "sleep" }!
check(HelperSetupDTO.dot(sleepRow, switchedOn: true) == nil, "sleep nil is the grey dot")
check(sleepRow.detail?.hasPrefix("it can't keep itself awake") == true, "sleep nil keeps its words")
check(HelperSetupDTO.ready(HelperSetupDTO.rows(good(sleep: true), appConfigured: true), switchedOn: true),
      "sleep true is Ready")
check(HelperSetupDTO.ready(HelperSetupDTO.rows(good(sleep: nil), appConfigured: true), switchedOn: true),
      "sleep unknown (empty words) is Ready")
check(!HelperSetupDTO.ready(HelperSetupDTO.rows(good(sleep: false), appConfigured: true), switchedOn: true),
      "a sleep false (never sent) would not pass as Ready")
// an older engine has no sleep row: five rows, still Ready
var older = good(sleep: nil); older.checks = older.checks?.filter { $0.id != "sleep" }
let olderRows = HelperSetupDTO.rows(older, appConfigured: true)
check(ids(olderRows) == ["login", "wired", "worker", "x265", "helper"], "an older engine's rows: \(ids(olderRows))")
check(HelperSetupDTO.ready(olderRows, switchedOn: true), "an older engine is still Ready")

// 4. the Helper switch: off is a choice (Ready, neutral dot); the answer from before switching it
// on is not Ready (why saving the switch checks again)
let off = HelperSetupDTO.rows(good(sleep: true, helperOk: nil), appConfigured: true)
let offRow = off.first { $0.id == "helper" }!
check(HelperSetupDTO.ready(off, switchedOn: false), "switched off, the rest passing, is Ready")
check(HelperSetupDTO.dot(offRow, switchedOn: false) == nil, "switched off, the status dot is neutral")
check(!HelperSetupDTO.ready(off, switchedOn: true), "the switched-off answer is not Ready once on")
check(HelperSetupDTO.recheckKeys.contains("helper_enabled"), "saving the Helper switch checks again")
for k in ["helper_host", "helper_user", "helper_key"] {
    check(HelperSetupDTO.recheckKeys.contains(k), "saving \(k) checks again")
}
check(HelperSetupDTO.recheckKeys.isDisjoint(with: ["plex_throttle": true].keys), "other saves don't")

// 5. a row's job button waits for another job, and for any check in flight
check(HelperSetupDTO.jobButtonDisabled(active: nil, runningThis: false, checking: true),
      "disabled while a check runs (the verdict may predate the job)")
check(!HelperSetupDTO.jobButtonDisabled(active: nil, runningThis: false, checking: false), "live when idle")
check(HelperSetupDTO.jobButtonDisabled(active: "helper_x265", runningThis: false, checking: false),
      "disabled while another job runs")
check(!HelperSetupDTO.jobButtonDisabled(active: "helper_worker", runningThis: true, checking: false),
      "its own running job keeps its spinner button")

print(failures.isEmpty ? "OK" : failures.joined(separator: "\n"))
'''


def _refuse(cmd, timeout):
    raise AssertionError(f"a bad address ran {cmd}")


def bad_address_answer(key_path):
    """The engine's real GET /api/helper/setup for Address "adam@192.168.1.50": nothing runs."""
    fields = {"helper_enabled": False, "helper_host": "adam@192.168.1.50", "helper_user": "adam",
              "helper_key": key_path}
    helper.reset_state()
    try:
        with mock.patch.object(settings, "get_settings", side_effect=lambda: dict(fields)), \
                mock.patch.object(helper, "_log"):
            return helper.setup_status(True, run=_refuse, local_x265=lambda: "")
    finally:
        helper.reset_state()


@unittest.skipUnless(shutil.which("swiftc") and all(os.path.exists(s) for s in SOURCES),
                     "needs the Swift toolchain the app is built with")
class HelperSetupRows(unittest.TestCase):
    def test_setup_shows_the_engine_rows_as_the_group_draws_them(self):
        d = tempfile.mkdtemp()
        try:
            out = bad_address_answer(os.path.join(d, "id_ed25519_visionary_helper"))
            self.assertFalse(out["configured"])
            payload = os.path.join(d, "bad.json")
            with open(payload, "w") as f:
                json.dump(out, f, allow_nan=False)
            main = os.path.join(d, "main.swift")
            with open(main, "w") as f:
                f.write(MAIN)
            binary = os.path.join(d, "helperrows")
            build = subprocess.run(["swiftc", *SOURCES, main, "-o", binary],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, f"does not compile:\n{build.stderr[-3000:]}")
            run = subprocess.run([binary, payload], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            self.assertEqual(run.stdout.strip(), "OK", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
