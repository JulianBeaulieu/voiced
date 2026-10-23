import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

final class SettingsEditTests: XCTestCase {
    private let current = FakeState.sample.settings
    private let engines = FakeState.sample.engines

    private func args(_ edit: SettingsEdit) -> [String: JSONValue] {
        edit.arguments(current: current, engines: engines)
    }

    func testSimpleFields() {
        XCTAssertEqual(args(.bind(.lan)), ["bind": "lan"])
        XCTAssertEqual(args(.zeroconf(false)), ["zeroconf": false])
        XCTAssertEqual(args(.browserSettings(true)), ["browser_settings": true])
        XCTAssertEqual(args(.ttsVoice("bf_emma")), ["tts.voice": "bf_emma"])
    }

    func testEveryEmittedKeyIsASettingKey() {
        let edits: [SettingsEdit] = [
            .bind(.lan), .apiKey("k"), .clearAPIKey, .zeroconf(false), .browserSettings(true),
            .sttEngine("fake"), .ttsEngine("soprano"), .ttsVoice("bf_emma"),
            .enabled(.stt, false), .idleUnload(.tts, seconds: 300), .ttsSpeed(1.2),
        ]
        for edit in edits {
            for key in args(edit).keys { XCTAssertNotNil(SettingKey(rawValue: key), key) }
        }
    }

    func testSpeedIsSentRoundedToHundredths() {
        XCTAssertEqual(args(.ttsSpeed(1.1 + 0.1)), ["tts.speed": 1.2])
        XCTAssertEqual(args(.ttsSpeed(1.0000001)), [:], "the same speed once rounded")
    }

    func testUnchangedValuesSendNothing() {
        XCTAssertEqual(args(.bind(.loopback)), [:])
        XCTAssertEqual(args(.zeroconf(true)), [:])
        XCTAssertEqual(args(.ttsVoice("af_heart")), [:])
        XCTAssertEqual(args(.ttsEngine("kokoro")), [:])
        XCTAssertEqual(args(.sttEngine("parakeet")), [:])
    }

    func testAPIKeyIsTrimmedAndNeverEmpty() {
        XCTAssertEqual(args(.apiKey("  s3cret\n")), ["api_key": "s3cret"])
        XCTAssertEqual(args(.apiKey("   ")), [:], "an empty key would clear it")
        XCTAssertEqual(args(.clearAPIKey), ["api_key": ""])
        var noKey = current
        noKey.apiKeySet = false
        XCTAssertEqual(SettingsEdit.clearAPIKey.arguments(current: noKey, engines: engines), [:])
    }

    func testSwitchingTTSEngineCarriesAValidModelAndVoice() {
        XCTAssertEqual(
            args(.ttsEngine("soprano")),
            [
                "tts.engine": "soprano", "tts.model": "mlx-community/Soprano-80M-bf16",
                "tts.voice": "default",
            ])
    }

    func testSwitchingTTSEngineKeepsAVoiceTheNewEngineAlsoHas() {
        var catalogue = engines
        catalogue.tts.append(
            TTSEngineInfo(name: "kokoro2", models: ["mlx-community/Kokoro-82M-bf16"], voices: ["af_bella", "af_heart"]))
        XCTAssertEqual(
            SettingsEdit.ttsEngine("kokoro2").arguments(current: current, engines: catalogue),
            ["tts.engine": "kokoro2"])
    }

    func testSwitchingSTTEngineCarriesAModel() {
        XCTAssertEqual(args(.sttEngine("fake")), ["stt.engine": "fake", "stt.model": "fake"])
    }

    func testWithoutACatalogueOnlyTheEngineIsSent() {
        XCTAssertEqual(
            SettingsEdit.ttsEngine("soprano").arguments(current: current, engines: nil),
            ["tts.engine": "soprano"])
    }

    func testTheFakeServerAcceptsEveryEngineSwitchTheAppSends() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }
        var settings = try await client.settings()
        for edit in [SettingsEdit.ttsEngine("soprano"), .ttsEngine("fake"), .ttsEngine("kokoro"), .sttEngine("fake")] {
            let result = try await client.setSettings(edit.arguments(current: settings, engines: engines))
            settings = result.settings
        }
        XCTAssertEqual(settings.tts.engine, "kokoro")
        XCTAssertEqual(settings.tts.voice, "af_heart")
        XCTAssertEqual(settings.stt.engine, "fake")
    }

    // MARK: - Off and idle unload (ADR 0033)

    func testOffAndIdleUnload() {
        XCTAssertEqual(args(.enabled(.stt, false)), ["stt.enabled": false])
        XCTAssertEqual(args(.enabled(.tts, true)), [:], "already on")
        XCTAssertEqual(args(.idleUnload(.tts, seconds: 300)), ["tts.idle_unload_s": 300])
        XCTAssertEqual(args(.idleUnload(.stt, seconds: 0)), [:], "already never")
    }

    func testOffAndIdleUnloadDecodeAndDefaultForAnOlderServer() throws {
        let newer = try decodeJSON(
            STTSettings.self,
            #"{"engine": "parakeet", "model": "m", "language": "en", "enabled": false, "idle_unload_s": 900}"#)
        XCTAssertEqual(newer.enabled, false)
        XCTAssertEqual(newer.idleUnloadS, 900)
        var payload = current
        payload.stt = newer
        XCTAssertFalse(payload.isEnabled(.stt))
        XCTAssertEqual(payload.idleUnloadSeconds(.stt), 900)
        // An older voiced sends neither: on, never unloaded.
        XCTAssertTrue(current.isEnabled(.tts))
        XCTAssertEqual(current.idleUnloadSeconds(.tts), 0)
        let roundTrip = try decodeJSON(
            STTSettings.self, String(decoding: try JSONEncoder().encode(newer), as: UTF8.self))
        XCTAssertEqual(roundTrip, newer)
    }

    func testIdleUnloadChoices() {
        XCTAssertEqual(IdleUnload.choices(including: 300), [0, 60, 300, 900, 3_600])
        XCTAssertEqual(IdleUnload.choices(including: 90), [0, 60, 90, 300, 900, 3_600])
        XCTAssertEqual(IdleUnload.label(0), "Never")
        XCTAssertEqual(IdleUnload.label(60), "After 1 minute unused")
        XCTAssertEqual(IdleUnload.label(900), "After 15 minutes unused")
        XCTAssertEqual(IdleUnload.label(3_600), "After 1 hour unused")
        XCTAssertEqual(IdleUnload.label(90), "After 90 seconds unused")
    }
}
