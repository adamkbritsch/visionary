import SwiftUI
import AppKit

extension Color {
    static let neutral = Color(nsColor: .secondaryLabelColor)
    static let labelC  = Color(nsColor: .labelColor)
    // App accent — the steel-blue from the Visionary icon's gradient (display-P3). A
    // touch brighter than the icon's deepest stop so it stays legible on the dark grey.
    static let brand     = Color(.displayP3, red: 0.42, green: 0.53, blue: 0.68)
    static let brandDeep = Color(.displayP3, red: 0.345, green: 0.405, blue: 0.490)
}

// MARK: - Design system (Liquid Glass theatre — cool steel foreground, warm OLED stage)

enum DS {
    // THEATRE stage: OLED near-black, COOL up top (the icon's graphite receding into the
    // dark), warming toward the floor — the room is lit from BELOW, like footlights / a
    // campfire story. The warmth lives only in the BACKGROUND; every foreground surface
    // stays cool steel (the icon's silver badge language).
    static let bgTop    = Color(.displayP3, red: 0.102, green: 0.110, blue: 0.120) // cool graphite, dark
    static let bgBase   = Color(.displayP3, red: 0.040, green: 0.044, blue: 0.050) // OLED near-black
    static let bgBottom = Color(.displayP3, red: 0.060, green: 0.045, blue: 0.034) // warm black floor

    // The warm light source — an ember glow, always at LOW opacity (never literal orange).
    static let ember     = Color(.displayP3, red: 1.00, green: 0.62, blue: 0.30)
    static let emberDeep = Color(.displayP3, red: 0.85, green: 0.42, blue: 0.18)
    static let warmWhite = Color(.displayP3, red: 1.00, green: 0.93, blue: 0.85)   // bottom-lit bevel edges

    // The icon's silver-glass plate (#EDEEF0 → steel-blue). Reserved for the title, the
    // hero progress number, and "lit" surfaces (active stage, running Stop button).
    static let silverBright = Color(.displayP3, red: 0.929, green: 0.933, blue: 0.941)
    static let badge = LinearGradient(colors: [silverBright, .brand],
                                      startPoint: .top, endPoint: .bottom)

    // Monochrome steel state ramp — state is BRIGHTNESS (+ pulse), never hue.
    static let steelBright = Color(.displayP3, red: 0.880, green: 0.900, blue: 0.930) // active / running / attention
    static let steel       = Color(.displayP3, red: 0.620, green: 0.700, blue: 0.800) // armed / positive-static
    static let steelDim    = Color(.displayP3, red: 0.480, green: 0.530, blue: 0.600) // idle / secondary

    // Text drawn ON a silver plate (the lit Stop button, the active stage's icon well).
    static let graphiteText = Color(.displayP3, red: 0.10, green: 0.11, blue: 0.12)

    // The one hue exception — genuine fault surfaces + the "not controlling the screen" indicator.
    static let fault = Color(.displayP3, red: 0.92, green: 0.30, blue: 0.27)
    // Screen-Control "off" rim — a MUTED red (desaturated toward the steel theme, still clearly reddish).
    static let quietRedLight = Color(.displayP3, red: 0.78, green: 0.44, blue: 0.42)
    static let quietRedDark  = Color(.displayP3, red: 0.44, green: 0.19, blue: 0.18)

    static let radiusCard: CGFloat = 16
    static let radiusControl: CGFloat = 10
}

extension View {
    /// Liquid-glass surface: translucent fill over the graphite gradient, silver bevel
    /// (bright top edge fading down), soft neutral drop shadow — the icon's badge plate
    /// as a panel. `tint` is a STEEL-BRIGHTNESS accent (never a hue). `inset: true`
    /// renders a recessed glass WELL instead (inputs, inner lists, segmented containers):
    /// darker fill, hairline, no shadow.
    func panel(_ radius: CGFloat = DS.radiusControl, tint: Color? = nil, inset: Bool = false) -> some View {
        modifier(GlassPanel(radius: radius, tint: tint, inset: inset))
    }
}

// A single silver "surface" the header's LIGHT elements (the title + the lit Activate button) are
// cut out of. The vertical ramp is anchored to a shared BAND rather than each element's own height,
// so a short element and a tall one line up on the same gradient — as if one gradient sheet sits
// behind the whole header and each light element is a window onto it, instead of each painting its
// own independent ramp. Dark elements (the status/power/window pills) don't use this.
func headerSurfaceGradient(height h: CGFloat, band: CGFloat = 40) -> LinearGradient {
    let e = max(0, (band - h) / (2 * max(h, 1)))     // stretch the ramp past this element to span the band
    return LinearGradient(colors: [DS.silverBright, .brand],
                          startPoint: UnitPoint(x: 0.5, y: -e), endPoint: UnitPoint(x: 0.5, y: 1 + e))
}
extension View {
    /// Paint self's shape (text glyphs, an icon) with the shared header surface — for light TEXT.
    func headerSurface(band: CGFloat = 40) -> some View {
        overlay { GeometryReader { g in
            headerSurfaceGradient(height: g.size.height, band: band).mask(self)
        } }
    }
}

private struct GlassPanel: ViewModifier {
    let radius: CGFloat; let tint: Color?; let inset: Bool
    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        content
            .background {
                if inset {
                    shape.fill(Color.black.opacity(0.28))             // recessed well (deeper on OLED black)
                } else {
                    ZStack {                                          // raised glass plate (quiet)
                        shape.fill(Color.black.opacity(0.20))         // grounding — stable text contrast
                        // LIT FROM BELOW: the fill brightens slightly toward the bottom edge,
                        // as if catching the stage's warm floor light.
                        shape.fill(LinearGradient(
                            colors: tint.map { [$0.opacity(0.05), $0.opacity(0.11)] }
                                 ?? [Color.white.opacity(0.020), Color.white.opacity(0.055)],
                            startPoint: .top, endPoint: .bottom))
                    }
                    .shadow(color: .black.opacity(0.30), radius: 6, y: 2)   // on the plate, not the text
                }
            }
            .overlay(shape.strokeBorder(LinearGradient(               // bevel: BOTTOM edge catches the
                colors: tint.map { [$0.opacity(0.10), $0.opacity(0.40)] } // warm light, top stays cool-dim
                     ?? [Color.white.opacity(inset ? 0.02 : 0.03),
                         DS.warmWhite.opacity(inset ? 0.07 : 0.13)],
                startPoint: .top, endPoint: .bottom), lineWidth: inset ? 0.7 : 1))
    }
}

/// Monochrome steel button. `lit: true` = a filled steel plate with dark text — clearly
/// the active/primary control without shouting (the Activated button, primary confirms).
/// `lit: false` = a quiet glass capsule outline.
struct SteelButtonStyle: ButtonStyle {
    var lit: Bool
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 14).padding(.vertical, 7)
            .foregroundStyle(lit ? DS.graphiteText : DS.steelBright)
            .background {
                if lit {                                  // lit plate = a cutout of the shared header surface
                    GeometryReader { g in Capsule().fill(headerSurfaceGradient(height: g.size.height)) }
                } else {
                    Capsule().fill(Color.white.opacity(configuration.isPressed ? 0.12 : 0.07))
                }
            }
            .overlay(Capsule().strokeBorder(LinearGradient(
                colors: [.white.opacity(lit ? 0.45 : 0.20), .white.opacity(lit ? 0.15 : 0.05)],
                startPoint: .top, endPoint: .bottom), lineWidth: 1))
            .shadow(color: .black.opacity(0.25), radius: 4, y: 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
    }
}

/// Steel progress bar: recessed capsule track + steel fill. `notches` (0..1 fractions,
/// e.g. Topaz's scene-cut segment boundaries) render as small ticks across the track, and
/// `flashKey` (the completed-segment count) triggers a tiny brightness pulse on the fill
/// each time a segment lands.
/// Two-layer steel progress bar (the Topaz segment design):
///   • BRIGHT front fill = COMPLETED segments only — it snaps to the last finished
///     boundary, and its leading edge IS the boundary indicator.
///   • DARK shadow fill = LIVE progress, creeping through the current segment at the
///     real encode rate, out ahead of the bright edge.
///   • When a segment lands, the bright fill SWEEPS quickly across the finished span
///     (+ a brief flash), swallowing it and its notch; only the NEXT upcoming notch shows.
/// Callers without notches (queue bars) pass completed == live → a plain single bar.
struct SteelBar: View {
    let completed: Double        // 0...1 — bright fill (last finished segment boundary)
    let live: Double             // 0...1 — shadow fill (real-time progress)
    var notches: [Double] = []   // interior boundaries (a trailing 1.0 is dropped)
    var flashKey: Int = 0        // increments → brief flash on the bright fill
    @State private var flash = false
    var body: some View {
        let bright = min(max(completed, 0), 1)
        let ahead = min(max(live, 0), 1)
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08))                 // track
                Capsule().fill(DS.steel.opacity(0.28))                    // shadow: live progress
                    .frame(width: max(6, geo.size.width * ahead))
                ForEach(Array(notches.filter { $0 > bright + 0.001 && $0 < 0.999 }.prefix(1).enumerated()),
                        id: \.offset) { _, n in                           // ONLY the next boundary
                    Rectangle().fill(Color.black.opacity(0.55))
                        .frame(width: 1.5)
                        .offset(x: geo.size.width * n)
                }
                Capsule().fill(LinearGradient(colors: [DS.steelBright, .brand],   // bright: completed
                                              startPoint: .top, endPoint: .bottom))
                    .frame(width: max(6, geo.size.width * bright))
                    .overlay(Capsule().fill(Color.white).opacity(flash ? 0.4 : 0))
                    .animation(.easeOut(duration: 0.55), value: bright)   // the completion sweep
            }
            .clipShape(Capsule())
        }
        .frame(height: 6)
        .onChange(of: flashKey) { old, new in
            guard new > old else { return }      // only forward progress flashes (not a resume reset)
            flash = true
            withAnimation(.easeOut(duration: 0.7)) { flash = false }
        }
    }
}

// MARK: - shared building blocks

struct Card<Content: View>: View {
    var title: String? = nil
    var systemImage: String? = nil
    var hint: String? = nil
    var accessory: AnyView? = nil          // optional trailing control in the header (e.g. a button)
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let title {
                HStack(spacing: 8) {
                    if let systemImage {
                        Image(systemName: systemImage).font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    Text(title.uppercased()).font(.system(size: 11, weight: .semibold)).tracking(0.8)
                        .foregroundStyle(DS.steelDim)
                    Spacer()
                    if let hint {
                        Text(hint).font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                    if let accessory { accessory }
                }
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .panel(DS.radiusCard)
    }
}

struct Pill: View {
    let systemImage: String
    let text: String
    var tint: Color = .neutral
    var iconOnly: Bool = false      // show JUST the icon; `text` becomes the hover tooltip
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage).font(.system(size: 11))
            if !iconOnly { Text(text).font(.system(size: 12, weight: .medium)) }
        }
        .foregroundStyle(tint)
        .padding(.horizontal, iconOnly ? 7 : 11).padding(.vertical, 6)
        .background(Capsule().fill(Color.white.opacity(0.05)))
        .overlay(Capsule().strokeBorder(LinearGradient(              // glass capsule bevel
            colors: [.white.opacity(0.18), .white.opacity(0.05)],
            startPoint: .top, endPoint: .bottom), lineWidth: 0.7))
        .help(iconOnly ? text : "")
    }
}

