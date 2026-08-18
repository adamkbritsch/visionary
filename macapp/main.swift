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
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         NSMenuDelegate, NSMenuItemValidation {
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
        // ONE computation, shared with the header readout (PipelineCard.unifiedProgress) —
        // user-dictated: the header icon + percentage ARE this bar, so the numbers must
        // match. The rule is this bar's original one: the finisher wins whenever it has a
        // number — a lane suspended for Resolve keeps its frozen % up, as it always did —
        // in display order (a lane-2-only remux now shows instead of nothing), else the
        // run-thread stage's live pct. Plain icon when idle.
        let pct: Double? = PipelineCard.unifiedProgress(s)?.pct
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

    // ---- menu bar -------------------------------------------------------
    // Hand-built AppKit menus: there is no SwiftUI `Scene`/`.commands` here (the app boots
    // through NSApplication.run() at the bottom of this file), and AppDelegate already holds
    // the one AppStore, so menu items reach every action directly.
    //
    // The EDIT MENU IS LOAD-BEARING, not decoration: an app that installs its own
    // NSApp.mainMenu dispatches ⌘X/⌘C/⌘V/⌘A/⌘Z through the main menu's key equivalents, so
    // without these items PASTE DOES NOT WORK IN ANY TEXT FIELD (user-caught 2026-08-18 —
    // the YouTube "paste a link" box is paste-first by design). Their target stays nil so
    // AppKit routes them down the responder chain to the focused field editor; giving them a
    // target would silently break them again.

    // Items whose title/state follow the live pipeline (refreshed in menuNeedsUpdate).
    private var runToggleItem: NSMenuItem?
    private var skipVideoItem: NSMenuItem?
    private var screenResumeItem: NSMenuItem?
    private var modeItems: [String: NSMenuItem] = [:]

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String = "",
                     _ mods: NSEvent.ModifierFlags = [.command],
                     target: AnyObject? = nil, tag: Int = 0) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        if !key.isEmpty { it.keyEquivalentModifierMask = mods }
        it.target = target                     // nil = responder chain (Edit, Hide, window ops)
        it.tag = tag
        menu.addItem(it)
        return it
    }

    private func submenu(_ menu: NSMenu, in parent: NSMenu) {
        let item = NSMenuItem(title: menu.title, action: nil, keyEquivalent: "")
        item.submenu = menu
        parent.addItem(item)
    }

    func buildMenu() {
        let main = NSMenu()
        let app = appMenu(), edit = editMenu(), view = viewMenu()
        let run = runMenu(), win = windowMenu(), help = helpMenu()
        for m in [app, edit, view, run, win, help] { submenu(m, in: main) }

        // Live menus ask us for fresh titles as they open — far cheaper than redrawing them
        // from the 1.5 s poll sink, which would churn menus nobody is looking at.
        view.delegate = self
        run.delegate = self

        NSApp.mainMenu = main
        NSApp.windowsMenu = win
        NSApp.helpMenu = help
    }

    private func appMenu() -> NSMenu {
        let m = NSMenu(title: "Visionary")
        m.addItem(withTitle: "About Visionary",
                  action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        m.addItem(.separator())
        add(m, "Settings\u{2026}", #selector(openSettings), ",", target: self)
        add(m, "Setup\u{2026}", #selector(openSetup), ",", [.command, .shift], target: self)
        m.addItem(.separator())
        let services = NSMenu(title: "Services")
        submenu(services, in: m)
        NSApp.servicesMenu = services
        m.addItem(.separator())
        add(m, "Hide Visionary", #selector(NSApplication.hide(_:)), "h")
        add(m, "Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        add(m, "Show All", #selector(NSApplication.unhideAllApplications(_:)))
        m.addItem(.separator())
        // ⌘Q is deliberately NOT quit — applicationShouldTerminate turns a plain user quit
        // into a minimize (a logout/shutdown still passes through). The appliance keeps running.
        add(m, "Close to Dock", #selector(NSApplication.terminate(_:)), "q")
        return m
    }

    private func editMenu() -> NSMenu {
        let m = NSMenu(title: "Edit")
        add(m, "Undo", Selector(("undo:")), "z")            // no public selector for these two
        add(m, "Redo", Selector(("redo:")), "z", [.command, .shift])
        m.addItem(.separator())
        add(m, "Cut", #selector(NSText.cut(_:)), "x")
        add(m, "Copy", #selector(NSText.copy(_:)), "c")
        add(m, "Paste", #selector(NSText.paste(_:)), "v")
        add(m, "Delete", #selector(NSText.delete(_:)))
        m.addItem(.separator())
        add(m, "Select All", #selector(NSText.selectAll(_:)), "a")
        return m
    }

    private func viewMenu() -> NSMenu {
        let m = NSMenu(title: "View")
        // Same order as the in-window ModeNavBar, so ⌘1/2/3 match left-to-right.
        modeItems["tv"] = add(m, "TV Shows", #selector(showTV), "1", target: self)
        modeItems["youtube"] = add(m, "YouTube", #selector(showYouTube), "2", target: self)
        modeItems["movie"] = add(m, "Movies", #selector(showMovies), "3", target: self)
        m.addItem(.separator())
        add(m, "Refresh Now", #selector(refreshNow), "r", [.command, .shift], target: self)
        add(m, "Rescan Plex Libraries", #selector(rescanLibraries), "", target: self)
        return m
    }

    private func runMenu() -> NSMenu {
        let m = NSMenu(title: "Run")
        // Title is replaced with the honest single verb on open (Activate / Deactivate).
        runToggleItem = add(m, "Activate", #selector(toggleRun), "r", target: self)
        m.addItem(.separator())
        skipVideoItem = add(m, "Skip Current Video", #selector(skipCurrentVideo), "", target: self)
        add(m, "Import YouTube Link from Clipboard", #selector(importFromClipboard),
            "v", [.command, .shift], target: self)
        m.addItem(.separator())
        let sc = NSMenu(title: "Screen Control")
        for (label, secs) in [("Pause 30 Minutes", 1800), ("Pause 1 Hour", 3600),
                              ("Pause 2 Hours", 7200), ("Pause 4 Hours", 14400)] {
            add(sc, label, #selector(pauseScreen(_:)), "", target: self, tag: secs)
        }
        sc.addItem(.separator())
        screenResumeItem = add(sc, "Resume Now", #selector(resumeScreen), "", target: self)
        submenu(sc, in: m)
        m.addItem(.separator())
        add(m, "Re-check Setup", #selector(recheckSetupNow), "", target: self)
        return m
    }

    private func windowMenu() -> NSMenu {
        let m = NSMenu(title: "Window")
        add(m, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(m, "Close", #selector(NSWindow.performClose(_:)), "w")     // → minimize, see windowShouldClose
        m.addItem(.separator())
        add(m, "Bring All to Front", #selector(NSApplication.arrangeInFront(_:)))
        return m
    }

    private func helpMenu() -> NSMenu {
        let m = NSMenu(title: "Help")
        add(m, "Open Dashboard in Browser", #selector(openDashboard), "", target: self)
        m.addItem(.separator())
        add(m, "Reveal Log in Finder", #selector(revealLog), "", target: self)
        add(m, "Reveal Scratch Folder in Finder", #selector(revealScratch), "", target: self)
        add(m, "Reveal Config Folder in Finder", #selector(revealConfig), "", target: self)
        return m
    }

    // ---- live menu state -------------------------------------------------

    func menuNeedsUpdate(_ menu: NSMenu) {
        switch menu.title {
        case "View":
            let mode = store.mode
            for (key, item) in modeItems { item.state = (key == mode) ? .on : .off }
        case "Run":
            runToggleItem?.title = store.activated ? "Deactivate" : "Activate"
            if let v = skippableVideo() {
                skipVideoItem?.title = "Skip \u{201C}\(v.label)\u{201D}"
            } else {
                skipVideoItem?.title = "Skip Current Video"
            }
            screenResumeItem?.isHidden = (store.quietUntil == nil)
        default:
            break
        }
    }

    /// The in-flight item IF it is a YouTube video — the only thing the pipeline can skip
    /// (there is no per-item abort for TV or movies; those need Deactivate). Mirrors the
    /// `skippable` rule the Pipeline card uses for its own skip button.
    private func skippableVideo() -> (label: String, name: String, channel: String?)? {
        guard store.state?.orchestrator?.running == true,
              let cur = store.state?.orchestrator?.current,
              cur.kind == "youtube",
              let name = cur.name, !name.isEmpty else { return nil }
        return (cur.title ?? name, name, cur.channel)
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(skipCurrentVideo) { return skippableVideo() != nil }
        if item.action == #selector(resumeScreen) { return store.quietUntil != nil }
        return true
    }

    // ---- menu actions ----------------------------------------------------

    /// Every command that shows something must first bring the window back: the appliance
    /// spends most of its life minimized, and Settings is a POPOVER anchored to the header
    /// gear — setting its flag against a hidden window would do nothing visible.
    private func revealWindow() {
        guard let w = window else { return }
        if w.isMiniaturized { w.deminiaturize(nil) }
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func toggleRun() { Task { await store.toggleAutomation() } }
    @objc func refreshNow() { Task { await store.refresh() } }
    @objc func rescanLibraries() { Task { await store.refreshLibrary() } }
    @objc func recheckSetupNow() { revealWindow(); Task { await store.recheckSetup() } }

    @objc func showTV() { switchMode("tv") }
    @objc func showYouTube() { switchMode("youtube") }
    @objc func showMovies() { switchMode("movie") }
    private func switchMode(_ m: String) { revealWindow(); Task { await store.setMode(m) } }

    @objc func openSettings() { revealWindow(); store.showSettings = true }
    @objc func openSetup() { revealWindow(); store.revealSetup = true; store.showSettings = true }

    @objc func pauseScreen(_ sender: NSMenuItem) {
        Task { await store.pauseScreenControl(seconds: sender.tag) }
    }
    @objc func resumeScreen() { Task { await store.resumeScreenControl() } }

    /// Deletes the download and tells youtarr to forget it, so it confirms first — the
    /// in-window button already does, and a menu item must not be the one unguarded path
    /// to a destructive action.
    @objc func skipCurrentVideo() {
        guard let v = skippableVideo() else { NSSound.beep(); return }
        revealWindow()
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = "Skip this video?"
        a.informativeText = "\u{201C}\(v.label)\u{201D} will be deleted from staging, and youtarr "
            + "will be told not to download it again."
        a.addButton(withTitle: "Skip and Delete")
        a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        Task { await store.deleteYoutubeVideo(channel: v.channel, name: v.name) }
    }

    /// The one genuinely menu-native command: copy a link in a browser, ⇧⌘V, done. It hands
    /// the URL to the YouTube tab's existing import field rather than building a second
    /// import path, so the resolve → confirm flow is exactly the one already shipped.
    @objc func importFromClipboard() {
        let text = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else { NSSound.beep(); return }
        revealWindow()
        store.pendingImportURL = text
        Task { await store.setMode("youtube") }
    }

    @objc func openDashboard() {
        if let u = URL(string: "http://127.0.0.1:8765") { NSWorkspace.shared.open(u) }
    }
    @objc func revealLog() { reveal("~/.topaz-pipeline/logs/upscaler.log") }
    @objc func revealConfig() { reveal("~/.topaz-pipeline") }
    @objc func revealScratch() {
        reveal(store.state?.scratch?.path ?? "~/topaz-scratch")
    }

    /// Select the path in Finder, falling back to its parent folder so the item never
    /// silently does nothing when a file hasn't been created yet.
    private func reveal(_ path: String) {
        let p = NSString(string: path).expandingTildeInPath
        let fm = FileManager.default
        var isDir: ObjCBool = false
        if fm.fileExists(atPath: p, isDirectory: &isDir) {
            if isDir.boolValue {
                NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: p)
            } else {
                NSWorkspace.shared.selectFile(p, inFileViewerRootedAtPath: "")
            }
            return
        }
        let parent = (p as NSString).deletingLastPathComponent
        if fm.fileExists(atPath: parent) {
            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: parent)
        } else {
            NSSound.beep()
        }
    }

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
