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
        ]
        for edit in edits {
            for key in args(edit).keys { XCTAssertNotNil(SettingKey(rawValue: key), key) }
        }
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
}
