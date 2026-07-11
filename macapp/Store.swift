import Foundation
import Combine
import AppKit

// Talks to the bundled Python dashboard server over the loopback API. Polls
// /api/state ~1.5 s for live state and POSTs user actions back. All UI state lives
// here so the SwiftUI views stay declarative.
@MainActor
final class AppStore: ObservableObject {
    @Published var state: StateDTO?
    @Published var seriesOptions: [String] = []
    @Published var seriesReachable = true
    @Published var moviesReachable = true
    @Published var movieLibrary: [MovieItemDTO] = []   // searchable pool (non-DV movies)
    @Published var channelLibrary: [YTSubscriptionDTO] = []   // the user's YouTube subscriptions (picker)
    @Published var ytConnected = false
    @Published var ytConfigured = false                        // Google OAuth client present in config?
    @Published var selftest: SelftestDTO?

    // In-flight ADD state, hoisted OUT of the mode views: the tab views are recreated on
    // every mode switch, so view-local @State silently dropped a pending add (preset
    // auto-detection mid-flight, or the preset chooser awaiting confirm) the moment you
    // changed tabs — the movie/series never reached the queue. Held here, the chooser is
    // still waiting when you come back to the tab.
    @Published var movieDetecting = false
    @Published var pendingMovie: MovieItemDTO? = nil
    @Published var moviePick = ""
    @Published var tvDetecting = false
    @Published var pendingSeries: String? = nil     // a show awaiting the preset chooser
    @Published var pendingSeriesSlot: Int? = nil    // slot to set it into (nil = change preset only)
    @Published var seriesPick = ""

    @Published var modeOverride: String? = nil    // optimistic nav VIEW → the selector chip slides on
                                                  // click, before the server round-trip lands
    var mode: String { modeOverride ?? state?.mode ?? "tv" }     // "tv" | "youtube" | "movie" (nav VIEW)
    var presetCatalog: [PresetDTO] { state?.show_profile?.catalog ?? [] }   // digital / film / 2D

    private let base = "http://127.0.0.1:8765"
    private var polling = false

