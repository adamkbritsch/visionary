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
    /// How far a status DOT sits back from the text it accompanies. The text already carries the
    /// state colour, so the dot is a secondary mark — one value, shared by the header puck and the
    /// pipeline timeline, so the two can't drift apart.
    static let dotQuiet    = 0.55

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
    // PEAK REPAIR: the span of the segment being re-encoded is CUT OUT of the bright fill
    // (its bytes are being replaced — it is no longer done) and refills left-to-right with
    // the repair encode's real frame progress. nil span = no repair running.
    var repairLo: Double? = nil
    var repairHi: Double? = nil
    var repairFrac: Double = 0
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
                if let lo = repairLo, let hi = repairHi, hi > lo {
                    let span = geo.size.width * (hi - lo)
                    Rectangle().fill(Color.black.opacity(0.6))            // the cut-out: back to track
                        .frame(width: span)
                        .offset(x: geo.size.width * lo)
                    Rectangle().fill(DS.steel.opacity(0.55))              // ...refilling with REAL frames
                        .frame(width: max(2, span * min(max(repairFrac, 0), 1)))
                        .offset(x: geo.size.width * lo)
                        .animation(.linear(duration: 0.4), value: repairFrac)
                }
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
    var color: Color = DS.steelBright.opacity(DS.dotQuiet)   // matches the header puck's dot
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
    // popover binding lives in the STORE so the "Finish setup" card can open it
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
            PowerPill()
            Button(action: { store.showSettings.toggle() }) {
                // A bare glyph, no plate: the gear is a way IN, never the action on this bar —
                // giving it a button chrome made it compete with Activate. Screen Control lives
                // inside the popup now, so the gear carries its one at-a-glance signal: it goes
                // red while the pipeline is holding off the screen, since that state pauses
                // Resolve and is easy to forget about.
                Image(systemName: "gearshape.fill")
                    .font(.system(size: 15, weight: .regular))
                    .foregroundStyle(store.quietMode ? DS.quietRedLight : DS.steel)
                    .contentShape(Rectangle())           // keep the whole glyph box clickable
            }
            .buttonStyle(.plain)
            .help("Settings")
            .popover(isPresented: $store.showSettings, arrowEdge: .bottom) {
                SettingsPopover().environmentObject(store)
            }
            Button(action: { store.showHistory.toggle() }) {
                // FINISHED: the look-back. Same bare-glyph treatment as the gear — a way in,
                // never an action competing with Activate on this bar.
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 15, weight: .regular))
                    .foregroundStyle(DS.steel)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Finished — everything upscaled, and fix a file's audio")
            .popover(isPresented: $store.showHistory, arrowEdge: .bottom) {
                HistoryPopover().environmentObject(store)
                    .task { await store.fetchHistory() }
            }
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
            .accessibilityIdentifier("activate")
        }
        // Top tighter than bottom (2 vs 13): the traffic lights overlay this bar
        // (.fullSizeContentView), and the symmetric padding left them floating in a
        // visible band of dead space above the elements — pulling the content up puts
        // it on the lights' visual line. The bottom keeps its full breathing room
        // against the pipeline card.
        .padding(.leading, 84).padding(.trailing, 20)
        .padding(.top, 2).padding(.bottom, 13)
        .frame(maxWidth: .infinity)
        .background(LinearGradient(colors: [DS.bgTop, DS.bgBase],     // graphite glass bar
                                   startPoint: .top, endPoint: .bottom))
        .overlay(alignment: .bottom) { Color.white.opacity(0.06).frame(height: 1) }
        .shadow(color: .black.opacity(0.20), radius: 4, y: 1)
    }
}


/// The header's ONE readout: power, battery, and how far along the item nearest to shipping is.
///
/// "Nearest to shipping" = the highest-index LIVE stage in PIPELINE (download → topaz → resolve
/// → remux → upload). Two stages run at once — the run thread owns download/topaz/resolve, the
/// finisher owns remux/upload/cleanup — so with Topaz at 41% and Remux at 17% this shows the
/// REMUX's 17%: that item is the one about to leave the machine.
struct PowerPill: View {
    @EnvironmentObject var store: AppStore

    // MEASURED, not eyeballed — NSFont.systemFont(12, .medium) with monospaced digits:
    //   "9%" 19.3   "80%" 27.1   "100%" 34.8
    // The old 32 was sized so "9%→10%" wouldn't shift and never accounted for a THIRD digit,
    // so a full battery truncated to "10…" — and idle is exactly when it tops off, which is
    // why it showed up with the pipeline deactivated. Clears the worst case with a little air.
    private static let pctSlot: CGFloat = 36

    /// (pipeline index, SF Symbol, percent) — EXACTLY the Dock bar's number in percentage
    /// form (user-dictated: the header readout IS the bar under the app icon). One shared
    /// computation, PipelineCard.unifiedProgress, so the two can never disagree.
    private var leadStage: (Int, String, Int)? {
        guard let up = PipelineCard.unifiedProgress(store.state) else { return nil }
        // `cleanup` has no PIPELINE card; it is past upload, so it borrows the upload glyph.
        let idx = PIPELINE.firstIndex { $0.key == up.stage } ?? (up.stage == "cleanup" ? PIPELINE.count : -1)
        guard idx >= 0 else { return nil }
        let sym = PIPELINE.indices.contains(idx) ? PIPELINE[idx].symbol : PIPELINE.last!.symbol
        return (idx, sym, Int(up.pct))
    }

    /// A battery glyph that actually reports the level, rather than a decorative one.
    private func batterySymbol(_ pct: Int?, charging: Bool) -> String {
        if charging { return "battery.100.bolt" }
        switch pct ?? 100 {
        case ..<13:  return "battery.0"
        case ..<38:  return "battery.25"
        case ..<63:  return "battery.50"
        case ..<88:  return "battery.75"
        default:     return "battery.100"
        }
    }

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
        let lead = leadStage
        // BARE TEXT — no capsule, no rim. This is a readout, not a control, and a plate made it
        // read as a button next to Activate. Attention is brightness, never hue.
        //
        // Grouping is done with SPACING, not separators: an icon sits tight against the number
        // it belongs to, and the groups are held apart by a wide gap. The gap does the work a
        // "·" would, without adding marks to a bar that is already dense.
        //
        // The slots stay fixed-width so a changing digit never shifts anything downstream, but
        // they are aligned LEADING. Trailing alignment put the slack INSIDE the group — a short
        // value like "92%" was pushed to the right edge of its box, stranding it several points
        // from its own icon and breaking the very pairing the spacing is there to create. Now
        // the slack falls on the outside, where it just reads as more space between groups.
        HStack(spacing: 13) {
            Text(label)
            if let cap {
                HStack(spacing: 3) {
                    Image(systemName: batterySymbol(cap, charging: p?.charging ?? false))
                        .font(.system(size: 11))
                    Text("\(cap)%").monospacedDigit()
                        // Fixed slot so 9%→10%→100% never shifts anything downstream; LEADING
                        // so the number stays welded to its icon whatever its width.
                        .frame(width: Self.pctSlot, alignment: .leading)
                }
            }
            if let lead {
                HStack(spacing: 3) {
                    Image(systemName: lead.1).font(.system(size: 11))
                    // The Dock bar's number, printed. With two remux lanes up that is the
                    // first lane in display order — the earliest episode, the row the card
                    // draws on top; the second lane still shows in the card.
                    Text("\(lead.2)%")
                        .monospacedDigit()
                        .frame(width: Self.pctSlot, alignment: .leading)
                }
            }
        }
        .font(.system(size: 12, weight: .medium))
        .foregroundStyle(ok ? DS.steel : DS.steelBright)
        .lineLimit(1)
        .help({
            var t = ac ? "Adapter wattage and battery level"
                       : "Running on battery — the pipeline pauses until the adapter is back"
            if let lead, PIPELINE.indices.contains(lead.0) {
                t += "\n\(PIPELINE[lead.0].name) \(lead.2)% — the same bar the Dock icon shows"
            }
            return t
        }())
    }
}

/// WHICH SCREEN RESOLVE RUNS ON.
///
/// Sits with Screen Control because it is the same subject: what the pipeline does to the
/// machine you are sitting at. Off by default — Resolve opens wherever it opens (the main
/// display), exactly as it always has.
///
/// A display has to be PROVEN before it can be chosen. Driving a screen nobody is looking
/// at turns a bad template match from a loud failure into silent wrong clicks, so "Test"
/// scores every template against that screen and the result is remembered.
///
/// Honest about what this does NOT fix, in the copy as well as here: the mouse pointer is
/// one object shared by every display. Hosting Resolve elsewhere stops it covering your
/// work; it cannot give the pipeline a cursor of its own.
struct ResolveHostSection: View {
    @EnvironmentObject var store: AppStore

    private var d: DisplaysDTO? { store.displays }
    private var hostable: [DisplayDTO] { (d?.displays ?? []).filter { !($0.main ?? false) } }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle(isOn: Binding(get: { d?.enabled ?? false },
                                 set: { v in Task { await store.setDisplayPinning(v) } })) {
                Text("Run Resolve on another screen").font(.system(size: 12))
            }
            .help("Off: Resolve opens on the main display, as it always has. On: it is moved "
                  + "to the highest-priority screen below that is eligible and proven.")

            if hostable.isEmpty {
                Text("No second screen attached.")
                    .font(.system(size: 11)).foregroundStyle(.tertiary)
            } else {
                ForEach(hostable) { disp in row(disp) }
                if (d?.enabled ?? false) {
                    Text(hostLine).font(.system(size: 11)).foregroundStyle(.tertiary)
                }
            }

            Divider().padding(.vertical, 2)
            warningRow
        }
        .task { await store.fetchDisplays() }
    }

    private var hostLine: String {
        if let h = d?.host, let n = h.name { return "Resolve will run on \(n)." }
        return "Resolve will run on the main display — \(d?.host_reason ?? "no screen chosen")."
    }

    @ViewBuilder private func row(_ disp: DisplayDTO) -> some View {
        let key = disp.key ?? ""
        let chosen = (d?.priority ?? []).contains(key)
        HStack(spacing: 8) {
            Image(systemName: "display").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            VStack(alignment: .leading, spacing: 1) {
                Text(disp.name ?? key).font(.system(size: 12)).foregroundStyle(DS.steel)
                Text(subtitle(disp)).font(.system(size: 10)).foregroundStyle(.tertiary)
            }
            Spacer()
            if disp.eligible == true {
                if store.smokeRunning == key {
                    ProgressView().controlSize(.small)
                } else if disp.smoke_pass == true {
                    Button(chosen ? "Chosen" : "Choose") {
                        Task { await store.setDisplayPriority(chosen ? [] : [key]) }
                    }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium))
                    .foregroundStyle(chosen ? DS.steel : Color.brand)
                } else {
                    Button("Test") { Task { await store.runDisplaySmoke(key) } }
                        .buttonStyle(.plain).font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.brand)
                        .help("Open Resolve full-screen on that display first, then Test — it "
                              + "scores every template against it.")
                }
            }
        }
    }

    private func subtitle(_ disp: DisplayDTO) -> String {
        if disp.eligible != true { return disp.why_not ?? "can't host Resolve" }
        if disp.smoke_pass == true {
            let best = disp.smoke_best.map { String(format: "%.2f", $0) } ?? "-"
            return "proven (best template \(best))"
        }
        return "not tested yet — Resolve's templates haven't been checked on this screen"
    }

    @ViewBuilder private var warningRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(isOn: Binding(get: { d?.warn_takeover ?? true },
                                 set: { v in Task { await store.setTakeoverWarning(v) } })) {
                Text("Warn before taking the screen").font(.system(size: 12))
            }
            Text("A notice appears here just before the pipeline takes the screen and mouse "
                 + "— roughly ten seconds of Resolve setup still runs after it, so it costs "
                 + "no time. Not a countdown: the exact moment isn't predictable.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)
            Text("The mouse pointer is shared by every screen — moving Resolve stops it "
                 + "covering your work, but the pipeline still borrows the pointer to click.")
                .font(.system(size: 10)).foregroundStyle(.tertiary)
        }
    }
}

/// SCREEN CONTROL, now a Settings row rather than a header button.
///
/// It can only be switched off for a WHILE — a duration or a clock time, never indefinitely.
/// Holding items before Resolve buffers their ~190 GiB Topaz intermediates against the disk
/// floor, so a forgotten "off" doesn't keep the Mac free, it quietly stalls the run. The
/// engine owns the deadline (`quiet_until`), so the pause still lifts if the app is closed.
struct ScreenControlSection: View {
    @EnvironmentObject var store: AppStore
    @State private var untilTime = Date().addingTimeInterval(3600)
    @State private var now = Date()