struct PulseDot: View {
    var color: Color = DS.steelBright
    @State private var on = false
    var body: some View {
        Circle().fill(color).frame(width: 8, height: 8)
            .overlay(
                Circle().stroke(color.opacity(0.55), lineWidth: 2)
                    .scaleEffect(on ? 2.4 : 1).opacity(on ? 0 : 1)
            )
            .onAppear { withAnimation(.easeOut(duration: 1.5).repeatForever(autoreverses: false)) { on = true } }
    }
}

func minutes(_ secs: Double?) -> Int? { secs.map { Int(($0 / 60).rounded()) } }

// The recolored Dolby Vision logo (steel-blue gradient field, double-D knocked out so the
// header shows through). NSImage renders the bundled SVG natively, keeping it crisp + the
// holes transparent; falls back to an SF Symbol if the asset is missing.
struct DolbyMark: View {
    var body: some View {
        if let url = Bundle.main.url(forResource: "DolbyVision", withExtension: "svg"),
           let img = NSImage(contentsOf: url) {
            Image(nsImage: img)
                .resizable().aspectRatio(contentMode: .fit)
                .frame(height: 24)
                .shadow(color: .black.opacity(0.4), radius: 3, y: 1)   // the icon's neutral shadow
                .accessibilityLabel("Dolby Vision")
        } else {
            Image(systemName: "sparkles.tv").font(.system(size: 24, weight: .medium)).foregroundStyle(.tint)
        }
    }
}

// MARK: - header

struct HeaderBar: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let on = store.activated          // appliance: the persisted arm state, not the transient run
        HStack(spacing: 14) {
            DolbyMark()
            VStack(alignment: .leading, spacing: 1) {
                Text("Visionary").font(.system(size: 16, weight: .bold))
                    .foregroundStyle(DS.silverBright).headerSurface()   // cutout of the shared header surface
                Text("4K Dolby Vision Upscaler")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            StatusPuck()
            PowerPill()
            ScreenControlButton()
            Button(action: { Task { await store.toggleAutomation() } }) {
                HStack(spacing: 7) {
                    // APPLIANCE toggle: Activate arms the standing mode (the engine then runs
                    // whenever it can — re-arming itself after stops/launches). Activated =
                    // a lit steel plate; deactivated = a quiet glass outline.
                    Image(systemName: "power").font(.system(size: 11, weight: .bold))
                    Text(on ? "Activated" : "Activate").font(.system(size: 13, weight: .semibold))
                }
            }
            .buttonStyle(SteelButtonStyle(lit: on))
            .help(on ? "Deactivate — stop running and stay idle until you activate again"
                     : "Activate — run whenever possible (re-arms itself after stops and relaunches)")
        }
        .padding(.leading, 84).padding(.trailing, 20).padding(.vertical, 13)
        .frame(maxWidth: .infinity)
        .background(LinearGradient(colors: [DS.bgTop, DS.bgBase],     // graphite glass bar
                                   startPoint: .top, endPoint: .bottom))
        .overlay(alignment: .bottom) { Color.white.opacity(0.06).frame(height: 1) }
        .shadow(color: .black.opacity(0.20), radius: 4, y: 1)
    }
}

// The header status "puck" — a compact, glanceable replacement for the old status banner.
// Monochrome steel: state is BRIGHTNESS + pulse — bright silver pulsing = running, mid
// steel = armed/paused (+ the orchestrator message), dim = idle.
struct StatusPuck: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let s = store.state
        let on = s?.automation_enabled ?? false
        let o = s?.orchestrator
        let running = o?.running ?? false
        let p = o?.progress
        let tint: Color = running ? DS.steelBright : (on ? DS.steel : DS.steelDim)
        let text: String = {
            if running {
                var parts: [String] = []
                if let ep = o?.episode, !ep.isEmpty { parts.append(ep) }
                if let st = (p?.stage ?? o?.stage), !st.isEmpty { parts.append(st.capitalized) }
                if let pc = p?.pct { parts.append("\(pc)%") }
                if let m = minutes(p?.eta_secs), m > 0 { parts.append("~\(m)m") }
                return parts.isEmpty ? (o?.message ?? "Running") : parts.joined(separator: " · ")
            }
            return on ? (o?.message ?? "Armed") : "Idle"
        }()
        let shown = text.count > 46 ? String(text.prefix(45)) + "…" : text   // cap so the puck hugs content
        HStack(spacing: 6) {
            if running {
                PulseDot(color: tint).frame(width: 8, height: 8)
            } else {
                Circle().fill(tint).frame(width: 7, height: 7)
            }
            Text(shown).font(.system(size: 12, weight: .medium)).lineLimit(1)
                .foregroundStyle(tint)
        }
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(Capsule().fill(tint.opacity(0.08)))
        .overlay(Capsule().strokeBorder(tint.opacity(0.22), lineWidth: 0.8))
    }
}

struct PowerPill: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let p = store.state?.power
        let ac = p?.external_connected ?? false
        let ok = (p?.adequate ?? false) && ac        // adequate = the >=140 W brick is connected
        let cap = p?.capacity
        let watts = p?.adapter_watts
        let label: String = {
            if !ac { return "On battery" }
            if let w = watts { return ok ? "\(w) W" : "\(w) W — needs 140 W" }
            return "Wall power"
        }()
        Pill(systemImage: ac ? "powerplug.fill" : "battery.50",
             text: label + (cap != nil ? " · \(cap!)%" : ""),
             tint: ok ? DS.steel : DS.steelBright)   // attention = bright; the icon/wording carries meaning
    }
}

/// "Screen Control" — toggles whether the pipeline may take over the screen (run the DaVinci Resolve
/// stage). Sized to the PowerPill so it doesn't outshout the neighbouring cards. SAME neutral plate in
/// both states; when it is NOT controlling the screen (Quiet Mode on) it gets a muted-red glowing rim.
/// Turning it back on drains the Topaz-done backlog straight to Resolve.
struct ScreenControlButton: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let off = store.quietMode          // Quiet Mode ON ⇒ the pipeline is NOT controlling the screen
        Button(action: { Task { await store.toggleQuietMode() } }) {
            Text("Screen Control")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(DS.steelBright)                     // same text colour in both states
                .padding(.horizontal, 11).padding(.vertical, 6)      // same height as the PowerPill (140 W) card
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .overlay {                                           // rim: a muted-red laser outline when NOT
                    if off {                                          // controlling the screen, else the quiet bevel
                        Capsule().strokeBorder(DS.quietRedLight, lineWidth: 1)
                            .shadow(color: DS.quietRedLight.opacity(0.7), radius: 2)
                            .shadow(color: DS.quietRedDark.opacity(0.6), radius: 1)
                            .opacity(0.8)                             // 80% of the previous rim strength
                    } else {
                        Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.8)
                    }
                }
        }
        .buttonStyle(.plain)
        .help(off ? "Screen control OFF — the pipeline won't run Resolve or take the screen (drains when you turn it back on)"
                  : "Screen control ON — the pipeline may run Resolve. Click to stop it taking the screen while you work.")
    }
}

// MARK: - issues banner

struct IssuesBanner: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let lines = (store.state?.log ?? []).suffix(6)
        if !lines.isEmpty {
            // THE one hue exception in the monochrome scheme: genuine faults stay red so an
            // overnight failure is unmissable at a glance — but on the glass plate recipe.
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 16))
                    .frame(width: 40, height: 40)
                    .background(RoundedRectangle(cornerRadius: 10).fill(DS.fault.opacity(0.16)))
                    .foregroundStyle(DS.fault)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Recent issues").font(.system(size: 14, weight: .semibold))
                    Text(lines.joined(separator: "\n"))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                let shape = RoundedRectangle(cornerRadius: DS.radiusCard, style: .continuous)
                ZStack {
                    shape.fill(Color.black.opacity(0.18))
                    shape.fill(DS.fault.opacity(0.10))
                }
                .shadow(color: .black.opacity(0.35), radius: 8, y: 2)
            }
            .overlay(RoundedRectangle(cornerRadius: DS.radiusCard, style: .continuous)
                .strokeBorder(LinearGradient(colors: [DS.fault.opacity(0.55), DS.fault.opacity(0.15)],
                                             startPoint: .top, endPoint: .bottom), lineWidth: 1))
        }
    }
}

// MARK: - pipeline

// Monochrome steel: stages carry no per-stage hue — they differ by symbol, name, and
// number; the ACTIVE stage is the lit one (silver badge well + bevel border + pulse).
struct StageInfo { let key, name, symbol, desc, how: String }

let PIPELINE: [StageInfo] = [
    .init(key: "download", name: "Download", symbol: "arrow.down.circle",
          desc: "Pull the 1080p source from the NAS to local scratch.", how: "FTP RETR · size-verified"),
    .init(key: "topaz", name: "Topaz", symbol: "cpu",
          desc: "Upscale 1080p → 4K (or clean an already-4K source). ProRes HQ 10-bit, range preserved — never SDR↔HDR.", how: "per-show preset"),
    .init(key: "resolve", name: "Resolve", symbol: "wand.and.stars",
          desc: "Scene-cut, Dolby Vision analyze, render mute master — HDR inherited from the project.", how: "H.265 Main10 · DV 8.1"),
    .init(key: "remux", name: "Remux", symbol: "square.stack.3d.up",
          desc: "Re-encode the DV video under a hard peak-bitrate cap, fold the original audio + subtitles back on, smart loudness boost.", how: "x265 peak-cap · DV 8.1"),
    .init(key: "upload", name: "Upload", symbol: "arrow.up.circle",
          desc: "Push the finished master into the NAS Plex library.", how: "FTP STOR · owner 1000:10"),
]

enum StageRole { case run, finisher, inactive }

// Per-item time formatters for the finisher card (shared with the finishing lane logic).
func finHMS(_ secs: Double?) -> String? {
    guard let s = secs, s >= 1 else { return nil }
    let t = Int(s.rounded()); let h = t / 3600, m = (t % 3600) / 60, sec = t % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
}
func finLeft(_ secs: Double?) -> String? {
    guard let s = secs, s > 0 else { return nil }
    let t = Int(s.rounded())
    if t < 90 { return "~\(t)s left" }
    if t < 5400 { return "~\(Int((s / 60).rounded())) min left" }
    return "~\(t / 3600)h \((t % 3600) / 60)m left"
}

