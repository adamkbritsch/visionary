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
    var seg_secs: Double?     // topaz: that segment's PROJECTED TOTAL = time already spent in it +
                              // its remaining eta. Gating on this instead of the remainder keeps the
                              // countdown from vanishing exactly as the segment finishes.
    var preset: String?
    var pct: Int?
    var ep_secs_done: Double?
    var ep_secs_total: Double?
    var eta_secs: Double?
    var elapsed_secs: Double? // wall time spent in the current stage so far (live "elapsed" stopwatch)
    var takeover_in: Int?     // the countdown's LENGTH, for context only
    var takeover_at: Int?     // epoch when the screen + mouse will be taken. Absolute so the
                              // app ticks its own countdown rather than showing whichever
                              // number the last 1.5s poll caught. 0 = taken/cancelled.
}

struct HoldDTO: Codable {
    var code: String?           // "power"|"power-grace"|"disk"|"backlog"|"resolve-gate"|"dual-remux"
                                // |"quiet"|"stall"|"retry"|"nas"|"empty"|"done"|"parked"
    var secs: Int?              // power-grace: seconds until the run pauses
    var held: Int?              // stall: items waiting behind the stalled Resolve
    var attempt: Int?           // retry: which attempt this is...
    var of: Int?                // ...out of how many before it counts as a real stall
    var fails: Int?             // parked: consecutive failures that gave up on the item
    var at: String?             // parked: the stage it kept failing at
}

struct OrchestratorDTO: Codable {
    var enabled: Bool?
    var running: Bool?
    var episode: String?
    var stage: String?          // STALE BY DESIGN: set at stage start, never cleared on completion,
                                // so it still names a stage during the 60 s retry after a failure.
                                // Use `stage_active` for liveness — never this.
    var stage_active: Bool?     // a heavy stage is EXECUTING right now. nil = an older engine; fall
                                // back to `progress != nil`, never to false (that would claim
                                // nothing is running mid-encode on a version skew).
    var hold: HoldDTO?          // set while the run thread is deliberately not executing a stage
    var message: String?
    var ended_reason: String?
    var progress: ProgressDTO?
    var current: UpNextDTO?     // the item ACTUALLY processing (so the header shows a YouTube video
                               // as channel+title, not the next TV episode inferred from up-next)
    var finishing: FinishingDTO?   // the item the FINISHER thread is draining (remux/upload/cleanup)
    var finishing2: FinishingDTO?  // the 2nd remux lane (backlog drain only — usually nil)
    var plex_playing: Bool?        // a Plex client is streaming → the prefetcher is standing down
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
    var holding: String?        // non-nil while this lane is preempted (Resolve has the machine)
    var fast: Bool?             // the 4K fast path (no Topaz) — a much shorter remux
    // Identity — `abandon_series` matches lanes on exactly these, and the puck labels with them.
    var series: String?
    var movie: Bool?
    var youtube: Bool?
    var source: String?
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
    var featurette_count: Int?     // season-00 specials; >0 makes the "Featurettes last" toggle relevant
}

/// Per-show settings that apply to ANY show — active, or merely queued as a slot's
/// follow-up (all keyed by show name in show_profiles.json, so they persist before it runs).
struct ShowSettingsDTO: Codable {
    var preset: String?
    var configured: Bool?
    var unwatched_first: Bool?
    var featurettes_last: Bool?
    var has_featurettes: Bool?
    var normalize_audio: Bool?
    var replace_source: Bool?
    var output_mode: String?      // "auto" | "sdr" | "dv1000" | "dv2000"
    var output_mode_effective: String?   // what it will ACTUALLY master as (auto already resolved)
}

