import AppKit
import FakeControlServerKit
import SwiftUI
import TestSupport
import VoicedControl
import VoicedMenuBarUI

// Renders the status window, the settings window and the installer in each state, in light and dark, to PNG
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
    /// The bundle carries the installer, so "not installed" offers it.
    var canInstall = false
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
    Shot(name: "not-installed-installer", scenario: nil, installed: false, canInstall: true),
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
            case .status: await render(PopoverView(model: model, canInstall: shot.canInstall), dark: dark, to: file)
            case .settings:
                await render(SettingsView(model: model, loginItem: LoginItem(), pane: shot.pane), dark: dark, to: file)
            }
            print(file.path)
        }
        model.stop()
        server?.stop()
    }

    await renderInstaller(in: scratch)

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

// MARK: - Installer

/// One installer screen: how to set up the fake runner, and what to do to get there.
struct InstallerShot {
    var name: String
    var setup: @MainActor (FakeInstallRunner) -> Void = { _ in }
    var server = false
    var otherAccount = false
    var capacity: Int64 = 412_000_000_000
    var stallTimeout: Duration = .seconds(60)
    /// Files an interrupted run left in the home.
    var leftovers: (URL) -> Void = { _ in }
    var steps: @MainActor (Installer) async -> Void = { _ in }
}

@MainActor
func until(_ installer: Installer, _ condition: (InstallerPhase) -> Bool) async {
    for _ in 0..<250 where !condition(installer.phase) {
        try? await Task.sleep(for: .milliseconds(20))
    }
}

@MainActor
func failed(_ installer: Installer) async {
    await until(installer) { if case .failed = $0 { true } else { false } }
}

@MainActor
func reachApproval(_ installer: Installer) async {
    installer.install()
    await until(installer) { if case .approval = $0 { true } else { false } }
}

@MainActor
func reachHomeAssistant(_ installer: Installer) async {
    await reachApproval(installer)
    installer.approve()
    await until(installer) { $0 == .homeAssistant(.asking) }
}

@MainActor
func respondToInstall(_ runner: FakeInstallRunner, _ response: FakeInstallRunner.Response) {
    runner.respond = { command in
        command.arguments.first == "install" ? response : FakeInstallRunner.defaultResponse(command)
    }
}

