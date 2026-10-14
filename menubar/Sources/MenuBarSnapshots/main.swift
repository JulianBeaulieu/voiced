import AppKit
import FakeControlServerKit
import SwiftUI
import VoicedControl
import VoicedMenuBarUI

// Renders the popover in each state, in light and dark, to PNG files, for documentation and
// pull requests. Every state is driven through the real client against the fake server.
//
//     swift run MenuBarSnapshots [output directory]

let output = URL(
    fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "snapshots", isDirectory: true)
try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

struct Shot {
    var name: String
    var scenario: FakeScenario?
    var installed = true
    var tab: AppModel.Tab = .status
    var edits: [SettingsEdit] = []
}

let shots = [
    Shot(name: "running", scenario: .running),
    Shot(name: "starting", scenario: .starting),
    Shot(name: "degraded", scenario: .degraded),
    Shot(name: "stopped", scenario: .stopped),
    Shot(name: "not-installed", scenario: nil, installed: false),
    Shot(name: "not-running", scenario: nil, installed: true),
    Shot(name: "newer-protocol", scenario: .newerProtocol),
    Shot(name: "settings", scenario: .running, tab: .settings),
    Shot(name: "lan-without-key", scenario: .running, edits: [.clearAPIKey, .bind(.lan)]),
    Shot(name: "lan-without-key-settings", scenario: .lanWithoutKey, tab: .settings),
]

@MainActor
func waitUntil(_ condition: () -> Bool) async {
    for _ in 0..<60 where !condition() {
        try? await Task.sleep(for: .milliseconds(50))
    }
}

@MainActor
func render<Content: View>(_ view: Content, dark: Bool, to url: URL) {
    let host = NSHostingView(
        rootView: view.background(Color(nsColor: .windowBackgroundColor)))
    let window = NSWindow(
        contentRect: .zero, styleMask: [.borderless], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    window.contentView = host
    host.layoutSubtreeIfNeeded()
    host.setFrameSize(host.fittingSize)
    window.setContentSize(host.fittingSize)
    RunLoop.main.run(until: Date().addingTimeInterval(0.3))
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
    host.cacheDisplay(in: host.bounds, to: rep)
    try? rep.representation(using: .png, properties: [:])?.write(to: url)
}

@MainActor
func run() async {
    let scratch = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(
        "vs-\(getpid())", isDirectory: true)
    let daemons = scratch.appendingPathComponent("LaunchDaemons", isDirectory: true)
    try? FileManager.default.createDirectory(at: daemons, withIntermediateDirectories: true)

    for shot in shots {
        let socket = scratch.appendingPathComponent("\(shot.name.prefix(8)).sock").path
        var server: FakeControlServer?
        if let scenario = shot.scenario {
            server = FakeControlServer(socketPath: socket, scenario: scenario)
            try? server?.start()
        }
        // The not-running state needs an installed voiced: a watchdog plist in a stand-in
        // LaunchDaemons directory.
        let plist = daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist")
        if shot.installed {
            FileManager.default.createFile(atPath: plist.path, contents: Data())
        } else {
            try? FileManager.default.removeItem(at: plist)
        }
        let model = AppModel(
            socketPath: socket, installProbe: InstallProbe(launchDaemonsDirectory: daemons),
            opener: { _ in true })
        model.start()
        await waitUntil {
            switch model.connection {
            case .connecting: false
            case .connected: model.status != nil && model.settings != nil && model.engines != nil
            case .unreachable, .incompatible: true
            }
        }
        for edit in shot.edits { await model.apply(edit) }
        model.tab = shot.tab
        // Tall enough to show the whole settings form in one picture.
        let view = PopoverView(model: model, loginItem: LoginItem(), settingsHeight: 760)
        for dark in [false, true] {
            let file = output.appendingPathComponent("\(shot.name)-\(dark ? "dark" : "light").png")
            render(view, dark: dark, to: file)
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
        render(icons, dark: dark, to: file)
        print(file.path)
    }
    try? FileManager.default.removeItem(at: scratch)
}

NSApplication.shared.setActivationPolicy(.prohibited)
Task { @MainActor in
    await run()
    exit(0)
}
NSApplication.shared.run()