    // Mirrors settings.MAX_QUIET_SECONDS — the UI can't offer a pause the engine would clamp.
    private static let maxSeconds = 4 * 3600
    private static let presets: [(String, Int)] = [
        ("30m", 1800), ("1h", 3600), ("2h", 7200), ("4h", 14400),
    ]

    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    private func remaining(_ until: Date) -> String {
        let s = max(0, Int(until.timeIntervalSince(now)))
        return s >= 3600 ? "\(s / 3600)h \((s % 3600) / 60)m" : "\(max(1, s / 60))m"
    }

    private static let clock: DateFormatter = {
        let f = DateFormatter(); f.timeStyle = .short; f.dateStyle = .none; return f
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Screen control").font(.system(size: 13, weight: .medium))
                    Text(store.quietMode
                         ? "Paused — the pipeline is leaving the screen alone."
                         : "The pipeline may take the screen to run Dolby Vision.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 10)
                Text(store.quietMode ? "Paused" : "On")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(store.quietMode ? DS.quietRedLight : DS.steel)
            }

            if let until = store.quietUntil {
                // PAUSED: say exactly when it comes back, and offer the way out.
                HStack(spacing: 8) {
                    Text("Back on at \(Self.clock.string(from: until)) · \(remaining(until)) left")
                        .font(.system(size: 11)).foregroundStyle(DS.steel).monospacedDigit()
                    Spacer()
                    Button("Resume now") { Task { await store.resumeScreenControl() } }
                        .buttonStyle(.plain)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(DS.steelBright)
                        .padding(.horizontal, 9).padding(.vertical, 4)
                        .background(Capsule().fill(Color.white.opacity(0.07)))
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.8))
                }
            } else {
                // ON: the only way to switch it off is to say for how long.
                HStack(spacing: 6) {
                    Text("Pause for").font(.system(size: 11)).foregroundStyle(.secondary)
                    ForEach(Self.presets, id: \.0) { label, secs in
                        Button(label) { Task { await store.pauseScreenControl(seconds: secs) } }
                            .buttonStyle(.plain)
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(DS.steelBright)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Capsule().fill(Color.white.opacity(0.07)))
                            .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.8))
                    }
                }
                HStack(spacing: 6) {
                    Text("or until").font(.system(size: 11)).foregroundStyle(.secondary)
                    DatePicker("", selection: $untilTime, displayedComponents: .hourAndMinute)
                        .labelsHidden().datePickerStyle(.field).fixedSize()
                    // Show what the picked time actually BUYS, so the 4 h clamp is visible
                    // rather than silent — picking a time already gone today would otherwise
                    // look like it did nothing in particular.
                    Text(durationLabel(secondsUntilChosenTime()))
                        .font(.system(size: 11)).monospacedDigit()
                        .foregroundStyle(secondsUntilChosenTime() >= Self.maxSeconds
                                         ? DS.quietRedLight : DS.steel)
                    Button("Set") {
                        Task { await store.pauseScreenControl(seconds: secondsUntilChosenTime()) }
                    }
                    .buttonStyle(.plain)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(DS.steelBright)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.8))
                    Spacer()
                }
                Text("Longest pause is 4 hours — beyond that, held items fill the disk and the run stalls.")
                    .font(.system(size: 10)).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onReceive(tick) { now = $0 }
    }

    /// "→ 1h 30m", or "→ 4h (max)" when the pick was long enough to be clamped.
    private func durationLabel(_ secs: Int) -> String {
        let h = secs / 3600, m = (secs % 3600) / 60
        let body = h > 0 ? (m > 0 ? "\(h)h \(m)m" : "\(h)h") : "\(m)m"
        return secs >= Self.maxSeconds ? "→ \(body) (max)" : "→ \(body)"
    }

    /// The picker gives an hour+minute; resolve it to the NEXT time that clock reads (so
    /// 1 AM chosen at 11 PM means tonight's 1 AM, not this morning's) and cap at 4 h out.
    private func secondsUntilChosenTime() -> Int {
        let cal = Calendar.current
        let c = cal.dateComponents([.hour, .minute], from: untilTime)
        let n = Date()
        var target = cal.date(bySettingHour: c.hour ?? 0, minute: c.minute ?? 0, second: 0, of: n) ?? n
        if target <= n { target = cal.date(byAdding: .day, value: 1, to: target) ?? target }
        return min(Int(target.timeIntervalSince(n)), Self.maxSeconds)
    }
}

// MARK: - pipeline

// Monochrome steel: stages carry no per-stage hue — they differ by symbol, name, and
// number; the ACTIVE stage is the lit one (silver badge well + bevel border + pulse).
struct StageInfo { let key, name, symbol, desc, how: String }

// The Extend step is NOT in the static table: it joins the card dynamically (see
// PipelineCard) only when it is real for the current item — hide-inert-UI.
let EXTEND_STAGE = StageInfo(
    key: "extend", name: "Extend", symbol: "arrow.left.and.right.square",
    desc: "AI-outpaint the 4:3 borders to 16:9 before the upscale — only the side strips "
        + "are generated; the original picture ships untouched.",
    how: "WAN VACE \u{00B7} chunked")

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
        // A lane counts as LIVE only when it is actually working. `finishing` is set the
        // moment the finisher CLAIMS an item — before any encoding — and it keeps its
        // last-known percentage while suspended for Resolve. Keying the card on its mere
        // presence opened Remux the instant you hit Activate, looking exactly like work in
        // progress, and left it open beside an active Resolve that had just frozen it.
        // EVERY live lane's stage, not just the first one's. The two lanes are independent and
        // routinely sit in DIFFERENT stages — lane 1 uploading while lane 2 is still remuxing.
        // Collapsing them to one stage put both lanes' rows inside whichever card happened to
        // win, so a still-remuxing lane 2 was drawn under UPLOAD, segment counter and all,
        // reading as if two episodes were being uploaded at once. Nothing was: the engine had
        // them on separate stages and _upload_lock serializes NAS pushes anyway.
        let finStages: Set<String> = Set([o?.finishing, o?.finishing2]
            .compactMap { PipelineCard.laneLive($0) ? $0?.stage : nil })
        let activeCount = (runStage != nil ? 1 : 0) + finStages.subtracting([runStage ?? ""]).count
        let twoUp = activeCount >= 2
        // The current-episode name MOVES into each active card's top-right (below). The header
        // hint is only the idle next-up preview now — nil while anything is processing.
        let headerHint: String? = (runStage != nil || !finStages.isEmpty) ? nil : nowProcessing
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
            // HIDE-INERT-UI: the Extend step only joins the card when it is real for the
            // CURRENT item — the run is in it, or the running episode's show opted in
            // (and probed 4:3). Every other item keeps the classic five steps.
            let showExtend: Bool = {
                if runStage == "extend" { return true }
                guard let c = cur, c.kind == "episode",
                      let s = store.state?.series?.shows?.first(where: { $0.name == c.series })
                else { return false }
                return (s.extend_borders ?? false) && s.aspect == "4:3"
            }()
            let stages: [StageInfo] = {
                guard showExtend else { return PIPELINE }
                var a = PIPELINE
                a.insert(EXTEND_STAGE, at: 1)
                return a
            }()
            HStack(alignment: .top, spacing: 6) {
                ForEach(Array(stages.enumerated()), id: \.offset) { i, st in
                    let role: StageRole = (st.key == runStage) ? .run
                        : finStages.contains(st.key) ? .finisher : .inactive
                    StageView(index: i + 1, info: st, role: role, twoUp: twoUp,
                              episode: episodeLabel(role, stageKey: st.key))
                    if i < stages.count - 1 {
                        Image(systemName: "chevron.right").font(.system(size: 11)).foregroundStyle(.tertiary)
                            .padding(.top, 21)
                    }
                }
            }
        }
    }

    /// Is this lane doing work right now? Not merely claimed, and not frozen.
    ///  * `holding` set  -> suspended for Resolve; Resolve is the live stage, not this.
    ///  * `pct` nil      -> claimed but nothing encoded yet (the state at Activate).
    static func laneLive(_ f: FinishingDTO?) -> Bool {
        guard let f, f.stage?.isEmpty == false else { return false }
        if f.holding != nil { return false }
        // upload/cleanup report no percentage but ARE running; only remux has one to wait
        // for — EXCEPT a fast-path remux (RPU inject / ship-the-render): those stream-copy
        // and never publish a pct, yet run for many minutes on a movie-sized file. Gating
        // them on pct made the movie's remux invisible while its files plainly grew
        // (user-caught 2026-08-06). Like upload, claimed = running for those.
        if f.stage == "remux" && f.fast == true { return true }
        return f.stage != "remux" || f.pct != nil
    }

    /// THE Dock bar's number + which pipeline step it belongs to. The header readout is
    /// DEFINED as this bar in percentage form, with the step's icon (user-dictated
    /// 2026-08-06) — one computation, so the two can never disagree. ONE pick among
    /// everything actively carrying a number: the EARLIEST episode processing anywhere
    /// (user-dictated) — whichever step it is on, finisher lane or run thread, a lane
    /// suspended for Resolve included (its frozen %, exactly what the bar shows).
    /// Non-episodes (movies, YouTube — no SxxEyy ordinal) sort after episodes; among
    /// themselves, finisher-before-run keeps the old precedence. nil = idle → plain
    /// icon, no header number.
    static func unifiedProgress(_ s: StateDTO?) -> (stage: String, pct: Double)? {
        let o = s?.orchestrator
        var cands: [(ord: (Int, Int)?, stage: String, pct: Double)] = []
        for f in lanesInDisplayOrder(o) {
            if let st = f.stage, !st.isEmpty, let p = f.pct {
                cands.append((f.episodeOrdinal, st, p))
            }
        }
        if o?.stage_active ?? (o?.progress != nil),
           let pr = o?.progress, let st = pr.stage ?? o?.stage, let p = pr.pct {
            cands.append((EpisodeOrdinal.parse(pr.ep ?? o?.current?.ep), st, Double(p)))
        }
        let best = cands.enumerated().min { a, b in
            switch (a.element.ord, b.element.ord) {
            case let (x?, y?): return x == y ? a.offset < b.offset : x < y
            case (_?, nil):    return true
            case (nil, _?):    return false
            case (nil, nil):   return a.offset < b.offset
            }
        }
        return best.map { ($0.element.stage, $0.element.pct) }
    }

    /// Both finisher lanes in DISPLAY order: the earliest episode first (user-dictated —
    /// with two remuxes up, the top row is always the earlier episode), engine order
    /// (lane 1 first) for ties and for items with no SxxEyy ordinal (movies, YouTube;
    /// ordinals outrank titles). Explicitly stable — sorted by (ordinal, lane index) —
    /// because Swift's sort() isn't, and rows must never swap arbitrarily mid-run.
    /// EVERY place that reads both lanes goes through this, so the card rows and the
    /// header's dual percentage can never disagree on which lane is which.
    static func lanesInDisplayOrder(_ o: OrchestratorDTO?) -> [FinishingDTO] {
        let lanes = [o?.finishing, o?.finishing2].compactMap { $0 }
        return lanes.enumerated().sorted { a, b in
            switch (a.element.episodeOrdinal, b.element.episodeOrdinal) {
            case let (x?, y?): return x == y ? a.offset < b.offset
                                             : (x.season, x.episode) < (y.season, y.episode)
            case (_?, nil):    return true
            case (nil, _?):    return false
            case (nil, nil):   return a.offset < b.offset
            }
        }.map(\.element)
    }

    // The concise episode token shown in an active card's top-right.
    func episodeLabel(_ role: StageRole, stageKey: String) -> String? {
        let o = store.state?.orchestrator
        switch role {
        case .finisher:
            // Only the lanes in THIS stage. "x2" is a lie unless both are actually here.
            let mine = PipelineCard.lanesInDisplayOrder(o).filter {
                $0.stage == stageKey && PipelineCard.laneLive($0)
            }
            if mine.count > 1 { return "\u{00D7}2" }
            guard let f = mine.first else { return nil }
            // TV lanes carry the show beside the episode token (user-asked); movie/
            // YouTube lanes' ep IS already a title — never double it.
            if f.movie != true, f.youtube != true, let s = f.series, !s.isEmpty {
                let parts = [f.ep ?? "", store.seriesTitle(s)].filter { !$0.isEmpty }
                if !parts.isEmpty { return parts.joined(separator: " \u{00B7} ") }
            }
            return f.ep                                               // already a display string
        case .run:      return runEpisodeShort(o?.current)
        case .inactive: return nil
        }
    }
    func runEpisodeShort(_ it: UpNextDTO?) -> String? {
        guard let it else { return nil }
        switch it.kind {
        case "movie":   return store.movieTitle(it.name, it.title)
        case "youtube": return (it.title?.isEmpty == false) ? it.title : epTitle(it.name)
        default:
            // "S06E07 · Show" — ep first so truncation clips the show's tail, never the
            // token (the label renders lineLimit(1) in the card's top-right).
            let ep = it.ep ?? epTitle(it.source_name) ?? ""
            let show = store.seriesTitle(it.series ?? "")
            let parts = [ep, show].filter { !$0.isEmpty }
            return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
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
                        // Resolve is the one stage whose work is INVISIBLE from here — it is
                        // driving a real UI, possibly on a screen you are not looking at. A
                        // live frame of that screen is the only honest progress indicator it
                        // has, and it is how you see a wrong-screen or stuck-dialog failure
                        // without going to look.
                        // The preview ENDS once the render starts (user-dictated):
                        // analysis is over, and each frame is a 4K screencapture
                        // taken while the machine renders.
                        if info.key == "resolve"
                            && store.state?.orchestrator?.progress?.rendering != true {
                            ResolvePreview()
                        }
                        if role == .finisher { FinisherProgress(stageKey: info.key) }
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

/// A live frame of the screen the Resolve stage is driving, inside its pipeline card.
///
/// Polled rather than streamed: each frame is a real `screencapture` of a 3840x2160 panel,
/// so the cadence is deliberately slow — this is a "what is it doing" window, not a video
/// feed, and the machine is busy rendering. The server downscales and JPEG-encodes so the
/// loopback isn't carrying full frames.
struct ResolvePreview: View {
    @EnvironmentObject var store: AppStore
    @State private var frame: NSImage?
    @State private var failed = false


    var body: some View {
        Group {
            if let img = frame {
                Image(nsImage: img)
                    .resizable().aspectRatio(contentMode: .fit)
                    .frame(maxWidth: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(Color.white.opacity(0.10), lineWidth: 1))
                    // Click-to-expand affordance: quiet corner glyph, always there — a
                    // hover-only hint is invisible on a machine nobody is sitting at.
                    .overlay(alignment: .bottomTrailing) {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                            .font(.system(size: 9, weight: .semibold))
                            .padding(4)
                            .background(Circle().fill(Color.black.opacity(0.45)))
                            .foregroundStyle(.secondary)
                            .padding(5)
                    }
                    .contentShape(Rectangle())
                    .onTapGesture {
                        withAnimation(.easeInOut(duration: 0.22)) {
                            store.resolvePreviewExpanded = true
                        }
                    }
                    .help("Live view of the screen Resolve is running on — click to expand")
            } else if !failed {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.white.opacity(0.05))
                    .frame(height: 96)
                    .overlay(ProgressView().controlSize(.small))
            }
        }
        // THE FREEZE. This used to drive itself off a stored
        // `Timer.publish(...).autoconnect()`. A stored property is rebuilt every time the
        // view is re-initialised, and the pipeline card re-renders on every 1.5 s state
        // poll — so the 2 s timer was reset before it could ever fire, `load()` ran exactly
        // once from `.task`, and the preview sat on that first frame forever. It showed the
        // desktop because that is what the screen looked like before Resolve had opened.
        //
        // A `.task` loop is tied to the view's IDENTITY, not to its re-initialisation, so it
        // survives the re-renders and keeps running.
        .task {
            while !Task.isCancelled {
                // While the full-width overlay is up, ITS loop owns the polling — two
                // pollers would double the 4K screencapture load for the same pixels.
                if !store.resolvePreviewExpanded {
                    await load()
                }
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
        }
    }

    private func load() async {
        // Sequential by construction now — the loop awaits each load before sleeping — so no
        // in-flight guard is needed and requests cannot pile up.
        guard let url = URL(string: "http://127.0.0.1:8765/api/resolve-preview.jpg") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            // 204 = the server has no frame yet; keep whatever is on screen and keep asking.
            guard code == 200, !data.isEmpty, let img = NSImage(data: data) else { return }
            frame = img
            failed = false
        } catch {
            if frame == nil { failed = true }    // only give up the placeholder if never loaded
        }
    }
}

/// The Resolve preview blown up to the FULL window width (click the card's preview to
/// open, click anywhere to close). Same polled source as the card — its loop pauses
/// while this one runs, so the server still captures the 4K panel once per tick.
/// Auto-collapses when the Resolve stage stops being the active run stage: the frames
/// go stale the moment the pass ends, and a dead fullscreen frame reads as a hang.
struct ExpandedResolvePreview: View {
    @EnvironmentObject var store: AppStore
    @State private var frame: NSImage?

    private var resolveActive: Bool {
        let o = store.state?.orchestrator
        return (o?.stage_active ?? false) && ((o?.progress?.stage ?? o?.stage) == "resolve")
            && o?.progress?.rendering != true      // the render phase closes the preview too
    }

    var body: some View {
        ZStack {
            Color.black.opacity(0.72)          // scrim: the app recedes, the screen is the subject
            VStack(spacing: 10) {
                if let img = frame {
                    Image(nsImage: img)
                        .resizable().aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity)          // the full window width
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.14), lineWidth: 1))
                } else {
                    ProgressView().controlSize(.large)
                }
                Text("Live view of the screen Resolve is driving — click anywhere to close")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.22)) { store.resolvePreviewExpanded = false }
        }
        .task {
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
        }
        .onChange(of: resolveActive) { _, active in
            if !active {
                withAnimation(.easeInOut(duration: 0.22)) { store.resolvePreviewExpanded = false }
            }
        }
        .transition(.opacity.combined(with: .scale(scale: 0.98)))
    }

    private func load() async {
        // The 1080p-class variant — the card's 420w tile stretched to the full window
        // was mush. Same server-side capture; only the encode differs.
        guard let url = URL(string: "http://127.0.0.1:8765/api/resolve-preview.jpg?size=big") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        if let (data, resp) = try? await URLSession.shared.data(for: req),
           (resp as? HTTPURLResponse)?.statusCode == 200, !data.isEmpty,
           let img = NSImage(data: data) {
            frame = img
        }
    }
}