struct PipelineCard: View {
    @EnvironmentObject var store: AppStore
    @State private var confirmingSkip = false
    var body: some View {
        let o = store.state?.orchestrator
        let running = (store.state?.automation_enabled ?? false) && (o?.running ?? false)
        let cur = o?.current
        let skippable = running && cur?.kind == "youtube"
        // The two independently-active stages under the topaz/remux overlap: the RUN thread's
        // stage (download/topaz/resolve) and the FINISHER thread's stage (remux/upload/cleanup).
        let runStage = running ? o?.stage : nil
        let finStage = o?.finishing?.stage
        let twoUp = (runStage != nil) && (finStage != nil) && (runStage != finStage)
        // The current-episode name MOVES into each active card's top-right (below). The header
        // hint is only the idle next-up preview now — nil while anything is processing.
        let headerHint: String? = (runStage != nil || finStage != nil) ? nil : nowProcessing
        Card(title: "The pipeline", systemImage: "arrow.triangle.branch", hint: headerHint,
             accessory: skippable ? AnyView(
                Button { confirmingSkip = true } label: {
                    Label("Skip", systemImage: "forward.end")
                        .font(.system(size: 11, weight: .medium)).foregroundStyle(DS.steelDim)
                }
                .buttonStyle(.plain)
                .help("Skip & delete this video — stops the encode, deletes the download, youtarr forgets it")
                .confirmationDialog("Skip & delete \"\(cur?.title ?? "this video")\"?",
                                    isPresented: $confirmingSkip, titleVisibility: .visible) {
                    Button("Skip & delete", role: .destructive) {
                        Task { await store.deleteYoutubeVideo(channel: cur?.channel, name: cur?.name ?? "") }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("Stops the encode now; the video is deleted and never re-downloaded.")
                }) : nil) {
            HStack(alignment: .top, spacing: 6) {
                ForEach(Array(PIPELINE.enumerated()), id: \.offset) { i, st in
                    let role: StageRole = (st.key == runStage) ? .run
                        : (st.key == finStage) ? .finisher : .inactive
                    StageView(index: i + 1, info: st, role: role, twoUp: twoUp,
                              episode: episodeLabel(role))
                    if i < PIPELINE.count - 1 {
                        Image(systemName: "chevron.right").font(.system(size: 11)).foregroundStyle(.tertiary)
                            .padding(.top, 21)
                    }
                }
            }
        }
    }

    // The concise episode token shown in an active card's top-right.
    func episodeLabel(_ role: StageRole) -> String? {
        let o = store.state?.orchestrator
        switch role {
        case .finisher: return o?.finishing?.ep                       // already a display string
        case .run:      return runEpisodeShort(o?.current)
        case .inactive: return nil
        }
    }
    func runEpisodeShort(_ it: UpNextDTO?) -> String? {
        guard let it else { return nil }
        switch it.kind {
        case "movie":   return store.movieTitle(it.name, it.title)
        case "youtube": return (it.title?.isEmpty == false) ? it.title : epTitle(it.name)
        default:        return it.ep ?? epTitle(it.source_name)       // "S06E07"
        }
    }

    // The item the pipeline is on when IDLE — previews the next-up item in the header. While
    // running, the per-card episode labels carry this instead (see episodeLabel).
    var nowProcessing: String? {
        let o = store.state?.orchestrator
        let first = (o?.running == true ? o?.current : nil) ?? store.state?.up_next?.first
        guard let first else { return nil }
        switch first.kind {
        case "movie":
            return store.movieTitle(first.name, first.title)
        case "youtube":
            let vid = (first.title?.isEmpty == false) ? (first.title ?? "") : epTitle(first.name)
            return [vid, first.channel ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
        default:
            let ep = [first.ep ?? "", epTitle(first.source_name)].filter { !$0.isEmpty }.joined(separator: " ")
            let show = store.seriesTitle(first.series ?? "")
            return [ep, show].filter { !$0.isEmpty }.joined(separator: " · ")
        }
    }
}

struct StageView: View {
    let index: Int
    let info: StageInfo
    var role: StageRole = .inactive
    var twoUp: Bool = false            // two stages live at once → inactive cards condense to icons
    var episode: String? = nil         // this card's episode, shown top-right when active
    @EnvironmentObject var store: AppStore
    var isActive: Bool { role != .inactive }
    var condensed: Bool { role == .inactive && twoUp }
    var body: some View {
        if condensed {
            // Just the icon — two stages need the room. Name/desc live in the tooltip.
            Image(systemName: info.symbol).font(.system(size: 14, weight: .medium))
                .frame(width: 30, height: 30)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(Color.white.opacity(0.05)))
                .foregroundStyle(DS.steelDim)
                .frame(width: 46, height: 58, alignment: .center)
                .panel(DS.radiusControl, tint: nil, inset: true)
                .help("\(info.name): \(info.desc)")
                .animation(.easeInOut(duration: 0.22), value: twoUp)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 9) {
                    Image(systemName: info.symbol).font(.system(size: 14, weight: .medium))
                        .frame(width: 30, height: 30)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                            .fill(Color.white.opacity(isActive ? 0.10 : 0.05)))
                        .foregroundStyle(isActive ? DS.steelBright : DS.steelDim)
                    Text(info.name).font(.system(size: isActive ? 15 : 13, weight: .semibold))
                        .foregroundStyle(isActive ? DS.steelBright : Color.labelC)
                    if isActive { PulseDot() }
                    Spacer(minLength: 4)
                    // top-right: this card's EPISODE while active, else the stage index number
                    if isActive, let ep = episode, !ep.isEmpty {
                        Text(ep).font(.system(size: 11, weight: .semibold)).monospacedDigit()
                            .foregroundStyle(DS.steelBright).lineLimit(1)
                            .help("Now in \(info.name): \(ep)")
                    } else {
                        Text("\(index)").font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(isActive ? DS.steelBright : DS.steelDim)
                    }
                }
                if isActive {
                    VStack(alignment: .leading, spacing: 7) {
                        Text(info.how).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                        if role == .finisher { FinisherProgress() }    // reads orchestrator.finishing
                        else { StageProgress(stageKey: info.key) }     // reads orchestrator.progress
                    }
                }
            }
            .padding(13)
            .frame(minWidth: isActive ? (twoUp ? 220 : 280) : 90, maxWidth: .infinity, alignment: .topLeading)
            .panel(DS.radiusControl, tint: isActive ? DS.steelBright : nil, inset: !isActive)
            .overlay {
                if isActive {                                   // a quiet steel edge marks the live stage
                    RoundedRectangle(cornerRadius: DS.radiusControl, style: .continuous)
                        .strokeBorder(DS.steelBright.opacity(0.35), lineWidth: 1)
                }
            }
            .help("\(info.desc)  (\(info.how))")
            .animation(.easeInOut(duration: 0.22), value: isActive)
        }
    }
}

