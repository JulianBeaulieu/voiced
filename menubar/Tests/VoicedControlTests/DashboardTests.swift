import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

/// Contract payloads, written out as JSON, reduced to what the popover shows.
final class DashboardTests: XCTestCase {
    private func status(stt: String, tts: String, installed: Bool = true) throws -> StatusPayload {
        try decodeJSON(
            StatusPayload.self,
            """
            {"installed": \(installed), "services": {"stt": \(stt), "tts": \(tts),
             "watchdog": {"state": "running", "pid": 10, "health": null, "engine": null,
                          "model": null, "warm": false, "uptime_s": 50.0, "restarts": 0}}}
            """)
    }

    private let runningSTT = """
        {"state": "running", "pid": 11, "health": {"status": "ok"}, "engine": "parakeet",
         "model": "mlx-community/parakeet-tdt-0.6b-v3", "warm": true, "uptime_s": 3725.4, "restarts": 0}
        """
    private let runningTTS = """
        {"state": "running", "pid": 12, "health": {"status": "ok"}, "engine": "kokoro",
         "model": "mlx-community/Kokoro-82M-bf16", "warm": true, "uptime_s": 59.9, "restarts": 2}
        """

    private func connected(_ payload: StatusPayload?) -> Dashboard {
        DashboardReducer.reduce(connection: .connected(version: "0.0.1"), status: payload, socketPath: "/s")
    }

    func testBothWarmIsRunning() throws {
        let dashboard = connected(try status(stt: runningSTT, tts: runningTTS))
        XCTAssertEqual(dashboard.health, .running)
        XCTAssertEqual(dashboard.headline, "Running")
        XCTAssertEqual(dashboard.rows.map(\.kind), [.stt, .tts])
        XCTAssertEqual(dashboard.rows[0].engineLine, "parakeet · parakeet-tdt-0.6b-v3")
        XCTAssertEqual(dashboard.rows[0].detailLine, "up 1 h 2 min")
        XCTAssertEqual(dashboard.rows[1].detailLine, "up 59 s · 2 restarts")
        XCTAssertNil(dashboard.notice)
        XCTAssertEqual(dashboard.rows[1].tone, .good)
    }

    func testLoadingModelIsStarting() throws {
        let loading = #"{"state": "running", "pid": 12, "engine": "kokoro", "warm": false, "uptime_s": 3, "restarts": 0}"#
        let dashboard = connected(try status(stt: runningSTT, tts: loading))
        XCTAssertEqual(dashboard.health, .attention)
        XCTAssertEqual(dashboard.headline, "Starting")
        XCTAssertEqual(dashboard.rows[1].stateLabel, "Loading model")
        XCTAssertEqual(dashboard.rows[1].tone, .busy)
        XCTAssertEqual(dashboard.rows[1].detailLine, "up 3 s")
    }

    func testDegradedWinsOverStarting() throws {
        let degraded = #"{"state": "degraded", "pid": 12, "warm": true, "uptime_s": 10, "restarts": 1}"#
        let starting = #"{"state": "starting", "pid": 13, "warm": false, "uptime_s": 1, "restarts": 0}"#
        let dashboard = connected(try status(stt: starting, tts: degraded))
        XCTAssertEqual(dashboard.health, .attention)
        XCTAssertEqual(dashboard.headline, "Degraded")
        XCTAssertEqual(dashboard.rows[1].tone, .bad)
        XCTAssertEqual(dashboard.rows[1].engineLine, nil)
        XCTAssertEqual(dashboard.rows[1].problem, "Not passing health checks.")
        XCTAssertNil(dashboard.rows[0].problem)
    }

    func testDegradedShowsTheLastError() throws {
        let degraded = """
            {"state": "degraded", "pid": 12, "warm": true, "uptime_s": 10, "restarts": 0,
             "health": {"status": "degraded", "last_error": "RuntimeError: boom"}}
            """
        let dashboard = connected(try status(stt: runningSTT, tts: degraded))
        XCTAssertEqual(dashboard.rows[1].problem, "Last error: RuntimeError: boom")
    }

    func testBothStoppedIsOff() throws {
        let stopped = #"{"state": "stopped", "pid": null, "warm": false, "uptime_s": null, "restarts": 0}"#
        let dashboard = connected(try status(stt: stopped, tts: stopped))
        XCTAssertEqual(dashboard.health, .off)
        XCTAssertEqual(dashboard.headline, "Stopped")
        XCTAssertEqual(dashboard.rows[0].detailLine, "Not running")
        XCTAssertTrue(dashboard.isControllable, "stopped services can still be restarted")
    }

    func testOneStoppedNamesIt() throws {
        let stopped = #"{"state": "stopped", "pid": null, "warm": false, "uptime_s": null, "restarts": 0}"#
        let dashboard = connected(try status(stt: runningSTT, tts: stopped))
        XCTAssertEqual(dashboard.health, .attention)
        XCTAssertEqual(dashboard.headline, "TTS stopped")
    }

    func testUnknownStateAndExtraFieldsAreTolerated() throws {
        let odd = #"{"state": "hibernating", "pid": 1, "warm": true, "uptime_s": 1, "restarts": 0, "new_field": [1]}"#
        let dashboard = connected(try status(stt: runningSTT, tts: odd))
        XCTAssertEqual(dashboard.rows[1].stateLabel, "Unknown")
        XCTAssertEqual(dashboard.health, .attention)
    }