// The finisher stage's live progress (remux/upload/cleanup on the overlap thread). Reads
// orchestrator.finishing (its OWN pct/elapsed/eta), never orchestrator.progress (the run stage).
struct FinisherProgress: View {
    @EnvironmentObject var store: AppStore

    /// The lanes CURRENTLY IN THIS STAGE, stacked. Filtering by stage is the whole point: the
    /// two lanes are independent and routinely differ — lane 1 uploading while lane 2 still
    /// remuxes — and drawing both in one card put a remux's progress, segment counter and all,
    /// under the Upload heading.
    let stageKey: String

    var body: some View {
        let o = store.state?.orchestrator
        // pct-gated, as it always was: a claimed-but-not-yet-encoding lane has no progress
        // to draw, and an empty bar reads as "running at 0%". Rows come pre-ordered:
        // earliest episode on top (user-dictated), whichever engine lane holds it.
        let mine = PipelineCard.lanesInDisplayOrder(o).filter {
            $0.stage == stageKey && ($0.pct != nil || $0.step != nil)
        }
        // With BOTH rows up, each must name its own episode. The card's top-right says "x2"
        // then, so it can no longer carry the first one's — which left it anonymous while the
        // second was labelled.
        let dual = mine.count > 1
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(mine.enumerated()), id: \.offset) { i, f in
                LaneProgress(f: f, primary: i == 0, showEpisode: dual)
            }
        }
        .padding(.top, 3)
    }
}

/// ONE remux lane. Extracted from FinisherProgress so the second lane can reuse it
/// verbatim — the two lanes must not drift into two renderings of the same thing.
///
/// Two asymmetries are deliberate, not oversights:
///  * Lane 2 carries NO elapsed clock. _set_finishing2_progress does not publish
///    elapsed_secs ("the elapsed bookkeeping stays single-slot on lane 1"), so finHMS would
///    be nil anyway. Showing a true-but-partial row beats inventing a number to match.
///    Rows are ordered by EPISODE (earliest on top), so the clockless row can be either
///    position — the clock follows the engine lane, not the display slot.
///  * Only the CARD pulses, never a row. Two PulseDots beat against each other — each
///    starts its own repeatForever on its own onAppear with no phase lock.
private struct LaneProgress: View {
    let f: FinishingDTO
    let primary: Bool
    /// Label the row with its episode. False for a lone lane, whose identity is already in
    /// the card's top-right; true whenever two rows are up and that slot reads "x2".
    var showEpisode: Bool = false

    // Fixed slots, sized to their worst cases ("100%", "~12h 59m left"), so a changing
    // digit never moves the container edge and lane 2's eta sits directly under lane 1's —
    // the layout makes the comparison instead of the reader.
    private static let pctSlot: CGFloat = 46
    private static let etaSlot: CGFloat = 96

    private var held: String? { f.holding }

