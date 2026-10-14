import FakeControlServerKit
import Foundation
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

@MainActor
final class AppModelTests: XCTestCase {
    private var scratch: URL!

    override func setUp() async throws {
        scratch = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    private func makeModel(socket: String, installed: Bool = false, opened: OpenedBox? = nil) -> AppModel {
        if installed {
            FileManager.default.createFile(
                atPath: scratch.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist").path,
                contents: Data())
        }
        return AppModel(
            socketPath: socket, installProbe: InstallProbe(launchDaemonsDirectory: scratch),
            backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)),
            opener: { target in
                opened?.targets.append(target)
                return true
            })
    }

    final class OpenedBox { var targets: [String] = [] }

    private func isConnected(_ model: AppModel) -> Bool {
        if case .connected = model.connection { return model.status != nil && model.settings != nil && model.engines != nil }
        return false
    }

    func testNoSocketAndNoInstallShowsInstallHelp() async {
        let model = makeModel(socket: temporarySocketPath())
        model.start()
        defer { model.stop() }
        let reached = await eventually { model.connection == .unreachable(installed: false) }
        XCTAssertTrue(reached)
        XCTAssertTrue(model.dashboard.showsInstallHelp)
        XCTAssertEqual(model.dashboard.health, .off)
    }

    func testNoSocketButInstalledIsNotRunning() async {
        let model = makeModel(socket: temporarySocketPath(), installed: true)
        model.start()
        defer { model.stop() }
        let reached = await eventually { model.connection == .unreachable(installed: true) }
        XCTAssertTrue(reached)
        XCTAssertEqual(model.dashboard.headline, "Not running")
    }

    func testConnectsWhenTheServerAppearsAndReconnectsAfterItGoesAway() async throws {
        let socket = temporarySocketPath()
        let model = makeModel(socket: socket, installed: true)
        model.start()
        defer { model.stop() }
        let waited = await eventually { model.connection == .unreachable(installed: true) }
        XCTAssertTrue(waited)

        var options = FakeControlServer.Options()
        options.version = "9.9.9"
        let server = FakeControlServer(socketPath: socket, scenario: .running, options: options)
        try server.start()
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)
        XCTAssertEqual(model.serverVersion, "9.9.9")
        XCTAssertEqual(model.dashboard.health, .running)

        // A dropped connection is retried on the backoff schedule.
        server.dropConnections()
        let reconnected = await eventually { server.acceptedConnections >= 2 && self.isConnected(model) }
        XCTAssertTrue(reconnected)

        // The server going away shows as not running, and coming back reconnects.
        server.stop()
        let down = await eventually { model.connection == .unreachable(installed: true) }
        XCTAssertTrue(down)
        XCTAssertNil(model.status)
        try server.start()
        let back = await eventually { self.isConnected(model) }
        XCTAssertTrue(back)
        server.stop()
    }

    func testNewerProtocolIsShownAsIncompatible() async throws {
        let server = try startServer(.newerProtocol)
        defer { server.stop() }
        let model = makeModel(socket: server.socketPath)
        model.start()
        defer { model.stop() }
        let refused = await eventually {
            if case .incompatible = model.connection { return true }
            return false
        }
        XCTAssertTrue(refused)
        XCTAssertEqual(model.dashboard.headline, "Incompatible version")
        XCTAssertTrue(model.dashboard.message?.contains("protocol 2") ?? false)
    }

    func testStatusEventsUpdateTheDashboard() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = makeModel(socket: server.socketPath)
        model.start()
        defer { model.stop() }
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)

        server.update { $0[.tts].state = .degraded }
        let degraded = await eventually { model.dashboard.headline == "Degraded" }
        XCTAssertTrue(degraded)
    }

    func testSettingsEditsSendTheRightArgumentsAndCollectRestarts() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = makeModel(socket: server.socketPath)
        model.start()
        defer { model.stop() }
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)

        let saved = await model.apply(.ttsEngine("soprano"))
        XCTAssertTrue(saved)
        let sent = server.requests.last { $0.op == "settings.set" }
        XCTAssertEqual(
            sent?.args,
            [
                "tts.engine": "soprano", "tts.model": "mlx-community/Soprano-80M-bf16",
                "tts.voice": "default",
            ])
        XCTAssertEqual(model.settings?.tts.engine, "soprano")
        XCTAssertEqual(model.restartRequired, [.tts])

        await model.restartPending()
        XCTAssertEqual(server.requests.last?.op, "service.restart")
        XCTAssertEqual(server.requests.last?.args, ["service": "tts"])
        XCTAssertEqual(model.restartRequired, [])
    }

    func testLANWithoutKeyShowsTheWarning() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = makeModel(socket: server.socketPath)
        model.start()
        defer { model.stop() }
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)
        XCTAssertFalse(model.showsLANWarning)

        await model.apply(.clearAPIKey)
        await model.apply(.bind(.lan))
        XCTAssertEqual(model.warnings, ["lan_without_api_key"])
        XCTAssertTrue(model.showsLANWarning)
        XCTAssertEqual(model.restartRequired, [.stt, .tts])

        await model.apply(.apiKey("k"))
        XCTAssertFalse(model.showsLANWarning)
        // The key went in and was never echoed back.
        XCTAssertEqual(server.requests.last?.args, ["api_key": "k"])
        XCTAssertEqual(model.settings?.apiKeySet, true)
    }

    func testRejectedSettingShowsTheServerMessage() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = makeModel(socket: server.socketPath)
        model.start()
        defer { model.stop() }
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)

        let saved = await model.apply(.ttsVoice("nobody"))
        XCTAssertFalse(saved)
        XCTAssertEqual(model.errorMessage, "Not saved. tts.voice: nobody is not a kokoro voice")
        XCTAssertEqual(model.settings?.tts.voice, "af_heart")
    }

    func testOpenPassesTheServersTargetToTheOpener() async throws {
        let server = try startServer()
        defer { server.stop() }
        let opened = OpenedBox()
        let model = makeModel(socket: server.socketPath, opened: opened)
        model.start()
        defer { model.stop() }
        let connected = await eventually { self.isConnected(model) }
        XCTAssertTrue(connected)

        await model.open(.statusPage)
        await model.open(.logsDirectory)
        XCTAssertEqual(opened.targets.first, "http://127.0.0.1:8880/")
        XCTAssertTrue(opened.targets.last?.hasSuffix("/Library/Logs/voiced") ?? false)
    }
}

