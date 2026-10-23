import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

@MainActor
final class AppModelTests: XCTestCase {
    final class Opened { var actions: [OpenAction] = [] }

    private func makeModel(
        socket: String, installed: Bool = false, opened: Opened? = nil,
        backoff: Backoff = Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)),
        livenessTimeout: Duration = .seconds(25), restartTimeout: Duration = .seconds(30)
    ) throws -> AppModel {
        let daemons = try launchDaemonsDirectory(watchdogUser: installed ? NSUserName() : nil)
        return AppModel(
            socketPath: socket, installProbe: InstallProbe(launchDaemonsDirectory: daemons),
            backoff: backoff, livenessTimeout: livenessTimeout, restartTimeout: restartTimeout,
            opener: { action in
                opened?.actions.append(action)
                return true
            })
    }

    private func isReady(_ model: AppModel) -> Bool {
        if case .connected = model.connection {
            return model.status != nil && model.settings != nil && model.engines != nil
        }
        return false
    }

    private func connectedModel(_ server: FakeControlServer, opened: Opened? = nil) async throws -> AppModel {
        let model = try makeModel(socket: server.socketPath, opened: opened)
        model.start()
        let ready = await eventually { self.isReady(model) }
        XCTAssertTrue(ready)
        return model
    }

    // MARK: - Connection

    func testNoSocketAndNoInstallShowsInstallHelp() async throws {
        let model = try makeModel(socket: temporarySocketPath())
        model.start()
        defer { model.stop() }
        let reached = await eventually {
            model.connection == .unreachable(installed: false, reason: .cannotConnect(.noSocket))
        }
        XCTAssertTrue(reached)
        XCTAssertTrue(model.dashboard.showsInstallHelp)
        XCTAssertEqual(model.dashboard.health, .off)
    }

    func testNoSocketButInstalledIsNotRunning() async throws {
        let model = try makeModel(socket: temporarySocketPath(), installed: true)
        model.start()
        defer { model.stop() }
        let reached = await eventually {
            model.connection == .unreachable(installed: true, reason: .cannotConnect(.noSocket))
        }
        XCTAssertTrue(reached)
        XCTAssertEqual(model.dashboard.headline, "Not running")
    }

    func testConnectsWhenTheServerAppearsAndReconnectsAfterItGoesAway() async throws {
        let socket = temporarySocketPath()
        let model = try makeModel(socket: socket, installed: true)
        model.start()
        defer { model.stop() }
        let waited = await eventually {
            model.connection == .unreachable(installed: true, reason: .cannotConnect(.noSocket))
        }
        XCTAssertTrue(waited)

        var options = FakeControlServer.Options()
        options.version = "9.9.9"
        let server = FakeControlServer(socketPath: socket, scenario: .running, options: options)
        try server.start()
        let connected = await eventually { self.isReady(model) }
        XCTAssertTrue(connected)
        XCTAssertEqual(model.serverVersion, "9.9.9")
        XCTAssertEqual(model.dashboard.health, .running)

        // A dropped connection is retried on the backoff schedule.
        server.dropConnections()
        let reconnected = await eventually { server.acceptedConnections >= 2 && self.isReady(model) }
        XCTAssertTrue(reconnected)

        // The server going away shows as not running, and coming back reconnects.
        server.stop()
        let down = await eventually {
            model.connection == .unreachable(installed: true, reason: .cannotConnect(.noSocket))
        }
        XCTAssertTrue(down)
        XCTAssertNil(model.status)
        try server.start()
        let back = await eventually { self.isReady(model) }
        XCTAssertTrue(back)
        server.stop()
    }

    func testASilentServerIsDroppedAfterTheLivenessTimeout() async throws {
        // The fake pushes status only on change here; a real hung watchdog sends nothing.
        let server = try startServer { $0.statusInterval = 600 }
        defer { server.stop() }
        let model = try makeModel(
            socket: server.socketPath, backoff: Backoff(initial: .seconds(5), maximum: .seconds(5)),
            livenessTimeout: .milliseconds(300))
        model.start()
        defer { model.stop() }
        let ready = await eventually { self.isReady(model) }
        XCTAssertTrue(ready)
        let dropped = await eventually {
            if case .unreachable(_, .stoppedAnswering) = model.connection { return true }
            return false
        }
        XCTAssertTrue(dropped)
        XCTAssertEqual(model.dashboard.headline, "Not answering")
    }

    func testStatusEventsKeepTheConnectionAlive() async throws {
        // Events every 0.2 s against a 2 s limit, watched for longer than the limit: a
        // scheduler stall on a busy runner stays far below the limit.
        let server = try startServer { $0.statusInterval = 0.2 }
        defer { server.stop() }
        let model = try makeModel(socket: server.socketPath, livenessTimeout: .seconds(2))
        model.start()
        defer { model.stop() }
        let ready = await eventually { self.isReady(model) }
        XCTAssertTrue(ready)
        try await Task.sleep(for: .seconds(3))
        XCTAssertTrue(isReady(model))
        XCTAssertEqual(server.acceptedConnections, 1)
    }

    func testAFailedFirstRequestIsAFailedConnection() async throws {
        let server = try startServer { $0.failingOps = ["engines.list"] }
        defer { server.stop() }
        let model = try makeModel(
            socket: server.socketPath, backoff: Backoff(initial: .seconds(5), maximum: .seconds(5)))
        model.start()
        defer { model.stop() }
        let failed = await eventually {
            if case .unreachable(_, .setupFailed(let detail)) = model.connection {
                return detail.contains("engines.list failed")
            }
            return false
        }
        XCTAssertTrue(failed)
        XCTAssertNil(model.status)
    }

    func testHelloThenCloseReadsAsDisconnected() async throws {
        let server = try startServer { $0.closeOnFirstRequest = true }
        defer { server.stop() }
        let model = try makeModel(
            socket: server.socketPath, backoff: Backoff(initial: .seconds(5), maximum: .seconds(5)))
        model.start()
        defer { model.stop() }
        let ended = await eventually {
            if case .unreachable = model.connection { return true }
            return false
        }
        XCTAssertTrue(ended)
        XCTAssertEqual(model.connection, .unreachable(installed: false, reason: .disconnected))
    }

    func testStopThenStartRunsOneConnection() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try makeModel(socket: server.socketPath)
        model.start()
        model.stop()
        model.start()
        defer { model.stop() }
        let ready = await eventually { self.isReady(model) }
        XCTAssertTrue(ready)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(server.acceptedConnections, 1)
    }

    func testStopInterruptsAConnectAttempt() async throws {
        let server = try startServer { $0.firstLine = "" }
        defer { server.stop() }
        let model = try makeModel(socket: server.socketPath)
        model.start()
        let accepted = await eventually { server.acceptedConnections == 1 }
        XCTAssertTrue(accepted)
        model.stop()
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(model.connection, .connecting, "a stopped model does not change state")
        XCTAssertEqual(server.acceptedConnections, 1)
    }

    func testNewerProtocolIsShownAsIncompatible() async throws {
        let server = try startServer(.newerProtocol)
        defer { server.stop() }
        let model = try makeModel(socket: server.socketPath)
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
        let model = try await connectedModel(server)
        defer { model.stop() }

        server.update { $0[.tts].state = .degraded }
        let degraded = await eventually { model.dashboard.headline == "Degraded" }
        XCTAssertTrue(degraded)
    }

    func testASettingsEventFromAnotherClientRefreshesTheModel() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        let other = try await connectedClient(server)
        defer { other.close() }
        _ = try await other.setSettings(["tts.voice": "bf_emma"])
        let updated = await eventually { model.settings?.tts.voice == "bf_emma" }
        XCTAssertTrue(updated)
    }

    // MARK: - Settings

    func testSettingsEditsSendTheRightArgumentsAndCollectRestarts() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

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

    func testSwitchingOffAndBackOnThroughTheFakeServer() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        let switchedOff = await model.apply(.enabled(.stt, false))
        XCTAssertTrue(switchedOff)
        XCTAssertEqual(server.requests.last { $0.op == "settings.set" }?.args, ["stt.enabled": false])
        XCTAssertEqual(model.restartRequired, [.stt])
        await model.restart(.service(.stt))
        let off = await eventually { model.status?[.stt]?.state == .off }
        XCTAssertTrue(off)
        XCTAssertEqual(model.dashboard.rows.first?.stateLabel, "Off")

        let unloadSet = await model.apply(.idleUnload(.tts, seconds: 300))
        XCTAssertTrue(unloadSet)
        XCTAssertEqual(server.requests.last { $0.op == "settings.set" }?.args, ["tts.idle_unload_s": 300])
        XCTAssertEqual(model.settings?.tts.residency.idleUnloadSeconds, 300)

        let switchedOn = await model.apply(.enabled(.stt, true))
        XCTAssertTrue(switchedOn)
        await model.restart(.service(.stt))
        let back = await eventually { model.status?[.stt]?.state == .running }
        XCTAssertTrue(back)
    }

    func testTheSTTOffScenario() async throws {
        let server = try startServer(.sttOff)
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }
        let shown = await eventually { model.status?[.stt]?.state == .off }
        XCTAssertTrue(shown)
        XCTAssertEqual(model.settings?.stt.residency.enabled, false)
        XCTAssertEqual(model.settings?.tts.residency.idleUnloadSeconds, 300)
        XCTAssertEqual(model.dashboard.rows.map(\.stateLabel), ["Off", "Idle, model unloaded"])
        XCTAssertEqual(model.dashboard.health, .running)
    }

    func testAnUnloadTimeThatIsNotWholeSecondsIsRefused() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }
        do {
            _ = try await client.setSettings(["tts.idle_unload_s": 1.5])
            XCTFail("accepted")
        } catch ControlError.remote(let error) {
            XCTAssertEqual(error.field, "tts.idle_unload_s")
        }
    }

    func testSwitchingToPhononTakesItsModelAndDefaultVoiceThenAPresetByName() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        _ = await model.apply(.ttsEngine("phonon"))
        XCTAssertEqual(
            server.requests.last { $0.op == "settings.set" }?.args,
            ["tts.engine": "phonon", "tts.model": "gradium/phonon-31m-en", "tts.voice": "default"])
        // Voice ids are the catalogue's names, spaces and all.
        _ = await model.apply(.ttsVoice("The Snow Queen"))
        XCTAssertEqual(server.requests.last { $0.op == "settings.set" }?.args, ["tts.voice": "The Snow Queen"])
        XCTAssertEqual(model.settings?.tts.voice, "The Snow Queen")
    }

    func testARestartByAnyoneClearsTheRestartNotice() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        await model.apply(.ttsVoice("bf_emma"))
        XCTAssertEqual(model.restartRequired, [.tts])
        // The watchdog restarted it, or `voiced start` did: a new pid.
        server.update { $0[.tts].pid = 9999 }
        let cleared = await eventually { model.restartRequired.isEmpty }
        XCTAssertTrue(cleared)
    }

    func testLANWithoutKeyShowsTheWarning() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }
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

    func testOtherWarningsAreShown() async throws {
        let server = try startServer { $0.environmentOverrides = [.ttsVoice] }
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }
        await model.apply(.ttsVoice("bf_emma"))
        XCTAssertEqual(model.otherWarnings.count, 1)
        XCTAssertTrue(model.otherWarnings[0].contains("overrides tts.voice"))
    }

    func testRejectedSettingShowsTheFieldAndMessage() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        let saved = await model.apply(.ttsVoice("nobody"))
        XCTAssertFalse(saved)
        XCTAssertEqual(model.errorMessage, "Not saved. tts.voice: nobody is not a kokoro voice")
        XCTAssertEqual(model.settings?.tts.voice, "af_heart")
    }

    // MARK: - Actions

    func testARestartThatOutlastsTheTimeoutCountsAsRequested() async throws {
        let server = try startServer { $0.unansweredOps = ["service.restart"] }
        defer { server.stop() }
        let model = try makeModel(socket: server.socketPath, restartTimeout: .milliseconds(200))
        model.start()
        defer { model.stop() }
        let ready = await eventually { self.isReady(model) }
        XCTAssertTrue(ready)

        await model.restart(.service(.tts))
        XCTAssertNil(model.errorMessage)
        XCTAssertTrue(model.restarting.isEmpty)
        XCTAssertTrue(isReady(model), "the connection stays up")
    }

    func testARefusedServiceKeepsItsRestartNoticeAndSaysWhy() async throws {
        let server = try startServer {
            $0.refusedRestarts = [.stt: "stt: io.example.voiced.stt is installed but not loaded in launchd"]
        }
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }

        await model.apply(.bind(.lan))
        XCTAssertEqual(model.restartRequired, [.stt, .tts])
        await model.restartPending()
        XCTAssertEqual(server.requests.last?.args, ["service": "all"])
        XCTAssertEqual(model.restartRequired, [.stt], "only tts restarted")
        XCTAssertEqual(
            model.errorMessage, "Not restarted: stt: io.example.voiced.stt is installed but not loaded in launchd")
    }

    func testAFullRefusalIsWordedLikeAPartialOne() async throws {
        let server = try startServer {
            $0.refusedRestarts = [.tts: "tts: io.example.voiced.tts has no launchd plist"]
        }
        defer { server.stop() }
        let model = try await connectedModel(server)
        defer { model.stop() }
        await model.restart(.service(.tts))
        XCTAssertEqual(model.errorMessage, "Not restarted: tts: io.example.voiced.tts has no launchd plist")
    }

    func testOpenChecksTheServersTargetBeforeOpening() async throws {
        let server = try startServer()
        defer { server.stop() }
        let opened = Opened()
        let model = try await connectedModel(server, opened: opened)
        defer { model.stop() }

        await model.open(.statusPage)
        XCTAssertEqual(opened.actions, [.webPage(URL(string: "http://127.0.0.1:8880/")!)])

        // A folder that does not exist on this machine is refused, not opened.
        let missing = "/nonexistent-\(UUID().uuidString)"
        server.update { $0.settings.paths?.logs = missing }
        await model.open(.logsDirectory)
        XCTAssertEqual(opened.actions.count, 1)
        XCTAssertEqual(model.errorMessage, "Not opening \(missing): it does not exist.")

        // An app bundle where a folder is expected exists but is refused.
        server.update { $0.settings.paths?.logs = "/System/Applications/Calculator.app" }
        await model.open(.logsDirectory)
        XCTAssertEqual(opened.actions.count, 1)
        XCTAssertEqual(
            model.errorMessage,
            "Not opening /System/Applications/Calculator.app: it is not the kind of thing asked for.")

        // A successful open clears the banner.
        await model.open(.statusPage)
        XCTAssertEqual(opened.actions.count, 2)
        XCTAssertNil(model.errorMessage)
    }
}

final class SettingsTextTests: XCTestCase {
    func testLanguageAndSpeedAreReadable() {
        XCTAssertEqual(SettingsView.languageName("zz-unknown-code"), "zz-unknown-code")
        XCTAssertFalse(SettingsView.languageName("en").isEmpty)
        XCTAssertNotEqual(SettingsView.languageName("en"), "en")
        XCTAssertEqual(SettingsView.speedText(1), "1×")
        // The decimal separator follows the user's locale.
        let fractional = SettingsView.speedText(1.25)
        XCTAssertTrue(fractional.hasPrefix("1") && fractional.hasSuffix("25×"), fractional)
    }
}