    var body: some View {
        let pct = f.pct ?? 0
        let live = min(1, max(0, pct / 100))
        let stepOnly = f.step != nil && f.pct == nil     // label-only phase: no honest number
        // Same notched segment bar as Topaz — the remux is segmented too (dvcap ~5-min chunks):
        // bright fill = completed segments (snaps to the last finished boundary + a flash when
        // one lands), dark shadow = live progress through the current segment. Non-segmented
        // finisher stages (upload) send no notches → a plain single bar, unchanged.
        let notches = f.notches ?? []
        let done = f.seg_done ?? 0
        let completed: Double = notches.isEmpty ? live
            : (done >= notches.count ? 1.0 : (done > 0 ? notches[done - 1] : 0))
        // Peak repair: cut the segment being re-encoded out of the bar and refill it with
        // the repair's real frame progress. The span comes from the notch plan (segment z
        // runs from notch z-1 to notch z).
        let z = (f.repair_seg ?? 0) - 1
        let repairing = z >= 0 && z < notches.count
        let rLo: Double? = repairing ? (z == 0 ? 0 : notches[z - 1]) : nil
        let rHi: Double? = repairing ? notches[z] : nil
        let rFrac: Double = {
            guard repairing, let d = f.repair_done, let t = f.repair_total, t > 0 else { return 0 }
            return Double(d) / Double(t)
        }()
        VStack(alignment: .leading, spacing: 4) {
            if showEpisode, let ep = f.ep, !ep.isEmpty {
                Text(ep).font(.system(size: 11, weight: .medium)).monospacedDigit()
                    .foregroundStyle(DS.steel).lineLimit(1)
            }
            // FAST-REMUX STEPS (inject / ship): one labeled bar at a time — the bar is
            // the CURRENT step's own progress; a label-only phase (no watchable output)
            // shows its label with an empty track, never a fake number.
            if let step = f.step {
                Text(step).font(.system(size: 11, weight: .medium))
                    .foregroundStyle(DS.steel).lineLimit(1)
            }
            SteelBar(completed: completed, live: live, notches: notches, flashKey: done,
                     repairLo: rLo, repairHi: rHi, repairFrac: rFrac)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(stepOnly ? "—" : String(format: "%.0f%%", pct))
                    .font(.system(size: primary ? 17 : 13, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(held == nil ? DS.steelBright : DS.steelDim)
                    .frame(width: primary ? nil : Self.pctSlot, alignment: .leading)
                if primary, let c = finHMS(f.elapsed_secs) {
                    Text(c).font(.system(size: 12, weight: .medium)).monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                // HELD, not stalled. A lane paused because Resolve took the machine keeps its
                // last-known percentage on purpose; without saying so it reads as a hung encode.
                if let h = held {
                    Text("held").font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary).help(h)
                } else if let seg = f.repair_seg {
                    // PEAK REPAIR, not stuck. The pass runs after the bar hit 100% — the
                    // master measured over the peak cap, and only the offending segment(s)
                    // are being re-encoded at a tighter cap. Without this the lane sat at
                    // "100%" with no motion and read as a hung remux.
                    let extent = (f.repair_of ?? 0) > 1
                        ? " · \(f.repair_k ?? 1)/\(f.repair_of ?? 1)" : ""
                    Text("repairing peaks · seg \(seg)\(extent)")
                        .font(.system(size: 12, weight: .medium)).monospacedDigit()
                        .foregroundStyle(.secondary)
                        .help("The finished encode measured over the peak-bitrate cap. Only the "
                              + "flagged segment(s) are re-encoded at a tighter cap, then re-gated.")
                } else if let e = finLeft(f.eta_secs) {
                    Text(e).font(.system(size: 12, weight: .medium)).monospacedDigit()
                        .foregroundStyle(.secondary)
                        .frame(width: primary ? nil : Self.etaSlot, alignment: .leading)
                }
                if let d = f.seg_done, let t = f.seg_total, t > 0 {
                    Spacer()
                    Text("\(min(d + 1, t))/\(t)")
                        .font(.system(size: 11)).monospacedDigit().foregroundStyle(.tertiary)
                }
            }
        }
        .opacity(held == nil ? 1 : 0.65)
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
                        // The stage eta alone reads as "hours away", so show the CURRENT segment's
                        // countdown for near-term motion. Gated on that segment's projected TOTAL
                        // (time spent in it + what's left) against 'seg_eta_after_minutes' — judged
                        // on the segment itself rather than an average across segments, and on its
                        // total rather than its remainder, so the number stays put for the whole
                        // segment instead of disappearing right as it counts down to zero.
                        let segGate = Double(store.state?.settings?.seg_eta_after_minutes ?? 15) * 60
                        let segEta: String = {
                            guard (pr.seg_secs ?? 0) > segGate,
                                  let e = pr.seg_eta_secs, e > 0 else { return "" }
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
        let k = store.state?.settings?.youtube_videos_per_burst ?? 1
        let cadence = k > 1 ? "\(k) videos per episode"
                            : (n == 1 ? "1 video per episode" : "1 video per \(n) episodes")
        Card(title: title, systemImage: icon,
             hint: "TV in order · movies run whole when due · " + cadence) {
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
            // The next empty slot is just a search bar — to pick the first show, or add another.
            // The width is the 'max_active_shows' setting (Settings ▸ Advanced ▸ Shows at once);
            // lowering it below the running count hides the bar rather than dropping a show.
            // This one stays LIVE while the pipeline is armed: appending to the rotation drops
            // nothing, so no in-flight item is abandoned, and the run picks the new show up on
            // its next turn. Only REPLACING a slot is locked.
            if active.count < (s?.settings?.max_active_shows ?? 3) {
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
                             disabled: !store.seriesReachable) { id in
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
            // Armed, a filled slot loses its search bar (the show can't be swapped mid-run), so
            // nothing renders here — the library refresh that shared that row moves up into the
            // title row instead of being left behind on a line of its own.
            if !locked { seriesPicker(index: index, active: active, catalog: catalog) }
            HStack(spacing: 12) {
                Label(store.seriesTitle(name), systemImage: "tv").font(.system(size: 13, weight: .medium)).lineLimit(1)
                // No lock pill: armed, the controls it warned about are simply gone (the
                // search bar disappears and the settings collapse to one line), so the badge
                // was labelling an absence.
                if index == 0 && !store.seriesReachable {
                    Pill(systemImage: "wifi.slash", text: "NAS unreachable", tint: DS.steelBright, iconOnly: true)
                }
                Spacer()
                QueueCounts(q: show.queue)
                // Refresh still works mid-run, and it is what keeps the add-a-show picker below
                // current — so it survives the search bar it used to sit beside.
                if index == 0 && locked { LibraryRefreshButton { await store.refreshLibrary() } }
                if index > 0 && !locked {
                    Button { Task { await store.removeSeries(name) } } label: {
                        Image(systemName: "xmark.circle.fill").font(.system(size: 13)).foregroundStyle(.secondary)
                    }.buttonStyle(.plain).help("Remove from round-robin")
                }
            }
            // While the run holds these four, every Change button is gone and the rows are
            // four lines of static text — so they collapse into one readable line instead.
            if locked {
                LockedSettingsLine(
                    preset: catalog.first { $0.key == key }?.label ?? (key.isEmpty ? "—" : key),
                    isDefaultPreset: !(show.configured ?? false),
                    output: show.output_mode_effective ?? "dv1000",
                    normalized: show.normalize_audio ?? true,
                    replaces: show.replace_source ?? true,
                    extending: show.aspect == "4:3" && (show.extend_borders ?? false))
            } else {
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
                    Button("Change") { store.seriesPick = key.isEmpty ? (catalog.first?.key ?? "") : key
                                       store.pendingSeriesSlot = nil; store.pendingSeries = name }
                        .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
                    Spacer()
                }
                OutputModeRow(key: name, effective: show.output_mode_effective ?? "dv1000")
                NormalizeAudioRow(key: name, on: show.normalize_audio ?? true)
                ReplaceSourceRow(key: name, on: show.replace_source ?? true)
                if show.aspect == "4:3", store.state?.series?.borders_ready == true {
                    ExtendBordersRow(key: name, on: show.extend_borders ?? false)
                    if show.extend_borders == true {
                        ExtendPromptRow(key: name, prompt: show.extend_prompt ?? "")
                        if (show.extend_sets ?? 0) > 0 {
                            ExtendSetMemoryRow(key: name, sets: show.extend_sets ?? 0)
                        }
                    }
                }
            }
            if (show.queue?.featurette_count ?? 0) > 0 {
                FeaturettesLastRow(key: name, on: show.featurettes_last ?? true,
                                   count: show.queue?.featurette_count ?? 0)
            }
            UnwatchedFirstRow(key: name, on: show.unwatched_first ?? true)
            NextUpRow(show: name, next: show.next_up, armed: show.next_up_armed ?? false,
                      active: active, profile: show.next_up_profile, catalog: catalog,
                      nearDone: show.near_done ?? false)
            if let q = show.queue { QueueProgress(q: q) }     // the per-show total progress bar (moved here)
        }
    }

}

// The four per-show settings on ONE line, for a show the run has locked. Nothing here is
// actionable while the pipeline is armed (every Change button is hidden), so four stacked
// rows are four lines of dead weight — this says the same thing in one, in pipeline order:
// what Topaz does, what Resolve masters, what the remux does to audio, what the upload does
// to the source. Same glyphs as the expanded rows, so it reads as the same information.
private struct LockedSettingsLine: View {
    let preset: String
    let isDefaultPreset: Bool
    let output: String            // the RESOLVED range (dv1000 / dv2000 / sdr)
    let normalized: Bool
    let replaces: Bool
    var extending: Bool = false   // 4:3 show with border extension ON (hidden otherwise)

    private var outputLabel: String {
        switch output {
        case "sdr":    return "SDR"
        case "dv2000": return "Dolby Vision 2000 nits"
        default:       return "Dolby Vision 1000 nits"
        }
    }

    var body: some View {
        HStack(spacing: 7) {
            item("cpu", preset + (isDefaultPreset ? " (default)" : ""))
            dot
            item("wand.and.stars", outputLabel)
            dot
            item("square.stack.3d.up", normalized ? "Normalized audio" : "Original audio")
            dot
            item("arrow.up.circle", replaces ? "Replaces source" : "Keeps source")
            if extending {
                dot
                item("arrow.left.and.right.square", "Extends to 16:9")
            }
            Spacer(minLength: 0)
        }
        .lineLimit(1)
        .help("Locked while the run is armed — Topaz preset, what Resolve masters, the remux's "
              + "audio, and the upload's source policy. Stop the run to change any of them.")
    }

    private var dot: some View {
        Circle().fill(DS.steelDim.opacity(0.45)).frame(width: 2.5, height: 2.5)
    }

    private func item(_ symbol: String, _ text: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(DS.steelDim)
            Text(text).font(.system(size: 12)).foregroundStyle(DS.steel)
        }
        .fixedSize()
    }
}

// Compact per-show checkbox — under each show's preset so it's set per show, not global.
// A standalone view (not a TVMode method) so the QUEUED follow-up show can carry the same
// control: it keys on the show NAME, so it is settable before that show is ever active.
// Season-00 specials (Lost's "Missing Pieces" mobisodes, featurettes) are real SxxExx
// files, and "S00" sorts before "S01" — so without this they get upscaled BEFORE the show
// itself. Shown ONLY when the show actually has some (inert noise otherwise).
private struct FeaturettesLastRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let on: Bool
    let count: Int
    var body: some View {
        Toggle(isOn: Binding(get: { on },
                             set: { v in Task { await store.setFeaturettesLast(key, v) } })) {
            Text("Featurettes last").font(.system(size: 12)).foregroundStyle(.secondary)
        }
        .help("On: the \(count) season-00 special\(count == 1 ? "" : "s") run after the whole "
              + "show. Off: they keep numeric order, which puts them FIRST.")
    }
}

private struct UnwatchedFirstRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let on: Bool
    var body: some View {
        Toggle(isOn: Binding(get: { on },
                             set: { v in Task { await store.setShowUnwatchedFirst(key, v) } })) {
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
    @State private var confirming = false
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "square.stack.3d.up").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            Text(on ? "Normalized audio" : "Original audio")
                .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .help("Remux audio: normalized = quiet audio boosted to the loudness target; original = bit-exact copy")
            Button("Change") { confirming = true }
                .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
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

// AI border extension (4:3 -> 16:9) — per-show opt-in, rendered ONLY when the show's
// sources probe 4:3 AND the extender is fully installed (hide-inert-UI: absent
// otherwise, never disabled). Same preset-style shape as NormalizeAudioRow.
private struct ExtendBordersRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let on: Bool
    @State private var confirming = false

    private var projection: String {
        guard let spc = store.bordersStatus?.sec_per_chunk, spc > 0 else {
            return "overnight-scale \u{2014} hours per episode"
        }
        let chunks = 25.0 * 60.0 * 24.0 / 81.0          // a 25-min episode at ~24 fps
        let h = chunks * spc / 3600.0
        return String(format: "~%.1f h per 25-min episode", h)
    }

    private var helpText: String {
        "AI border extension: outpaints the left/right borders to 16:9 before the "
            + "upscale. Only the borders are generated \u{2014} the original picture ships "
            + "untouched (" + projection + ")."
    }
    private var dialogMessage: String {
        if on {
            return "Episodes not yet processed ship at their original 4:3. Nothing already made changes."
        }
        return "Adds an Extend step before the upscale \u{2014} " + projection + ". Only the "
            + "generated side strips are AI; the original frames ship untouched. Each episode "
            + "is checked individually, so a show that goes widescreen in later seasons "
            + "extends only its 4:3 episodes \u{2014} wide episodes and specials pass through."
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.left.and.right.square").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            Text(on ? "Extends to 16:9" : "Keeps 4:3")
                .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .help(helpText)
            Button("Change") { confirming = true }
                .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
            Spacer()
        }
        .confirmationDialog(on ? "Stop extending this show to 16:9?"
                               : "Extend this show's borders to 16:9?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(on ? "Keep the original 4:3" : "Extend to 16:9") {
                Task { await store.setExtendBorders(key, !on) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(dialogMessage)
        }
    }
}

// The show's WING PROMPT (continuity): what the generated side wings should contain
// ("dark wood bar interior, neon beer signs"). Rendered only under an ENABLED extend
// row. A gentle style bias — the fast sampler runs at cfg 1.0, so it nudges palette
// and content, it cannot pin geometry. Changing it re-generates unprocessed chunk work.
private struct ExtendPromptRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let prompt: String                    // "" = the built-in default
    @State private var editing = false
    @State private var draft = ""

    var body: some View {
        if editing {
            HStack(spacing: 6) {
                TextField("what the generated wings should show\u{2026}", text: $draft)
                    .textFieldStyle(.roundedBorder).font(.system(size: 11))
                    .onSubmit { save() }
                Button("Save") { save() }
                    .buttonStyle(.plain).font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.brand)
                Button("Cancel") { editing = false }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            .padding(.leading, 20)
        } else {
            HStack(spacing: 6) {
                Image(systemName: "text.quote").font(.system(size: 10)).foregroundStyle(DS.steelDim)
                Text(prompt.isEmpty ? "Default wing prompt" : prompt)
                    .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                    .help("Describes what the AI-generated side wings should contain for this "
                          + "show \u{2014} e.g. \"dark wood bar interior, neon beer signs\". A "
                          + "gentle style bias applied to every episode; empty uses the built-in "
                          + "default. Changing it re-generates any unfinished extend work.")
                Button("Change") { draft = prompt; editing = true }
                    .buttonStyle(.plain).font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Color.brand)
                Spacer()
            }
            .padding(.leading, 20)
        }
    }

    private func save() {
        editing = false
        Task { await store.setExtendPrompt(key, draft.trimmingCharacters(in: .whitespacesAndNewlines)) }
    }
}

// The show's SET MEMORY (continuity): how many distinct sets the extender has learned
// wings for. Rendered only when the book is non-empty; Reset is the recovery lever when
// the AI took a set a wrong direction — the next episode re-invents fresh.
private struct ExtendSetMemoryRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let sets: Int
    @State private var confirming = false

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "square.grid.2x2").font(.system(size: 10)).foregroundStyle(DS.steelDim)
            Text("Set memory: \(sets) set\(sets == 1 ? "" : "s")")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .help("Wing inventions the extender has learned for this show's recurring "
                      + "sets \u{2014} reused across scenes and episodes so the generated "
                      + "borders stay consistent.")
            Button("Reset") { confirming = true }
                .buttonStyle(.plain).font(.system(size: 11, weight: .medium))
                .foregroundStyle(Color.brand)
            Spacer()
        }
        .padding(.leading, 20)
        .confirmationDialog("Forget this show's \(sets) remembered set\(sets == 1 ? "" : "s")?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button("Reset set memory", role: .destructive) {
                Task { await store.resetSetBook(key) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Future episodes re-invent the borders fresh and re-learn from there. "
                 + "Episodes already made are untouched.")
        }
    }
}

// Per-slot "Up next" row: the show queued to take THIS slot the moment its current show
// finishes (clean handoff — no interleaving). Deliberately NOT gated by the run lock: the
// slot's CURRENT show can't be swapped mid-run, but queueing what comes AFTER only records
// a future intent, so it's safe (and useful) to set while the pipeline is running.
// At <10% remaining the follow-up is ARMED — locked in, and its first episodes start
// pre-downloading so the handoff doesn't begin with a cold download.
private struct NextUpRow: View {
    @EnvironmentObject var store: AppStore
    let show: String
    let next: String?
    let armed: Bool
    let active: [String]
    let profile: ShowSettingsDTO?
    let catalog: [PresetDTO]
    let nearDone: Bool          // >=90% of the current show is done
    @State private var picking = false

