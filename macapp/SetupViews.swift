import SwiftUI
import AppKit

// The in-app onboarding: every README setup step as a live row inside Settings —
// connection fields (writing ~/.topaz-pipeline/config.json through the redacted
// /api/config), one-click dependency installs with live output, version/display
// checks, TCC grant buttons, the Resolve project import, and the optional NAS
// helper. Auto-expanded until every preflight check passes, collapsed after.

private struct SetupGroupLabel: View {
    let text: String
    var body: some View {
        Text(text.uppercased()).font(.system(size: 10, weight: .semibold))
            .tracking(1.1).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 6)
    }
}

private struct CheckDot: View {
    let ok: Bool?
    var body: some View {
        Circle().fill(ok == true ? Color.green.opacity(0.75)
                      : ok == false ? DS.fault : Color.secondary.opacity(0.4))
            .frame(width: 7, height: 7)
    }
}

/// Monospaced value with a copy button — the pattern YouTubeSetupPanel established.
struct CopyValueRow: View {
    let value: String
    var body: some View {
        HStack(spacing: 6) {
            Text(value).font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary).lineLimit(2).textSelection(.enabled)
            Spacer(minLength: 4)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(value, forType: .string)
            } label: { Image(systemName: "doc.on.doc").font(.system(size: 10)) }
                .buttonStyle(.plain).help("Copy")
        }
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.white.opacity(0.05)))
    }
}

struct SetupSection: View {
    @EnvironmentObject var store: AppStore
    @State private var expanded = false
    @State private var seeded = false

    private var complete: Bool { store.preflight?.setup_complete ?? false }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            DisclosureGroup(isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 10) {
                    // REQUIRED first (user-dictated), optional integrations last.
                    NASGroup()
                    DependenciesGroup()
                    ApplicationsGroup()
                    PermissionsGroup()
                    ResolveProjectsGroup()
                    BorderExtenderGroup()
                    OptionalConnectionsGroup()
                    NASExtrasGroup()
                    HStack {
                        Button("Recheck everything") { Task { await store.recheckSetup() } }
                            .buttonStyle(SteelButtonStyle(lit: false))
                        Spacer()
                    }.padding(.top, 2)
                }.padding(.top, 8)
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: complete ? "checkmark.seal" : "wrench.and.screwdriver")
                        .font(.system(size: 12))
                        .foregroundStyle(complete ? Color.green.opacity(0.8) : DS.steelBright)
                    Text("Setup").font(.system(size: 13, weight: .semibold))
                    Spacer()
                    Text(complete ? "complete" : "needs attention")
                        .font(.system(size: 11))
                        .foregroundStyle(complete ? .secondary : DS.steelBright)
                }.contentShape(Rectangle())
            }
        }
        .task {
            await store.fetchSetup()
            if !seeded { expanded = !complete; seeded = true }   // open until healthy
        }
        .onChange(of: complete) { done in
            if done { expanded = false }                          // collapse on the green flip
        }
    }
}

// MARK: - connections (the config.json fields)
// One parameterized struct, instantiated twice: the REQUIRED NAS group sits at the top
// of the section, the optional integrations (Plex/TMDb/youtarr/relay) at the bottom
// (user-dictated ordering). The field/save/test machinery is shared.

private struct NASGroup: View {
    var body: some View { ConnectionsGroup(optional: false) }
}

private struct OptionalConnectionsGroup: View {
    var body: some View { ConnectionsGroup(optional: true) }
}

private struct ConnectionsGroup: View {
    let optional: Bool
    @EnvironmentObject var store: AppStore
    // field state seeded from the REDACTED config view; secrets always arrive "" and
    // show a "saved" placeholder instead (typing replaces; explicit clear sends "")
    @State private var f: [String: String] = [:]
    @State private var seeded = false
    @State private var tests: [String: ConfigTestDTO] = [:]
    @State private var testing: String? = nil

    private func secretSaved(_ key: String) -> Bool {
        store.configDTO?.secrets_set?[key] ?? false
    }

    private func field(_ label: String, _ key: String, secret: Bool = false,
                       hint: String = "") -> some View {
        HStack(spacing: 8) {
            Text(label).font(.system(size: 12)).frame(width: 92, alignment: .leading)
            Group {
                if secret {
                    SecureField(secretSaved(key) ? "•••• saved" : hint,
                                text: Binding(get: { f[key] ?? "" },
                                              set: { f[key] = $0 }))
                } else {
                    TextField(hint, text: Binding(get: { f[key] ?? "" },
                                                  set: { f[key] = $0 }))
                }
            }
            .textFieldStyle(.roundedBorder).font(.system(size: 12))
            if secret && secretSaved(key) {
                Button {
                    f[key] = ""
                    Task { await store.saveConfig([key: ""]) }   // explicit clear
                } label: { Image(systemName: "xmark.circle").font(.system(size: 10)) }
                    .buttonStyle(.plain).help("Clear the saved value")
            }
        }
    }