// The finisher stage's live progress (remux/upload/cleanup on the overlap thread). Reads
// orchestrator.finishing (its OWN pct/elapsed/eta), never orchestrator.progress (the run stage).
struct FinisherProgress: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        if let f = store.state?.orchestrator?.finishing, let pct = f.pct {
            let live = min(1, max(0, pct / 100))
            // Same notched segment bar as Topaz — the remux is segmented too (dvcap ~5-min chunks):
            // bright fill = completed segments (snaps to the last finished boundary + a flash when
            // one lands), dark shadow = live progress through the current segment. Non-segmented
            // finisher stages (upload) send no notches → a plain single bar, unchanged.
            let notches = f.notches ?? []
            let done = f.seg_done ?? 0
            let completed: Double = notches.isEmpty ? live
                : (done >= notches.count ? 1.0 : (done > 0 ? notches[done - 1] : 0))
            VStack(alignment: .leading, spacing: 5) {
                SteelBar(completed: completed, live: live, notches: notches, flashKey: done)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(String(format: "%.0f%%", pct))
                        .font(.system(size: 17, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(DS.steelBright)
                    if let c = finHMS(f.elapsed_secs) {
                        Text(c).font(.system(size: 12, weight: .medium)).monospacedDigit().foregroundStyle(.secondary)
                    }
                    if let e = finLeft(f.eta_secs) {
                        Text(e).font(.system(size: 12, weight: .medium)).monospacedDigit().foregroundStyle(.secondary)
                    }
                    if let d = f.seg_done, let t = f.seg_total, t > 0 {
                        Spacer()
                        Text("\(min(d + 1, t))/\(t)")
                            .font(.system(size: 11)).monospacedDigit().foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(.top, 3)
        }
    }
}

struct StageProgress: View {
    let stageKey: String
    @EnvironmentObject var store: AppStore
    var body: some View {
        let pr = store.state?.orchestrator?.progress
        if let pr, pr.stage == stageKey, let pct = pr.pct {
            VStack(alignment: .leading, spacing: 5) {
                // Two-layer topaz bar: bright = completed segments (snapped to the last finished
                // boundary, quick sweep + flash when one lands); dark shadow = live progress
                // through the current segment. No notch plan yet → plain single bar.
                let live = Double(pct) / 100
                let notches = pr.notches ?? []
                let done = pr.seg_done ?? 0
                let completed: Double = notches.isEmpty ? live
                    : (done >= notches.count ? 1.0 : (done > 0 ? notches[done - 1] : 0))
                SteelBar(completed: completed, live: live,
                         notches: notches, flashKey: done)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    // The hero number — prominent but solid steel (the gradient stays on the title).
                    Text("\(pct)%")
                        .font(.system(size: 17, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(DS.steelBright)
                    Text(elapsedClock(pr.elapsed_secs))       // stopwatch: time spent so far (counts up)
                        .font(.system(size: 12, weight: .medium)).monospacedDigit().foregroundStyle(.secondary)
                    Text(etaSuffix(pr.eta_secs))              // remaining (counts down): " · ~7 min left"
                        .font(.system(size: 12, weight: .medium)).monospacedDigit().foregroundStyle(.secondary)
                    if let d = pr.seg_done, let t = pr.seg_total, t > 0 {
                        Spacer()
                        // Slow content (segments averaging >15 min): the stage eta alone reads
                        // as "hours away", so show the CURRENT segment's eta for near-term motion.
                        let segEta: String = {
                            guard (pr.avg_seg_secs ?? 0) > 900, let e = pr.seg_eta_secs, e > 0
                            else { return "" }
                            return etaSuffix(e).replacingOccurrences(of: " left", with: "")
                        }()
                        Text("\(min(d + 1, t))/\(t)\(segEta)")
                            .font(.system(size: 11)).monospacedDigit().foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(.top, 3)
        }
    }

    // " · ~7 min left" / " · ~45s left" / " · ~1h 12m left" — empty until an estimate exists.
    func etaSuffix(_ secs: Double?) -> String {
        guard let s = secs, s > 0 else { return "" }
        let t = Int(s.rounded())
        if t < 90 { return " · ~\(t)s left" }
        if t < 5400 { return " · ~\(Int((s / 60).rounded())) min left" }
        return " · ~\(t / 3600)h \((t % 3600) / 60)m left"
    }

    // Elapsed stopwatch — counts UP: "9:12" (mm:ss) or "1:09:12" (h:mm:ss). Empty until a second passes.
    func elapsedClock(_ secs: Double?) -> String {
        guard let s = secs, s >= 1 else { return "" }
        let t = Int(s.rounded()); let h = t / 3600, m = (t % 3600) / 60, sec = t % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec) : String(format: "%d:%02d", m, sec)
    }
}

// MARK: - current series

struct SeriesCard: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let mode = store.mode
        let locked = store.state?.automation_enabled ?? false
        let title = mode == "movie" ? "Current library" : (mode == "youtube" ? "YouTube channels" : "Current series")
        let icon = mode == "movie" ? "film.stack" : (mode == "youtube" ? "play.rectangle" : "tv")
        // the ONE statement of the scheduling model — kept accurate: movies and videos run
        // start-to-finish when their slot comes up; YouTube is cadence-gated, not a queue-jumper
        let n = store.state?.settings?.youtube_every_tv_episodes ?? 2
        Card(title: title, systemImage: icon,
             hint: "TV in order · movies run whole when due · 1 video per \(n) episodes") {
            ModeNavBar()                              // the TV / YouTube / Movies view toggle
            switch mode {
            case "movie":   MovieMode(locked: locked)
            case "youtube": YouTubeMode(locked: locked)
            default:        TVMode(locked: locked)
            }
            // THE queue — one global processing order, identical in every tab. The modes
            // above only ADD to it; switching tabs never hides or changes it.
            if let up = store.state?.up_next, !up.isEmpty {
                Divider().padding(.vertical, 2)
                UpNextView(items: up,
                           showSeries: (store.state?.series?.active?.count ?? 0) > 1)
            }
        }
    }
}

// A segmented nav bar across the top of the section: TV Shows | Movies. Always switchable —
// it's just a VIEW toggle now (the movie queue is a priority interrupt, not a separate run
// mode), so you can flip to Movies and add to the queue even while a TV run is going.
struct ModeNavBar: View {
    @EnvironmentObject var store: AppStore
    @Namespace private var chipNS                     // shared id so the active chip SLIDES between segments
    var body: some View {
        let mode = store.mode
        HStack(spacing: 4) {
            seg("TV Shows", "tv", "tv", mode)
            seg("YouTube", "youtube", "play.rectangle", mode)
            seg("Movies", "movie", "film.stack", mode)
        }
        .padding(4)
        .panel(DS.radiusControl, inset: true)         // recessed glass track
        .animation(.spring(response: 0.32, dampingFraction: 0.82), value: mode)   // slide to the clicked tab
    }
    @ViewBuilder func seg(_ title: String, _ value: String, _ icon: String, _ mode: String) -> some View {
        let on = mode == value
        Button {
            if !on { Task { await store.setMode(value) } }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .medium))
                Text(title).font(.system(size: 13, weight: .semibold))
            }
            .foregroundStyle(on ? DS.steelBright : DS.steelDim)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 7)
            .background {                              // active segment = ONE raised glass chip that slides
                if on {                               // (matchedGeometryEffect interpolates its frame across
                    RoundedRectangle(cornerRadius: 6, style: .continuous)   // segments when `mode` changes)
                        .fill(Color.white.opacity(0.10))
                        .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.14), lineWidth: 1))
                        .matchedGeometryEffect(id: "modeChip", in: chipNS)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// Brief inline status while a just-picked title is auto-detecting its preset (shotonwhat + TMDb).
struct DetectingRow: View {
    var body: some View {
        HStack(spacing: 7) {
            ProgressView().controlSize(.small)
            Text("Detecting preset…").font(.system(size: 12)).foregroundStyle(.secondary)
        }
    }
}

// The preset chooser shown as a STEP when selecting a show / adding a movie (so the preset
// is set at add-time, not in Settings). Bound to `pick`; Confirm/Cancel handled by the parent.
struct PresetChooser: View {
    let title: String
    let catalog: [PresetDTO]
    @Binding var pick: String
    let confirmLabel: String
    let onConfirm: () -> Void
    let onCancel: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text("Pick a Topaz preset for \(pretty(title))")
                .font(.system(size: 13, weight: .semibold))
            HStack(alignment: .top, spacing: 14) {
                Picker("", selection: $pick) { ForEach(catalog) { Text($0.label).tag($0.key) } }
                    .labelsHidden().frame(maxWidth: 230)
                Text(catalog.first { $0.key == pick }?.desc ?? "")
                    .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                Button(confirmLabel, action: onConfirm).buttonStyle(SteelButtonStyle(lit: true))
                    .disabled(pick.isEmpty)
                Button("Cancel", action: onCancel).buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading).panel(DS.radiusControl, tint: DS.steel)
    }
}

// A reusable search-as-you-type picker (the series + movie lists are too long for a plain
// dropdown). Picking calls onSelect(id) and clears the query.
struct PickOption: Identifiable {
    let id: String
    let label: String
    var detail: String? = nil   // secondary line under the label (e.g. a movie's routing tags)
}

struct SearchablePicker: View {
    let placeholder: String
    let options: [PickOption]
    var disabled = false
    let onSelect: (String) -> Void
    @State private var query = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundStyle(.secondary)
                TextField(placeholder, text: $query).textFieldStyle(.plain).font(.system(size: 13))
                if !query.isEmpty {
                    Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 9).padding(.vertical, 7)
            .panel(8, inset: true)                     // recessed input well
            .opacity(disabled ? 0.5 : 1).disabled(disabled)
            if !query.isEmpty && !disabled {
                let matches = options.filter { $0.label.localizedCaseInsensitiveContains(query) }
                let shown = Array(matches.prefix(50))
                VStack(alignment: .leading, spacing: 0) {
                    if shown.isEmpty {
                        Text("No matches").font(.system(size: 12)).foregroundStyle(.secondary)
                            .padding(.vertical, 8).padding(.horizontal, 9)
                    } else {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(shown) { m in
                                    Button { onSelect(m.id); query = "" } label: {
                                        VStack(alignment: .leading, spacing: 1) {
                                            Text(m.label).font(.system(size: 13)).lineLimit(1)
                                            if let d = m.detail, !d.isEmpty {
                                                Text(d).font(.system(size: 10.5))
                                                    .foregroundStyle(.secondary).lineLimit(1)
                                            }
                                        }
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding(.vertical, 6).padding(.horizontal, 9)
                                        .contentShape(Rectangle())
                                    }.buttonStyle(.plain)
                                    Divider()
                                }
                            }
                        }.frame(maxHeight: 200)
                        if matches.count > shown.count {
                            Text("…and \(matches.count - shown.count) more — keep typing to narrow")
                                .font(.system(size: 11)).foregroundStyle(.tertiary).padding(7)
                        }
                    }
                }
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(DS.bgBase))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.10), lineWidth: 0.7))
            }
        }
    }
}

// Rescan the library (reusable). For TV it asks Plex to refresh its section(s) + re-pulls show
// titles + the NAS list; for movies it re-pulls the pool. Shows a spinner while in flight.
struct LibraryRefreshButton: View {
    var help = "Rescan for new or renamed shows (via Plex)"
    let action: () async -> Void
    @State private var spinning = false
    var body: some View {
        Button {
            guard !spinning else { return }
            spinning = true
            Task { await action(); spinning = false }
        } label: {
            Group {
                if spinning { ProgressView().controlSize(.small) }
                else { Image(systemName: "arrow.clockwise").font(.system(size: 13, weight: .semibold)) }
            }
            .frame(width: 16, height: 16)
            .padding(.vertical, 7).padding(.horizontal, 9)
            .foregroundStyle(DS.steel)
            .panel(8, inset: true)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(spinning)
        .help(help)
    }
}

// TV mode: search + pick a series (choosing its preset once, the first time), walk its
// episodes (unwatched first). The series stays LOCKED while a run is active.
private struct TVMode: View {
    @EnvironmentObject var store: AppStore
    let locked: Bool
    // In-flight pick state lives in the STORE (same tab-switch bug as MovieMode — a
    // mid-detection or awaiting-confirm series selection must survive leaving the tab).
    var body: some View {
        let s = store.state
        let shows = s?.series?.shows ?? []
        let active = s?.series?.active ?? shows.compactMap { $0.name }
        let catalog = store.presetCatalog
        VStack(alignment: .leading, spacing: 12) {
            if store.tvDetecting { DetectingRow() }
            // Shared preset chooser — appears when a picked show has no preset yet, or on "Change".
            if let ps = store.pendingSeries {
                PresetChooser(title: store.seriesTitle(ps), catalog: catalog, pick: $store.seriesPick,
                              confirmLabel: store.pendingSeriesSlot != nil ? "Select series" : "Update preset") {
                    Task {
                        if let slot = store.pendingSeriesSlot { await store.setSlotWithPreset(slot, ps, store.seriesPick) }
                        else { await store.setPreset(ps, store.seriesPick) }
                        store.pendingSeries = nil; store.pendingSeriesSlot = nil
                    }
                } onCancel: { store.pendingSeries = nil; store.pendingSeriesSlot = nil }
            }
            // One identical block per active show (a replica of the first); each round-robins.
            ForEach(Array(shows.enumerated()), id: \.element.id) { i, show in
                showBlock(index: i, show: show, active: active, catalog: catalog)
            }
            // The next empty slot is just a search bar — to pick the first show, or add the 2nd/3rd.
            if active.count < 3 {
                seriesPicker(index: active.count, active: active, catalog: catalog)
            }
            // (the shared queue renders once in SeriesCard, below every mode)
            if (s?.up_next ?? []).isEmpty && !active.isEmpty {
                Text("All caught up — every source has a DV master.")
                    .font(.system(size: 13)).foregroundStyle(DS.steel)
            }
        }
    }

    // A slot's search picker (changes THIS slot's show, or adds when the slot is empty). Slot 0
    // also carries the library refresh button — every other slot omits it (the only difference).
    @ViewBuilder
    func seriesPicker(index: Int, active: [String], catalog: [PresetDTO]) -> some View {
        let filled = index < active.count
        let placeholder = active.isEmpty ? "Search for a series…"
            : (filled ? "Search to change this series…" : "Add a series to round-robin…")
        HStack(alignment: .top, spacing: 8) {
            SearchablePicker(placeholder: placeholder,
                             options: store.seriesOptions
                                 .filter { nm in !active.enumerated().contains { $0.offset != index && $0.element == nm } }
                                 .map { PickOption(id: $0, label: store.seriesTitle($0)) },
                             disabled: locked || !store.seriesReachable) { id in
                Task {
                    let prof = await store.profileFor(id)
                    if prof?.configured == true {
                        await store.setSlot(index, id)               // preset known → set straight away
                    } else {
                        store.tvDetecting = true                     // try shotonwhat/TMDb first
                        let auto = await store.detectPreset("tv", id)
                        store.tvDetecting = false
                        if let key = auto {
                            await store.setSlotWithPreset(index, id, key)   // confident → auto-apply
                        } else {
                            store.seriesPick = prof?.preset ?? catalog.first?.key ?? ""
                            store.pendingSeriesSlot = index
                            store.pendingSeries = id                 // no match → ask, then set this slot
                        }
                    }
                }
            }
            if index == 0 { LibraryRefreshButton { await store.refreshLibrary() } }
        }
    }

