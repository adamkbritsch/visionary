import AppKit
import SwiftUI

// `Visionary --snapshots DIR` renders the README's card screenshots from the LIVE dashboard state
// and exits (user-dictated 2026-09-30: "take other new screenshots of the app").
//
// It is a second process that only READS the running app's loopback API — no server of its own, no
// window shown, no Dock icon, no POST — so the window in use is never touched: no tab flips, no
// popover opens, no focus is taken. Each card is laid out at the window's real content width in an
// offscreen window on the main screen (so it renders at the display's 2x scale) and drawn with
// cacheDisplay. The app draws no system materials, so an offscreen render is what the window shows.

/// Opens collapsible sections (the "Next up" list) for a snapshot; false everywhere else.
private struct SnapshotExpandKey: EnvironmentKey { static let defaultValue = false }
extension EnvironmentValues {
    var snapshotExpanded: Bool {
        get { self[SnapshotExpandKey.self] }
        set { self[SnapshotExpandKey.self] = newValue }
    }
}

@MainActor
enum Snapshots {
    static let cardWidth: CGFloat = 1040          // the window's 1080 less its 20 pt gutters

    static func run(_ dir: String) async {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let store = AppStore()
        await store.refresh()
        await store.fetchSeries()
        await store.fetchMovies()
        await store.fetchChannels()
        guard store.state != nil else {
            FileHandle.standardError.write("snapshots: the dashboard at 127.0.0.1:8765 did not answer\n".data(using: .utf8)!)
            return
        }
        func card<V: View>(_ v: V) -> some View {
            v.padding(.horizontal, 20).padding(.vertical, 16)
                .frame(width: cardWidth + 40)
                .background(TheatreStage(scrollY: 0))
        }
        store.modeOverride = "tv"
        await shot(card(SeriesCard().environment(\.snapshotExpanded, true)), store, "\(dir)/queue.png")
        store.modeOverride = "movie"
        await shot(card(SeriesCard()), store, "\(dir)/movies.png")
        store.modeOverride = "youtube"
        await shot(card(SeriesCard()), store, "\(dir)/youtube.png")
        store.modeOverride = nil
        await shot(card(HStack(alignment: .top, spacing: 16) { ScratchPowerCard(); ScratchContentsCard() }),
                   store, "\(dir)/scratch.png")
        await shot(SettingsPopover()
                       .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                       .fill(Color(.displayP3, red: 0.13, green: 0.135, blue: 0.145))),
                   store, "\(dir)/settings.png", fixedWidth: 430)
        await shot(card(PipelineCard()), store, "\(dir)/pipeline.png")
    }

    /// Lay `view` out at its natural height for the width, in an offscreen window on the main screen,
    /// and write it as a PNG at the screen's backing scale.
    static func shot<V: View>(_ view: V, _ store: AppStore, _ path: String,
                              fixedWidth: CGFloat? = nil) async {
        let root = view.environmentObject(store)
            .environment(\.colorScheme, .dark)
            .tint(Color.brand)
            .fixedSize(horizontal: fixedWidth == nil, vertical: true)
        let host = NSHostingView(rootView: root)
        host.appearance = NSAppearance(named: .darkAqua)
        let size = host.fittingSize
        let w = fixedWidth ?? size.width, h = size.height
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1600, height: 1000)
        let win = NSWindow(contentRect: NSRect(x: screen.minX, y: screen.minY, width: w, height: h),
                           styleMask: [.borderless], backing: .buffered, defer: false)
        win.appearance = NSAppearance(named: .darkAqua)
        win.isOpaque = false
        win.backgroundColor = .clear
        win.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: w, height: h)
        // Never ordered in: nothing appears on screen. A few run-loop turns let SwiftUI settle async
        // layout (the chip bar measures itself, images decode) before the draw.
        for _ in 0..<6 {
            host.layoutSubtreeIfNeeded()
            try? await Task.sleep(nanoseconds: 150_000_000)
        }
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
            print("snapshot: \(path) \(rep.pixelsWide)x\(rep.pixelsHigh)")
        }
        win.contentView = nil
    }
}