    /// The row is INVISIBLE until the slot's show is ≥90% done — queueing a successor is
    /// meaningless noise before then (user-dictated: hide inert controls, don't disable
    /// them). Once a follow-up IS queued it stays visible regardless, so it can always be
    /// seen, reconfigured or cleared.
    private var visible: Bool { (next?.isEmpty == false) || nearDone || picking }

    var body: some View {
        if visible { content }
    }

    @ViewBuilder private var content: some View {
        VStack(alignment: .leading, spacing: 6) {
            headerRow
            // The QUEUED show's own settings — configurable BEFORE it starts, so it never
            // begins a run on defaults. Same controls as an active show; they key on the
            // show NAME, which is why they can be set while it's only queued.
            if let n = next, !n.isEmpty, !picking {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Image(systemName: "cpu").font(.system(size: 12)).foregroundStyle(DS.steelDim)
                        Text(catalog.first { $0.key == (profile?.preset ?? "") }?.label
                             ?? (profile?.preset ?? "—"))
                            .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                            .padding(.horizontal, 7).padding(.vertical, 2)
                            .background(Capsule().fill(Color.white.opacity(0.07)))
                            .help("Topaz preset for the queued show")
                        if !(profile?.configured ?? false) {
                            Text("(default)").font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                        Button("Change") {
                            store.seriesPick = profile?.preset ?? catalog.first?.key ?? ""
                            store.pendingSeriesSlot = nil       // preset-only: does NOT change the slot
                            store.pendingSeries = n
                        }
                        .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
                        Spacer()
                    }
                    OutputModeRow(key: n, effective: profile?.output_mode_effective ?? "dv1000")
                    NormalizeAudioRow(key: n, on: profile?.normalize_audio ?? true)
                    ReplaceSourceRow(key: n, on: profile?.replace_source ?? true)
                    if profile?.aspect == "4:3", store.state?.series?.borders_ready == true {
                        ExtendBordersRow(key: n, on: profile?.extend_borders ?? false)
                        if profile?.extend_borders == true {
                            ExtendPromptRow(key: n, prompt: profile?.extend_prompt ?? "")
                            if (profile?.extend_sets ?? 0) > 0 {
                                ExtendSetMemoryRow(key: n, sets: profile?.extend_sets ?? 0)
                            }
                        }
                    }
                    if profile?.has_featurettes == true {
                        FeaturettesLastRow(key: n, on: profile?.featurettes_last ?? true, count: 0)
                    }
                    UnwatchedFirstRow(key: n, on: profile?.unwatched_first ?? true)
                }
                .padding(.leading, 20)      // nested under the up-next line — these are ITS settings
            }
        }
    }

    @ViewBuilder private var headerRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.turn.down.right").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            if picking {
                SearchablePicker(placeholder: "Queue a show for when this one finishes…",
                                 options: store.seriesOptions
                                     .filter { $0 != show && !active.contains($0) }
                                     .map { PickOption(id: $0, label: store.seriesTitle($0)) },
                                 disabled: !store.seriesReachable) { id in
                    picking = false
                    Task {
                        await store.setNextUp(show, id)
                        // Same smart flow as picking a slot's show: settle its preset NOW
                        // (auto-detect, else ask) so the queued show is fully configured
                        // long before it is promoted.
                        if await store.profileFor(id)?.configured != true {
                            store.tvDetecting = true
                            let auto = await store.detectPreset("tv", id)
                            store.tvDetecting = false
                            if let key = auto {
                                await store.setPreset(id, key)
                            } else {
                                store.seriesPick = catalog.first?.key ?? ""
                                store.pendingSeriesSlot = nil
                                store.pendingSeries = id
                            }
                        }
                    }
                }
                Button("Cancel") { picking = false }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
            } else if let n = next, !n.isEmpty {
                Text("Up next: \(store.seriesTitle(n))")
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel).lineLimit(1)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .help("Takes this slot the moment the current show finishes")
                if armed {
                    Text("ready").font(.system(size: 11, weight: .medium)).foregroundStyle(Color.brand)
                        .help("Under 10% left — the follow-up is locked in and pre-downloading")
                }
                Button("Change") { picking = true }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
                Button("Clear") { Task { await store.setNextUp(show, "") } }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                Button("Queue a show for when this one finishes") { picking = true }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
                    .help("Pick the show that takes this slot next — offered once this show is 90% done, and settable while the pipeline runs")
            }
            Spacer()
        }
    }
}

// Per-item "Replace source" row (shows + movies — NOT YouTube, whose folder-split has no
// Plex-visible source): same preset-style shape as NormalizeAudioRow. ON (default) = after
// the 4K master is size-verified on the NAS, the superseded source is permanently deleted;
// OFF = both files stay and Plex serves them as one item with two versions. Change requires
// a confirmation because replacing burns the re-run option for future upscale models.
private struct ReplaceSourceRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    let on: Bool
    @State private var confirming = false
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.up.circle").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            Text(on ? "Replaces source" : "Keeps source")
                .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .help("Upload policy: replace = delete the source once the 4K master is verified on the NAS; keep = Plex serves both versions and the source stays for a future re-run")
            Button("Change") { confirming = true }
                .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
            Spacer()
        }
        .confirmationDialog("Switch to \(on ? "keeping" : "replacing") the source?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(on ? "Keep the source beside the master" : "Replace the source with the master") {
                Task { await store.setReplaceSource(key, !on) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Replacing permanently deletes each source after its 4K master is verified — "
                 + "a future re-run with better upscale models needs the source again. Keeping "
                 + "both costs the source's size and shows one item with two versions in Plex.")
        }
    }
}

// Per-item "Output range" row (shows, movies, YouTube channels): what Resolve DELIVERS.
// AUTO is the default and ALWAYS produces 1000-nit Dolby Vision, whatever the intake
// range (user-dictated 2026-08-09: 2000-nit is MANUAL-ONLY — it stays in the picker but
// nothing selects it automatically). Auto never yields a non-DV master either — SDR is
// also a manual choice. The three explicit values PIN the output regardless of what came
// in. Same preset-style shape as the rows above; the confirm sheet lists the ones you
// aren't on.
private struct OutputModeRow: View {
    @EnvironmentObject var store: AppStore
    let key: String
    /// What the item will ACTUALLY master as — the engine already resolved "automatic"
    /// against the source range, so the row never shows a word the user has to translate.
    let effective: String
    @State private var confirming = false

    private static let modes = ["dv1000", "dv2000", "sdr"]
    private static func label(_ m: String) -> String {
        switch m {
        case "sdr":    return "SDR"
        case "dv2000": return "Dolby Vision 2000 nits"
        default:       return "Dolby Vision 1000 nits"
        }
    }
    private var current: String { Self.modes.contains(effective) ? effective : "dv1000" }

    var body: some View {
        HStack(spacing: 8) {
            // The same glyph the RESOLVE stage carries in the timeline — this is a Resolve
            // setting, and the row sits in pipeline order between Topaz and the remux.
            Image(systemName: "wand.and.stars").font(.system(size: 12)).foregroundStyle(DS.steelDim)
            Text(Self.label(current))
                .font(.system(size: 12, weight: .medium)).foregroundStyle(DS.steel)
                .padding(.horizontal, 7).padding(.vertical, 2)
                .background(Capsule().fill(Color.white.opacity(0.07)))
                .help("What Resolve masters this as. Left alone everything masters to "
                      + "1000-nit Dolby Vision; the 2000-nit target and SDR are manual "
                      + "choices only.")
            Button("Change") { confirming = true }
                .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.brand)
            Spacer()
        }
        .confirmationDialog("Output range", isPresented: $confirming, titleVisibility: .visible) {
            // The CURRENT value first and as the default action, so the highlighted button is
            // what the item is on now — not whatever happened to sort first.
            Button(Self.label(current) + " (current)") {}
                .keyboardShortcut(.defaultAction)
            ForEach(Self.modes.filter { $0 != current }, id: \.self) { m in
                Button(Self.label(m)) { Task { await store.setOutputMode(key, m) } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Left alone, every item masters to 1000-nit Dolby Vision — the 2000-nit "
                 + "target is never chosen automatically. Picking a value pins it whatever "
                 + "the source is. SDR masters are named differently from Dolby Vision ones, "
                 + "so anything already finished keeps the range it shipped with.")
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
                                 options: store.movieLibrary.map { m in
                                     PickOption(id: m.id, label: store.movieTitle(m.name, m.title ?? m.name),
                                                detail: m.has_dv == true
                                                    ? [m.pipelineHint, "already DV — companion on the seedbox"]
                                                        .filter { !$0.isEmpty }.joined(separator: " — ")
                                                    : m.pipelineHint)
                                 },
                                 disabled: !store.moviesReachable) { id in
                    if let m = store.movieLibrary.first(where: { $0.id == id }) {
                        if m.has_dv == true {
                            // DV-badged movies are COMBINE-ONLY (user-dictated): the tap
                            // starts the seedbox companion search instead of a plain add.
                            Task { await store.companionAction("search", name: m.name ?? "",
                                                               dir: m.dir, title: m.title) }
                            return
                        }
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
            // COMPANION COMBINE flows in progress (search → candidates → verdict card →
            // confirm). Driven entirely by the state poll's companions map — no view-local
            // state, so a tab switch mid-pairing loses nothing. Confirmed pairings leave
            // this list and appear as COMBINE rows in the queue below.
            let companions = (store.state?.movies?.companions ?? [:])
                .filter { $0.value.status != nil && $0.value.status != "confirmed" }
                .sorted { ($0.value.title ?? $0.key) < ($1.value.title ?? $1.key) }
            ForEach(companions, id: \.key) { name, c in
                CompanionPanel(name: name, c: c)
            }
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
                    // the library now lists EVERYTHING (DV titles are combine-only) — the
                    // count that matters is still how many have no DV yet
                    Text("\(store.movieLibrary.filter { $0.has_dv != true }.count) movies without DV")
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
    @State private var confirmCancel = false

    /// Is this movie actively IN the pipeline (run thread or a finisher lane)? Then the
    /// ✕ is a real cancel — hours of work discarded — and deserves a confirmation. A
    /// merely-queued movie removes silently, as it always did.
    private var inFlight: Bool {
        let o = store.state?.orchestrator
        if o?.current?.kind == "movie" && o?.current?.name == m.name { return true }
        for f in [o?.finishing, o?.finishing2] {
            if let f, f.movie == true, f.source == m.name { return true }
        }
        return false
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: m.combine == true ? "arrow.triangle.merge" : "film")
                    .foregroundStyle(.secondary).font(.system(size: 12))
                Text(store.movieTitle(m.name, m.title ?? m.name)).font(.system(size: 13)).lineLimit(1)
                Spacer()
                if m.combine == true {
                    // a combine item has no Topaz preset — the pill says what it IS instead
                    Text("COMBINE")
                        .font(.system(size: 10, weight: .semibold)).foregroundStyle(DS.steelBright)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Capsule().fill(Color.white.opacity(0.07)))
                        .help("Best-of merge with its seedbox companion (DV + best audio)")
                } else {
                    Text(catalog.first { $0.key == m.preset }?.label ?? (m.preset ?? "—"))
                        .font(.system(size: 11, weight: .medium)).foregroundStyle(DS.steel)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Capsule().fill(Color.white.opacity(0.07)))
                    Button {
                        Task { await store.companionAction("search", name: m.name ?? "",
                                                           dir: m.dir, title: m.title) }
                    } label: {
                        Image(systemName: "link.badge.plus").foregroundStyle(.secondary)
                            .font(.system(size: 11))
                    }.buttonStyle(.plain)
                    .help("Pair a seedbox companion copy (best-of combine: DV + best audio)")
                }
                Button {
                    if inFlight { confirmCancel = true }
                    else if let n = m.name { Task { await store.removeMovie(n) } }
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }.buttonStyle(.plain)
                .help(inFlight ? "Cancel this movie — it is mid-pipeline" : "Remove from the queue")
                .confirmationDialog("This movie is mid-pipeline.",
                                    isPresented: $confirmCancel, titleVisibility: .visible) {
                    Button("Cancel it and discard its work", role: .destructive) {
                        if let n = m.name { Task { await store.removeMovie(n) } }
                    }
                    Button("Keep processing", role: .cancel) {}
                } message: {
                    Text("Its unfinished download, Resolve and remux work is thrown away. "
                         + "Anything already uploaded to the NAS stays.")
                }
            }
            .padding(.vertical, 7).padding(.horizontal, 10)
            .contentShape(Rectangle())
            .onTapGesture { if m.combine != true { onTap() } }   // combine rows have no preset
            .help(m.combine == true ? "Companion combine — tracks were decided on the verdict card"
                                    : "Tap to change this movie's Topaz preset")
            // OUTSIDE the tappable HStack — the row tap opens the preset chooser, and the
            // Change buttons must not trigger it. Keyed by TITLE (the movie's settings key).
            // COMBINE rows hide the inert controls (user rule): no Topaz/Resolve encode to
            // range-pin, and MKV lossless audio is never boosted — only the source's fate
            // is still a real choice.
            if m.combine != true {
                OutputModeRow(key: m.title ?? m.name ?? "", effective: m.output_mode_effective ?? "dv1000")
                    .padding(.horizontal, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                NormalizeAudioRow(key: m.title ?? m.name ?? "", on: m.normalize_audio ?? true)
                    .padding(.horizontal, 10).padding(.top, 4)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            ReplaceSourceRow(key: m.title ?? m.name ?? "", on: m.replace_source ?? true)
                .padding(.horizontal, 10).padding(.bottom, 7)
                .padding(.top, m.combine == true ? 0 : 4)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
        }
    }
}