    func testMissingServiceIsUnknown() throws {
        let payload = try decodeJSON(StatusPayload.self, #"{"installed": true, "services": {}}"#)
        let dashboard = connected(payload)
        XCTAssertEqual(dashboard.rows.map(\.stateLabel), ["Unknown", "Unknown"])
    }

    func testConnectedButNotInstalledKeepsTheControls() throws {
        let dashboard = connected(try status(stt: runningSTT, tts: runningTTS, installed: false))
        XCTAssertEqual(dashboard.health, .running)
        XCTAssertFalse(dashboard.showsInstallHelp)
        XCTAssertTrue(dashboard.isControllable)
        XCTAssertTrue(dashboard.notice?.contains("not installed as LaunchDaemons") ?? false)
    }

    private func unreachable(_ failure: ConnectFailure, installed: Bool = true) -> Dashboard {
        DashboardReducer.reduce(
            connection: .unreachable(installed: installed, reason: .cannotConnect(failure)), status: nil,
            socketPath: "/Users/someone/.voiced/control.sock")
    }

    func testMissingSocketDependsOnTheInstall() {
        let missing = unreachable(.noSocket, installed: false)
        XCTAssertTrue(missing.showsInstallHelp)
        XCTAssertEqual(missing.headline, "Not installed")

        let down = unreachable(.noSocket)
        XCTAssertFalse(down.showsInstallHelp)
        XCTAssertEqual(down.health, .off)
        XCTAssertEqual(down.headline, "Not running")
        XCTAssertEqual(down.command, "voiced start watchdog")
    }

    func testEachConnectFailureHasItsOwnExplanation() {
        XCTAssertEqual(unreachable(.notListening).command, "voiced start watchdog")
        XCTAssertTrue(unreachable(.notListening).message?.contains("left over") ?? false)
        for failure: ConnectFailure in [.permissionDenied, .notASocket, .pathTooLong, .noHello, .closedBeforeHello, .other("x")] {
            let dashboard = unreachable(failure)
            XCTAssertNil(dashboard.command, "only a missing or stale socket is fixed by starting the watchdog")
            XCTAssertFalse(dashboard.showsInstallHelp)
            XCTAssertNotNil(dashboard.message)
        }
        XCTAssertTrue(unreachable(.permissionDenied).message?.contains("/Users/someone/.voiced/control.sock") ?? false)
        // Not installed but something odd at the path: explain the oddity, not the install.
        XCTAssertFalse(unreachable(.notASocket, installed: false).showsInstallHelp)
    }

    func testStoppedAnsweringAndDisconnected() {
        let silent = DashboardReducer.reduce(
            connection: .unreachable(installed: true, reason: .stoppedAnswering), status: nil, socketPath: "/s")
        XCTAssertEqual(silent.headline, "Not answering")
        XCTAssertEqual(silent.health, .off)
    }

    func testConnectingAndIncompatible() {
        XCTAssertEqual(DashboardReducer.reduce(connection: .connecting, status: nil, socketPath: "/s").health, .attention)
        XCTAssertEqual(connected(nil).headline, "Connecting")
        let incompatible = DashboardReducer.reduce(connection: .incompatible(message: "nope"), status: nil, socketPath: "/s")
        XCTAssertEqual(incompatible.health, .off)
        XCTAssertEqual(incompatible.message, "nope")
    }

    func testIconSymbols() {
        XCTAssertEqual(OverallHealth.running.symbolName, "waveform")
        XCTAssertEqual(OverallHealth.attention.symbolName, "waveform.badge.exclamationmark")
        XCTAssertEqual(OverallHealth.off.symbolName, "waveform.slash")
    }

    func testSettingsPayloadDecodesTheContractShape() throws {
        let settings = try decodeJSON(
            SettingsPayload.self,
            """
            {"bind": "lan", "api_key_set": false, "zeroconf": true,
             "stt": {"engine": "parakeet", "model": "m", "language": "en"},
             "tts": {"engine": "kokoro", "model": "k", "voice": "af_heart", "speed": 1.0},
             "browser_settings": false,
             "ports": {"stt_http": 8881, "tts_http": 8880, "stt_wyoming": 10300, "tts_wyoming": 10200},
             "paths": {"config": "/c", "logs": "/l", "state": "/s"}}
            """)
        XCTAssertTrue(settings.isLANWithoutAPIKey)
        XCTAssertEqual(settings.ports?.sttWyoming, 10300)
        XCTAssertEqual(settings.paths?.logs, "/l")
    }

    func testUnknownBindReadsAsLAN() throws {
        let settings = try decodeJSON(
            SettingsPayload.self,
            """
            {"bind": "0.0.0.0", "api_key_set": true,
             "stt": {"engine": "e", "model": "m", "language": "en"},
             "tts": {"engine": "e", "model": "m", "voice": "v", "speed": 1}}
            """)
        XCTAssertEqual(settings.bind, .lan)
    }

    func testDurations() {
        XCTAssertEqual(Formatting.duration(0), "0 s")
        XCTAssertEqual(Formatting.duration(59.9), "59 s")
        XCTAssertEqual(Formatting.duration(60), "1 min")
        XCTAssertEqual(Formatting.duration(3_600), "1 h")
        XCTAssertEqual(Formatting.duration(11_520), "3 h 12 min")
        XCTAssertEqual(Formatting.duration(86_400 * 2 + 3_600 * 4 + 5), "2 d 4 h")
        XCTAssertEqual(Formatting.duration(-3), "0 s")
    }

    func testEngineLine() {
        XCTAssertEqual(Formatting.engineLine(engine: "kokoro", model: "org/Kokoro-82M"), "kokoro · Kokoro-82M")
        XCTAssertEqual(Formatting.engineLine(engine: "fake", model: "fake"), "fake")
        XCTAssertEqual(Formatting.engineLine(engine: nil, model: "m"), "m")
        XCTAssertNil(Formatting.engineLine(engine: nil, model: nil))
    }
}