    // One show's block — identical for every slot (the "replica"): its change-picker, title +
    // counts, preset + Change, unwatched-first, and its OWN progress bar. Slot 0 gets the refresh;
    // the others get a remove (×). A divider separates stacked shows.
    @ViewBuilder
    func showBlock(index: Int, show: SeriesShowDTO, active: [String], catalog: [PresetDTO]) -> some View {
        let name = show.name ?? ""
        let key = show.preset ?? ""
        if index > 0 { Divider().padding(.vertical, 1) }
        VStack(alignment: .leading, spacing: 10) {
            seriesPicker(index: index, active: active, catalog: catalog)
            HStack(spacing: 12) {
                Label(store.seriesTitle(name), systemImage: "tv").font(.system(size: 13, weight: .medium)).lineLimit(1)
                if index == 0 && locked {
                    Pill(systemImage: "lock.fill", text: "Locked — stop the run to change", tint: DS.steelBright, iconOnly: true)
                } else if index == 0 && !store.seriesReachable {
                    Pill(systemImage: "wifi.slash", text: "NAS unreachable", tint: DS.steelBright, iconOnly: true)
                }
                Spacer()
                QueueCounts(q: show.queue)
                if index > 0 && !locked {
                    Button { Task { await store.removeSeries(name) } } label: {
                        Image(systemName: "xmark.circle.fill").font(.system(size: 13)).foregroundStyle(.secondary)
                    }.buttonStyle(.plain).help("Remove from round-robin")
                }
            }
            HStack(spacing: 8) {
                Image(systemName: "cpu").font(.system(size: 12)).foregroundStyle(DS.steelDim)
                Text(catalog.first { $0.key == key }?.label ?? (key.isEmpty ? "—" : key))
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .help("Topaz preset")
                if !(show.configured ?? false) {
                    Text("(default)").font(.system(size: 11)).foregroundStyle(.tertiary)
                }
                if !locked {
                    Button("Change") { store.seriesPick = key.isEmpty ? (catalog.first?.key ?? "") : key
                                       store.pendingSeriesSlot = nil; store.pendingSeries = name }
                        .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
                }
                Spacer()
            }
            unwatchedToggle(name, show.unwatched_first ?? true)
            NormalizeAudioRow(key: name, on: show.normalize_audio ?? true, locked: locked)
            if let q = show.queue { QueueProgress(q: q) }     // the per-show total progress bar (moved here)
        }
    }

    // Compact per-show checkbox — sits under each show's preset so it's set per show, not global.
    @ViewBuilder
    func unwatchedToggle(_ show: String, _ on: Bool) -> some View {
        Toggle(isOn: Binding(get: { on },
                             set: { v in Task { await store.setShowUnwatchedFirst(show, v) } })) {
            Text("Unwatched episodes first").font(.system(size: 12)).foregroundStyle(.secondary)
        }
        .help("On: skip ahead to episodes you haven't watched. Off: start at the beginning of the show.")
    }
}

// Per-item "Normalize audio" row — the SAME control under a TV show, a queued movie, and a
// YouTube channel, formatted like the Topaz preset row (icon + value capsule + Change): this
// is decided ONCE at the start of a show and deliberately hard to flip later, because a show
// whose episodes mix boosted and original audio is exactly the inconsistency the per-item
// setting exists to prevent. Change requires a confirmation for the same reason. `key` is the
// item's show_profiles string (show name / movie title / channel folder — the same key its
// Topaz preset uses), which is also what the remux stage looks up (p.series) to gate the boost.
private struct NormalizeAudioRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let on: Bool
    var locked: Bool = false          // hides Change (TV passes the run-lock; movies/channels false)
    @State private var confirming = false
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "square.stack.3d.up").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            Text(on ? "Normalized audio" : "Original audio")
                .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .help("Remux audio: normalized = quiet audio boosted to the loudness target; original = bit-exact copy")
            if !locked {
                Button("Change") { confirming = true }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
            }
            Spacer()
        }
        .confirmationDialog("Switch to \(on ? "original (bit-exact)" : "normalized") audio?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(on ? "Use original audio" : "Use normalized audio") {
                Task { await store.setNormalizeAudio(key, !on) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Decide this at the start of a show — episodes already made keep their current "
                 + "audio, so changing it mid-show leaves the show inconsistent.")
        }
    }
}

// Movie mode: search the library and queue specific movies, each with its own preset chosen
// in the add step. Movies can be added ANY time (even during a run) — they jump ahead of the
// next TV episode, then the TV show continues.
private struct MovieMode: View {
    @EnvironmentObject var store: AppStore
    let locked: Bool
    // NOTE: the in-flight add state (pendingMovie/moviePick/movieDetecting) lives in the
    // STORE, not view @State — this view is recreated on every tab switch, and view-local
    // state silently dropped a mid-detection or awaiting-confirm add (the reported "added
    // a movie, switched tabs, it never showed up" bug).
    var body: some View {
        let items = store.state?.movies?.selected?.items ?? []
        let catalog = store.presetCatalog
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 8) {
                SearchablePicker(placeholder: "Search movies to add…",   // never locked — addable mid-run
                                 options: store.movieLibrary.map { PickOption(id: $0.id, label: store.movieTitle($0.name, $0.title ?? $0.name), detail: $0.pipelineHint) },
                                 disabled: !store.moviesReachable) { id in
                    if let m = store.movieLibrary.first(where: { $0.id == id }) {
                        let queued = items.contains { $0.name == m.name }
                        Task {
                            let prof = await store.profileFor(m.title ?? "")
                            if queued {                                  // re-pick a queued movie → edit its preset
                                store.moviePick = prof?.preset ?? catalog.first?.key ?? ""
                                store.pendingMovie = m
                            } else if prof?.configured == true {
                                await store.addMovieWithPreset(m, preset: prof?.preset ?? "")  // saved → add straight
                            } else {
                                store.movieDetecting = true              // try shotonwhat/TMDb first
                                let auto = await store.detectPreset("movie", "", name: m.name)
                                store.movieDetecting = false
                                if let key = auto {
                                    await store.addMovieWithPreset(m, preset: key)   // confident → auto-add
                                } else {
                                    store.moviePick = prof?.preset ?? catalog.first?.key ?? ""
                                    store.pendingMovie = m               // no match → ask its preset
                                }
                            }
                        }
                    }
                }
                LibraryRefreshButton(help: "Rescan the Movies library") { await store.fetchMovies() }
            }
            if store.movieDetecting { DetectingRow() }
            if let pm = store.pendingMovie {
                let queued = items.contains { $0.name == pm.name }
                PresetChooser(title: store.movieTitle(pm.name, pm.title ?? pm.name), catalog: catalog,
                              pick: $store.moviePick,
                              confirmLabel: queued ? "Update preset" : "Add to queue") {
                    Task { await store.addMovieWithPreset(pm, preset: store.moviePick); store.pendingMovie = nil }
                } onCancel: { store.pendingMovie = nil }
            }
            HStack(spacing: 12) {
                if !store.moviesReachable {
                    Pill(systemImage: "wifi.slash", text: "NAS unreachable", tint: DS.steelBright, iconOnly: true)
                } else {
                    Text("\(store.movieLibrary.count) movies without DV")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                }
                Spacer()
                Pill(systemImage: "tray.full", text: "\(items.count) queued", tint: items.isEmpty ? DS.steelDim : DS.steel)
            }
            if items.isEmpty {
                Text("Search above to add movies to the queue.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(items) { m in
                        MovieRow(m: m, catalog: catalog) {
                            store.moviePick = m.preset ?? catalog.first?.key ?? ""
                            store.pendingMovie = m                                     // tap → change its preset
                        }
                    }
                }.panel(DS.radiusControl, inset: true)
            }
        }
        .onAppear { if store.movieLibrary.isEmpty { Task { await store.fetchMovies() } } }
    }
}

private struct MovieRow: View {
    @EnvironmentObject var store: AppStore
    let m: MovieItemDTO
    let catalog: [PresetDTO]
    let onTap: () -> Void
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: "film").foregroundStyle(.secondary).font(.system(size: 12))
                Text(store.movieTitle(m.name, m.title ?? m.name)).font(.system(size: 13)).lineLimit(1)
                Spacer()
                Text(catalog.first { $0.key == m.preset }?.label ?? (m.preset ?? "—"))
                    .font(.system(size: 11, weight: .medium)).foregroundStyle(DS.steel)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                Button { if let n = m.name { Task { await store.removeMovie(n) } } } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }.buttonStyle(.plain)
            }
            .padding(.vertical, 7).padding(.horizontal, 10)
            .contentShape(Rectangle())
            .onTapGesture { onTap() }
            .help("Tap to change this movie's Topaz preset")
            // OUTSIDE the tappable HStack — the row tap opens the preset chooser, and the
            // Change button must not trigger it. Keyed by TITLE (the movie's settings key).
            NormalizeAudioRow(key: m.title ?? m.name ?? "", on: m.normalize_audio ?? true)
                .padding(.horizontal, 10).padding(.bottom, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
        }
    }
}

