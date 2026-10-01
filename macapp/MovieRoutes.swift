import Foundation

// The Movies pane's decisions with no SwiftUI in them, so engine/test_movie_routes.py can compile
// this file with Models.swift and RUN them (the Cadence.swift pattern: the dial shipped broken
// twice when it could only be read, never executed).

/// WHAT THE SOURCE FILE IS, as far as its release name says before it is probed.
///
/// The chips are named for the INPUT — 1080p & below, 4K SDR, 4K HDR10, DV 7, DV 8.1 — not the route
/// the pipeline will take (user-dictated 2026-09-30; they used to read Passthrough / Convert /
/// Upscale). What the pipeline will DO with each kind is still said, in the chip's tooltip.
/// They read the SAME filename-parsed tags the row already shows in its own `pipelineHint`
/// ("4K · HDR · HEVC — fast path ~2.5× runtime"), so a chip describes the name, not a probe:
/// the real routing is decided by plan.choose_plan AFTER the source is probed.
///
/// A row that is ALREADY Dolby Vision is in none of the first three — the pipeline refuses a DV
/// source — so it is never counted under them. (Before the DV 7 filter every such row fell under
/// 4K SDR: the name tag "DV" replaces "HDR", so a 4K DV remux read as 4K SDR.)
enum MovieFilter: String, CaseIterable {
    // Declaration order IS the chip order, left to right: up the ladder of what the source is,
    // then the watch-state chip (user-dictated 2026-09-30). All stays first — it is the reset.
    case all, hdAndBelow, uhdSDR, uhdHDR10, dvP7, dv81, unwatched

    var label: String {
        switch self {
        case .all:        return "All"
        case .hdAndBelow: return "1080p & below"
        case .uhdSDR:     return "4K SDR"
        case .uhdHDR10:   return "4K HDR10"
        case .dvP7:       return "DV 7"
        case .dv81:       return "DV 8.1"
        case .unwatched:  return "Unwatched"
        }
    }

    /// What the chip holds and what happens to it, for the tooltip.
    var hint: String {
        switch self {
        case .all:        return "Every movie the library can offer"
        case .uhdHDR10:   return "4K HDR10 (or HDR10+) sources — the original stream is kept and "
                               + "only the Dolby Vision layer is added, so these are the quick ones"
        case .uhdSDR:     return "4K SDR sources — Resolve converts them to HDR, so the video is "
                               + "re-encoded under the peak cap"
        case .hdAndBelow: return "1080p, 720p and SD sources — a full Topaz upscale to 4K, roughly "
                               + "5× runtime"
        case .dvP7:       return "Dolby Vision profile 7 sources — become profile 8.1 in place, "
                               + "under the same name. The HDR10 video is copied bit for bit and "
                               + "only the Dolby Vision metadata is rewritten: no Topaz, no "
                               + "Resolve, no re-encode"
        case .dv81:       return "Dolby Vision 8.1 sources a best-of combine can still improve: "
                               + "Shuttle has found a copy on the seedbox and the NAS copy is not "
                               + "Dolby Atmos yet. Tap one to pair it. Listed only once there is "
                               + "such a pair"
        case .unwatched:  return "Not yet watched, according to Plex"
        }
    }

    func matches(_ m: MovieItemDTO) -> Bool {
        let t = Set(m.tags ?? [])
        let dv = m.has_dv == true
        switch self {
        case .all:        return true
        case .uhdHDR10:   return !dv && t.contains("4K") && t.contains("HDR")
        case .uhdSDR:     return !dv && t.contains("4K") && !t.contains("HDR")
        case .hdAndBelow: return !dv && !t.contains("4K")
        case .dvP7:       return m.dv_profile == 7
        // Only a PROVEN profile 8: every DV row that is not profile 7 is listed only because the
        // combine curation let it through (engine/movies.py _dv_row_visible), and a web release is
        // often profile 5, which is not 8.1 (review 2026-09-30). A listed row whose profile is not
        // known yet gets a one-time header probe (companion sweep) and joins on the next refresh.
        case .dv81:       return dv && m.dv_profile == 8
        case .unwatched:  return m.watched != true
        }
    }
}

/// The profile 7 -> 8.1 lane as the Movies pane shows it.
enum DVConvert {
    /// Queue entries keyed by the library row they came from (the FTP wire name; the entry's own
    /// name is the real filename, which differs only for non-ASCII titles).
    static func byRow(_ queue: [DVQueueEntryDTO]) -> [String: DVQueueEntryDTO] {
        var out: [String: DVQueueEntryDTO] = [:]
        for e in queue {
            if let k = e.row ?? e.name { out[k] = e }
        }
        return out
    }

    /// The entry for a library row, if that movie is in the lane's queue.
    static func entry(for m: MovieItemDTO, in queue: [DVQueueEntryDTO]) -> DVQueueEntryDTO? {
        guard let n = m.name else { return nil }
        return byRow(queue)[n]
    }

    /// Profile 7 rows that "Queue all" would add: every one not already in the queue. A FAILED
    /// one is left out on purpose — it failed for a reason the row states; it is retried by hand.
    static func addable(_ library: [MovieItemDTO], queue: [DVQueueEntryDTO]) -> [MovieItemDTO] {
        let have = byRow(queue)
        return library.filter { $0.dv_profile == 7 && $0.name != nil && have[$0.name!] == nil }
    }

    static func gb(_ bytes: Int?) -> String {
        guard let b = bytes, b > 0 else { return "0 GB" }
        let v = Double(b) / 1e9
        if v >= 1000 { return String(format: "%.2f TB", v / 1000) }
        return v >= 100 ? String(format: "%.0f GB", v) : String(format: "%.1f GB", v)
    }