struct SeriesShowDTO: Codable, Identifiable {   // one active round-robin show (all rendered the same)
    var name: String?
    var preset: String?
    var configured: Bool?
    var unwatched_first: Bool?
    var featurettes_last: Bool?   // per-show: season-00 specials go after the whole show
    var normalize_audio: Bool?    // per-show loudness-boost gate (default on)
    var replace_source: Bool?     // per-show upload policy: master replaces the source (default on)
    var output_mode: String?      // what Resolve OUTPUTS: auto | sdr | dv1000 | dv2000
    var output_mode_effective: String?   // auto already resolved against the source range
    var next_up: String?          // show queued to take this slot when this one finishes
    var next_up_armed: Bool?      // <10% left -> the follow-up is locked in + pre-downloading
    var near_done: Bool?          // >=90% done -> only then is "queue a follow-up" offered
    var next_up_profile: ShowSettingsDTO?   // the QUEUED show's own settings (configurable early)
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
    var replace_source: Bool?    // per-movie upload policy (keyed by title, like preset)
    var output_mode: String?     // per-movie output range (keyed by title, like preset)
    var output_mode_effective: String?  // auto already resolved against the source range
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
    var output_mode: String?  // per-channel output range (keyed by folder, like preset)
    var output_mode_effective: String?   // auto already resolved (youtarr sources are SDR)
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
    var quiet_until: Int?       // epoch second quiet_mode lifts by itself (0/absent = not paused)
    var poll_minutes: Int?
    var dim_after_minutes: Int?           // idle this long → backlight 0 (0 = Off); no auto-restore
    var max_peak_mbps: Int?               // hard 1-second peak-bitrate ceiling on every shipped master
    var audio_target_lufs: Int?           // smart loudness boost target (measured per item; 0 = off)
    var max_youtube_minutes: Int?
    var youtube_every_tv_episodes: Int?   // serve 1 YouTube video per this many TV episodes
    var min_adapter_watts: Int?           // power sufficiency = a brick of at least this wattage
    var passthrough_min_mbps: Int?        // 4K fast path: a 4K source at/above this skips Topaz (0 = off)

    // Scheduling / capacity — universal, and none of them change how a file is encoded.
    var max_active_shows: Int?            // how many shows share the round-robin
    var finisher_lanes: Int?              // max concurrent remuxes (1 = a quieter machine)
    var min_free_gb: Int?                 // disk floor that must stay free before an item may start
    var prefetch_cap_gb: Int?             // download-ahead buffer ceiling (0 = off)
    var max_episode_fails: Int?           // failures of one episode before it's parked
    var unplug_grace_seconds: Int?        // power-blip tolerance mid-stage
    var seg_eta_after_minutes: Int?       // show the current segment's ETA while THAT segment has
                                          // more than this left (a readout threshold, not behaviour)
    // NOTE: max_peak_mbps + audio_target_lufs stay in the DTO (the state poll carries them) but
    // deliberately have NO control — they dictate the output bytes. Everything above decides
    // only what runs, when, and what qualifies.
}

extension SettingsDTO {
    /// Read one Int setting by its engine key, so the Settings popup can render every numeric
    /// knob with ONE row component instead of a bespoke Binding per field. Keys are exactly the
    /// ones in engine/settings.py DEFAULT_SETTINGS — an unknown key reads nil and the row falls
    /// back to its own default, so a typo degrades to "shows the default" rather than crashing.
    subscript(int key: String) -> Int? {
        switch key {
        case "poll_minutes":              return poll_minutes
        case "dim_after_minutes":         return dim_after_minutes
        case "max_peak_mbps":             return max_peak_mbps
        case "audio_target_lufs":         return audio_target_lufs
        case "max_youtube_minutes":       return max_youtube_minutes
        case "youtube_every_tv_episodes": return youtube_every_tv_episodes
        case "min_adapter_watts":         return min_adapter_watts
        case "passthrough_min_mbps":      return passthrough_min_mbps
        case "max_active_shows":          return max_active_shows
        case "finisher_lanes":            return finisher_lanes
        case "min_free_gb":               return min_free_gb
        case "prefetch_cap_gb":           return prefetch_cap_gb
        case "max_episode_fails":         return max_episode_fails
        case "unplug_grace_seconds":      return unplug_grace_seconds
        case "seg_eta_after_minutes":     return seg_eta_after_minutes
        default:                          return nil
        }
    }
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
    var replace_source: Bool?
    var output_mode: String?
    var output_mode_effective: String?
    var catalog: [PresetDTO]?
}

struct DisplayDTO: Codable, Identifiable {
    var key: String?
    var name: String?
    var main: Bool?
    var builtin: Bool?
    var eligible: Bool?          // passes the SAME rule preflight has always used
    var why_not: String?         // plain-language reason when it can't host
    var smoke_pass: Bool?        // templates PROVEN to match on this screen
    var smoke_best: Double?
    var smoke_when: String?
    var scale: Double?
    var backing: [Int]?
    var size_pt: [Int]?
    var origin: [Double]?
    var id: String { key ?? "" }
}

struct DisplaysDTO: Codable {
    var displays: [DisplayDTO]?
    var priority: [String]?      // ordered keys, highest first
    var enabled: Bool?           // the master switch (default off)
    var fallback_main: Bool?
    var warning_seconds: Int?
    var host: DisplayDTO?        // what would actually be driven right now
    var host_reason: String?
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
    var log: [String]?          // Recent failures. Surfaced ONLY inside Settings > Advanced --
                                // never on the main page, where it was an unwanted banner.
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