// YouTube mode: search youtarr's channels, queue channels to upscale. A queued channel's videos
// (newest first) process as a priority tier ahead of TV; once a channel is done it drops off and
// the TV show continues. Addable/reorderable any time, just like movies.
private struct YouTubeMode: View {
    @EnvironmentObject var store: AppStore
    let locked: Bool
    @State private var pending: YouTubeChannelDTO? = nil   // a queued channel awaiting a preset change
    @State private var pick = ""
    var body: some View {
        let yt = store.state?.youtube
        let items = yt?.items ?? []
        let connected = yt?.connected ?? store.ytConnected
        let configured = store.ytConfigured
        let catalog = store.presetCatalog
        let queued = Set(items.compactMap { $0.channelId })
        VStack(alignment: .leading, spacing: 12) {
            if !connected && !configured {
                // The button opens the browser if keys are present; if not, connectYouTube just
                // re-reads configured (panel stays put) — either way one click does the right thing.
                YouTubeSetupPanel { Task { await store.connectYouTube() } }
            } else if !connected {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Connect your YouTube account to upscale channels from your subscriptions.")
                        .font(.system(size: 13)).foregroundStyle(.secondary)
                    Button { Task { await store.connectYouTube() } } label: {
                        Label("Connect YouTube", systemImage: "play.rectangle.fill")
                    }.buttonStyle(SteelButtonStyle(lit: true))
                    Text("Opens Google sign-in in your browser (one-time), then reads your subscriptions.")
                        .font(.system(size: 11)).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
                }
            } else {
                HStack(alignment: .top, spacing: 8) {
                    SearchablePicker(placeholder: "Search your subscriptions to add…",
                                     options: store.channelLibrary.filter { !queued.contains($0.channelId ?? "") }
                                         .map { PickOption(id: $0.channelId ?? "", label: $0.title ?? "") },
                                     disabled: false) { id in
                        if let s = store.channelLibrary.first(where: { $0.channelId == id }) {
                            Task { await store.addChannel(id, s.title ?? id) }
                        }
                    }
                    LibraryRefreshButton(help: "Refresh subscriptions") { await store.fetchChannels() }
                }
                if let pc = pending {
                    PresetChooser(title: pc.title ?? "", catalog: catalog, pick: $pick, confirmLabel: "Update preset") {
                        Task { await store.setChannelPreset(pc.folder_name ?? "", pick); pending = nil }
                    } onCancel: { pending = nil }
                }
                HStack(spacing: 12) {
                    Text("\(store.channelLibrary.count) subscriptions").font(.system(size: 13)).foregroundStyle(.secondary)
                    Spacer()
                    Pill(systemImage: "tray.full", text: "\(items.count) queued", tint: items.isEmpty ? DS.steelDim : DS.steel)
                }
                CadenceControl(every: store.state?.settings?.youtube_every_tv_episodes ?? 2) { n in
                    Task { await store.setYoutubeEveryTv(n) }
                }
                if items.isEmpty {
                    Text("Search your subscriptions above to add a channel.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                } else {
                    VStack(spacing: 0) {
                        ForEach(items) { ch in
                            ChannelRow(ch: ch, catalog: catalog) {
                                pick = ch.preset ?? catalog.first?.key ?? ""; pending = ch
                            }
                        }
                    }.panel(DS.radiusControl, inset: true)
                }
            }
        }
        .onAppear { Task { await store.fetchChannels() } }
    }
}

// Global YouTube cadence: how many TV episodes play per 1 YouTube video. YouTube 4K-SDR upscales are
// far slower than a 1080p episode, so this throttles them so they don't crowd out TV.
private struct CadenceControl: View {
    let every: Int
    let onChange: (Int) -> Void
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "rectangle.stack.badge.play")
                .font(.system(size: 15)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text("YouTube cadence").font(.system(size: 13, weight: .medium))
                Text("1 video every \(every) TV episode\(every == 1 ? "" : "s")")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Stepper(value: Binding(get: { every }, set: { onChange($0) }), in: 1...50) { EmptyView() }
                .labelsHidden().fixedSize()
        }
        .padding(10).panel(DS.radiusControl, inset: true)
    }
}

// Shown when no Google OAuth client is in config yet — the "Connect" button would be a no-op, so
// walk the user through the one-time Google Cloud setup instead of a dead button.
private struct YouTubeSetupPanel: View {
    let onRecheck: () -> Void
    private let redirect = "http://localhost:8765/oauth/youtube"
    private let configPath = "~/.topaz-pipeline/config.json"

    private func step(_ n: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text("\(n)").font(.system(size: 11, weight: .bold)).foregroundStyle(DS.graphiteText)
                .frame(width: 18, height: 18).background(Circle().fill(DS.steel))
            Text(text).font(.system(size: 13)).foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
    private func copyRow(_ label: String, _ value: String) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(label).font(.system(size: 11)).foregroundStyle(.tertiary)
                Text(value).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
            }
            Spacer()
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(value, forType: .string)
            } label: { Image(systemName: "doc.on.doc") }
                .buttonStyle(.borderless).help("Copy")
        }.padding(10).panel(8, inset: true)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Set up Google sign-in (one-time)", systemImage: "key.fill")
                .font(.system(size: 14, weight: .semibold))
            Text("Reading your own YouTube subscriptions needs a free Google OAuth client. Create one once, paste two values into your config, then Connect.")
                .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 10) {
                step(1, "console.cloud.google.com → create/pick a project.")
                step(2, "APIs & Services → Library → enable “YouTube Data API v3”.")
                step(3, "OAuth consent screen → External → add your email as a Test user → add scope youtube.readonly.")
                step(4, "Credentials → Create OAuth client ID → type “Web application” → add this Authorized redirect URI:")
                copyRow("Authorized redirect URI", redirect)
                step(5, "Copy the client ID + secret into your config file, then click Connect below.")
                copyRow("Config file", configPath)
            }.padding(12).panel(DS.radiusControl, inset: true)
            HStack(spacing: 10) {
                Button { onRecheck() } label: {
                    Label("I’ve added my keys — Connect", systemImage: "checkmark.circle.fill")
                }.buttonStyle(SteelButtonStyle(lit: true))
                Button {
                    if let url = URL(string: "https://console.cloud.google.com/apis/credentials") {
                        NSWorkspace.shared.open(url)
                    }
                } label: { Label("Open Google Cloud", systemImage: "arrow.up.forward.square") }
                    .buttonStyle(SteelButtonStyle(lit: false))
            }
        }
    }
}

private struct ChannelRow: View {
    @EnvironmentObject var store: AppStore
    let ch: YouTubeChannelDTO
    let catalog: [PresetDTO]
    let onTap: () -> Void
    @State private var confirmingRemove = false
    var body: some View {
        let paused = ch.paused ?? false
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Button { Task { await store.setChannelPaused(ch.channelId ?? "", !paused) } } label: {
                    // Monochrome: Resume is BRIGHT (a paused channel wants attention), Pause is dim.
                    Label(paused ? "Resume" : "Pause", systemImage: paused ? "play.fill" : "pause.fill")
                        .font(.system(size: 11, weight: .medium)).frame(width: 62)
                        .foregroundStyle(paused ? DS.steelBright : DS.steelDim)
                        .padding(.vertical, 4)
                        .background(Capsule().fill(Color.white.opacity(paused ? 0.10 : 0.05)))
                        .overlay(Capsule().strokeBorder(Color.white.opacity(paused ? 0.25 : 0.10), lineWidth: 0.7))
                }.buttonStyle(.plain)
                    .help(paused ? "Resume — youtarr downloads + upscaling restart"
                                 : "Pause — stop downloading & upscaling this channel (keeps its files)")
                Text(ch.title ?? ch.folder_name ?? "").font(.system(size: 13)).lineLimit(1)
                    .foregroundStyle(paused ? .secondary : .primary)
                Group {
                    Picker("", selection: Binding(get: { ch.scope ?? "popular" },
                                                  set: { s in Task { await store.setChannelScope(ch.channelId ?? "", s) } })) {
                        Text("Most popular").tag("popular"); Text("All").tag("all")
                    }.labelsHidden().frame(width: 128).font(.system(size: 11))
                    Toggle("≤20m", isOn: Binding(get: { ch.capped ?? false },
                                                 set: { on in Task { await store.setChannelCap(ch.channelId ?? "", on) } }))
                        .toggleStyle(.checkbox).font(.system(size: 11))
                        .help("Only upscale videos 20 minutes or shorter for this channel")
                    Picker("", selection: Binding(get: { ch.max_age_days ?? 0 },
                                                  set: { d in Task { await store.setChannelMaxAge(ch.channelId ?? "", d) } })) {
                        Text("Any age").tag(0); Text("≤1 week").tag(7); Text("≤1 month").tag(30)
                        Text("≤3 months").tag(90); Text("≤6 months").tag(180); Text("≤1 year").tag(365)
                    }.labelsHidden().frame(width: 100).font(.system(size: 11))
                        .help("Download then DELETE videos older than this (0 = keep any age)")
                }.disabled(paused).opacity(paused ? 0.35 : 1)
                if paused {
                    Pill(systemImage: "pause.fill", text: "paused", tint: DS.steelDim, iconOnly: true)
                } else if (ch.downloaded ?? 0) == 0 {
                    Pill(systemImage: "clock", text: "waiting for youtarr", tint: DS.steelDim, iconOnly: true)
                } else {
                    Pill(systemImage: "tray.full", text: "\(ch.pending ?? 0) to upscale",
                         tint: (ch.pending ?? 0) > 0 ? DS.steelDim : DS.steel)
                }
                Spacer()
                Text(catalog.first { $0.key == ch.preset }?.label ?? (ch.preset ?? "—"))
                    .font(.system(size: 11, weight: .medium)).foregroundStyle(DS.steel)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .opacity(paused ? 0.35 : 1)
                    .onTapGesture { if !paused { onTap() } }
                Button { confirmingRemove = true } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }.buttonStyle(.plain).help("Remove channel + delete its videos")
            }
            .padding(.vertical, 7).padding(.horizontal, 10)
            .contentShape(Rectangle())
            // Under the channel's control row; keyed by FOLDER (the channel's settings key).
            // Dimmed with the row's other controls while paused (it sits outside their Group).
            NormalizeAudioRow(key: ch.folder_name ?? "", on: ch.normalize_audio ?? true)
                .disabled(paused).opacity(paused ? 0.35 : 1)
                .padding(.horizontal, 10).padding(.bottom, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
        }
        .confirmationDialog("Remove \(ch.title ?? ch.folder_name ?? "this channel")?",
                            isPresented: $confirmingRemove, titleVisibility: .visible) {
            Button("Remove & delete \(ch.downloaded ?? 0) video\((ch.downloaded ?? 0) == 1 ? "" : "s")",
                   role: .destructive) {
                if let c = ch.channelId { Task { await store.removeChannel(c) } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Deletes its downloaded videos and any 4K masters in Plex, and lets youtarr re-download it if you add it back.")
        }
    }
}