// COMPANION COMBINE: one movie's pairing flow, driven entirely by the state poll's
// companions map (search → candidates → probing → VERDICT CARD → confirm). The card is
// the user-dictated decision gate: nothing runs until "Run this combine" is pressed.
private struct CompanionPanel: View {
    @EnvironmentObject var store: AppStore
    let name: String
    let c: CompanionDTO

    private func gb(_ n: Int64?) -> String {
        let v = Double(n ?? 0) / 1e9
        return v >= 100 ? String(format: "%.0f GB", v) : String(format: "%.1f GB", v)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "arrow.triangle.merge").font(.system(size: 12))
                    .foregroundStyle(DS.steelBright)
                Text(store.movieTitle(name, c.title ?? name))
                    .font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Spacer()
                Button {
                    Task { await store.companionAction("dismiss", name: name) }
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }.buttonStyle(.plain).help("Abandon this pairing")
            }
            switch c.status ?? "" {
            case "searching":
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("Searching the seedbox for a companion copy…")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            case "found":
                Text("Pick the companion copy:")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                ForEach(c.candidates ?? []) { cand in
                    Button {
                        Task { await store.companionAction("pair", name: name, path: cand.path) }
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: cand.is_dir == true ? "folder" : "doc")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                            Text(cand.name ?? "").font(.system(size: 12)).lineLimit(1)
                            Spacer()
                            if (cand.size ?? 0) > 0 {
                                Text(gb(cand.size)).font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 5).padding(.horizontal, 8)
                        .contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.white.opacity(0.05)))
                }
            case "probing":
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("Probing both copies (codecs, DV, audio)…")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            case "ready":
                VerdictCard(name: name, c: c)
            default:      // error / vanished / mismatch
                HStack(spacing: 7) {
                    Image(systemName: "exclamationmark.triangle").font(.system(size: 11))
                        .foregroundStyle(.orange)
                    Text(c.error ?? (c.status == "vanished"
                                     ? "The companion is no longer on the seedbox."
                                     : "The copies are different cuts — they cannot combine."))
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    Spacer()
                    Button("Search again") {
                        Task { await store.companionAction("search", name: name, title: c.title) }
                    }.buttonStyle(SteelButtonStyle(lit: false)).controlSize(.small)
                }
            }
        }
        .padding(10)
        .panel(DS.radiusControl, inset: true)
    }
}

// The decision gate: exactly what ships from where, in plain rows, and a Confirm button.
private struct VerdictCard: View {
    @EnvironmentObject var store: AppStore
    let name: String
    let c: CompanionDTO

    private func sideLabel(_ side: String?) -> String {
        switch side ?? "" {
        case "nas": return "library copy"
        case "remote": return "seedbox copy"
        default: return side ?? "?"
        }
    }

    private func row(_ icon: String, _ label: String, _ value: String,
                     _ detail: String?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).font(.system(size: 11)).foregroundStyle(DS.steel)
                .frame(width: 14)
            Text(label).font(.system(size: 12, weight: .medium)).frame(width: 82, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(value).font(.system(size: 12))
                if let d = detail, !d.isEmpty {
                    Text(d).font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
    }

    var body: some View {
        let v = c.verdict
        VStack(alignment: .leading, spacing: 7) {
            if let specs = v?.specs {
                VStack(alignment: .leading, spacing: 2) {
                    if let s = specs["nas"] {
                        Text("Library: " + s).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    if let s = specs["remote"] {
                        Text("Seedbox: " + s).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
            Divider()
            row("film", "Video", sideLabel(v?.video_from), v?.video_why)
            row("sparkles.tv", "Dolby Vision",
                (v?.rpu_from == "resolve") ? "Resolve analysis"
                    : "real (P\(v?.rpu_profile ?? "?")) from the \(sideLabel(v?.rpu_from))",
                v?.rpu_why)
            row("speaker.wave.3", "Audio", sideLabel(v?.audio_from), v?.audio_why)
            row("gauge.with.needle", "Re-encode",
                (v?.reencode?.predicted == true)
                    ? "yes — over the playback ceiling"
                    : "no — ships its original bits",
                (v?.reencode?.basis == "estimate")
                    ? "estimated from the average bitrate — re-checked before shipping"
                    : String(format: "measured 1-second peak: %.1f Mbps", v?.reencode?.mbps ?? 0))
            if let warn = v?.cut_warning {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle").font(.system(size: 10))
                        .foregroundStyle(.orange)
                    Text(warn + " — the combine will refuse if frames don't align.")
                        .font(.system(size: 11)).foregroundStyle(.orange)
                }
            }
            HStack(spacing: 8) {
                Button {
                    Task { await store.companionAction("confirm", name: name, title: c.title) }
                } label: {
                    Label("Run this combine", systemImage: "arrow.triangle.merge")
                }.buttonStyle(SteelButtonStyle(lit: true))
                Button("Cancel") {
                    Task { await store.companionAction("dismiss", name: name) }
                }.buttonStyle(SteelButtonStyle(lit: false))
                Spacer()
            }.padding(.top, 2)
        }
    }
}

// YouTube mode: search youtarr's channels, queue channels to upscale. A queued channel's videos
// (newest first) process as a priority tier ahead of TV; once a channel is done it drops off and
// the TV show continues. Addable/reorderable any time, just like movies.
// Paste any YouTube link — a playlist, a single video, or a channel. Two phases: RESOLVE
// (no side effects) so the user confirms against a real name and count, then IMPORT. A
// watch?v=…&list=… URL expresses both readings, so that one always asks which was meant
// (user-dictated) instead of guessing. Imported videos join the ordinary cadence between
// episodes — the companion app's send-to-Visionary button remains the jump-the-queue path.
private struct LinkImportRow: View {
    @EnvironmentObject var store: AppStore
    @State private var url = ""
    @State private var busy = false
    @State private var confirming: YTLinkResolveDTO? = nil
    @State private var confirmURL = ""
    @State private var note: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "link").font(.system(size: 11)).foregroundStyle(.secondary)
                    TextField("Paste a playlist, video or channel link\u{2026}", text: $url)
                        .textFieldStyle(.plain).font(.system(size: 13))
                        .onSubmit { Task { await resolve() } }
                    if !url.isEmpty {
                        Button { url = ""; note = nil; confirming = nil } label: {
                            Image(systemName: "xmark.circle.fill")
                        }.buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 9).padding(.vertical, 7)
                .panel(8, inset: true)
                Button { Task { await resolve() } } label: {
                    Label("Import", systemImage: "arrow.down.circle")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(SteelButtonStyle(lit: !url.isEmpty))
                .disabled(url.isEmpty || busy)
            }
            if let c = confirming { confirmPanel(c) }
            if let n = note {
                Text(n).font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onChange(of: store.pendingImportURL) { pasted in
            // Menu bar -> "Import YouTube Link from Clipboard". Fill this field and run the
            // SAME resolve -> confirm flow a typed link takes, so there is only ever one
            // import path (and the ambiguous case still asks).
            guard let pasted, !pasted.isEmpty else { return }
            url = pasted
            store.pendingImportURL = nil
            Task { await resolve() }
        }
    }

    @ViewBuilder
    private func confirmPanel(_ c: YTLinkResolveDTO) -> some View {
        let count = c.count ?? 0
        VStack(alignment: .leading, spacing: 8) {
            if c.ambiguous == true {
                Text("That link points at a video inside a playlist.")
                    .font(.system(size: 12, weight: .medium))
                Text(c.title.map { "Playlist: \($0)" } ?? "")
                    .font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                HStack(spacing: 8) {
                    Button("Just this video") { Task { await commit("video") } }
                        .buttonStyle(SteelButtonStyle(lit: true))
                    Button(count > 0 ? "Whole playlist (\(count))" : "Whole playlist") {
                        Task { await commit("playlist") }
                    }.buttonStyle(SteelButtonStyle(lit: false))
                    Button("Cancel") { confirming = nil }.buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            } else {
                Text(c.title ?? "Playlist").font(.system(size: 12, weight: .medium)).lineLimit(1)
                Text(count > 0 ? "\(count) videos" : "playlist")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Button(count > 0 ? "Import \(count) videos" : "Import") {
                        Task { await commit("playlist") }
                    }.buttonStyle(SteelButtonStyle(lit: true))
                    Button("Cancel") { confirming = nil }.buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10).panel(DS.radiusControl, inset: true)
    }

    private func resolve() async {
        guard !url.isEmpty, !busy else { return }
        busy = true; note = nil; confirming = nil
        let r = await store.resolveYoutubeLink(url)
        busy = false
        guard let r else { note = "Couldn't reach the engine."; return }
        switch r.status ?? "" {
        case "bad-url":
            note = "That doesn't look like a YouTube link."
        case "playlist-unreadable":
            note = "That playlist is private or unavailable."
        case "channel-unresolved":
            note = "Couldn't find that channel."
        default:
            // Ask when the link is ambiguous, and when it is a playlist (importing 200 videos
            // by accident is the surprise this step exists to prevent). A single video or a
            // channel is unambiguous and cheap, so it commits straight away.
            if r.ambiguous == true || (r.kind ?? "") == "playlist" {
                confirmURL = url; confirming = r
            } else {
                confirmURL = url
                await commit(nil)
            }
        }
    }

    private func commit(_ choice: String?) async {
        busy = true
        let out = await store.importYoutubeLink(confirmURL.isEmpty ? url : confirmURL, choice: choice)
        busy = false
        confirming = nil
        switch out?.status ?? "" {
        case "queued":
            let n = out?.count ?? 0
            let name = (out?.title ?? "").isEmpty ? "video" : (out?.title ?? "")
            note = n > 1 ? "Queued \(n) videos from \(name)." : "Queued 1 video."
            if out?.truncated == true, let t = out?.total {
                note = (note ?? "") + " That playlist has \(t); the rest weren't fetched."
            }
            url = ""; confirmURL = ""
        case "channel-queued":
            note = (out?.subscribed == true)
                ? "Added \(out?.title ?? "that channel") from your subscriptions."
                : "Added \(out?.title ?? "that channel") by link."
            url = ""; confirmURL = ""
        case "already-queued":
            note = "Already queued."; url = ""; confirmURL = ""
        case "youtarr-unreachable":
            note = "youtarr didn't respond, so nothing was queued."
        case "empty":
            note = "That playlist has nothing to import."
        case "bad-url":
            note = "That doesn't look like a YouTube link."
        default:
            note = "Import failed."
        }
    }
}

// Videos added by pasting a link, grouped by the import they came from, so a playlist can be
// dropped again as one thing. A batch disappears once all its videos are upscaled.
private struct ImportedGroup: View {
    @EnvironmentObject var store: AppStore
    let imports: [YTImportDTO]
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Imported").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
            VStack(spacing: 0) {
                ForEach(imports) { imp in
                    HStack(spacing: 9) {
                        Image(systemName: (imp.kind ?? "") == "playlist" ? "list.bullet.rectangle" : "play.rectangle")
                            .font(.system(size: 11)).foregroundStyle(DS.steelDim).frame(width: 16)
                        Text((imp.title ?? "").isEmpty ? "Single video" : (imp.title ?? ""))
                            .font(.system(size: 13)).lineLimit(1)
                        Spacer()
                        Pill(systemImage: "tray.full",
                             text: "\(imp.remaining ?? 0) of \(imp.count ?? 0) left", tint: DS.steel)
                        Button { Task { await store.dropYoutubeImport(imp.id ?? "") } } label: {
                            Image(systemName: "trash").font(.system(size: 11))
                        }.buttonStyle(.plain).foregroundStyle(.secondary)
                            .help("Forget this import — its videos that haven't been upscaled yet leave the queue")
                    }
                    .padding(.horizontal, 10).padding(.vertical, 7)
                }
            }.panel(DS.radiusControl, inset: true)
        }
    }
}

/// What Visionary has finished, and the one thing worth doing to a finished file: fixing
/// its audio. The revision is IN PLACE and audio-only — by the time an item completes its
/// source is usually gone (replace_source deletes the superseded original once the master
/// verifies; a YouTube staging folder is purged at cleanup), so a re-run is not on the table.
/// The master is re-measured, the loudness boost re-applied, and video + subtitles
/// stream-copied, which leaves Dolby Vision untouched and takes minutes.
struct HistoryPopover: View {
    @EnvironmentObject var store: AppStore
    @State private var confirming: HistoryItemDTO? = nil

    private func when(_ ts: Int?) -> String {
        guard let ts, ts > 0 else { return "" }
        let d = Date(timeIntervalSince1970: TimeInterval(ts))
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(d) ? "h:mm a" : "MMM d"
        return f.string(from: d)
    }

    private func icon(_ kind: String?) -> String {
        kind == "youtube" ? "play.rectangle" : kind == "movie" ? "film" : "tv"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text("Finished").font(.system(size: 13, weight: .semibold))
                Spacer()
                Button(store.historyScanning ? "Searching…" : "Find finished files") {
                    Task { await store.scanHistory() }
                }
                .buttonStyle(SteelButtonStyle(lit: false))
                .disabled(store.historyScanning)
                .help("Adopt masters upscaled before this list existed — it only records new work")
            }
            if store.history.isEmpty {
                Text("Nothing recorded yet. \"Find finished files\" picks up everything already on the NAS.")
                    .font(.system(size: 11.5)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.vertical, 6)
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(store.history) { it in row(it) }
                    }
                }.frame(maxHeight: 320)
                    .panel(DS.radiusControl, inset: true)
            }
        }
        .padding(14).frame(width: 430)
    }

    @ViewBuilder private func row(_ it: HistoryItemDTO) -> some View {
        let sub = [when(it.at), it.gain.map { "+\(String(format: "%.1f", $0)) dB" } ?? "",
                   (it.revised ?? 0) > 0 ? "revised" : ""]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        HStack(spacing: 9) {
            Image(systemName: icon(it.kind)).font(.system(size: 11)).foregroundStyle(DS.steelDim)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 1) {
                Text(it.title ?? it.nas_path ?? "").font(.system(size: 12.5)).lineLimit(1)
                if !sub.isEmpty {
                    Text(sub).font(.system(size: 10.5)).foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 6)
            if it.revising == true {
                Text("fixing…").font(.system(size: 10)).foregroundStyle(DS.steelBright)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Capsule().fill(Color.white.opacity(0.08)))
            } else if it.can_revise == true {
                Button { confirming = it } label: {
                    Image(systemName: "waveform").font(.system(size: 12))
                        .frame(width: 26, height: 24).contentShape(Rectangle())
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .help("Fix audio — re-measure this file and re-apply the loudness boost, in place")
            } else {
                // Say WHY rather than showing a dead control (hide-inert-UI): the only
                // refusal is lossless audio, which the pipeline never re-encodes.
                Text(it.why ?? "").font(.system(size: 10)).foregroundStyle(.tertiary)
                    .lineLimit(1).frame(maxWidth: 130, alignment: .trailing)
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .overlay(alignment: .top) { Divider().opacity(0.35) }
        .confirmationDialog("Fix the audio on \"\(confirming?.title ?? "this file")\"?",
                            isPresented: Binding(get: { confirming?.id == it.id },
                                                 set: { if !$0 { confirming = nil } }),
                            titleVisibility: .visible) {
            Button("Fix audio") {
                Task { await store.reviseAudio(it) }
                confirming = nil
            }
            Button("Cancel", role: .cancel) { confirming = nil }
        } message: {
            Text("Re-measures the published file and re-applies the loudness boost in place. "
                 + "Video and subtitles are copied untouched, so it takes minutes.")
        }
    }
}

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
                LinkImportRow()
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
                CadenceControl(every: store.state?.settings?.youtube_every_tv_episodes ?? 2,
                               burst: store.state?.settings?.youtube_videos_per_burst ?? 1) { n, k in
                    Task { await store.setYoutubeCadence(every: n, burst: k) }
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
                if let imps = yt?.imports, !imps.isEmpty { ImportedGroup(imports: imps) }
            }
        }
        .onAppear { Task { await store.fetchChannels() } }
    }
}

