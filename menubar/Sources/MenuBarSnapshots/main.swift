import AppKit
import FakeControlServerKit
import SwiftUI
import VoicedControl
import VoicedMenuBarUI

// Renders the status window and the settings window in each state, in light and dark, to PNG
// files. Pull requests that change the UI attach these. Every state is driven through the
// real client against the fake server, so the pictures show what the app shows.
//
//     swift run MenuBarSnapshots [output directory]

let output = URL(
    fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "snapshots", isDirectory: true)
try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

enum Window { case status, settings }

struct Shot {
    var name: String
    var scenario: FakeScenario?
    var installed = true
    var window = Window.status
    var edits: [SettingsEdit] = []
    var options = FakeControlServer.Options()
    /// The Settings pane to render.
    var pane = SettingsPane.general
}

var overriding = FakeControlServer.Options()
overriding.environmentOverrides = [.ttsVoice]

let shots = [
    Shot(name: "running", scenario: .running),
    Shot(name: "starting", scenario: .starting),
    Shot(name: "degraded", scenario: .degraded),
    Shot(name: "stopped", scenario: .stopped),
    Shot(name: "watchdog-without-install", scenario: .notInstalled),
    Shot(name: "not-installed", scenario: nil, installed: false),
    Shot(name: "not-running", scenario: nil, installed: true),
    Shot(name: "newer-protocol", scenario: .newerProtocol),
    Shot(name: "settings-general", scenario: .running, window: .settings, pane: .general),
    Shot(name: "settings-network", scenario: .running, window: .settings, pane: .network),
    Shot(name: "settings-stt", scenario: .running, window: .settings, pane: .speechToText),
    Shot(name: "settings-tts", scenario: .running, window: .settings, pane: .textToSpeech),
    Shot(name: "settings-about", scenario: .running, window: .settings, pane: .about),
    Shot(name: "settings-not-connected", scenario: nil, window: .settings, pane: .textToSpeech),
    Shot(name: "lan-without-key", scenario: .running, edits: [.clearAPIKey, .bind(.lan)]),
    Shot(name: "lan-without-key-settings", scenario: .running, window: .settings, edits: [.clearAPIKey, .bind(.lan)], pane: .network),
    // The key flow: none set on the LAN, then set (Remove and Replace appear, the warning goes).
    Shot(name: "key-1-not-set", scenario: .lanWithoutKey, window: .settings, pane: .network),
    Shot(name: "key-2-set", scenario: .lanWithoutKey, window: .settings, edits: [.apiKey("example-key")], pane: .network),
    Shot(name: "env-override-settings", scenario: .running, window: .settings, edits: [.ttsVoice("bf_emma")], options: overriding, pane: .textToSpeech),
]

/// Waits for the model to settle; the fake server answers in milliseconds.
@MainActor
func settle(_ model: AppModel) async {
    for _ in 0..<100 {
        switch model.connection {
        case .connected where model.status != nil && model.settings != nil && model.engines != nil: return
        case .unreachable, .incompatible: return
        default: try? await Task.sleep(for: .milliseconds(20))
        }
    }
}

@MainActor
func render<Content: View>(_ view: Content, dark: Bool, to url: URL) async {
    let host = NSHostingView(rootView: view.background(Color(nsColor: .windowBackgroundColor)))
    let window = NSWindow(contentRect: .zero, styleMask: [.borderless], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    window.contentView = host
    host.setFrameSize(host.fittingSize)
    window.setContentSize(host.fittingSize)
    // Let AppKit lay out and draw the hosted controls before capturing. A split view fills
    // its columns a few run loop turns later, so wait until two captures in a row agree.
    var previous: Data?
    for _ in 0..<20 {
        try? await Task.sleep(for: .milliseconds(150))
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        let png = rep.representation(using: .png, properties: [:])
        if let png, png == previous {
            try? png.write(to: url)
            return
        }
        previous = png
    }
    try? previous?.write(to: url)
}

/// A stand-in /Library/LaunchDaemons holding a watchdog plist for this user, or nothing.
func launchDaemons(in scratch: URL, installed: Bool) -> URL {
    let directory = scratch.appendingPathComponent(installed ? "installed" : "empty", isDirectory: true)
    try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    if installed {
        let plist: [String: Any] = ["ProgramArguments": ["/usr/local/bin/voiced", "watchdog"], "UserName": NSUserName()]
        let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist").path,
            contents: data)
    }
    return directory
}

@MainActor
func run() async {
    let scratch = URL(fileURLWithPath: "/tmp/voiced-snapshots-\(getpid())", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: scratch) }
    try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)

    for shot in shots {
        let socket = scratch.appendingPathComponent("\(shot.name.prefix(8)).sock").path
        var server: FakeControlServer?
        if let scenario = shot.scenario {
            server = FakeControlServer(socketPath: socket, scenario: scenario, options: shot.options)
            try? server?.start()
        }
        let model = AppModel(
            socketPath: socket,
            installProbe: InstallProbe(launchDaemonsDirectory: launchDaemons(in: scratch, installed: shot.installed)),
            opener: { _ in true })
        model.start()
        await settle(model)
        for edit in shot.edits { await model.apply(edit) }
        for dark in [false, true] {
            let file = output.appendingPathComponent("\(shot.name)-\(dark ? "dark" : "light").png")
            switch shot.window {
            case .status: await render(PopoverView(model: model), dark: dark, to: file)
            case .settings:
                await render(SettingsView(model: model, loginItem: LoginItem(), pane: shot.pane), dark: dark, to: file)
            }
            print(file.path)
        }
        model.stop()
        server?.stop()
    }

    // The three menu bar icons side by side.
    let icons = HStack(spacing: 18) {
        ForEach([OverallHealth.running, .attention, .off], id: \.symbolName) { health in
            VStack(spacing: 6) {
                Image(systemName: health.symbolName).font(.system(size: 16))
                Text(String(describing: health)).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    .padding(14)
    for dark in [false, true] {
        let file = output.appendingPathComponent("icons-\(dark ? "dark" : "light").png")
        await render(icons, dark: dark, to: file)
        print(file.path)
    }
}

NSApplication.shared.setActivationPolicy(.prohibited)
Task { @MainActor in
    await run()
    exit(0)
}
NSApplication.shared.run()
