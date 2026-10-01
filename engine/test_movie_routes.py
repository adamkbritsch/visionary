"""The Movies pane's filter chips and the Dolby Vision 7 -> 8.1 queue logic, COMPILED AND RUN.

macapp/MovieRoutes.swift holds them with no SwiftUI import (the Cadence.swift pattern), so this
test builds it with Models.swift and the Swift toolchain the app is built with, decodes a real-shaped
/api/state payload through the app's own DTOs, and asserts on what the pane would show and send.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

MACAPP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macapp")
SOURCES = [os.path.join(MACAPP, f) for f in ("Models.swift", "MovieRoutes.swift")]

MAIN = r'''
import Foundation
var failures: [String] = []
func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

func row(_ name: String, _ tags: [String], dv: Bool = false, p7: Bool = false, watched: Bool = false,
         bytes: Int = 50_000_000_000) -> MovieItemDTO {
    var m = MovieItemDTO()
    m.name = name; m.title = name; m.tags = tags; m.has_dv = dv; m.watched = watched; m.bytes = bytes
    if p7 { m.dv_profile = 7; m.dv_el = "FEL" }
    return m
}
let lib = [
    row("Plain4KHDR.mkv", ["4K", "HDR", "HEVC"]),
    row("Plain4KSDR.mkv", ["4K", "HEVC"]),
    row("Plain1080.mkv", ["1080p", "H264"], watched: true),
    row("Combine4KDV.mkv", ["4K", "DV", "HEVC"], dv: true),           // DV, not P7: combine-only
    row("P7a.mkv", ["4K", "DV", "HEVC", "REMUX"], dv: true, p7: true),
    row("P7b.mkv", ["4K", "DV", "HEVC", "REMUX"], dv: true, p7: true, bytes: 70_000_000_000),
    row("P7c.mkv", ["4K", "DV", "HEVC", "REMUX"], dv: true, p7: true),
]
var p8 = row("Converted.mkv", ["4K", "HDR", "HEVC", "REMUX"], dv: true); p8.dv_profile = 8
var p5 = row("WebP5.mkv", ["4K", "DV", "HEVC"], dv: true); p5.dv_profile = 5
let dv81 = (lib + [p8, p5]).filter(MovieFilter.dv81.matches).map { $0.name! }
// DV 8.1: only a PROVEN profile 8 — never a profile 7 (those convert), a known profile 5, or a DV
// row whose profile is not known yet
check(dv81 == ["Converted.mkv"], "dv81 -> \(dv81)")
// a converted file still named "HDR10" is DV now: not a 4K HDR10 upscale source
check(!MovieFilter.uhdHDR10.matches(p8), "a DV 8.1 row is not a 4K HDR10 source")
func names(_ f: MovieFilter) -> [String] { lib.filter(f.matches).map { $0.name! } }

// THE DV 7 CHIP lists exactly the known profile 7 rows...
check(names(.dvP7) == ["P7a.mkv", "P7b.mkv", "P7c.mkv"], "dvP7 -> \(names(.dvP7))")
check(MovieFilter.dvP7.label == "DV 7", "chip label")
// ...and no DV row inflates an upscale chip any more (the "DV" tag replaces "HDR", so every 4K
// DV row used to count as a 4K SDR Convert).
check(names(.uhdSDR) == ["Plain4KSDR.mkv"], "4K SDR -> \(names(.uhdSDR))")
check(names(.uhdHDR10) == ["Plain4KHDR.mkv"], "4K HDR10 -> \(names(.uhdHDR10))")
check(names(.hdAndBelow) == ["Plain1080.mkv"], "1080p & below -> \(names(.hdAndBelow))")
// the chips are named for what the SOURCE is, left to right from 1080p up to DV 8.1, then
// Unwatched (user-dictated 2026-09-30)
check(MovieFilter.allCases.map { $0.label }
      == ["All", "1080p & below", "4K SDR", "4K HDR10", "DV 7", "DV 8.1", "Unwatched"],
      "labels -> \(MovieFilter.allCases.map { $0.label })")
check(names(.all).count == lib.count, "all")
check(names(.unwatched).count == lib.count - 1, "unwatched")

// THE QUEUE, decoded from the shape the engine's state poll sends.
let json = """
{"dv_convert": {"running": true, "note": null,
  "fetch": {"name": "P7a.mkv", "title": "P7a", "phase": "download", "done": 25000000000,
            "total": 50000000000, "note": null, "throttled": true, "rate": 24500000},
  "ship": null,
  "summary": {"total": 3, "by_state": {"active": 1, "done": 1, "failed": 1},
              "saved_bytes": 8000000000, "bytes_left": 50000000000},
  "queue": [
    {"name": "P7a.mkv", "title": "P7a", "bytes": 50000000000, "state": "active", "phase": "download",
     "el": "FEL", "error": null, "size_out": null, "plex_pending": null, "row": "P7a.mkv"},
    {"name": "P7b.mkv", "title": "P7b", "bytes": 70000000000, "state": "done", "phase": null,
     "el": "MEL", "error": null, "size_out": 62000000000, "plex_pending": true, "row": "P7b.mkv"},
    {"name": "Gone.mkv", "title": "Gone", "bytes": 1, "state": "failed", "phase": null, "el": null,
     "error": "not on the NAS any more", "size_out": null, "plex_pending": null, "row": "Gone.mkv"}
  ]}}