// Global YouTube cadence: how many TV episodes play per 1 YouTube video. YouTube 4K-SDR upscales are
// far slower than a 1080p episode, so this throttles them so they don't crowd out TV.
// ONE dial spanning BOTH directions of the cadence as whole numbers (user-dictated
// 2026-08-17): "3 videos per episode" ... "1 per episode" ... "1 video every 10 episodes".
// A single Int position maps onto the engine's two whole-number knobs — no fractions:
//   position < 0  ->  every = 1,        burst = -position + 1   (K videos per episode)
//   position >= 0 ->  every = position + 1, burst = 1           (1 video per N episodes)
private struct CadenceControl: View {
    let every: Int
    let burst: Int
    let onChange: (Int, Int) -> Void          // (every, burst)

    private static let minPos = -9            // 10 videos per episode
    private static let maxPos = 49            // 1 video per 50 episodes

    private var position: Int { burst > 1 ? -(burst - 1) : every - 1 }

    private static func knobs(_ pos: Int) -> (Int, Int) {
        pos < 0 ? (1, -pos + 1) : (pos + 1, 1)
    }

    private var summary: String {
        if burst > 1 { return "\(burst) videos per TV episode" }
        return every == 1 ? "1 video per TV episode"
                          : "1 video every \(every) TV episodes"
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "rectangle.stack.badge.play")
                .font(.system(size: 15)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text("YouTube cadence").font(.system(size: 13, weight: .medium))
                Text(summary).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Stepper(value: Binding(get: { position },
                                   set: { p in let k = Self.knobs(p); onChange(k.0, k.1) }),
                    in: Self.minPos...Self.maxPos) { EmptyView() }
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
                        .font(.system(size: 11, weight: .medium)).lineLimit(1).fixedSize()
                        .frame(width: 76)   // fits "Resume" + icon; fixed so both states align across rows
                        .foregroundStyle(paused ? DS.steelBright : DS.steelDim)
                        .padding(.vertical, 4)
                        .background(Capsule().fill(Color.white.opacity(paused ? 0.10 : 0.05)))
                        .overlay(Capsule().strokeBorder(Color.white.opacity(paused ? 0.25 : 0.10), lineWidth: 0.7))
                }.buttonStyle(.plain)
                    .help(paused ? "Resume — youtarr downloads + upscaling restart"
                                 : "Pause — stop downloading & upscaling this channel (keeps its files)")
                Text(ch.title ?? ch.folder_name ?? "").font(.system(size: 13)).lineLimit(1)
                    .foregroundStyle(paused ? .secondary : .primary)
                if ch.via_link == true {
                    // Added by pasting a link and NOT one of your subscriptions — same behaviour,
                    // but the list shouldn't imply you follow it (user-dictated).
                    Pill(systemImage: "link", text: "added by link", tint: DS.steelDim)
                }
                Group {
                    Picker("", selection: Binding(get: { ch.scope ?? "popular" },
                                                  set: { s in Task { await store.setChannelScope(ch.channelId ?? "", s) } })) {
                        Text("Most popular").tag("popular"); Text("All").tag("all")
                    }.labelsHidden().frame(width: 128).font(.system(size: 11))
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
            OutputModeRow(key: ch.folder_name ?? "", effective: ch.output_mode_effective ?? "dv1000")
                .disabled(paused).opacity(paused ? 0.35 : 1)
                .padding(.horizontal, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
            NormalizeAudioRow(key: ch.folder_name ?? "", on: ch.normalize_audio ?? true)
                .disabled(paused).opacity(paused ? 0.35 : 1)
                .padding(.horizontal, 10).padding(.bottom, 7).padding(.top, 4)
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

// ONE slot standing in for a run of consecutive YouTube videos. Collapsed it reads
// "N YouTube videos" with the first title; expanded it is exactly the same rows with the
// same per-video controls, so nothing about how they process changes.
private struct VideoGroupRow: View {
    let group: UpNextGroup
    let parent: UpNextView
    @State private var open = false

    private var jumping: Int { group.items.filter { $0.priority == true }.count }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { withAnimation(.easeInOut(duration: 0.15)) { open.toggle() } } label: {
                HStack(spacing: 8) {
                    Color.clear.frame(width: 18, height: 1)      // videos are unnumbered
                    Image(systemName: "play.rectangle").font(.system(size: 11))
                        .foregroundStyle(DS.steelDim)
                    Text("\(group.items.count) YouTube videos")
                        .font(.system(size: 13, weight: .medium))
                    if jumping > 0 {
                        Text(jumping == 1 ? "1 jumping the queue" : "\(jumping) jumping the queue")
                            .font(.system(size: 10)).foregroundStyle(Color.brand)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.white.opacity(0.07)))
                    }
                    if !open, let first = group.items.first {
                        Text(first.title ?? first.name ?? "").font(.system(size: 12))
                            .foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: open ? "chevron.up" : "chevron.down")
                        .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            if open {
                ForEach(Array(group.items.enumerated()), id: \.element.id) { off, it in
                    HStack(spacing: 8) {
                        Color.clear.frame(width: 18, height: 1)
                        parent.row(it)
                        Spacer()
                        parent.controls(it, group.startIndex + off)
                    }.font(.system(size: 13)).padding(.leading, 8)
                }
            }
        }
    }
}

// "Next up" — collapsed shows the next item; tap to expand the next ~10 that will actually
// process (queued movies jump ahead of episodes).
/// A run of consecutive up-next entries. A run of 2+ YouTube videos becomes ONE slot.
struct UpNextGroup: Identifiable {
    let startIndex: Int
    let items: [UpNextDTO]
    var isVideoGroup: Bool { items.count > 1 && items.allSatisfy { $0.kind == "youtube" } }
    var id: String { "\(startIndex)|\(items.first?.id ?? "")|\(items.count)" }
}

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
                    // Count SLOTS, not entries — a run of videos draws as one row, so
                    // "+N" has to match what expanding actually reveals.
                    let slots = Self.grouped(items).count
                    if slots > 1 {
                        if !expanded {
                            Text("+\(slots - 1)").font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                    }
                }
                .font(.system(size: 13)).contentShape(Rectangle())
            }
            .buttonStyle(.plain).disabled(Self.grouped(items).count <= 1)
            if expanded {
                // Full list in processing order. Movies (only) get controls: ↑/↓ move them
                // anywhere — including between episodes — and × removes them. CONSECUTIVE
                // YOUTUBE VIDEOS COLLAPSE into ONE slot (they arrive in bursts and used to
                // dominate the list); the slot expands to the same rows with the same
                // controls — purely presentational.
                let ords = Self.episodeOrdinals(items)
                ForEach(Self.grouped(items)) { g in
                    if g.isVideoGroup {
                        VideoGroupRow(group: g, parent: self)
                    } else if let it = g.items.first {
                        HStack(spacing: 8) {
                            // Only episodes carry a number; everything else keeps the
                            // gutter width so the titles stay aligned.
                            Text(ords[g.startIndex].map(String.init) ?? "")
                                .font(.system(size: 11)).monospacedDigit()
                                .foregroundStyle(.tertiary).frame(width: 18, alignment: .trailing)
                            row(it)
                            Spacer()
                            controls(it, g.startIndex)
                        }.font(.system(size: 13))
                    }
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
            HStack(spacing: 1) {
                iconButton("chevron.up", enabled: idx > 0,
                           help: "Move earlier") { await store.queueAction("up", it) }
                iconButton("chevron.down", enabled: idx < items.count - 1,
                           help: "Move later") { await store.queueAction("down", it) }
                iconButton("xmark.circle.fill", enabled: true,
                           help: "Remove from the queue") { await store.queueAction("remove", it) }
            }
        } else if it.kind == "youtube" {
            // Run this one NOW: it jumps ahead of everything (cadence-exempt, ahead of due
            // movies) and the in-flight item yields at its next safe boundary.
            if it.priority == true {
                // Already queued to jump: SAY so. The pipeline only yields at the next Topaz
                // segment boundary (deliberate — the in-flight segment finishes first), which
                // can be a couple of minutes, and an unacknowledged press reads as broken.
                Text("running next").font(.system(size: 10)).foregroundStyle(Color.brand)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(Color.white.opacity(0.07)))
                    .help("Queued to jump the queue — starts when the current segment finishes")
            } else {
                iconButton("arrow.up.to.line", enabled: true,
                           help: "Run this video now — the current segment finishes first, "
                               + "then this starts (Resolve is never cut off) and whatever "
                               + "was running resumes afterwards") {
                    await store.runYoutubeNow(name: it.name ?? "")
                }
            }
            Button { confirmingVideoDelete = it } label: {
                Image(systemName: "xmark.circle.fill").font(.system(size: 12))
                    .frame(width: 26, height: 24, alignment: .center)
                    .contentShape(Rectangle())
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
    /// Item index -> its EPISODE number. Only TV episodes are numbered (user-dictated):
    /// movies and videos ride along between them, so numbering everything made the list
    /// read as though a video were "item 4 of the show".
    static func episodeOrdinals(_ items: [UpNextDTO]) -> [Int: Int] {
        var m: [Int: Int] = [:]
        var n = 0
        for (i, it) in items.enumerated() where it.kind == "episode" {
            n += 1
            m[i] = n
        }
        return m
    }

    /// Collapse runs of consecutive YouTube videos into one slot; everything else stays a
    /// row of its own. Order is never changed — only how it is drawn.
    static func grouped(_ items: [UpNextDTO]) -> [UpNextGroup] {
        var out: [UpNextGroup] = []
        var i = 0
        while i < items.count {
            if items[i].kind == "youtube" {
                var j = i
                while j < items.count && items[j].kind == "youtube" { j += 1 }
                out.append(UpNextGroup(startIndex: i, items: Array(items[i..<j])))
                i = j
            } else {
                out.append(UpNextGroup(startIndex: i, items: [items[i]]))
                i += 1
            }
        }
        return out
    }

    @ViewBuilder func iconButton(_ sym: String, enabled: Bool, help: String,
                                 _ act: @escaping () async -> Void) -> some View {
        Button { Task { await act() } } label: {
            // The glyph stays 12 pt; the FRAME is the click target (26x24), and
            // contentShape makes the whole frame hittable — a .plain button otherwise
            // hit-tests only the glyph's opaque pixels, a ~10 px sliver.
            Image(systemName: sym).font(.system(size: 12))
                .frame(width: 26, height: 24, alignment: .center)
                .contentShape(Rectangle())
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

// MARK: - Settings (header gear popover)

// One settings row: title, one-line explanation, live value, stepper. Every numeric range here
// MIRRORS engine/settings.py's LIMITS table, so the UI can never offer a value the engine would
// silently rewrite on save.
private struct SettingRow: View {
    @EnvironmentObject var store: AppStore
    let title: String
    let blurb: String
    let key: String
    let fallback: Int
    let range: ClosedRange<Int>
    var step: Int = 1
    var unit: String = ""
    var zeroLabel: String? = nil          // shown instead of "0 <unit>" when the value is 0

    private var value: Int { store.state?.settings?[int: key] ?? fallback }

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium))
                Text(blurb).font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 10)
            Text(value == 0 ? (zeroLabel ?? "0\(unit.isEmpty ? "" : " " + unit)")
                            : "\(value)\(unit.isEmpty ? "" : " " + unit)")
                .font(.system(size: 13, weight: .medium)).monospacedDigit()
                .foregroundStyle(DS.steel)
            Stepper(value: Binding(get: { value },
                                   set: { n in Task { await store.saveSettings([key: n]) } }),
                    in: range, step: step) { EmptyView() }
                .labelsHidden().fixedSize()
        }
    }
}