let installerShots = [
    InstallerShot(name: "installer-welcome"),
    InstallerShot(name: "installer-welcome-no-room", capacity: 1_500_000_000),
    InstallerShot(
        name: "installer-welcome-continue",
        leftovers: { home in
            let state = home.appendingPathComponent(".voiced")
            try? FileManager.default.createDirectory(at: state.appendingPathComponent("envs"), withIntermediateDirectories: true)
            try? Data(#"{"source": "app", "environment": null}"#.utf8).write(to: state.appendingPathComponent("install.json"))
        }),
    InstallerShot(
        name: "installer-environment",
        setup: { runner in
            runner.respond = { $0.arguments.first == "python" ? .init([], hangs: true) : FakeInstallRunner.defaultResponse($0) }
        },
        steps: { installer in
            installer.install()
            await until(installer) { if case .environment(let step) = $0 { step.hasPrefix("Installing") } else { false } }
        }),
    InstallerShot(
        name: "installer-downloading",
        setup: { respondToInstall($0, .init(RecordedProgress.downloading.map(ProcessOutput.stdout), delay: .milliseconds(40), hangs: true)) },
        steps: { installer in
            installer.install()
            await until(installer) {
                if case .installing(let progress) = $0 { progress.downloads.last?.bytes == 177_650_000 } else { false }
            }
        }),
    InstallerShot(
        name: "installer-approval",
        setup: { runner in
            runner.respond = { command in
                command.executable.hasSuffix("socketfilterfw")
                    ? .init([.stdout("Firewall is enabled. (State = 1)"), .exit(0)]) : FakeInstallRunner.defaultResponse(command)
            }
        },
        steps: reachApproval),
    InstallerShot(
        name: "installer-approving", setup: { $0.privilegedHangs = true },
        steps: { installer in
            await reachApproval(installer)
            installer.approve()
            await until(installer) { if case .approving = $0 { true } else { false } }
        }),
    InstallerShot(
        name: "installer-starting",
        setup: { $0.privilegedOutcomes = [.finished(status: 4, output: "")] },
        steps: { installer in
            await reachApproval(installer)
            installer.approve()
            await until(installer) { $0 == .starting }
        }),
    InstallerShot(name: "installer-home-assistant", server: true, steps: reachHomeAssistant),
    InstallerShot(
        name: "installer-home-assistant-connected", server: true,
        steps: { installer in
            await reachHomeAssistant(installer)
            installer.connectHomeAssistant(true)
            await until(installer) { if case .homeAssistant(.connected) = $0 { true } else { false } }
            installer.generateAPIKey()
            await until(installer) { if case .homeAssistant(.connected(let info)) = $0 { info.apiKey != nil } else { false } }
        }),
    InstallerShot(
        name: "installer-done", server: true,
        steps: { installer in
            await reachHomeAssistant(installer)
            installer.connectHomeAssistant(false)
        }),
    InstallerShot(
        name: "installer-failed-port",
        setup: {
            respondToInstall(
                $0,
                .progress(
                    [
                        RecordedProgress.hello, RecordedProgress.step("ports", "start"),
                        #"{"event": "log", "line": "port 10300: in use by python3.12 (pid 4242)"}"#,
                        RecordedProgress.error("ports", "Port 10300 is in use by python3.12 (pid 4242)."),
                    ], status: 1))
        },
        steps: { installer in
            installer.install()
            await failed(installer)
        }),
    InstallerShot(
        name: "installer-failed-stall",
        setup: { respondToInstall($0, .init(RecordedProgress.downloading.map(ProcessOutput.stdout), hangs: true)) },
        stallTimeout: .seconds(1),
        steps: { installer in
            installer.install()
            await failed(installer)
        }),
    InstallerShot(
        name: "installer-failed-bootstrap",
        setup: {
            $0.privilegedOutcomes = [
                .finished(status: 3, output: "failed io.github.julianbeaulieu.voiced.tts\nbootstrap failed: io.github.julianbeaulieu.voiced.tts")
            ]
        },
        steps: { installer in
            await reachApproval(installer)
            installer.approve()
            await failed(installer)
        }),
    InstallerShot(
        name: "installer-failed-network", setup: { $0.isOnline = false },
        steps: { installer in
            installer.install()
            await failed(installer)
        }),
    InstallerShot(name: "installer-refused", otherAccount: true),
]

@MainActor
func renderInstaller(in scratch: URL) async {
    guard let resources = try? installResources(in: scratch.appendingPathComponent("resources")) else { return }
    for shot in installerShots {
        let home = scratch.appendingPathComponent(shot.name, isDirectory: true)
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        shot.leftovers(home)
        let daemons = home.appendingPathComponent("LaunchDaemons", isDirectory: true)
        try? FileManager.default.createDirectory(at: daemons, withIntermediateDirectories: true)
        if shot.otherAccount {
            try? writePlist(
                ["ProgramArguments": ["/Users/alex/.voiced/bin/voiced", "watchdog"], "UserName": "root"],
                to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        }
        let socket = scratch.appendingPathComponent("i-\(shot.name.suffix(6)).sock").path
        var server: FakeControlServer?
        if shot.server {
            var options = FakeControlServer.Options()
            options.restartDuration = 0.1
            server = FakeControlServer(socketPath: socket, scenario: .running, options: options)
            server?.update { $0.settings.zeroconf = false }
            try? server?.start()
        }
        let runner = FakeInstallRunner()
        shot.setup(runner)
        let probe = InstallProbe(launchDaemonsDirectory: daemons)
        var configuration = InstallerConfiguration(resources: resources, home: home, installProbe: probe)
        configuration.startupTimeout = .seconds(600)
        configuration.stallTimeout = shot.stallTimeout
        configuration.pollInterval = .milliseconds(20)
        let capacity = shot.capacity
        configuration.availableCapacity = { _ in capacity }
        // Documentation names, not this Mac's.
        configuration.hostIdentity = { HostIdentity(host: "studio.local", address: "192.0.2.10") }
        let model = AppModel(
            socketPath: socket, installProbe: probe, backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)),
            opener: { _ in true })
        let installer = Installer(configuration: configuration, runner: runner, model: model)
        await shot.steps(installer)
        for dark in [false, true] {
            let file = output.appendingPathComponent("\(shot.name)-\(dark ? "dark" : "light").png")
            await render(InstallerView(installer: installer, loginItem: LoginItem(), close: {}), dark: dark, to: file)
            print(file.path)
        }
        installer.cancel()
        model.stop()
        server?.stop()
    }
}

NSApplication.shared.setActivationPolicy(.prohibited)
Task { @MainActor in
    await run()
    exit(0)
}
NSApplication.shared.run()