"""
struct Wrap: Codable { var dv_convert: DVConvertDTO? }
guard let dv = try? JSONDecoder().decode(Wrap.self, from: json.data(using: .utf8)!).dv_convert else {
    print("FAIL: the state payload does not decode"); exit(0)
}
let q = dv.queue ?? []
check(q.count == 3, "decoded queue")

// "Queue all" adds only what is not in the lane yet
let more = DVConvert.addable(lib, queue: q).map { $0.name! }
check(more == ["P7c.mkv"], "addable -> \(more)")
check(DVConvert.entry(for: lib[4], in: q)?.state == "active", "row -> entry")
check(DVConvert.entry(for: lib[0], in: q) == nil, "a plain row has no entry")

// the lines the pane shows
let live = DVConvert.entryLabel(q[0], lane: dv)
check(live.hasPrefix("Downloading 50%"), "live step -> \(live)")
check(live.contains("MB/s") && live.contains("throttled"), "rate + throttle -> \(live)")
let done = DVConvert.entryLabel(q[1], lane: dv)
check(done.hasPrefix("Converted to 8.1") && done.contains("8.0 GB smaller")
      && done.contains("Plex updates"), "done -> \(done)")
check(DVConvert.entryLabel(q[2], lane: dv) == "Failed: not on the NAS any more", "failed")
let sum = DVConvert.summaryLine(dv)
check(sum == "1 of 3 converted · 8.0 GB saved · 1 failed", "summary -> \(sum)")
check(DVConvert.fraction(dv.fetch) == 0.5, "fraction")

// a movie whose upload failed waits out a pause, then tries again: 5 attempts, then its files go
// (user, 2026-09-30)
var again = DVQueueEntryDTO(); again.name = "Again.mkv"; again.state = "active"; again.phase = "upload"
again.tries = 2; again.retry_at = 1_000_240; again.error = "NAS command failed (255)"
let t0 = Date(timeIntervalSince1970: 1_000_000)
let wait = DVConvert.entryLabel(again, lane: dv, now: t0)
check(wait == "Attempt 2 of 5 failed · trying again in 4 min — NAS command failed (255)",
      "retry wait -> \(wait)")
check(DVConvert.entryLabel(again, lane: dv, now: Date(timeIntervalSince1970: 1_000_230))
      .contains("trying again in under a minute"), "retry soon")
check(DVConvert.entryLabel(again, lane: dv, now: Date(timeIntervalSince1970: 1_000_300))
      .contains("trying again now"), "retry due")
check(DVConvert.waitingToRetry(again) && !DVConvert.waitingToRetry(q[0]), "waitingToRetry")
var onTry = again; onTry.name = "Again.mkv"; onTry.retry_at = nil     // the attempt is running
var shipping = dv
var up = DVLaneStepDTO(); up.name = "Again.mkv"; up.phase = "upload"; shipping.ship = up
check(DVConvert.entryLabel(onTry, lane: shipping).hasSuffix(" · attempt 3 of 5"),
      "attempt -> \(DVConvert.entryLabel(onTry, lane: shipping))")
var fetching = onTry; fetching.name = "P7a.mkv"                       // a download is no attempt
check(!DVConvert.entryLabel(fetching, lane: dv).contains("attempt"),
      "fetch step -> \(DVConvert.entryLabel(fetching, lane: dv))")
var withRetry = dv; withRetry.queue = q + [again]
check(DVConvert.summaryLine(withRetry) == "1 of 3 converted · 8.0 GB saved · 1 retrying · 1 failed",
      "summary with a retry -> \(DVConvert.summaryLine(withRetry))")
check(DVConvert.gb(13_580_000_000_000) == "13.58 TB", "TB -> \(DVConvert.gb(13_580_000_000_000))")

// the panel lists work in flight and failures, and only the next few queued
var many = q
for i in 0..<20 { var e = DVQueueEntryDTO(); e.name = "Q\(i).mkv"; e.state = "pending"; many.append(e) }
let vis = DVConvert.visible(many, pendingShown: 5).map { $0.name! }
check(vis.first == "P7a.mkv" && vis.contains("Gone.mkv") && !vis.contains("P7b.mkv")
      && vis.count == 2 + 5, "visible -> \(vis)")

// the link the transfers use
var wired = DVLinkDTO(); wired.iface = "en12"; wired.wired = true; wired.kind = "Ethernet"
wired.name = "Living Room 5G LAN"; wired.speed = "2.5 GbE"
check(DVConvert.linkLine(wired) == "Transfers over Ethernet · Living Room 5G LAN · 2.5 GbE",
      "wired -> \(DVConvert.linkLine(wired))")
var away = DVLinkDTO(); away.via = "tailscale"; away.bound = false
check(DVConvert.linkLine(away) == "Transfers over Tailscale — not over the NAS's local network",
      "tailscale -> \(DVConvert.linkLine(away))")
var fellBack = wired; fellBack.via = "tailscale"; fellBack.bound = true      // home, but the LAN failed
check(DVConvert.linkLine(fellBack).hasPrefix("Transfers over Tailscale"), "fell back -> \(DVConvert.linkLine(fellBack))")
var homeLan = wired; homeLan.via = "lan"; homeLan.bound = true
check(DVConvert.linkLine(homeLan).hasPrefix("Transfers over Ethernet"), "lan via")
var air = DVLinkDTO(); air.iface = "en0"; air.wired = false; air.kind = "Wi-Fi"
check(DVConvert.linkLine(air) == "Transfers over Wi-Fi — no Ethernet link to the NAS",
      "wifi -> \(DVConvert.linkLine(air))")
check(DVConvert.linkLine(nil) == "", "no link yet -> nothing shown")
var wf = DVLinkDTO(); wf.iface = "en0"; wf.bound = true; wf.wired = false; wf.kind = "Wi-Fi"
wf.priority = "wifi"
check(DVConvert.linkLine(wf) == "Transfers over Wi-Fi · Wi-Fi first", "wifi first -> \(DVConvert.linkLine(wf))")
var none = DVLinkDTO(); none.unavailable = true; none.priority = "ethernet_only"
check(DVConvert.linkLine(none).hasPrefix("Waiting for an Ethernet link"), "ethernet only, no cable")
check(DVConvert.networkChoices.map { $0.key } == ["ethernet", "wifi", "ethernet_only"], "choices")
var stale = wired; stale.age = 600
check(DVConvert.linkLine(stale) == "", "a stopped lane's old link is not shown as now")
var fresh = wired; fresh.age = 30
check(DVConvert.linkLine(fresh).hasPrefix("Transfers over Ethernet"), "a fresh link is shown")
// the setting decodes from the state poll
struct S: Codable { var settings: SettingsDTO? }
let sj = #"{"settings": {"nas_network": "ethernet_only"}}"#
check((try? JSONDecoder().decode(S.self, from: sj.data(using: .utf8)!))?.settings?.nas_network
      == "ethernet_only", "nas_network decodes")

if failures.isEmpty { print("OK") } else { for f in failures { print("FAIL: \(f)") } }
'''


@unittest.skipUnless(shutil.which("swiftc") and all(os.path.exists(s) for s in SOURCES),
                     "needs the Swift toolchain the app is built with")
class MovieRoutes(unittest.TestCase):
    def test_the_dv7_filter_and_the_lane_queue_as_the_pane_runs_them(self):
        d = tempfile.mkdtemp()
        try:
            main = os.path.join(d, "main.swift")
            with open(main, "w") as f:
                f.write(MAIN)
            binary = os.path.join(d, "routestest")
            build = subprocess.run(["swiftc", *SOURCES, main, "-o", binary],
                                   capture_output=True, text=True, timeout=600)
            self.assertEqual(build.returncode, 0, f"does not compile:\n{build.stderr[-3000:]}")
            run = subprocess.run([binary], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            self.assertEqual(run.stdout.strip(), "OK", run.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
