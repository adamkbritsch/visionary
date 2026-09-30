import Foundation

// The Movies pane's decisions with no SwiftUI in them, so engine/test_movie_routes.py can compile
// this file with Models.swift and RUN them (the Cadence.swift pattern: the dial shipped broken
// twice when it could only be read, never executed).

/// WHAT THE PIPELINE WILL DO WITH A MOVIE, as far as the row can know before it is probed.
///
/// These read the SAME filename-parsed tags the row already shows in its own `pipelineHint`
/// ("4K · HDR · HEVC — fast path ~2.5× runtime"), so a chip predicts a path, it does not
/// promise one: the real routing is decided by plan.choose_plan AFTER the source is probed.
/// Judged on `tags` rather than `route`, because route_hint only has two values and cannot
/// separate the passthrough (no re-encode at all) from the 4K SDR conversion.
///
/// A row that is ALREADY Dolby Vision takes none of the three upscale paths — the pipeline refuses
/// a DV source — so it is never counted under them. (Before the DV 7 filter every such row fell
/// under Convert: the name tag "DV" replaces "HDR", so a 4K DV remux read as 4K SDR.)
enum MovieFilter: String, CaseIterable {
    case all, passthrough, convert, upscale, dvP7, unwatched

    var label: String {
        switch self {
        case .all:         return "All"
        case .passthrough: return "Passthrough"
        case .convert:     return "Convert"
        case .upscale:     return "Upscale"
        case .dvP7:        return "DV 7"
        case .unwatched:   return "Unwatched"
        }
    }

    /// What the chip means, for the tooltip — the counts alone don't say why you'd pick one.
    var hint: String {
        switch self {
        case .all:         return "Every movie the library can offer"
        case .passthrough: return "4K HDR — the original stream is kept and only the Dolby "
                                + "Vision layer is added, so these are the quick ones"
        case .convert:     return "4K without HDR — Resolve converts it, so the video is "
                                + "re-encoded under the peak cap"
        case .upscale:     return "1080p and below — a full Topaz upscale, roughly 5× runtime"
        case .dvP7:        return "Dolby Vision profile 7 — becomes profile 8.1 in place, under "
                                + "the same name. The HDR10 video is copied bit for bit and only "
                                + "the Dolby Vision metadata is rewritten: no Topaz, no Resolve, "
                                + "no re-encode"
        case .unwatched:   return "Not yet watched, according to Plex"
        }
    }

    func matches(_ m: MovieItemDTO) -> Bool {
        let t = Set(m.tags ?? [])
        let dv = m.has_dv == true
        switch self {
        case .all:         return true
        case .passthrough: return !dv && t.contains("4K") && t.contains("HDR")
        case .convert:     return !dv && t.contains("4K") && !t.contains("HDR")
        case .upscale:     return !dv && !t.contains("4K")
        case .dvP7:        return m.dv_profile == 7
        case .unwatched:   return m.watched != true
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

    /// One line for a queue entry. `lane` supplies live progress for the entry being worked.
    static func entryLabel(_ e: DVQueueEntryDTO, lane: DVConvertDTO?) -> String {
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
            for step in [lane?.fetch, lane?.ship] {
                if let step, step.name == e.name { return stepLabel(step) }
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

    /// "Transfers over Ethernet · Living Room 5G LAN · 2.5 GbE", or the Wi-Fi fallback said
    /// plainly. Empty until the lane has connected once.
    static func linkLine(_ l: DVLinkDTO?) -> String {
        guard let l, let iface = l.iface else { return "" }
        if l.wired == true {
            let parts = ["Transfers over Ethernet", l.name ?? iface, l.speed ?? ""].filter { !$0.isEmpty }
            return parts.joined(separator: " · ")
        }
        return "Transfers over \(l.kind ?? iface) — no Ethernet link to the NAS"
    }

    /// The header line: "12 of 210 converted · 96.4 GB saved · 3 failed"
    static func summaryLine(_ d: DVConvertDTO?) -> String {
        let by = d?.summary?.by_state ?? [:]
        let total = d?.summary?.total ?? 0
        var parts = ["\(by["done"] ?? 0) of \(total) converted"]
        if let s = d?.summary?.saved_bytes, s > 0 { parts.append("\(gb(s)) saved") }
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