// "Next up" — collapsed shows the next item; tap to expand the next ~10 that will actually
// process (queued movies jump ahead of episodes).
private struct UpNextView: View {
    let items: [UpNextDTO]
    var showSeries: Bool = false        // round-robin: tag each episode with which show it's from
    @EnvironmentObject var store: AppStore
    @State private var expanded = false
    @State private var confirmingVideoDelete: UpNextDTO? = nil   // a video awaiting skip/delete confirm
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button { withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() } } label: {
                HStack(spacing: 8) {
                    Text("Next up").foregroundStyle(.secondary)
                    if !expanded, let first = items.first { row(first) }
                    Spacer()
                    if items.count > 1 {
                        if !expanded {
                            Text("+\(items.count - 1)").font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                    }
                }
                .font(.system(size: 13)).contentShape(Rectangle())
            }
            .buttonStyle(.plain).disabled(items.count <= 1)
            if expanded {
                // Full list in processing order. Movies (only) get controls: ↑/↓ move them
                // anywhere — including between episodes — and × removes them.
                ForEach(Array(items.enumerated()), id: \.element.id) { idx, it in
                    HStack(spacing: 8) {
                        Text("\(idx + 1)").font(.system(size: 11)).monospacedDigit()
                            .foregroundStyle(.tertiary).frame(width: 18, alignment: .trailing)
                        row(it)
                        Spacer()
                        controls(it, idx)
                    }.font(.system(size: 13))
                }
                if items.contains(where: { $0.kind == "movie" }) {
                    Text("Drag movies anywhere with ↑/↓ — they process in the slot you place them. × removes a movie.")
                        .font(.system(size: 10)).foregroundStyle(.tertiary).padding(.top, 1)
                }
            }
        }
    }
    // Monochrome: an item's KIND is its SF symbol + chip shape, not a hue — film = movie,
    // play.rectangle = youtube (channel chip), mono ep-code chip = episode.
    @ViewBuilder func row(_ it: UpNextDTO) -> some View {
        if it.kind == "movie" {
            Image(systemName: "film").font(.system(size: 11)).foregroundStyle(DS.steel)
            Text(store.movieTitle(it.name, it.title)).fontWeight(.semibold).lineLimit(1)
        } else if it.kind == "youtube" {
            Image(systemName: "play.rectangle").font(.system(size: 11)).foregroundStyle(DS.steel)
            if let ch = it.channel, !ch.isEmpty {
                Text(ch).font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 6).padding(.vertical, 1)
                    .background(Capsule().fill(Color.white.opacity(0.08))).foregroundStyle(DS.steel)
                    .lineLimit(1).layoutPriority(-1)
            }
            Text(it.title ?? it.name ?? "").fontWeight(.semibold).lineLimit(1)
        } else {
            if showSeries, let sname = it.series, !sname.isEmpty {
                Text(store.seriesTitle(sname)).font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 6).padding(.vertical, 1)
                    .background(Capsule().fill(Color.white.opacity(0.08))).foregroundStyle(DS.steelDim)
                    .lineLimit(1).layoutPriority(-1)
            }
            Text(it.ep ?? "").font(.system(.caption, design: .monospaced).weight(.bold))
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.08))).foregroundStyle(DS.steelBright)
            Text(epTitle(it.source_name)).fontWeight(.semibold).lineLimit(1)
        }
    }
    @ViewBuilder func controls(_ it: UpNextDTO, _ idx: Int) -> some View {
        // Movies: ↑/↓ move through the WHOLE queue; × removes (non-destructive — files stay).
        // YouTube: × SKIPS & DELETES the video — staging download gone, youtarr forgets it,
        // never re-downloaded (confirmed first). Episodes are auto-generated — no controls.
        if it.kind == "movie" {
            HStack(spacing: 9) {
                iconButton("chevron.up", enabled: idx > 0,
                           help: "Move earlier") { await store.queueAction("up", it) }
                iconButton("chevron.down", enabled: idx < items.count - 1,
                           help: "Move later") { await store.queueAction("down", it) }
                iconButton("xmark.circle.fill", enabled: true,
                           help: "Remove from the queue") { await store.queueAction("remove", it) }
            }
        } else if it.kind == "youtube" {
            Button { confirmingVideoDelete = it } label: {
                Image(systemName: "xmark.circle.fill").font(.system(size: 12))
                    .frame(width: 16, height: 16, alignment: .center)
            }
            .buttonStyle(.plain).foregroundStyle(.secondary).opacity(0.9)
            .help("Skip & delete — removes the download and youtarr never re-fetches it")
            .confirmationDialog("Skip & delete \"\(it.title ?? it.name ?? "this video")\"?",
                                isPresented: Binding(get: { confirmingVideoDelete?.id == it.id },
                                                     set: { if !$0 { confirmingVideoDelete = nil } }),
                                titleVisibility: .visible) {
                Button("Skip & delete", role: .destructive) {
                    Task { await store.deleteYoutubeVideo(channel: it.channel, name: it.name ?? "") }
                    confirmingVideoDelete = nil
                }
                Button("Cancel", role: .cancel) { confirmingVideoDelete = nil }
            } message: {
                Text("Deletes the downloaded video and tells youtarr to forget it — it won't be re-downloaded or upscaled.")
            }
        }
    }
    @ViewBuilder func iconButton(_ sym: String, enabled: Bool, help: String,
                                 _ act: @escaping () async -> Void) -> some View {
        Button { Task { await act() } } label: {
            Image(systemName: sym).font(.system(size: 12)).frame(width: 16, height: 16, alignment: .center)
        }
        .buttonStyle(.plain).foregroundStyle(.secondary)
        .disabled(!enabled).opacity(enabled ? 0.9 : 0.22).help(help)
    }
}

// shared queue widgets (TV + Movie)
private struct QueueCounts: View {
    let q: QueueDTO?
    var body: some View {
        if let q {
            Pill(systemImage: "tray.full", text: "\(q.remaining_count ?? 0) to upscale", tint: DS.steelDim)
            Pill(systemImage: "checkmark", text: "\(q.done_count ?? 0) done", tint: DS.steel)
        }
    }
}

private struct QueueProgress: View {
    let q: QueueDTO
    var body: some View {
        let total = (q.done_count ?? 0) + (q.remaining_count ?? 0)
        let frac = total > 0 ? Double(q.done_count ?? 0) / Double(total) : 0
        SteelBar(completed: frac, live: frac)
    }
}

// MARK: - settings + per-show preset

struct SettingsCard: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let st = store.state?.settings
        Card(title: "Settings", systemImage: "slider.horizontal.3") {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Required adapter").font(.system(size: 13, weight: .medium))
                        Text("Below this, everything pauses and the screen sleeps.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("\(st?.min_adapter_watts ?? 140) W")
                        .font(.system(size: 13, weight: .medium)).monospacedDigit()
                        .foregroundStyle(DS.steel)
                    Stepper(value: Binding(get: { st?.min_adapter_watts ?? 140 },
                                           set: { n in Task { await store.saveSettings(["min_adapter_watts": n]) } }),
                            in: 100...500, step: 10) { EmptyView() }
                        .labelsHidden().fixedSize()
                }
                // Re-check interval: engine setting only (poll_minutes, default 30 — the idle
                // re-poll when the series is complete/unselected). Label was stale ("while
                // paused" — power pauses use their own cadence); declutter pass 2026-07-06.
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Dim screen after").font(.system(size: 13, weight: .medium))
                        Text("Idle this long → screen off. Tap the brightness key to bring it back.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text((st?.dim_after_minutes ?? 15) == 0 ? "Off" : "\(st?.dim_after_minutes ?? 15) min")
                        .font(.system(size: 13, weight: .medium)).monospacedDigit()
                        .foregroundStyle(DS.steel)
                    Stepper(value: Binding(get: { st?.dim_after_minutes ?? 15 },
                                           set: { n in Task { await store.saveSettings(["dim_after_minutes": n]) } }),
                            in: 0...240, step: 5) { EmptyView() }
                        .labelsHidden().fixedSize()
                }
                // Audio loudness target: engine setting only (audio_target_lufs, default -16) —
                // deliberately NOT surfaced in Settings (same call as the peak cap: plumbing).
                // Peak bitrate cap: engine setting only (max_peak_mbps, default 50) — deliberately
                // NOT surfaced in Settings (user 2026-07-06: plumbing, not a knob).
            }
        }
    }
}

// (The per-show preset section was removed from Settings — preset is now chosen as a step
// when selecting a series / adding a movie. See PresetChooser + TVMode/MovieMode.)

// MARK: - readiness + power

struct KV: View {
    let k: String, v: String; var color: Color? = nil; var last = false
    init(_ k: String, _ v: String, color: Color? = nil, last: Bool = false) { self.k = k; self.v = v; self.color = color; self.last = last }
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(k).font(.system(size: 13)).foregroundStyle(.secondary)
                Spacer()
                Text(v).font(.system(size: 13, weight: .medium)).foregroundStyle(color ?? .labelC)
                    .multilineTextAlignment(.trailing)
            }.padding(.vertical, 8)
            if !last { Divider() }
        }
    }
}

// MARK: - scratch + outputs

struct ScratchPowerCard: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let sc = store.state?.scratch
        let p = store.state?.power
        let powerText = (p?.external_connected ?? false) ? ((p?.adequate ?? false) ? "Adequate" : "Battery draining") : "On battery"
        Card(title: "Scratch & power", systemImage: "internaldrive") {
            HStack(spacing: 13) {
                Image(systemName: "internaldrive.fill").font(.system(size: 20)).foregroundStyle(.tint)
                    .frame(width: 42, height: 42)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.white.opacity(0.05)))
                VStack(alignment: .leading, spacing: 2) {
                    Text(sc?.name ?? "—").font(.system(size: 14, weight: .semibold))
                    Text(sc?.path ?? "—").font(.system(size: 11, design: .monospaced)).foregroundStyle(.tertiary)
                }
                Spacer()
            }
            VStack(spacing: 0) {
                KV("Connection", (sc?.connected ?? false) ? "Always mounted" : "Disconnected",
                   color: (sc?.connected ?? false) ? DS.steel : DS.steelBright)
                KV("Free space", sc?.free_gb.map { "\(Int($0)) GB free" } ?? "—")
                KV("Power", powerText,
                   color: (p?.adequate ?? false) && (p?.external_connected ?? false) ? DS.steel : DS.steelBright)
                KV("Battery", "\(p?.capacity ?? 0)%" + ((p?.charging ?? false) ? " (charging)" : ""))
                KV("Current", "\(p?.amperage_ma ?? 0) mA")
                KV("Pauses when", "adapter under \(store.state?.settings?.min_adapter_watts ?? 140) W", last: true)
            }
        }
    }
}