    private func saveTest(_ title: String, keys: [String], what: String?) -> some View {
        HStack(spacing: 8) {
            Button("Save \(title)") {
                var body: [String: Any] = [:]
                for k in keys {
                    // secrets: only send when the user actually typed something (an
                    // empty secret field means "keep what's saved", never "clear")
                    let v = f[k] ?? ""
                    let isSecret = store.configDTO?.secrets_set?.keys.contains(k) ?? false
                    if isSecret && v.isEmpty { continue }
                    body[k] = v
                }
                Task { await store.saveConfig(body) }
            }.buttonStyle(SteelButtonStyle(lit: true))
            if let what {
                Button {
                    testing = what
                    Task {
                        tests[what] = await store.testConfig(what)
                        testing = nil
                        if what == "ftp", tests[what]?.ok == true {
                            await store.autoConnect()   // NAS works → find its services
                        }
                    }
                } label: {
                    if testing == what { ProgressView().controlSize(.small) }
                    else { Text("Test") }
                }.buttonStyle(SteelButtonStyle(lit: false))
                if let t = tests[what] {
                    HStack(spacing: 5) {
                        CheckDot(ok: t.ok)
                        Text(t.detail ?? "").font(.system(size: 11))
                            .foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            Spacer()
        }
    }

    private var ftpConfigured: Bool {
        !(store.configDTO?.fields?["ftp_hosts"] ?? "").isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if !optional {
                SetupGroupLabel(text: "NAS connection")
                Text("Required — everything talks to the NAS over FTP.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                field("NAS hosts", "ftp_hosts", hint: "100.x.y.z, nas.local")
                field("FTP port", "ftp_port", hint: "21")
                field("FTP user", "ftp_user")
                field("FTP password", "ftp_pass", secret: true)
                saveTest("NAS", keys: ["ftp_hosts", "ftp_port", "ftp_user", "ftp_pass"],
                         what: "ftp")
            } else {
                SetupGroupLabel(text: "Optional integrations")
                Text("All optional — each degrades gracefully when unset.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                if let found = store.autoConnected?.found {
                    // auto-identified the moment the NAS connection works — Plex's
                    // /identity and the relay's /healthz answer tokenless, youtarr by
                    // presence; the relay even verifies Shuttle's local token, so a
                    // found relay is already fully connected
                    ForEach(["plex", "youtarr", "relay"], id: \.self) { svc in
                        if let d = found[svc] {
                            HStack(spacing: 6) {
                                CheckDot(ok: d.ok == true ? true : nil)   // a miss is neutral
                                Text(d.detail ?? "").font(.system(size: 11))
                                    .foregroundStyle(.secondary).lineLimit(1)
                            }
                        }
                    }
                }
                field("Plex URL", "plex_url", hint: "auto-detected once the NAS connects")
                field("Plex token", "plex_token", secret: true)
                saveTest("Plex", keys: ["plex_url", "plex_token"], what: "plex")
                field("TMDb key", "tmdb_api_key", secret: true, hint: "optional — smart presets")
                saveTest("TMDb", keys: ["tmdb_api_key"], what: "tmdb")
                field("youtarr URL", "youtarr_url", hint: "http://nas:3087 (optional)")
                field("youtarr user", "youtarr_user")
                field("youtarr pass", "youtarr_pass", secret: true)
                saveTest("youtarr", keys: ["youtarr_url", "youtarr_user", "youtarr_pass"],
                         what: "youtarr")
                field("Shuttle relay", "shuttle_relay_url", hint: "http://nas:8789 (optional)")
                saveTest("relay", keys: ["shuttle_relay_url"], what: "relay")
            }
        }
        .onChange(of: store.configDTO?.fields) { fields in
            guard !seeded, let fields else { return }
            for (k, v) in fields where !(f[k]?.isEmpty == false) { f[k] = v }
            seeded = true
        }
        .onChange(of: store.autoConnected?.found) { found in
            // the STORE auto-saved found URLs into empty config keys; mirror them into
            // the visible fields here
            guard optional, let found else { return }
            for (svc, key) in [("plex", "plex_url"), ("youtarr", "youtarr_url"),
                               ("relay", "shuttle_relay_url")] {
                if let url = found[svc]?.url, !url.isEmpty, (f[key] ?? "").isEmpty {
                    f[key] = url
                }
            }
        }
        .task {
            // sweep on open once the NAS is configured (cheap: parallel 3 s probes)
            if optional, ftpConfigured, store.autoConnected == nil {
                await store.autoConnect()
            }
        }
    }
}

// MARK: - dependencies (one-click installs)

private struct DependenciesGroup: View {
    @EnvironmentObject var store: AppStore

    private func job(_ what: String) -> InstallJobDTO? { store.installStatus?.jobs?[what] }
    private func running(_ what: String) -> Bool {
        store.installStatus?.active == what && job(what)?.state == "running"
    }

    private func row(_ title: String, check id: String, install what: String) -> some View {
        let c = store.preflight?[check: id]
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                CheckDot(ok: c?.ok)
                Text(title).font(.system(size: 12, weight: .medium))
                Spacer()
                if c?.ok != true {
                    Button {
                        Task { await store.startInstall(what) }
                    } label: {
                        if running(what) { ProgressView().controlSize(.small) }
                        else { Text("Install") }
                    }.buttonStyle(SteelButtonStyle(lit: true))
                        .disabled(store.installStatus?.active != nil && !running(what))
                }
            }
            if let d = c?.detail, !d.isEmpty {
                Text(d).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            }
            if let j = job(what), running(what) || j.state == "failed" {
                // the live tail (or the failure's last lines) — the job IS the feedback
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(Array((j.tail ?? []).suffix(8).enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(j.state == "failed" ? DS.fault : .secondary)
                            .lineLimit(1)
                    }
                }
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.25)))
            }
            if c?.ok != true, let fix = c?.fix, !fix.isEmpty {
                CopyValueRow(value: fix)
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SetupGroupLabel(text: "Dependencies")
            if store.preflight?.brew_present == false {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        CheckDot(ok: false)
                        Text("Homebrew").font(.system(size: 12, weight: .medium))
                    }
                    Text("Its installer needs your admin password, so run it in Terminal "
                         + "first — the installs below unlock once it exists.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    CopyValueRow(value: "/bin/bash -c \"$(curl -fsSL https://raw."
                                 + "githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
                }
            }
            row("Command-line tools", check: "brew_tools", install: "brew_tools")
            row("Python OpenCV", check: "python_deps", install: "python_deps")
        }
    }
}

