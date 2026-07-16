import Foundation

// Codable mirrors of the local dashboard API (http://127.0.0.1:8765). Every field is
// optional so a partial/!!null payload never fails decoding. Property names match the
// JSON keys (snake_case) so no keyDecodingStrategy is needed.

struct PowerDTO: Codable {
    var external_connected: Bool?
    var charging: Bool?
    var capacity: Int?
    var amperage_ma: Int?
    var adapter_watts: Int?
    var draining_on_ac: Bool?
    var adequate: Bool?
}

struct ScratchDTO: Codable {
    var name: String?
    var connected: Bool?
    var path: String?
    var free_gb: Double?
    var source: String?
}

struct ProgressDTO: Codable {
    var stage: String?
    var ep: String?
    var notches: [Double]?    // topaz: segment boundaries as 0..1 fractions (progress-bar ticks)
    var seg_done: Int?        // topaz: fully-encoded segment count (drives the tiny flash)
    var seg_total: Int?
    var seg_eta_secs: Double? // topaz: eta for the CURRENT segment (windowed rate)
    var avg_seg_secs: Double? // topaz: projected average time per segment (gates showing seg eta)
    var preset: String?
    var pct: Int?
    var ep_secs_done: Double?
    var ep_secs_total: Double?
    var eta_secs: Double?
    var elapsed_secs: Double? // wall time spent in the current stage so far (live "elapsed" stopwatch)
}

struct OrchestratorDTO: Codable {
    var enabled: Bool?
    var running: Bool?
    var episode: String?
    var stage: String?
    var message: String?
    var ended_reason: String?
    var progress: ProgressDTO?
    var current: UpNextDTO?     // the item ACTUALLY processing (so the header shows a YouTube video
                               // as channel+title, not the next TV episode inferred from up-next)
    var finishing: FinishingDTO?   // the item the FINISHER thread is draining (remux/upload/cleanup)
}

struct FinishingDTO: Codable {
    var ep: String?
    var stage: String?
    var pct: Double?
    var frames: Int?
    var total: Int?
    var elapsed_secs: Double?
    var eta_secs: Double?       // engine-computed, from THIS attempt's live rate (restart-safe)
    var notches: [Double]?      // remux: segment boundaries as 0..1 fractions (same bar as topaz)
    var seg_done: Int?          // remux: fully-encoded segment count (drives the flash + counter)
    var seg_total: Int?
}

struct QueueNextDTO: Codable {
    var ep: String?
    var has_source: Bool?
    var has_dv: Bool?
    var source_name: String?
    var title: String?          // movies: clean display title (TV uses ep + source_name)
}

struct QueueDTO: Codable {
    var next: QueueNextDTO?
    var remaining_count: Int?
    var unwatched_count: Int?
    var done_count: Int?
    var source_count: Int?
}

struct SeriesShowDTO: Codable, Identifiable {   // one active round-robin show (all rendered the same)
    var name: String?
    var preset: String?
    var configured: Bool?
    var unwatched_first: Bool?
    var normalize_audio: Bool?    // per-show loudness-boost gate (default on)
    var queue: QueueDTO?
    var id: String { name ?? "" }
}

struct SeriesStateDTO: Codable {
    var selected: String?            // the PRIMARY series (active[0])
    var active: [String]?            // the round-robin set (1-3 nas dirs, ordered)
    var rotation: Int?               // index into active of whose turn is next
    var queue: QueueDTO?             // the primary's queue (back-compat)
    var shows: [SeriesShowDTO]?      // ALL active shows, in order — each rendered as the same block
    var titles: [String: String]?    // nas_dir -> Plex display title
}

struct MovieItemDTO: Codable, Identifiable {
    var name: String?      // basename (queue + preset id)
    var dir: String?       // the movie's FTP folder
    var title: String?     // clean display title
    var watched: Bool?
    var preset: String?    // the Topaz preset chosen for this queued movie
    var normalize_audio: Bool?   // per-movie loudness-boost gate (keyed by title, like preset)
    var tags: [String]?    // filename-parsed routing tags: 4K/1080p, HDR/DV, codec, REMUX
    var route: String?     // approximate route + duration hint ("fast path ~2.5× runtime")
    var id: String { name ?? title ?? "" }

    // "4K · HDR · HEVC — fast path ~2.5× runtime" (empty when the name carries no tags)
    var pipelineHint: String {
        let t = (tags ?? []).joined(separator: " · ")
        let parts = [t, route ?? ""].filter { !$0.isEmpty }
        return parts.joined(separator: " — ")
    }
}

struct MovieSelectedDTO: Codable {        // the curated queue
    var items: [MovieItemDTO]?
    var next: QueueNextDTO?
    var count: Int?
}

struct MoviesStateDTO: Codable {
    var selected: MovieSelectedDTO?       // what's queued to process
    var library: [MovieItemDTO]?          // the searchable pool (non-DV movies)
    var reachable: Bool?
    var titles: [String: String]?         // file basename -> Plex movie title
}