struct ScratchContentsCard: View {
    @EnvironmentObject var store: AppStore
    struct EpisodeGroup: Identifiable { let stem: String; let items: [ScratchItemDTO]; let total: Int; var id: String { stem } }
    var body: some View {
        let items = store.state?.scratch_contents ?? []
        let groups = Self.grouped(items)
        Card(title: "Scratch contents", systemImage: "folder",
             hint: groups.isEmpty ? "" : "\(groups.count) episode\(groups.count > 1 ? "s" : "") · \(items.count) item\(items.count > 1 ? "s" : "")",
             accessory: AnyView(revealButton)) {
            if groups.isEmpty {
                Text("Nothing in topaz-scratch right now.").font(.system(size: 12)).foregroundStyle(.tertiary)
            } else {
                // One block PER EPISODE — a labeled header + that episode's files, with clear space
                // between episodes so it's obvious which working files belong to which item.
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(groups) { g in
                        VStack(alignment: .leading, spacing: 0) {
                            HStack(spacing: 8) {
                                Text(Self.groupLabel(g.stem)).font(.system(size: 11.5, weight: .semibold))
                                    .foregroundStyle(DS.steelBright).lineLimit(1).truncationMode(.middle)
                                Spacer()
                                Text(Self.sizeLabel(g.total)).font(.system(size: 11, weight: .medium))
                                    .monospacedDigit().foregroundStyle(.tertiary)
                            }
                            .padding(.bottom, 5)
                            ForEach(Array(g.items.enumerated()), id: \.element.id) { i, it in
                                HStack(spacing: 11) {
                                    Image(systemName: Self.icon(it))
                                        .foregroundStyle((it.is_dir ?? false) ? DS.steel : .secondary).frame(width: 18)
                                    Text(Self.role(it.name ?? "")).font(.system(size: 12)).foregroundStyle(.secondary)
                                    Spacer()
                                    Text(Self.sizeLabel(it.bytes ?? 0)).font(.system(size: 13, weight: .medium))
                                        .monospacedDigit().foregroundStyle(.secondary)
                                }.padding(.vertical, 6)
                                if i < g.items.count - 1 { Divider().opacity(0.5) }
                            }
                        }
                    }
                }
            }
        }
    }
    // little header button → opens topaz-scratch in a Finder window
    private var revealButton: some View {
        Button(action: openInFinder) {
            Image(systemName: "arrow.up.forward.app").font(.system(size: 12))
        }
        .buttonStyle(.plain).foregroundStyle(.secondary)
        .help("Open topaz-scratch in Finder")
    }
    private func openInFinder() {
        let path = store.state?.scratch?.path ?? NSString(string: "~/topaz-scratch").expandingTildeInPath
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }

    // Group scratch files by the EPISODE they belong to. Every working file for an item shares a
    // stem (the source basename); the pipeline appends a stage marker (`_cfr`, `_prob4_upscaled`,
    // ` HDR10 DV upscaled`, `.remuxsegs`). Groups are ordered biggest-first (matches the flat list's
    // feel — the episode holding the most scratch leads).
    static func grouped(_ items: [ScratchItemDTO]) -> [EpisodeGroup] {
        var order: [String] = []
        var byStem: [String: [ScratchItemDTO]] = [:]
        for it in items {
            let k = stem(it.name ?? "")
            if byStem[k] == nil { order.append(k) }
            byStem[k, default: []].append(it)
        }
        return order.map { k -> EpisodeGroup in
            let its = byStem[k] ?? []
            return EpisodeGroup(stem: k, items: its, total: its.reduce(0) { $0 + ($1.bytes ?? 0) })
        }.sorted { $0.total > $1.total }
    }

    // The episode stem = everything before the first pipeline stage marker (or, for the bare source
    // file, the name minus its extension).
    static func stem(_ name: String) -> String {
        for mark in [" HDR10 DV upscaled", "_prob4_upscaled", "_cfr."] {
            if let r = name.range(of: mark) { return String(name[..<r.lowerBound]) }
        }
        if let dot = name.lastIndex(of: "."), name.distance(from: dot, to: name.endIndex) <= 6 {
            return String(name[..<dot])          // a source file `<stem>.<ext>`
        }
        return name
    }

    // A concise header: prefer the "SxxExx …" portion (drops the show-name prefix); else the stem.
    static func groupLabel(_ stem: String) -> String {
        if let r = stem.range(of: "[Ss][0-9]{1,2}[Ee][0-9]{1,3}", options: .regularExpression) {
            return String(stem[r.lowerBound...]).trimmingCharacters(in: .whitespaces)
        }
        return stem
    }

    // Which pipeline artifact a scratch file is — so a grouped row reads "CFR / Topaz segments / …"
    // instead of repeating the (already-in-the-header) episode name.
    static func role(_ name: String) -> String {
        let n = name.lowercased()
        if n.hasSuffix(".remuxsegs") { return "Remux segments" }
        if n.contains("_prob4_upscaled.segments") { return "Topaz segments" }
        if n.contains("_prob4_upscaled") { return "Topaz ProRes" }
        if n.contains(" hdr10 dv upscaled") { return n.hasSuffix(".mov") ? "DV render" : "Master" }
        if n.contains("_cfr.") { return "CFR source" }
        return "Source"
    }
    static func icon(_ it: ScratchItemDTO) -> String {
        if it.is_dir ?? false { return "folder.fill" }
        let n = (it.name ?? "").lowercased()
        return (n.hasSuffix(".mp4") || n.hasSuffix(".mov") || n.hasSuffix(".mkv") || n.hasSuffix(".m4v"))
            ? "film" : "doc"
    }
    static func sizeLabel(_ bytes: Int) -> String {
        let b = Double(bytes)
        if b >= 1e9 { return String(format: "%.2f GB", b / 1e9) }
        if b >= 1e6 { return String(format: "%.0f MB", b / 1e6) }
        if b >= 1e3 { return String(format: "%.0f KB", b / 1e3) }
        return "\(bytes) B"
    }
}

// MARK: - grants + footer

struct GrantsCard: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let t = store.selftest
        let grantsOK = (t?.screen_recording ?? false) && (t?.accessibility ?? false)
        // HARD requirements (exact Resolve/Topaz builds + the 16" MBP built-in display —
        // engine/versions.py). hard_ok false → the server refuses to arm; explain why here.
        if let t, t.hard_ok == false {
            Card(title: "Unsupported setup", systemImage: "xmark.octagon") {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(DS.steelBright)
                        Text("Visionary requires DaVinci Resolve Studio 18.6.0, Topaz Video AI 7.0.1, and the 16-inch MacBook Pro built-in display (3456×2234). It will not arm until they match.")
                            .font(.system(size: 13, weight: .medium))
                    }
                    ForEach((t.found ?? [:]).sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                        Text("\(k): \(v)").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
        }
        if t != nil && !grantsOK {
            Card(title: "Permissions", systemImage: "lock.shield") {
                HStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(DS.steelBright)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("The Resolve stage needs Screen Recording + Accessibility").font(.system(size: 13, weight: .medium))
                        Text("Screen Recording: \(yn(t?.screen_recording))   ·   Accessibility: \(yn(t?.accessibility))")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Request Accessibility") { Task { await store.requestAccessibility() } }
                        .buttonStyle(SteelButtonStyle(lit: false))
                }
            }
        }
    }
    func yn(_ b: Bool?) -> String { (b ?? false) ? "granted" : "not granted" }
}

struct FooterBar: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        HStack {
            Spacer()
            Text("updated \(store.state?.generated_at ?? "—")")
                .font(.system(size: 11, design: .monospaced)).foregroundStyle(.tertiary)
        }
    }
}

// MARK: - root

// Tracks the ScrollView's content offset (0 at the top, negative as you scroll down).
private struct ScrollYKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// The THEATRE stage: an OLED near-black room, cool graphite at the top (the icon's tile
/// receding into darkness) with a warm ember glow rising from the floor — footlights, a
/// campfire story. The light RESPONDS to scroll: scrolling down lifts the light source,
/// scrolling back up lets it settle (subtle parallax — the room feels physical).
/// GPU: the glow layers are flattened once into a Metal texture (`drawingGroup`) and the
/// scroll motion is a pure `.offset` transform — composited on the GPU, never re-rendered.
private struct TheatreStage: View {
    var scrollY: CGFloat            // ScrollView content minY (0 at top, negative scrolled down)
    var body: some View {
        // scroll DOWN (minY negative) → the light LIFTS toward you and BRIGHTENS a touch
        // (walking closer to the fire); scroll back up → it settles and dims to its resting
        // glow. The dashboard's scroll range is short (~200-300pt), so the response is sized
        // to be clearly felt across that little travel while staying conforming.
        let lift = min(40, max(-180, scrollY * 0.35))            // ≤180pt rise (+ a hint of dip on bounce)
        let rise = max(0, min(0.28, -scrollY / 600))             // up to +28% brightness while scrolled
        ZStack {
            LinearGradient(stops: [.init(color: DS.bgTop, location: 0),      // cool, dark ceiling
                                   .init(color: DS.bgBase, location: 0.45),  // OLED black mid
                                   .init(color: DS.bgBottom, location: 1)],  // warm black floor
                           startPoint: .top, endPoint: .bottom)
            // The warm light source — two soft radial layers anchored just below the floor.
            // Rendered at full strength, rasterized ONCE (Metal), then driven entirely by
            // composited alpha + translation — the GPU-cheap path.
            ZStack {
                RadialGradient(colors: [DS.ember.opacity(0.20), DS.ember.opacity(0.06), .clear],
                               center: .init(x: 0.5, y: 1.18), startRadius: 60, endRadius: 700)
                RadialGradient(colors: [DS.emberDeep.opacity(0.15), .clear],
                               center: .init(x: 0.5, y: 1.28), startRadius: 0, endRadius: 340)
            }
            .drawingGroup()                          // rasterize once (Metal) …
            .opacity(0.72 + rise)                    // … resting glow ≈ today's look, brightens on scroll
            .offset(y: lift)                         // … and moves as a pure GPU transform
            // A faint cool sheen up top — the icon's silver glass answering the warm floor.
            RadialGradient(colors: [Color.white.opacity(0.025), .clear],
                           center: .init(x: 0.5, y: -0.1), startRadius: 0, endRadius: 520)
        }
        .ignoresSafeArea()
    }
}

struct RootView: View {
    @EnvironmentObject var store: AppStore
    @State private var scrollY: CGFloat = 0
    var body: some View {
        VStack(spacing: 0) {
            HeaderBar()
            ScrollView {
                VStack(spacing: 16) {
                    GrantsCard()
                    PipelineCard()
                    SeriesCard()
                    SettingsCard()
                    HStack(alignment: .top, spacing: 16) { ScratchPowerCard(); ScratchContentsCard() }
                    FooterBar()
                }
                .padding(20)
                .frame(maxWidth: 1080)
                .frame(maxWidth: .infinity)
                .background(GeometryReader { g in     // publish the scroll offset (drives the light)
                    Color.clear.preference(key: ScrollYKey.self,
                                           value: g.frame(in: .named("stage")).minY)
                })
            }
            .coordinateSpace(name: "stage")
        }
        .onPreferenceChange(ScrollYKey.self) { scrollY = $0 }
        .frame(width: 1080, height: 620)     // fixed: fold lands at the Current-series card's end
                                             // with the pipeline stage expanded (settings live below the fold)
        .background(TheatreStage(scrollY: scrollY))
        .tint(Color.brand)   // steel-blue accent app-wide, echoing the Visionary icon
    }
}

// MARK: - format helpers

func pretty(_ s: String) -> String { s.count > 54 ? String(s.prefix(52)) + "…" : s }
func epTitle(_ name: String?) -> String {
    guard let name else { return "" }
    if let r = name.range(of: #"[sS]\d+[eE]\d+\s+(.+?)\s*\("#, options: .regularExpression) {
        let m = String(name[r])
        if let t = m.range(of: #"\d\s+"#, options: .regularExpression) {
            return String(m[t.upperBound...]).trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "(", with: "")
        }
    }
    return ""
}