    /// 0...1 for a step with a known total, else nil.
    static func fraction(_ s: DVLaneStepDTO?) -> Double? {
        guard let s, let d = s.done, let t = s.total, t > 0 else { return nil }
        return min(1, max(0, Double(d) / Double(t)))
    }

    /// "Downloading 42%" / "Converting 45%" / "Uploading 10% · throttled" / "Waiting: …"
    static func stepLabel(_ s: DVLaneStepDTO) -> String {
        if let note = s.note, !note.isEmpty { return note.prefix(1).uppercased() + note.dropFirst() }
        let verb: String
        switch s.phase {
        case "checking": verb = "Checking the NAS copy"
        case "download": verb = "Downloading"
        case "convert":  verb = "Converting"
        case "upload":   verb = "Uploading"
        case "swap":     verb = "Replacing the original"
        default:         verb = (s.phase ?? "Working").capitalized
        }
        var out = verb
        if let f = fraction(s) { out += " \(Int((f * 100).rounded(.down)))%" }
        if s.phase == "download" || s.phase == "upload" {
            if let r = s.rate, r > 0 { out += String(format: " · %.0f MB/s", Double(r) / 1e6) }
            if s.throttled == true { out += " · throttled while Plex is in use" }
        }
        return out
    }

    /// Waiting out the pause before its next attempt (its upload or swap failed).
    static func waitingToRetry(_ e: DVQueueEntryDTO) -> Bool {
        e.state == "active" && (e.retry_at ?? 0) > 0
    }

    /// One line for a queue entry. `lane` supplies live progress for the entry being worked.
    static func entryLabel(_ e: DVQueueEntryDTO, lane: DVConvertDTO?, now: Date = Date()) -> String {
        let of = lane?.ship_tries ?? 5
        switch e.state {
        case "done":
            let saved = (e.bytes ?? 0) - (e.size_out ?? e.bytes ?? 0)
            var s = "Converted to 8.1"
            if saved > 0 { s += " · \(gb(saved)) smaller" }
            if e.plex_pending == true { s += " · Plex updates when nobody is watching" }
            return s
        case "failed":
            return "Failed: " + (e.error ?? "unknown error")
        case "active":
            let tries = e.tries ?? 0
            if let step = lane?.fetch, step.name == e.name { return stepLabel(step) }
            if let step = lane?.ship, step.name == e.name {      // the attempts are the upload's
                return stepLabel(step) + (tries > 0 ? " · attempt \(tries + 1) of \(of)" : "")
            }
            if waitingToRetry(e) {
                let left = Int(((e.retry_at ?? 0) - now.timeIntervalSince1970).rounded(.up))
                let when = left <= 0 ? "now" : left < 60 ? "in under a minute"
                    : "in \(Int((Double(left) / 60).rounded(.up))) min"
                var s = "Attempt \(tries) of \(of) failed · trying again \(when)"
                if let err = e.error, !err.isEmpty { s += " — " + err }
                return s
            }
            switch e.phase {
            case "converted": return "Converted · waiting to upload"
            case "upload":    return "Uploading"
            case "swap":      return "Uploaded · waiting to replace the original"
            default:          return "In progress"
            }
        default:
            return "Queued"
        }
    }

    /// "Transfers over Ethernet · Living Room 5G LAN · 2.5 GbE", the Wi-Fi case said plainly, or
    /// the Ethernet-only wait. Empty until the lane has connected once.
    static func linkLine(_ l: DVLinkDTO?) -> String {
        guard let l else { return "" }
        if let age = l.age, age > 120 { return "" }     // a stopped lane's leftover, not "now"
        if l.unavailable == true { return "Waiting for an Ethernet link to the NAS — Ethernet only" }
        guard let iface = l.iface else { return "" }
        let name = l.name ?? iface
        if l.wired == true {
            return ["Transfers over Ethernet", name, l.speed ?? ""].filter { !$0.isEmpty }
                .joined(separator: " · ")
        }
        if l.bound == true && l.priority == "wifi" {
            return "Transfers over Wi-Fi · Wi-Fi first"
        }
        return "Transfers over \(l.kind ?? iface) — no Ethernet link to the NAS"
    }

    /// The NAS network setting's three choices, in the order the control shows them.
    static let networkChoices: [(key: String, label: String)] =
        [("ethernet", "Ethernet first"), ("wifi", "Wi-Fi first"), ("ethernet_only", "Ethernet only")]

    /// The header line: "12 of 210 converted · 96.4 GB saved · 3 failed"
    static func summaryLine(_ d: DVConvertDTO?) -> String {
        let by = d?.summary?.by_state ?? [:]
        let total = d?.summary?.total ?? 0
        var parts = ["\(by["done"] ?? 0) of \(total) converted"]
        if let s = d?.summary?.saved_bytes, s > 0 { parts.append("\(gb(s)) saved") }
        let retrying = (d?.queue ?? []).filter(waitingToRetry).count
        if retrying > 0 { parts.append("\(retrying) retrying") }
        if let f = by["failed"], f > 0 { parts.append("\(f) failed") }
        return parts.joined(separator: " · ")
    }

    /// Which entries the panel lists: everything in flight or failed, then the next few queued.
    /// Finished ones are summarized in the header instead of listed — 210 rows is not a panel.
    static func visible(_ queue: [DVQueueEntryDTO], pendingShown: Int = 5) -> [DVQueueEntryDTO] {
        let working = queue.filter { $0.state == "active" }
        let failed = queue.filter { $0.state == "failed" }
        let pending = queue.filter { $0.state == "pending" || $0.state == nil }
        return working + failed + Array(pending.prefix(pendingShown))
    }
}
