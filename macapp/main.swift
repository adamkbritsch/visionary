import Cocoa
import SwiftUI
import ServiceManagement
import Combine

// Native macOS APPLIANCE app. Launches the bundled Python dashboard server (the backend
// that owns the pipeline + the TCC-sensitive Resolve UI work), then renders a real
// SwiftUI interface that polls the loopback API. No WebView anywhere.
// Appliance model: the app opens at login, can't be closed — only minimized — and while
// ACTIVATED the engine runs whenever it can (the server re-arms it by itself).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var server: Process?
    let store = AppStore()
    private var cancellables = Set<AnyCancellable>()
    private let dockProgress = DockProgressView(frame: NSRect(x: 0, y: 0, width: 128, height: 128))

    func applicationDidFinishLaunching(_ notification: Notification) {
        startServer()
        buildMenu()
        registerLoginItem()

        // FIXED-SIZE window (like Boom 3D): no .resizable, no fullscreen, no zoom button —
        // the appliance has one size; only close (→ minimize) and minimize remain.
        let rect = NSRect(x: 0, y: 0, width: 1080, height: 620)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "Visionary"
        window.collectionBehavior = [.fullScreenNone]        // hard-block fullscreen/tiling
        window.standardWindowButton(.zoomButton)?.isHidden = true
        window.center()
        window.setFrameAutosaveName("MainWindow")            // position persists; size is fixed
        window.setContentSize(NSSize(width: 1080, height: 620))   // normalize any old saved size
        window.styleMask.insert(.fullSizeContentView)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden          // the HeaderBar is the title bar
        window.appearance = NSAppearance(named: .darkAqua)   // stock system dark grey
        window.contentView = NSHostingView(rootView: RootView().environmentObject(store))
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        store.start()

        // Mirror the active job onto the Dock icon (DaVinci-Resolve-style bar under the icon).
        // Fires on every ~1.5 s poll; the draw is cheap and no-ops when the % is unchanged.
        store.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] s in self?.updateDockProgress(s) }
            .store(in: &cancellables)
    }

    /// DaVinci-Resolve-style Dock progress: while a pipeline stage is actively processing, draw
    /// a thin bar under the app icon tracking that stage's %; revert to the plain icon when idle.
    /// Gate: the top-level stage must be set AND the live progress must be for THAT stage (the
    /// orchestrator keeps `running` true and retains stale `progress` while idle-waiting on
    /// power/disk/NAS, so `running` + a bare `pct` would leave a frozen bar up between jobs).
    private func updateDockProgress(_ s: StateDTO?) {
        let orch = s?.orchestrator
        // A SINGLE bar. The REMUX (finisher) WINS whenever it's in flight — so during the dual system
        // the Dock shows the remux, not topaz. When no remux is running, fall back to the run-thread
        // stage (download/topaz/resolve) so the icon still shows progress for those. Plain icon when idle.
        // The run-thread branch gates on progress.stage == the live stage, so a stale `pct` retained
        // while idle-waiting on power/disk/NAS can't leave a frozen bar up between jobs.
        var pct: Double? = nil
        if let f = orch?.finishing?.pct {
            pct = f                                             // remux/upload in flight → it wins
        } else if let stage = orch?.stage, let pr = orch?.progress, pr.stage == stage, let p = pr.pct {
            pct = Double(p)                                     // no finisher → the run-thread stage's bar
        }
        if let pct = pct {
            dockProgress.progress = min(1, max(0, pct / 100))
            if NSApp.dockTile.contentView !== dockProgress { NSApp.dockTile.contentView = dockProgress }
            NSApp.dockTile.display()
        } else if NSApp.dockTile.contentView != nil {
            NSApp.dockTile.contentView = nil                    // nothing active → hand the tile back to the icon
            NSApp.dockTile.display()
        }
    }

    func startServer() {
        guard let res = Bundle.main.resourcePath else { return }
        let pkill = Process()
        pkill.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pkill.arguments = ["-f", "dashboard/server.py"]
        try? pkill.run(); pkill.waitUntilExit()

        let dashDir = res + "/engine/dashboard"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [dashDir + "/server.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: dashDir)
        try? p.run()
        server = p
    }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Visionary",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Visionary",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Close to Dock",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let runItem = NSMenuItem(); main.addItem(runItem)
        let runMenu = NSMenu(title: "Run")
        let toggle = NSMenuItem(title: "Activate / Deactivate", action: #selector(toggleRun), keyEquivalent: "r")
        toggle.target = self; runMenu.addItem(toggle)
        let refresh = NSMenuItem(title: "Refresh now", action: #selector(refreshNow), keyEquivalent: "")
        refresh.target = self; runMenu.addItem(refresh)
        runItem.submenu = runMenu

        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = winMenu

        NSApp.mainMenu = main
        NSApp.windowsMenu = winMenu
    }

    @objc func toggleRun() { Task { await store.toggleAutomation() } }
    @objc func refreshNow() { Task { await store.refresh() } }

    // ---- appliance lifecycle: open at login, never fully closes ----

    /// The app registers itself as a login item so the appliance is always up.
    private func registerLoginItem() {
        if SMAppService.mainApp.status != .enabled {
            try? SMAppService.mainApp.register()
        }
    }

    // Close (red button / ⌘W) and Quit (⌘Q) both MINIMIZE — the appliance keeps running in
    // the Dock. (Deploys/updates still replace it via SIGTERM, which bypasses this flow.)
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.miniaturize(nil)
        return false
    }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        // NEVER block the SYSTEM: a logout/shutdown/restart (and OS-update restarts) sends a
        // quit Apple event carrying a 'why?' (kAEQuitReason) attribute — let those through, or
        // the appliance would abort every shutdown ("Visionary interrupted shut down").
        // Only a plain user ⌘Q (no quit reason) is converted to minimize.
        if let evt = NSAppleEventManager.shared().currentAppleEvent,
           evt.attributeDescriptor(forKeyword: AEKeyword(0x7768793F)) != nil {   // 'why?'
            return .terminateNow
        }
        window?.miniaturize(nil)
        return .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) { server?.terminate() }
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }
    // Clicking the Dock icon of a minimized appliance brings the window back.
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window?.makeKeyAndOrderFront(nil); window?.deminiaturize(nil) }
        return true
    }
}