// A knob whose 0 means OFF: the stepper can't express the jump from its floor to 0, so the
// toggle owns on/off and the stepper is HIDDEN (not disabled) while off — a control that does
// nothing in the current context shouldn't be on screen.
private struct OptionalSettingRow: View {
    @EnvironmentObject var store: AppStore
    let title: String
    let blurb: String
    let key: String
    let fallback: Int                     // the value restored when it's switched back on
    let range: ClosedRange<Int>
    var step: Int = 1
    var unit: String = ""

    private var value: Int { store.state?.settings?[int: key] ?? fallback }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 13, weight: .medium))
                    Text(blurb).font(.system(size: 11)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 10)
                Toggle("", isOn: Binding(get: { value != 0 },
                                         set: { on in
                                             Task { await store.saveSettings([key: on ? fallback : 0]) }
                                         }))
                    .labelsHidden().toggleStyle(.switch).controlSize(.small)
            }
            if value != 0 {
                HStack {
                    Spacer()
                    Text("\(value)\(unit.isEmpty ? "" : " " + unit)")
                        .font(.system(size: 13, weight: .medium)).monospacedDigit()
                        .foregroundStyle(DS.steel)
                    Stepper(value: Binding(get: { value },
                                           set: { n in Task { await store.saveSettings([key: n]) } }),
                            in: range, step: step) { EmptyView() }
                        .labelsHidden().fixedSize()
                }
            }
        }
    }
}

private struct SettingsGroupLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold)).tracking(0.6)
            .foregroundStyle(.tertiary)
    }
}

// The header gear's popup. Everything in it is UNIVERSAL — per-show options (preset, normalize
// audio, replaces source, unwatched first, featurettes last, up next) live on each show's block.
// Nothing here changes how a file is ENCODED: the loudness target and the peak bitrate cap stay
// engine-only on purpose. These knobs decide what runs, when, how much at once, and what qualifies.
struct SettingsPopover: View {
    @EnvironmentObject var store: AppStore
    @State private var advanced = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                SetupSection()          // onboarding: fields + checks + installs (SetupViews.swift)
            Divider()
            Text("Settings").font(.system(size: 15, weight: .semibold))

                ScreenControlSection()
                ResolveHostSection()
                Divider()

                // The basics are the ones you actually reach for: what the run is chewing
                // through, how much of the machine it takes, and the screen. Everything that is
                // set once and forgotten — the hardware pins, the disk floors, the failure
                // thresholds — sits under Advanced.
                SettingRow(title: "Shows at once",
                           blurb: "How many shows share the rotation, one episode from each in turn.",
                           key: "max_active_shows", fallback: 3, range: 1...4)
                SettingRow(title: "Remux lanes",
                           blurb: "The second lane opens only when items stack up behind the first — steady state is one remux either way. 1 switches it off and leaves Topaz the whole GPU.",
                           key: "finisher_lanes", fallback: 2, range: 1...2)
                SettingRow(title: "Dim screen after",
                           blurb: "Idle this long → screen off. Tap the brightness key to bring it back.",
                           key: "dim_after_minutes", fallback: 15,
                           range: 0...240, step: 5, unit: "min", zeroLabel: "Off")

                Divider()

                DisclosureGroup(isExpanded: $advanced) {
                    VStack(alignment: .leading, spacing: 14) {
                        SettingsGroupLabel(text: "Power").padding(.top, 6)
                        SettingRow(title: "Required adapter",
                                   blurb: "Below this, everything pauses and the screen sleeps. A hardware fact — set it once.",
                                   key: "min_adapter_watts", fallback: 140,
                                   range: 100...500, step: 10, unit: "W")
                        SettingRow(title: "Unplug grace",
                                   blurb: "How long a power blip is tolerated mid-stage before the stage is abandoned.",
                                   key: "unplug_grace_seconds", fallback: 60,
                                   range: 0...600, step: 15, unit: "s")

                        SettingsGroupLabel(text: "Scheduling").padding(.top, 4)
                        SettingRow(title: "Re-check interval",
                                   blurb: "How often to look again when there's nothing to do.",
                                   key: "poll_minutes", fallback: 30,
                                   range: 1...1440, step: 5, unit: "min")

                        SettingsGroupLabel(text: "Disk").padding(.top, 4)
                        SettingRow(title: "Keep free",
                                   blurb: "Space that must stay free before an item may start. An episode needs roughly 205 GB while it upscales (re-measured); a 4K fast-path movie peaks around 320 GB — keep this at 350+ with movies queued.",
                                   key: "min_free_gb", fallback: 400,
                                   range: 200...2000, step: 25, unit: "GB")
                        OptionalSettingRow(title: "Download ahead",
                                           blurb: "Stage upcoming sources early so the GPU never waits on a download. Off fetches each one when its turn comes.",
                                           key: "prefetch_cap_gb", fallback: 100,
                                           range: 25...500, step: 25, unit: "GB")

                        SettingsGroupLabel(text: "When things go wrong").padding(.top, 4)
                        SettingRow(title: "Retries before skipping",
                                   blurb: "How many times one episode may fail before it's set aside and the run moves on.",
                                   key: "max_episode_fails", fallback: 5, range: 1...20)

                        SettingsGroupLabel(text: "Readouts").padding(.top, 4)
                        SettingRow(title: "Segment ETA after",
                                   blurb: "Show the current segment's countdown while that segment still has longer than this to run. A segment about to finish doesn't need its own number.",
                                   key: "seg_eta_after_minutes", fallback: 15,
                                   range: 1...120, unit: "min")

                        // Recent failures live HERE and nowhere else — tucked behind Advanced,
                        // not on the page. Absent entirely when there is nothing to report, so
                        // it never occupies space just to say "no issues".
                        if let lines = store.state?.log, !lines.isEmpty {
                            SettingsGroupLabel(text: "Recent issues").padding(.top, 4)
                            Text(lines.suffix(6).joined(separator: "\n"))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(DS.fault)
                                .textSelection(.enabled)          // copyable — the point of showing it
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }

                        SettingsGroupLabel(text: "What qualifies").padding(.top, 4)
                        OptionalSettingRow(title: "4K fast path",
                                           blurb: "A 4K source at or above this bitrate skips Topaz — it's already sharp enough to go straight to Dolby Vision.",
                                           key: "passthrough_min_mbps", fallback: 12,
                                           range: 5...200, unit: "Mbps")
                        SettingRow(title: "Remuxes beside a fast-path Resolve",
                                   blurb: "Never (the default) gives every Resolve the whole machine. Raising it lets a fast-path title's Resolve share with this many running remuxes.",
                                   key: "resolve_share_remuxes", fallback: 0,
                                   range: 0...2, zeroLabel: "Never")
                    }
                } label: {
                    Text("Advanced settings").font(.system(size: 13, weight: .medium))
                }

                Divider()
                Text("These apply to everything. Per-show options live on each show.")
                    .font(.system(size: 11)).foregroundStyle(.tertiary)
            }
            .padding(16)
        }
        .frame(width: 430)
        .frame(maxHeight: 560)
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
        // ORDER MATTERS: the transient suffixes must match before the generic
        // "hdr10 dv upscaled" catch-all — a movie mid-remux briefly holds FOUR files
        // with that stem, and all of them read as "Master" (user-caught 2026-08-06).
        if n.hasSuffix(".remuxsegs") { return "Remux segments" }
        if n.hasSuffix(".src.hevc") { return "Original stream · temp" }
        if n.hasSuffix(".inject.hevc") { return "DV-injected stream · temp" }
        if n.hasSuffix(".ship.hevc") { return "Render stream · temp" }
        if n.hasSuffix(".capped.hevc") { return "Capped stream · temp" }
        if n.hasSuffix(".tracks.mp4") { return "Audio tracks · temp" }
        if n.hasSuffix(".dv.mp4") { return "DV wrap · temp" }
        if n.contains("_mezz.mp4.segments") { return "Compat copy segments" }
        if n.hasSuffix("_mezz.mp4") { return "Resolve compat copy" }
        if n.contains("_prob4_upscaled.segments") { return "Topaz segments" }
        if n.contains("_prob4_upscaled") { return "Topaz ProRes" }
        if n.contains(" hdr10 dv upscaled") { return n.hasSuffix(".mov") ? "DV render" : "4K DV master" }
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
        // the arm-gate 409's face: the server named exactly what refused — show it
        if let err = store.lastError {
            Card(title: "Can't start", systemImage: "exclamationmark.octagon") {
                Text(err).font(.system(size: 12)).foregroundStyle(DS.fault)
                    .textSelection(.enabled)
            }
        }
        // HARD requirements (exact Resolve/Topaz builds + a 2.0-backing-scale display —
        // engine/versions.py). hard_ok false → the server refuses to arm; explain why here.
        if let t, t.hard_ok == false {
            Card(title: "Unsupported setup", systemImage: "xmark.octagon") {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(DS.steelBright)
                        Text("Visionary requires DaVinci Resolve Studio 18.6.0, Topaz Video AI 7.0.1, and a main display rendering at 2.0x (Retina/HiDPI — a built-in panel, a 4K/5K monitor in its default scaled mode, or a 4K dummy plug). It will not arm until they match.")
                            .font(.system(size: 13, weight: .medium))
                    }
                    ForEach((t.found ?? [:]).sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                        Text("\(k): \(v)").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
        }
        // FIRST-RUN: everything else healthy but setup incomplete (no config yet, tools
        // missing, projects not imported) — point at the one place that finishes it.
        if let t, t.hard_ok != false, t.setup_complete == false {
            Card(title: "Finish setup", systemImage: "wrench.and.screwdriver") {
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("A few setup steps are still open").font(.system(size: 13, weight: .medium))
                        Text("Connections, dependencies, permissions and the Resolve import all live in one place.")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Open Setup") { store.showSettings = true }
                        .buttonStyle(SteelButtonStyle(lit: true))
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
                    // (Settings moved to the header gear — see HeaderBar / SettingsPopover.)
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
        .overlay {
            // The Resolve preview at the FULL window width (click the card's preview to
            // open). Mounted HERE — the one place that spans the whole window — so the
            // card's tile can hand off to something genuinely edge to edge.
            if store.resolvePreviewExpanded {
                ExpandedResolvePreview()
            }
        }
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