// MARK: - border extender (optional)
// AI border extension (4:3 -> 16:9): Comfy Desktop detection + the four WAN model
// downloads (engine-computed curl argv into Comfy's own models dir; a truncated file
// resumes in place). Optional — never part of setup_complete; the per-show
// "Extend borders" row only appears once everything here is green.
private struct BorderExtenderGroup: View {
    @EnvironmentObject var store: AppStore

    private var st: BordersStatusDTO? { store.bordersStatus }
    private func job(_ what: String) -> InstallJobDTO? { store.installStatus?.jobs?[what] }
    private func running(_ what: String) -> Bool {
        store.installStatus?.active == what && job(what)?.state == "running"
    }
    private static func size(_ b: Int64?) -> String {
        let v = Double(b ?? 0)
        return v >= 1_000_000_000 ? String(format: "%.1f GB", v / 1_073_741_824.0)
                                  : String(format: "%.0f MB", v / 1_048_576.0)
    }

    private func modelRow(_ what: String) -> some View {
        let m = st?.models?[what]
        let state = m?.state ?? "missing"
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                CheckDot(ok: state == "ok")
                Text(m?.label ?? what).font(.system(size: 12, weight: .medium))
                Text(state == "ok" ? Self.size(m?.bytes)
                     : state == "truncated" ? Self.size(m?.bytes) + " of " + Self.size(m?.expected)
                     : Self.size(m?.expected))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                if state != "ok" {
                    Button {
                        Task { await store.startInstall(what) }
                    } label: {
                        if running(what) { ProgressView().controlSize(.small) }
                        else { Text(state == "truncated" ? "Resume" : "Install") }
                    }.buttonStyle(SteelButtonStyle(lit: true))
                        .disabled(store.installStatus?.active != nil && !running(what))
                }
            }
            if let j = job(what), j.state == "failed" {
                VStack(alignment: .leading, spacing: 1) {
                    ForEach(Array((j.tail ?? []).suffix(4).enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(DS.fault).lineLimit(1)
                    }
                }
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.25)))
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SetupGroupLabel(text: "Border extender (optional)")
            Text("AI-outpaints 4:3 shows to 16:9 before the upscale (per-show opt-in \u{2014} "
                 + "the option appears on 4:3 shows once this is installed). Drives Comfy "
                 + "Desktop's own ComfyUI headlessly on port \(st?.env?.port ?? 8189).")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                CheckDot(ok: st?.env?.ok)
                Text("Comfy Desktop").font(.system(size: 12, weight: .medium))
                if st?.env?.ok == true {
                    Text("ComfyUI \(st?.env?.comfy_version ?? "?")"
                         + ((st?.env?.desktop_version).map { " \u{00B7} app \($0)" } ?? ""))
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            if st?.env?.ok != true {
                Text((st?.env?.missing ?? st?.missing ?? ["Open Setup after installing Comfy Desktop."])
                        .joined(separator: " \u{00B7} "))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if st?.env?.ok == true {
                modelRow("borders_vace")
                modelRow("borders_umt5")
                modelRow("borders_causvid")
                modelRow("borders_vae")
            }
        }
        .task { await store.fetchBordersStatus() }
    }
}

