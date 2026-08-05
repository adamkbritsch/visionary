import Cocoa
import SwiftUI
import ServiceManagement
import Combine

// Native macOS APPLIANCE app. Launches the bundled Python dashboard server (the backend
// that owns the pipeline + the TCC-sensitive Resolve UI work), then renders a real
// SwiftUI interface that polls the loopback API. No WebView anywhere.
// Appliance model: the app opens at login, can't be closed — only minimized — and while
// ACTIVATED the engine runs whenever it can (the server re-arms it by itself).
/// A button that works in a NON-ACTIVATING panel. The warning panel deliberately never
/// takes focus, so every click on it arrives as a "first mouse" event — and NSButton
/// swallows those by default, treating the click as "activate my app" and firing no
/// action. Without this override the panel's buttons look normal and do nothing.
final class FirstMouseButton: NSButton {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

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
        // Deliberately NO setFrameAutosaveName: restoring the saved frame is what used to
        // override the centring (the restore runs when the name is SET, right after center()),
        // so the window reappeared wherever it was last dragged.
        window.setContentSize(NSSize(width: 1080, height: 620))
        window.styleMask.insert(.fullSizeContentView)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden          // the HeaderBar is the title bar
        window.appearance = NSAppearance(named: .darkAqua)   // stock system dark grey
        window.contentView = NSHostingView(rootView: RootView().environmentObject(store))
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
        // Centre LAST, once the frame has stopped moving. Doing it earlier measured wrong:
        // inserting .fullSizeContentView re-derives the frame from the content rect (the
        // titlebar stops counting), which shifted an already-centred window by the titlebar's
        // height. With every mutation done, what we centre is what is shown.
        Self.centreOnScreen(window)
        NSApp.activate(ignoringOtherApps: true)

        store.start()

        // Mirror the active job onto the Dock icon (DaVinci-Resolve-style bar under the icon).
        // Fires on every ~1.5 s poll; the draw is cheap and no-ops when the % is unchanged.
        store.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] s in
                self?.updateDockProgress(s)
                self?.updateTakeoverWarning()
            }
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
        if !flag {
            if let w = window { Self.centreOnScreen(w) }   // back from the Dock: centred again
            window?.makeKeyAndOrderFront(nil); window?.deminiaturize(nil)
        }
        return true
    }

    // --- SCREEN TAKEOVER WARNING ------------------------------------------------------
    // A borderless panel on the MAIN display, not a notification. UNUserNotificationCenter
    // would need a new framework, an authorization prompt, and — decisively — is SUPPRESSED
    // under Focus / Do Not Disturb, which is exactly the state an overnight machine is in.
    // It also cannot carry a live countdown. This needs no entitlement and always shows.
    private var takeoverPanel: NSPanel?
    private var takeoverLabel: NSTextField?
    private var takeoverLive = false        // false = about to, true = happening now

    private func updateTakeoverWarning() {
        // Two states, one panel, NO timer: "about to take the screen" and "using it right
        // now". A countdown was tried and removed — it had to start at the top of the stage,
        // but the takeover lands whenever setup() finishes, so it hit zero and then sat at
        // "now..." for minutes.
        if store.takeoverActive { showTakeoverWarning(live: true); return }
        if store.takeoverSoon { showTakeoverWarning(live: false); return }
        hideTakeoverWarning()
    }

    private func hideTakeoverWarning() {
        takeoverLive = false
        takeoverPanel?.orderOut(nil)
        takeoverPanel = nil
        takeoverLabel = nil
    }

    private func showTakeoverWarning(live: Bool) {
        // Rebuild when the MODE changes — the wording and buttons differ between them.
        if takeoverPanel != nil && takeoverLive == live { return }
        if takeoverPanel != nil { hideTakeoverWarning() }
        takeoverLive = live
        let w: CGFloat = 380, h: CGFloat = 96
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: w, height: h),
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.level = .screenSaver
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        // Must NEVER take focus — a warning that interrupts you is its own interruption.
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]

        let bg = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        bg.material = .hudWindow
        bg.blendingMode = .behindWindow
        bg.state = .active
        bg.wantsLayer = true
        bg.layer?.cornerRadius = 14
        bg.layer?.masksToBounds = true

        let title = NSTextField(labelWithString: live ? "Visionary is using the mouse"
                                                     : "Visionary needs the screen")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.frame = NSRect(x: 16, y: h - 34, width: w - 32, height: 18)
        let label = NSTextField(labelWithString: live
            ? "It will be handed back in a moment"
            : "About to take the screen and mouse")
        label.font = .systemFont(ofSize: 12)
        label.textColor = .secondaryLabelColor
        label.frame = NSRect(x: 16, y: h - 56, width: w - 32, height: 16)

        var controls: [NSView] = [title, label]
        if live {
            // Mid-takeover the only useful action is to END it. Screen Control off already
            // calls orchestrator.reclaim_screen(), which aborts the Resolve pass within
            // ~5 s — proven machinery, not a second cancellation path.
            let stop = FirstMouseButton(title: "Give it back", target: self,
                                        action: #selector(takeoverDefer))
            stop.bezelStyle = .rounded
            stop.frame = NSRect(x: w - 146, y: 12, width: 130, height: 24)
            controls.append(stop)
        } else {
            // No "Go ahead": with no countdown there is nothing to wave through. The only
            // useful action is still to push it away.
            let later = FirstMouseButton(title: "Not now (30m)", target: self,
                                         action: #selector(takeoverDefer))
            later.bezelStyle = .rounded
            later.frame = NSRect(x: w - 146, y: 12, width: 130, height: 24)
            controls.append(later)
        }

        for v in controls { bg.addSubview(v) }
        panel.contentView = bg
        if let sf = NSScreen.main?.frame {
            panel.setFrameOrigin(NSPoint(x: (sf.midX - w / 2).rounded(),
                                         y: (sf.maxY - h - 60).rounded()))
        }
        panel.orderFrontRegardless()
        takeoverPanel = panel
        takeoverLabel = label
        // Tick locally every second — the poll is 1.5 s, so a poll-driven countdown would
        // visibly stutter and skip numbers.
    }

    @objc private func takeoverDefer() {
        // Reuses Screen Control's proven deferral: the engine owns the deadline, so the
        // pause lifts by itself even if the app never reopens.
        hideTakeoverWarning()
        Task { await store.pauseScreenControl(seconds: 1800) }
    }

    /// Halfway between the two centres worth having. LITERAL centres on the whole screen
    /// (`frame`) — equal glass top and bottom, but it reads slightly low because the menu bar
    /// eats the top. USABLE centres on `visibleFrame`, which excludes the menu bar and the
    /// Dock — optically balanced, but measurably above true centre. The midpoint of the two
    /// origins splits the difference. Horizontally they agree, so x is unaffected.
    /// (NSWindow.center() is neither: it seats the window a third of the slack from the top.)
    static func centreOnScreen(_ w: NSWindow) {
        guard let sc = w.screen ?? NSScreen.main else { return }
        let f = w.frame
        let literal = NSPoint(x: sc.frame.midX - f.width / 2,
                              y: sc.frame.midY - f.height / 2)
        let usable = NSPoint(x: sc.visibleFrame.midX - f.width / 2,
                             y: sc.visibleFrame.midY - f.height / 2)
        w.setFrameOrigin(NSPoint(x: ((literal.x + usable.x) / 2).rounded(),
                                 y: ((literal.y + usable.y) / 2).rounded()))
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