    func start() {
        guard !polling else { return }
        polling = true
        Task { await self.fetchSeries() }
        Task {
            var tick = 0
            while self.polling {
                await self.refresh()
                if tick % 8 == 0 { await self.runSelftest() }   // re-check grants ~every 12 s, not just at launch
                tick += 1
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    // reads
    func refresh() async {
        if let s: StateDTO = await get("/api/state") { self.state = s }
    }
    func fetchSeries() async {
        if let d: SeriesListDTO = await get("/api/series") {
            self.seriesOptions = d.series ?? []
            self.seriesReachable = d.reachable ?? !((d.series ?? []).isEmpty)
        } else {
            self.seriesReachable = false
        }
    }
    // Refresh button: ask Plex to rescan for new/renamed shows + re-pull titles, then re-list.
    func refreshLibrary() async {
        await post("/api/refresh-library", [:])
        await fetchSeries()
        await refresh()                 // state now carries the fresh {dir: title} map
    }
    // A show's display name = its Plex title, falling back to the (prettified) NAS folder.
    func seriesTitle(_ dir: String) -> String { state?.series?.titles?[dir] ?? pretty(dir) }
    // A movie's Plex title (matched by file basename), falling back to its filename-derived title.
    func movieTitle(_ name: String?, _ fallback: String?) -> String {
        if let n = name, let t = state?.movies?.titles?[n] { return t }
        return fallback ?? ""
    }
    func runSelftest() async {
        if let t: SelftestDTO = await get("/api/selftest") { self.selftest = t }
    }

    // writes
    // Appliance: the toggle tracks the PERSISTED activation (settings.activated), not the
    // transient run state — while activated the app keeps running (re-arming itself), and the
    // button must read + toggle THAT. A run ends only when you Deactivate.
    var activated: Bool { state?.settings?.activated ?? state?.automation_enabled ?? false }
    func toggleAutomation() async {
        await post("/api/automation", ["enabled": !activated])
        await refresh()
    }
    var quietMode: Bool { state?.settings?.quiet_mode ?? false }
    func toggleQuietMode() async {
        await post("/api/quiet-mode", ["enabled": !quietMode])   // persists + reclaims the screen if turning ON
        await refresh()
    }
    func saveSettings(_ body: [String: Any]) async {
        await post("/api/settings", body); await refresh()
    }
    // YouTube cadence: serve 1 YouTube video per `n` TV episodes (engine clamps 1…50).
    func setYoutubeEveryTv(_ n: Int) async {
        await saveSettings(["youtube_every_tv_episodes": max(1, min(50, n))])
    }
    // Put a show in round-robin slot `index` (replace that slot, or append for the empty slot).
    // Each show's own picker uses this — changing one slot leaves the others alone.
    func setSlot(_ index: Int, _ name: String) async {
        guard !name.isEmpty else { return }
        await post("/api/select", ["series": name, "action": "at", "index": index])
        await refresh()
    }
    // Same, plus set the show's preset in one step (when it has none yet).
    func setSlotWithPreset(_ index: Int, _ name: String, _ preset: String) async {
        guard !name.isEmpty else { return }
        await post("/api/select", ["series": name, "action": "at", "index": index])
        await post("/api/show-profile", ["show": name, "preset": preset])
        await refresh()
    }
    func removeSeries(_ name: String) async {
        guard !name.isEmpty else { return }
        await post("/api/select", ["series": name, "action": "remove"]); await refresh()
    }
    // Change a target's preset WITHOUT re-selecting (the "Change preset" affordance).
    func setPreset(_ show: String, _ key: String) async {
        guard !show.isEmpty, !key.isEmpty else { return }
        await post("/api/show-profile", ["show": show, "preset": key]); await refresh()
    }
    // Per-show: process unwatched episodes first (on) vs start at the beginning (off).
    func setShowUnwatchedFirst(_ show: String, _ on: Bool) async {
        guard !show.isEmpty else { return }
        await post("/api/show-profile", ["show": show, "unwatched_first": on]); await refresh()
    }
    func setMode(_ m: String) async {            // the nav bar VIEW (doesn't gate processing)
        modeOverride = m                         // optimistic → the chip slides now, not after the round-trip
        await post("/api/mode", ["mode": m])
        await refresh()
        modeOverride = nil                       // server state (refreshed to `m`) is now authoritative
        if m == "movie" { await fetchMovies() }
        if m == "youtube" { await fetchChannels() }
    }
    func fetchChannels() async {                  // entering YouTube mode / manual refresh
        if let d: ChannelsResponseDTO = await get("/api/channels") {
            self.channelLibrary = d.channels ?? []
            self.ytConnected = d.connected ?? false
            self.ytConfigured = d.configured ?? false
        }
        await refresh()
    }
    func connectYouTube() async {                 // open the Google consent page in the browser
        if let d: YTConnectDTO = await postDecode("/api/youtube-connect", ["action": "start"]) {
            self.ytConfigured = d.configured ?? false
            if let u = d.auth_url, let url = URL(string: u) {
                NSWorkspace.shared.open(url)      // client configured → hand off to the browser
            }
            // else: no client in config yet — the view shows the setup panel (ytConfigured == false)
        }
    }
    func disconnectYouTube() async {
        await post("/api/youtube-connect", ["action": "disconnect"]); await fetchChannels()
    }
    func addChannel(_ channelId: String, _ title: String, scope: String = "popular") async {
        guard !channelId.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "add", "channelId": channelId,
                                          "title": title, "scope": scope]); await refresh()
    }
    func removeChannel(_ channelId: String) async {
        await post("/api/youtube-queue", ["action": "remove", "channelId": channelId]); await refresh()
    }
    // Skip/delete ONE video: aborts it if currently processing, deletes its download from
    // staging, and tells youtarr to forget + never re-download it.
    func deleteYoutubeVideo(channel: String?, name: String) async {
        await post("/api/youtube-queue", ["action": "delete", "channel": channel ?? "", "name": name])
        await refresh()
    }
    func setChannelScope(_ channelId: String, _ scope: String) async {
        guard !channelId.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "scope", "channelId": channelId, "scope": scope]); await refresh()
    }
    func setChannelCap(_ channelId: String, _ on: Bool) async {   // per-channel ≤20-min length limit
        guard !channelId.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "cap", "channelId": channelId, "capped": on]); await refresh()
    }
    func setChannelPaused(_ channelId: String, _ on: Bool) async {   // pause: stop work, keep files
        guard !channelId.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "paused", "channelId": channelId, "paused": on]); await refresh()
    }
    func setChannelMaxAge(_ channelId: String, _ days: Int) async {   // delete videos older than N days
        guard !channelId.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "max_age", "channelId": channelId, "max_age_days": days]); await refresh()
    }
    func setChannelPreset(_ folder: String, _ key: String) async {
        guard !folder.isEmpty, !key.isEmpty else { return }
        await post("/api/youtube-queue", ["action": "preset", "folder": folder, "preset": key]); await refresh()
    }
    func fetchMovies() async {                    // entering Movie mode / manual refresh
        if let d: MoviesStateDTO = await get("/api/movies") {
            self.movieLibrary = d.library ?? []
            self.moviesReachable = d.reachable ?? !(d.library ?? []).isEmpty
        } else {
            self.moviesReachable = false
        }
        await refresh()
    }
    // Add a movie to the queue WITH its chosen preset (the add step). Idempotent — also used
    // to update an already-queued movie's preset.
    func addMovieWithPreset(_ m: MovieItemDTO, preset: String) async {
        await post("/api/movie-queue", ["action": "add", "name": m.name ?? "",
                                        "dir": m.dir ?? "", "title": m.title ?? "", "preset": preset])
        await refresh()
    }
    func removeMovie(_ name: String) async {
        await post("/api/movie-queue", ["action": "remove", "name": name]); await refresh()
    }
    // Manage the combined up-next queue. action: "remove" | "up" | "down". A movie removes
    // outright / reorders among queued movies; an episode's "remove" defers it to the end.
    func queueAction(_ action: String, _ item: UpNextDTO) async {
        var body: [String: Any] = ["action": action, "kind": item.kind ?? ""]
        if item.kind == "movie" { body["name"] = item.name ?? "" } else { body["ep"] = item.ep ?? "" }
        await post("/api/queue-action", body); await refresh()
    }
    // The current preset for a show/movie title — drives the add/select step (skip vs ask).
    func profileFor(_ show: String) async -> ShowProfileDTO? {
        let enc = show.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? show
        return await get("/api/show-profile?show=\(enc)")
    }
    func requestAccessibility() async {
        await post("/api/request-accessibility", [:]); await runSelftest()
    }

    // Auto-detect a preset for a just-picked, unconfigured title (shotonwhat film/digital + TMDb
    // animation). Returns a preset key to auto-apply, or nil = no confident match → open the picker.
    // Never blocks the add for long: the server swallows all source failures and returns null fast.
    func detectPreset(_ kind: String, _ id: String, name: String? = nil) async -> String? {
        var body: [String: Any] = ["kind": kind]
        if kind == "movie" { body["name"] = name ?? id } else { body["show"] = id }
        let dto: DetectPresetDTO? = await postDecode("/api/detect-preset", body)
        return dto?.preset
    }

    // transport
    private func get<T: Decodable>(_ path: String) async -> T? {
        guard let url = URL(string: base + path) else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return try JSONDecoder().decode(T.self, from: data)
        } catch { return nil }
    }
    private func postDecode<T: Decodable>(_ path: String, _ body: [String: Any]) async -> T? {
        guard let url = URL(string: base + path) else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return try JSONDecoder().decode(T.self, from: data)
        } catch { return nil }
    }
    @discardableResult
    private func post(_ path: String, _ body: [String: Any]) async -> Bool {
        guard let url = URL(string: base + path) else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do { _ = try await URLSession.shared.data(for: req); return true }
        catch { return false }
    }
}