/// The Dock tile's content while a job runs: the real app icon with a thin, glassy steel
/// progress bar seated near the bottom — the same read as DaVinci Resolve's render bar.
/// Installed as `NSApp.dockTile.contentView` only while a stage is processing, then removed
/// (reverting to the system-drawn glass icon) when idle, so nothing lingers between jobs.
final class DockProgressView: NSView {
    var progress: Double = 0 { didSet { if progress != oldValue { needsDisplay = true } } }   // the remux's %

    override func draw(_ dirtyRect: NSRect) {
        // Base layer: the real app icon. Taking over contentView means WE own the whole tile,
        // so the icon has to be redrawn under the bar (else it'd vanish behind our view).
        NSApp.applicationIconImage?.draw(in: bounds, from: .zero, operation: .sourceOver, fraction: 1)

        // A single thin flat bar seated at the very bottom of the tile so it reads as evenly spaced
        // between the squircle's bottom edge (~9% up) and the macOS running-indicator dot (which the
        // Dock draws ~9% BELOW the tile) — i.e. underneath the icon, not on the glass. No gloss/shadow.
        let h = bounds.height * 0.050
        let inset = bounds.width * 0.135
        let track = NSRect(x: bounds.minX + inset, y: bounds.height * 0.016,
                           width: bounds.width - inset * 2, height: h)
        drawBar(track, progress: progress)
    }

    private func drawBar(_ track: NSRect, progress: Double) {
        let p = CGFloat(max(0, min(1, progress)))
        let r = track.height / 2
        let trackPath = NSBezierPath(roundedRect: track, xRadius: r, yRadius: r)
        // Quiet recessed track: flat translucent fill + a single hairline rim so the unfilled
        // remainder still reads on the glass — nothing raised or glossy.
        NSColor.black.withAlphaComponent(0.30).setFill()
        trackPath.fill()
        NSColor.white.withAlphaComponent(0.10).setStroke()
        trackPath.lineWidth = 0.75
        trackPath.stroke()
        // Cool metallic fill matching the chrome Dolby mark. Clipped to the rounded track so a
        // small % stays a clean rounded sliver (no white nub) and the right edge is a crisp
        // vertical seam — a real progress fill, not a glossy pill.
        guard p > 0 else { return }
        NSGraphicsContext.saveGraphicsState()
        trackPath.setClip()
        let fill = NSRect(x: track.minX, y: track.minY, width: track.width * p, height: track.height)
        NSGradient(starting: NSColor(calibratedRed: 0.95, green: 0.965, blue: 0.99, alpha: 1),
                   ending:   NSColor(calibratedRed: 0.70, green: 0.75, blue: 0.82, alpha: 1))?
            .draw(in: fill, angle: -90)
        NSGraphicsContext.restoreGraphicsState()
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate                 // NSApplication.delegate is weak; the local
    NSApp.setActivationPolicy(.regular)      // binding keeps it alive across the blocking run()
    objc_setAssociatedObject(app, "delegate", delegate, .OBJC_ASSOCIATION_RETAIN)
    app.run()
}