struct YouTubeChannelDTO: Codable, Identifiable {   // a queued channel (standing subscription)
    var channelId: String?
    var title: String?
    var folder_name: String?
    var scope: String?        // "popular" | "all"
    var capped: Bool?         // per-channel length limit (≤ max_youtube_minutes) on/off
    var paused: Bool?         // paused → no downloading/upscaling, but keeps existing files
    var max_age_days: Int?    // delete/skip videos older than this many days (0 = no limit)
    var preset: String?       // per-channel Topaz preset (keyed by folder)
    var normalize_audio: Bool?   // per-channel loudness-boost gate (keyed by folder, like preset)
    var pending: Int?         // videos to upscale (within cap + scope, not done)
    var downloaded: Int?      // videos youtarr has on disk
    var id: String { channelId ?? title ?? "" }
}

struct YouTubeStateDTO: Codable {
    var items: [YouTubeChannelDTO]?       // the queued channels (unlimited)
    var count: Int?
    var connected: Bool?                  // is the YouTube account connected (OAuth)?
}

struct YTSubscriptionDTO: Codable, Identifiable {   // one of the user's real subscriptions
    var channelId: String?
    var title: String?
    var id: String { channelId ?? title ?? "" }
}

struct ChannelsResponseDTO: Codable {     // /api/channels
    var channels: [YTSubscriptionDTO]?
    var connected: Bool?
    var configured: Bool?                  // Google OAuth client (id+secret) present in config?
}

struct YTConnectDTO: Codable {            // /api/youtube-connect
    var connected: Bool?
    var configured: Bool?                  // Google OAuth client present? (false → Connect can't do anything)
    var auth_url: String?
    var subscriptions: Int?
}

struct SettingsDTO: Codable {
    var activated: Bool?        // appliance mode: persisted arm state (survives stops/relaunches)
    var quiet_mode: Bool?       // QUIET MODE: defer the screen-invasive Resolve stage so the laptop stays usable
    var pause_on_battery_drain: Bool?
    var topaz_min_watts: Int?
    var poll_minutes: Int?
    var dim_after_minutes: Int?           // idle this long → backlight 0 (0 = Off); no auto-restore
    var max_peak_mbps: Int?               // hard 1-second peak-bitrate ceiling on every shipped master
    var audio_target_lufs: Int?           // smart loudness boost target (measured per item; 0 = off)
    var max_youtube_minutes: Int?
    var youtube_every_tv_episodes: Int?   // serve 1 YouTube video per this many TV episodes
    var min_adapter_watts: Int?           // power sufficiency = a brick of at least this wattage
}

struct PresetDTO: Codable, Identifiable {
    var key: String
    var label: String
    var desc: String
    var id: String { key }
}

struct ShowProfileDTO: Codable {
    var show: String?
    var configured: Bool?
    var preset: String?
    var unwatched_first: Bool?
    var normalize_audio: Bool?
    var catalog: [PresetDTO]?
}

struct DetectPresetDTO: Codable {   // /api/detect-preset → an auto-detected key, or nil = ask
    var preset: String?
}

struct ScratchItemDTO: Codable, Identifiable {   // one entry currently in the topaz-scratch folder
    var name: String?
    var bytes: Int?
    var is_dir: Bool?
    var id: String { name ?? "" }
}

struct WindowDTO: Codable {
    var start: String?
    var end: String?
    var in_window: Bool?
}

struct EmptyJob: Codable {}   // job is an object when queued, null otherwise — presence = queued

struct UpNextDTO: Codable, Identifiable {
    var kind: String?          // "movie" | "episode" | "youtube"
    var ep: String?            // episodes (→ defer key)
    var source_name: String?   // episodes (→ title)
    var series: String?        // episodes: which show (round-robin) — nas dir
    var title: String?         // movies + youtube videos
    var name: String?          // movies + youtube (basename → remove/reorder key)
    var channel: String?       // youtube: which channel
    var id: String { [kind, series, channel, ep, title, source_name, name].compactMap { $0 }.joined(separator: "|") }
}

struct StateDTO: Codable {
    var automation_enabled: Bool?
    var status: String?
    var power: PowerDTO?
    var scratch: ScratchDTO?
    var window: WindowDTO?
    var job: EmptyJob?
    var generated_at: String?
    var scratch_contents: [ScratchItemDTO]?   // plain preview of the topaz-scratch folder
    var mode: String?                  // "tv" | "movie" | "youtube" — the nav bar VIEW
    var up_next: [UpNextDTO]?          // next ~10 items to process (movies + youtube jump ahead of episodes)
    var series: SeriesStateDTO?
    var movies: MoviesStateDTO?
    var youtube: YouTubeStateDTO?
    var orchestrator: OrchestratorDTO?
    var settings: SettingsDTO?
    var show_profile: ShowProfileDTO?
    var log: [String]?
}

struct SeriesListDTO: Codable {
    var series: [String]?
    var selected: String?
    var queue: QueueDTO?
    var reachable: Bool?
}

struct SelftestDTO: Codable {
    var screen_recording: Bool?
    var accessibility: Bool?
    var cliclick_installed: Bool?
    var ok: Bool?
    // exact-version / exact-hardware gates (engine/preflight.py; see engine/versions.py) —
    // all optional so an older engine's selftest payload still decodes.
    var resolve_version_ok: Bool?
    var topaz_version_ok: Bool?
    var display_ok: Bool?
    var hard_ok: Bool?                 // false → the server refuses to arm (409)
    var found: [String: String]?       // failing check id -> human detail ("found X; require Y")
}