// MARK: - applications (detected, not installable from here)

private struct ApplicationsGroup: View {
    @EnvironmentObject var store: AppStore

    private func row(_ title: String, _ id: String) -> some View {
        let c = store.preflight?[check: id]
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 8) {
                CheckDot(ok: c?.ok)
                Text(title).font(.system(size: 12, weight: .medium))
                Spacer()
            }
            if let d = c?.detail, !d.isEmpty {
                Text(d).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
            }
            if c?.ok == false, let fix = c?.fix, !fix.isEmpty {
                Text(fix).font(.system(size: 11)).foregroundStyle(DS.steelBright).lineLimit(4)
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SetupGroupLabel(text: "Applications & hardware")
            row("DaVinci Resolve Studio", "resolve_version")
            row("Topaz Video AI", "topaz_version")
            row("Display", "display")
            row("Power", "power_adapter")
        }
    }
}

// MARK: - permissions (TCC)

private struct PermissionsGroup: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let t = store.selftest
        VStack(alignment: .leading, spacing: 8) {
            SetupGroupLabel(text: "Permissions")
            HStack(spacing: 8) {
                CheckDot(ok: t?.screen_recording)
                Text("Screen Recording").font(.system(size: 12, weight: .medium))
                Spacer()
                Button("Request") { Task { await store.requestScreenRecording() } }
                    .buttonStyle(SteelButtonStyle(lit: t?.screen_recording != true))
                Button("Open Settings") {
                    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:"
                        + "com.apple.preference.security?Privacy_ScreenCapture")!)
                }.buttonStyle(SteelButtonStyle(lit: false))
            }
            HStack(spacing: 8) {
                CheckDot(ok: t?.accessibility)
                Text("Accessibility").font(.system(size: 12, weight: .medium))
                Spacer()
                Button("Request") { Task { await store.requestAccessibility() } }
                    .buttonStyle(SteelButtonStyle(lit: t?.accessibility != true))
            }
            Text("macOS shows each prompt once; relaunch Visionary after granting.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

// MARK: - resolve projects (the bundled import)

private struct ResolveProjectsGroup: View {
    @EnvironmentObject var store: AppStore
    var body: some View {
        let c = store.preflight?[check: "resolve_artifacts"]
        let j = store.installStatus?.jobs?["import_resolve"]
        let running = store.installStatus?.active == "import_resolve"
        VStack(alignment: .leading, spacing: 6) {
            SetupGroupLabel(text: "Resolve projects & DV preset")
            HStack(spacing: 8) {
                CheckDot(ok: c?.ok)
                Text(c?.detail ?? "not checked yet").font(.system(size: 11))
                    .foregroundStyle(.secondary).lineLimit(2)
                Spacer()
                Button {
                    Task { await store.importResolve() }
                } label: {
                    if running { ProgressView().controlSize(.small) }
                    else { Text("Import projects & preset") }
                }.buttonStyle(SteelButtonStyle(lit: c?.ok != true))
                    .disabled(store.installStatus?.active != nil && !running)
            }
            Text("Quit DaVinci Resolve first — it rewrites the preset file on exit.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            if let steps = j?.steps {
                ForEach(steps) { s in
                    HStack(spacing: 6) {
                        CheckDot(ok: s.ok)
                        Text("\(s.step ?? "?"): \(s.detail ?? "")")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            } else if j?.state == "failed" {
                Text((j?.tail ?? []).suffix(3).joined(separator: " · "))
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(DS.fault)
            }
        }
    }
}

// MARK: - NAS extras (optional helper)

private struct NASExtrasGroup: View {
    @EnvironmentObject var store: AppStore
    @State private var result: DvProbeDTO? = nil
    @State private var uploading = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SetupGroupLabel(text: "NAS extras (optional)")
            HStack(spacing: 8) {
                Text("DV probe — precise Dolby Vision detection for files you didn't "
                     + "make with Visionary. Without it, filename marks cover the rest.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button {
                    uploading = true
                    Task { result = await store.uploadDvProbe(); uploading = false }
                } label: {
                    if uploading { ProgressView().controlSize(.small) }
                    else { Text("Upload to NAS") }
                }.buttonStyle(SteelButtonStyle(lit: false))
            }
            if let r = result {
                if r.ok == true {
                    Text("Uploaded to \(r.uploaded_to ?? "") — add this line to the NAS "
                         + "task scheduler (cron):")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    CopyValueRow(value: r.cron ?? "")
                } else {
                    HStack(spacing: 6) {
                        CheckDot(ok: false)
                        Text(r.detail ?? "upload failed").font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}