final class EnvironmentTests: XCTestCase {
    func testBackoffDoublesToItsCapAndResets() {
        var backoff = Backoff(initial: .milliseconds(500), maximum: .seconds(3))
        XCTAssertEqual(
            (0..<5).map { _ in backoff.next() },
            [.milliseconds(500), .seconds(1), .seconds(2), .seconds(3), .seconds(3)])
        backoff.reset()
        XCTAssertEqual(backoff.next(), .milliseconds(500))
    }

    func testSocketLocationOrder() throws {
        let home = URL(fileURLWithPath: "/Users/someone")
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "voiced-menubar-tests-\(UUID().uuidString)"))
        XCTAssertEqual(
            SocketLocation.resolve(environment: [:], defaults: defaults, home: home),
            "/Users/someone/.voiced/control.sock")
        defaults.set("~/elsewhere.sock", forKey: SocketLocation.defaultsKey)
        XCTAssertEqual(
            SocketLocation.resolve(environment: [:], defaults: defaults, home: home),
            "/Users/someone/elsewhere.sock")
        XCTAssertEqual(
            SocketLocation.resolve(
                environment: ["VOICED_CONTROL_SOCKET": "/tmp/x.sock"], defaults: defaults, home: home),
            "/tmp/x.sock")
    }

    func testInstallProbeLooksForTheWatchdogPlist() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let probe = InstallProbe(launchDaemonsDirectory: directory)
        XCTAssertFalse(probe.isInstalled())
        FileManager.default.createFile(atPath: directory.appendingPathComponent("com.other.watchdog.plist").path, contents: nil)
        XCTAssertFalse(probe.isInstalled())
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("io.github.julianbeaulieu.voiced.stt.plist").path, contents: nil)
        XCTAssertFalse(probe.isInstalled())
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist").path,
            contents: nil)
        XCTAssertTrue(probe.isInstalled())
    }
}

@MainActor
final class SettingsEventTests: XCTestCase {
    func testASettingsEventFromAnotherClientRefreshesTheModel() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = AppModel(
            socketPath: server.socketPath,
            installProbe: InstallProbe(launchDaemonsDirectory: FileManager.default.temporaryDirectory),
            opener: { _ in true })
        model.start()
        defer { model.stop() }
        let connected = await eventually { model.settings != nil }
        XCTAssertTrue(connected)

        let other = try await connectedClient(server)
        defer { other.close() }
        _ = try await other.setSettings(["tts.voice": "bf_emma"])
        let updated = await eventually { model.settings?.tts.voice == "bf_emma" }
        XCTAssertTrue(updated)
    }
}
