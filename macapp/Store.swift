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
    // UI: the Resolve preview blown up to the full window width (click to toggle). Lives
    // here, not in view @State — the card and the RootView overlay both need it, and the
    // pipeline card is re-initialised on every 1.5 s poll.
    @Published var resolvePreviewExpanded = false

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

    // In-app Setup (onboarding): the popover binding lives HERE so the first-run card
    // can open Settings programmatically; the setup payloads are fetched on demand
    // (popover open), never on the poll — /api/config stays off the state broadcast.
    @Published var showSettings = false
    @Published var lastError: String? = nil       // the app's first real error surface
    @Published var preflight: PreflightDTO?
    @Published var configDTO: ConfigDTO?
    @Published var installStatus: InstallStatusDTO?
    @Published var bordersStatus: BordersStatusDTO?
    @Published var mediaLibs: MediaLibrariesDTO?
    @Published var autoConnected: AutoConnectDTO?  // tokenless NAS-host service sweep

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
    func seriesTitle(_ dir: String) -> String {
        AppStore.withYear(state?.series?.titles?[dir] ?? pretty(dir), from: dir)
    }
    // A movie's Plex title (matched by file basename), falling back to its filename-derived title.
    func movieTitle(_ name: String?, _ fallback: String?) -> String {
        let t = (name.flatMap { state?.movies?.titles?[$0] }) ?? fallback ?? ""
        return AppStore.withYear(t, from: name ?? fallback ?? "")
    }

    /// Release year in parentheses after the name. Plex titles usually drop the year ("Lost"),
    /// while the thing it came from carries it ("Lost (2004)", "Movie.2019.1080p...") — so the
    /// year is lifted from the SOURCE string and appended when the title has none of its own.
    /// A title that already ends in a year is returned untouched, so nothing doubles up.
    static func withYear(_ title: String, from source: String) -> String {
        let t = title.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, t.range(of: "\\((19|20)\\d\\d\\)$", options: .regularExpression) == nil,
              let r = source.range(of: "(?<![0-9])(19|20)\\d\\d(?![0-9])", options: .regularExpression)
        else { return t }
        return "\(t) (\(source[r]))"
    }
    // --- WHICH SCREEN RESOLVE RUNS ON -------------------------------------------------
    // Deliberately NOT part of /api/state: that polls every 1.5 s and re-enumerating
    // displays at that rate is pointless. Fetched when Settings opens, and when macOS says
    // the display arrangement changed.
    @Published var displays: DisplaysDTO?
    @Published var smokeRunning: String? = nil     // key currently being template-tested

    func fetchDisplays() async {
        if let d: DisplaysDTO = await get("/api/displays") { self.displays = d }
    }
    func setDisplayPinning(_ on: Bool) async {
        await post("/api/displays", ["resolve_host_pinning": on]); await fetchDisplays()
    }
    func setDisplayPriority(_ keys: [String]) async {
        await post("/api/displays", ["priority": keys]); await fetchDisplays()
    }
    func setTakeoverWarning(_ on: Bool) async {
        await post("/api/displays", ["warn_takeover": on]); await fetchDisplays()
    }
    /// Score every template against one screen and remember the result. A display cannot
    /// host Resolve until this passes — driving a screen nobody is watching turns a bad
    /// template match from a loud failure into silent wrong clicks.
    func runDisplaySmoke(_ key: String) async {
        smokeRunning = key
        await post("/api/display-smoke", ["display": key])
        smokeRunning = nil
        await fetchDisplays()
    }

    /// About to take the screen and mouse — roughly ten seconds of real setup work still to
    /// run. Deliberately not a countdown: see ProgressDTO.takeover_soon.
    var takeoverSoon: Bool { state?.orchestrator?.progress?.takeover_soon == true }
    /// True only while the pipeline is ACTUALLY moving the pointer — within
    /// MOUSE_IN_USE_SECONDS of the last click. Self-expiring: recomputed against `now` on
    /// every poll, so nothing is left on screen if a stage dies mid-way, and the hour of
    /// screenshot-only analysis after the clicks does not keep it up.
    static let mouseInUseWindow: TimeInterval = 10
    var takeoverActive: Bool {
        guard let at = state?.orchestrator?.progress?.mouse_at, at > 0 else { return false }
        return Date().timeIntervalSince1970 - TimeInterval(at) < AppStore.mouseInUseWindow
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
        let enabling = !activated
        let (ok, data) = await postResult("/api/automation", ["enabled": enabling])
        if !ok, enabling, let data,
           let e = try? JSONDecoder().decode(ApiErrorDTO.self, from: data) {
            // the arm-gate 409 finally gets a face: name the first failing check
            if let c = e.checks?.first {
                lastError = "Refusing to arm — \(c.id ?? "?"): \(c.detail ?? "")"
            } else {
                lastError = e.error ?? "Refusing to arm"
            }
        } else if ok {
            lastError = nil
        }
        await refresh()
    }
    // SCREEN CONTROL. quietMode == true means the pipeline is NOT using the screen.
    // Turning it off is always a TIMED pause — the engine restores it at `quietUntil`
    // even if the app never reopens, so it can't be left off until the disk fills.
    var quietMode: Bool { state?.settings?.quiet_mode ?? false }
    /// When the pause self-cancels, or nil when screen control is on.
    var quietUntil: Date? {
        guard quietMode, let t = state?.settings?.quiet_until, t > 0 else { return nil }
        return Date(timeIntervalSince1970: TimeInterval(t))
    }
    /// Stop the pipeline using the screen for `seconds` (engine clamps to 4 h).
    func pauseScreenControl(seconds: Int) async {
        await post("/api/quiet-mode", ["enabled": true, "seconds": seconds])
        await refresh()
    }
    /// Hand the screen back to the pipeline now, cancelling any pause.
    func resumeScreenControl() async {
        await post("/api/quiet-mode", ["enabled": false])
        await refresh()
    }
    func saveSettings(_ body: [String: Any]) async {
        await post("/api/settings", body); await refresh()
    }
    // YouTube cadence: serve 1 YouTube video per `n` TV episodes (engine clamps 1…50).
    func setYoutubeEveryTv(_ n: Int) async {
        await saveSettings(["youtube_every_tv_episodes": max(1, min(50, n))])
    }

    /// BOTH whole-number knobs of the YouTube cadence in one write, so the dial can't
    /// land on a torn pair (e.g. burst applied without every, briefly serving a burst
    /// every N episodes when the user asked for N-per-episode).
    func setYoutubeCadence(every: Int, burst: Int) async {
        await saveSettings(["youtube_every_tv_episodes": max(1, min(50, every)),
                            "youtube_videos_per_burst": max(1, min(10, burst))])
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

    // Per-show: run season-00 specials/featurettes AFTER the whole show (on) vs in numeric
    // order, where "S00" sorts ahead of "S01" and they'd be upscaled first.
    func setFeaturettesLast(_ show: String, _ on: Bool) async {
        guard !show.isEmpty else { return }
        await post("/api/show-profile", ["show": show, "featurettes_last": on]); await refresh()
    }
    // Per-item (TV show / movie title / channel folder — the item's show_profiles key):
    // apply the smart loudness boost during remux (on) vs keep audio bit-exact (off).
    func setNormalizeAudio(_ key: String, _ on: Bool) async {
        guard !key.isEmpty else { return }
        await post("/api/show-profile", ["show": key, "normalize_audio": on]); await refresh()
    }

    // Per-show: AI border extension (4:3 -> 16:9) before the upscale. The row only
    // renders on 4:3 shows with the models installed; the stage re-gates per episode.
    func setExtendBorders(_ key: String, _ on: Bool) async {
        guard !key.isEmpty else { return }
        await post("/api/show-profile", ["show": key, "extend_borders": on]); await refresh()
    }

    // Per-show wing prompt for the extend stage ("" = the built-in default).
    func setExtendPrompt(_ key: String, _ text: String) async {
        guard !key.isEmpty else { return }
        await post("/api/show-profile", ["show": key, "extend_prompt": text]); await refresh()
    }

    // Forget a show's remembered wing inventions (set-reference book): the next episode
    // re-invents fresh. Never touches finished masters.
    func resetSetBook(_ key: String) async {
        guard !key.isEmpty else { return }
        await post("/api/borders/reset-set-book", ["show": key]); await refresh()
    }

    // Per-item (show / movie title) upload policy: master replaces the source (on) vs both kept (off).
    func setReplaceSource(_ key: String, _ on: Bool) async {
        guard !key.isEmpty else { return }
        await post("/api/show-profile", ["show": key, "replace_source": on]); await refresh()
    }

    /// What Resolve should OUTPUT for this item. "auto" always resolves to 1000-nit
    /// Dolby Vision (2000-nit is manual-only); the rest pin it. Note "sdr"
    /// also changes the master's FILENAME, so it only affects items not yet shipped.
    func setOutputMode(_ key: String, _ mode: String) async {
        guard !key.isEmpty else { return }
        await post("/api/show-profile", ["show": key, "output_mode": mode]); await refresh()
    }

    // Per-slot "Up next": the show that takes this slot when the current one finishes.
    // Allowed WHILE THE PIPELINE RUNS — it only records a future intent (pass "" to clear).
    func setNextUp(_ show: String, _ next: String) async {
        guard !show.isEmpty else { return }
        await post("/api/next-up", ["show": show, "next": next]); await refresh()
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
    /// "Run this video now": jumps it ahead of everything and asks the in-flight item to
    /// yield at its next SAFE boundary (a Topaz segment boundary — Resolve is never cut off).
    func runYoutubeNow(name: String) async {
        guard !name.isEmpty else { return }
        let (_, data) = await postResult("/api/youtube-queue",
                                        ["action": "prioritize", "name": name])
        if let data, let e = try? JSONDecoder().decode(ApiErrorDTO.self, from: data),
           let err = e.error {
            lastError = e.detail ?? err
        }
        await refresh()
    }

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
    // COMPANION COMBINE control. action: "search" | "pair" | "unpair" | "confirm" | "dismiss".
    // Status flows back through the state poll's movies.companions map (async workers).
    func companionAction(_ action: String, name: String, path: String? = nil,
                         dir: String? = nil, title: String? = nil) async {
        var body: [String: Any] = ["action": action, "name": name]
        if let path { body["path"] = path }
        if let dir { body["dir"] = dir }
        if let title { body["title"] = title }
        await post("/api/companion", body); await refresh()
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
    func requestScreenRecording() async {
        await post("/api/request-screen-recording", [:]); await runSelftest()
    }

    // ---- in-app Setup (onboarding) ----------------------------------------------
    func fetchSetup() async {
        if let p: PreflightDTO = await get("/api/preflight") { preflight = p }
        if let c: ConfigDTO = await get("/api/config") { configDTO = c }
        await fetchInstallStatus()
        await fetchBordersStatus()
        await fetchMediaLibs()
    }
    /// Which NAS folders Plex says are TV / Movies / YouTube (+ overrides + what's live).
    func fetchMediaLibs() async {
        if let m: MediaLibrariesDTO = await get("/api/media-libraries") { mediaLibs = m }
    }
    /// Route one Plex library to a kind (nil/"" = back to Plex's default).
    func setMediaLibKind(_ key: String, _ kind: String?) async {
        var kinds = mediaLibs?.overrides ?? [:]
        if let k = kind, !k.isEmpty { kinds[key] = k } else { kinds.removeValue(forKey: key) }
        let (_, data) = await postResult("/api/media-libraries", ["kinds": kinds])
        if let data, let m = try? JSONDecoder().decode(MediaLibrariesDTO.self, from: data) {
            mediaLibs = m
        }
    }
    /// Apply the detected folders (writes them to config; takes effect next launch).
    func applyMediaLibs() async {
        let (_, data) = await postResult("/api/media-libraries",
                                        ["apply": true, "kinds": mediaLibs?.overrides ?? [:]])
        if let data, let m = try? JSONDecoder().decode(MediaLibrariesDTO.self, from: data) {
            mediaLibs = m
        }
    }
    func fetchBordersStatus() async {
        if let b: BordersStatusDTO = await get("/api/borders/status") { bordersStatus = b }
    }
    func recheckSetup() async {
        if let p: PreflightDTO = await get("/api/preflight?fresh=1") { preflight = p }
        await runSelftest()
    }
    /// Save changed config fields. The response is the REDACTED view (secrets round-trip
    /// as set/unset booleans only — values never come back).
    func saveConfig(_ body: [String: Any]) async {
        let (_, data) = await postResult("/api/config", body)
        if let data, let c = try? JSONDecoder().decode(ConfigDTO.self, from: data) {
            configDTO = c
        }
        await recheckSetup()
    }
    func testConfig(_ what: String) async -> ConfigTestDTO? {
        let (_, data) = await postResult("/api/config-test", ["what": what])
        guard let data else { return nil }
        return try? JSONDecoder().decode(ConfigTestDTO.self, from: data)
    }
    /// AUTO-CONNECT everything discoverable once the NAS works: Plex (:32400/identity,
    /// tokenless), youtarr (:3087 presence), the Shuttle relay (:8789/healthz + its
    /// token verified from Shuttle's own local file — a found relay is a COMPLETE
    /// connection). Each found URL auto-saves when its field was empty — the same values
    /// the engine's fallbacks would use, now visible.
    func autoConnect() async {
        let (_, data) = await postResult("/api/config-test", ["what": "auto-connect"])
        guard let data,
              let t = try? JSONDecoder().decode(AutoConnectDTO.self, from: data) else { return }
        autoConnected = t
        var save: [String: Any] = [:]
        for (svc, key) in [("plex", "plex_url"), ("youtarr", "youtarr_url"),
                           ("relay", "shuttle_relay_url")] {
            if let f = t.found?[svc], f.ok == true, let url = f.url, !url.isEmpty,
               (configDTO?.fields?[key] ?? "").isEmpty {
                save[key] = url
            }
        }
        if !save.isEmpty { await saveConfig(save) }
    }
    func fetchInstallStatus() async {
        if let s: InstallStatusDTO = await get("/api/setup/install-status") { installStatus = s }
    }
    /// Kick an installer job and follow it to completion (the row shows the live tail).
    func startInstall(_ what: String) async {
        let (_, data) = await postResult("/api/setup/install", ["what": what])
        if let data, let e = try? JSONDecoder().decode(ApiErrorDTO.self, from: data),
           let err = e.error {
            lastError = err == "homebrew-missing"
                ? "Homebrew isn't installed — run its installer in Terminal first (command in the row)."
                : err == "busy" ? "Another install is still running." : err
            return
        }
        await followInstalls()
    }
    func importResolve() async {
        let (ok, data) = await postResult("/api/setup/import-resolve", [:])
        if !ok, let data, let e = try? JSONDecoder().decode(ApiErrorDTO.self, from: data) {
            lastError = e.detail ?? e.error
            return
        }
        await followInstalls()
    }
    /// Poll the job registry once a second until the active job finishes, then recheck.
    private func followInstalls() async {
        for _ in 0..<10800 {         // model downloads are GB-class — outlive the old 15 min
            await fetchInstallStatus()
            if installStatus?.active?.hasPrefix("borders_") == true {
                await fetchBordersStatus()   // the row's live progress IS the on-disk byte count
            }
            if installStatus?.active == nil { break }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        await recheckSetup()
        await fetchBordersStatus()      // a finished borders_* download flips its row
    }
    func uploadDvProbe() async -> DvProbeDTO? {
        let (_, data) = await postResult("/api/setup/install-dv-probe", [:])
        guard let data else { return nil }
        return try? JSONDecoder().decode(DvProbeDTO.self, from: data)
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
    /// POST that KEEPS the status code + body — the classic `post` throws both away,
    /// which is how the arm-gate 409 stayed invisible for months. Setup endpoints and
    /// toggleAutomation use this.
    private func postResult(_ path: String, _ body: [String: Any]) async -> (ok: Bool, data: Data?) {
        guard let url = URL(string: base + path) else { return (false, nil) }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            return ((200...299).contains(code), data)
        } catch { return (false, nil) }
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
