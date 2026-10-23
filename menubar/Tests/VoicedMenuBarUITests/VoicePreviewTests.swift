import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

/// Answers like the watchdog would: at once, with an error, or after a while.
@MainActor
final class FakeSpeaker: VoiceSpeaker {
    enum Behaviour {
        case speaks
        case fails(Error)
        case slow(Duration)
    }

    var behaviour: Behaviour
    private(set) var asked: [(text: String, voice: String)] = []
    /// A slow answer that was cancelled: the preview's connection was closed.
    private(set) var cancelled = 0

    init(_ behaviour: Behaviour) {
        self.behaviour = behaviour
    }

    func speak(_ text: String, voice: String) async throws -> Data {
        asked.append((text, voice))
        switch behaviour {
        case .speaks: break
        case .fails(let error): throw error
        case .slow(let delay):
            do {
                try await Task.sleep(for: delay)
            } catch {
                cancelled += 1
                throw error
            }
        }
        return Data("RIFF \(voice)".utf8)
    }
}

/// Records what it was given and finishes when told to.
@MainActor
final class FakePlayer: PreviewPlayer {
    private(set) var played: [Data] = []
    private(set) var stops = 0
    var refuses = false
    private var finished: (@MainActor () -> Void)?

    func play(_ wav: Data, finished: @escaping @MainActor () -> Void) throws {
        if refuses { throw PreviewPlayerError.cannotPlay }
        played.append(wav)
        self.finished = finished
    }

    func stop() {
        stops += 1
        finished = nil
    }

    /// The audio ends by itself.
    func finish() {
        let finished = self.finished
        self.finished = nil
        finished?()
    }
}

@MainActor
final class VoicePreviewTests: XCTestCase {
    func testPlaysTheLineThenGoesIdle() async {
        let speaker = FakeSpeaker(.speaks)
        let player = FakePlayer()
        let preview = VoicePreview(speaker: speaker, player: player)

        await preview.play("af_heart", line: "Hi, I'm Heart.")
        XCTAssertEqual(preview.state, .playing(voice: "af_heart"))
        XCTAssertTrue(preview.isActive("af_heart"))
        XCTAssertFalse(preview.isActive("bf_emma"))
        XCTAssertEqual(speaker.asked.map(\.text), ["Hi, I'm Heart."])
        XCTAssertEqual(player.played, [Data("RIFF af_heart".utf8)])
        player.finish()
        XCTAssertEqual(preview.state, .idle)
    }

    func testAFailureIsOneLineForThatVoice() async {
        let remote = RemoteError(code: "service_unavailable", message: "the tts service is not answering")
        let preview = VoicePreview(speaker: FakeSpeaker(.fails(ControlError.remote(remote))), player: FakePlayer())

        await preview.play("af_heart", line: "Hi, I'm Heart.")
        guard case .failed(let voice, let message) = preview.state else { return XCTFail("\(preview.state)") }
        XCTAssertEqual(voice, "af_heart")
        XCTAssertTrue(message.hasPrefix("Not played: "), message)
        XCTAssertFalse(message.contains("\n"))
        XCTAssertFalse(preview.isActive("af_heart"), "the button offers to try again")
    }

    func testAPlayerThatCannotPlayIsAFailureToo() async {
        let player = FakePlayer()
        player.refuses = true
        let preview = VoicePreview(speaker: FakeSpeaker(.speaks), player: player)
        await preview.play("af_heart", line: "Hi.")
        XCTAssertEqual(preview.state, .failed(voice: "af_heart", message: "Not played: The audio could not be played."))
    }

    func testWhileWaitingTheButtonStopsAndCancelsTheRequest() async {
        let speaker = FakeSpeaker(.slow(.milliseconds(200)))
        let player = FakePlayer()
        let preview = VoicePreview(speaker: speaker, player: player)

        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        let loading = await eventually { preview.state == .loading(voice: "af_heart") }
        XCTAssertTrue(loading)
        XCTAssertTrue(preview.isActive("af_heart"), "a second click stops")
        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        XCTAssertEqual(preview.state, .idle)
        let cancelled = await eventually { speaker.cancelled == 1 }
        XCTAssertTrue(cancelled, "stop cancels the request, which closes its connection")
        XCTAssertEqual(preview.state, .idle)
        XCTAssertEqual(player.played, [])
    }

    func testASecondClickStopsPlayback() async {
        let player = FakePlayer()
        let preview = VoicePreview(speaker: FakeSpeaker(.speaks), player: player)
        await preview.play("af_heart", line: "Hi.")
        preview.toggle("af_heart", line: "Hi.")
        XCTAssertEqual(preview.state, .idle)
        player.finish()  // nothing left to finish
        XCTAssertEqual(preview.state, .idle)
    }

    func testAnotherVoiceReplacesTheOneBefore() async {
        let speaker = FakeSpeaker(.slow(.milliseconds(150)))
        let player = FakePlayer()
        let preview = VoicePreview(speaker: speaker, player: player)

        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        _ = await eventually { preview.state == .loading(voice: "af_heart") }
        speaker.behaviour = .speaks
        await preview.play("bf_emma", line: "Hi, I'm Emma.")
        try? await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(preview.state, .playing(voice: "bf_emma"))
        XCTAssertEqual(player.played, [Data("RIFF bf_emma".utf8)])
    }

    func testUnavailableWhileTheServiceCannotSpeak() {
        XCTAssertNil(VoicePreview.unavailableReason(ServiceStatus(state: .running)))
        XCTAssertNil(VoicePreview.unavailableReason(ServiceStatus(state: .degraded)))
        let idle = ServiceStatus(state: .running, health: ["status": "idle", "model_loaded": false])
        XCTAssertNil(VoicePreview.unavailableReason(idle), "the request waits while the model loads")
        XCTAssertEqual(VoicePreview.unavailableReason(ServiceStatus(state: .off)), "Turn text to speech on to hear a voice.")
        for state in [ServiceState.starting, .stopped, .missingModel, .unknown] {
            XCTAssertNotNil(VoicePreview.unavailableReason(ServiceStatus(state: state)), "\(state)")
        }
        XCTAssertNotNil(VoicePreview.unavailableReason(nil))
    }

    /// Through the real client to the fake server: the WAV reaches the player.
    func testAppModelSpeaksThroughTheControlSocket() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = AppModel(socketPath: server.socketPath)
        model.start()
        defer { model.stop() }
        let ready = await eventually { model.settings != nil }
        XCTAssertTrue(ready)
        let player = FakePlayer()
        let preview = VoicePreview(speaker: model, player: player)

        await preview.play("af_heart", line: "Hi, I'm Heart.")
        XCTAssertEqual(preview.state, .playing(voice: "af_heart"))
        XCTAssertEqual(player.played.first.map { String(decoding: $0.prefix(4), as: UTF8.self) }, "RIFF")
        XCTAssertEqual(server.requests.last { $0.op == "tts.speak" }?.args["text"], "Hi, I'm Heart.")
    }

    /// A preview has a connection of its own, so the app's requests go on while it loads, and
    /// stopping it closes only that one.
    func testAPreviewDoesNotHoldUpTheAppsConnection() async throws {
        let server = try startServer { $0.speakDelay = 5 }
        defer { server.stop() }
        let model = AppModel(socketPath: server.socketPath)
        model.start()
        defer { model.stop() }
        let ready = await eventually { model.settings != nil }
        XCTAssertTrue(ready)
        let connections = server.acceptedConnections
        let preview = VoicePreview(speaker: model, player: FakePlayer())

        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        let asked = await eventually { server.requests.contains { $0.op == "tts.speak" } }
        XCTAssertTrue(asked)
        XCTAssertEqual(server.acceptedConnections, connections + 1)
        let started = ContinuousClock.now
        let saved = await model.apply(.ttsVoice("bf_emma"))
        XCTAssertTrue(saved)
        XCTAssertLessThan(ContinuousClock.now - started, .seconds(1))
        XCTAssertEqual(server.openConnections, 2)
        preview.stop()
        XCTAssertEqual(preview.state, .idle)
        let closed = await eventually { server.openConnections == 1 }
        XCTAssertTrue(closed, "stop closes the preview's connection, which the server sees")
        XCTAssertEqual(model.connection, .connected(version: "0.0.1"), "the app's own connection stays")
    }

    /// A new default speed needs no restart, so nothing is offered above the pane.
    func testSpeedSavesWithoutARestart() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = AppModel(socketPath: server.socketPath)
        model.start()
        defer { model.stop() }
        let ready = await eventually { model.settings != nil }
        XCTAssertTrue(ready)

        let saved = await model.apply(.ttsSpeed(1 + 0.1))
        XCTAssertTrue(saved)
        XCTAssertEqual(model.settings?.tts.speed, 1.1)
        XCTAssertEqual(model.restartRequired, [])
        XCTAssertEqual(server.currentState.settings.tts.speed, 1.1)
    }
}

final class PreviewLineTests: XCTestCase {
    func testTheVoiceSaysItsNameInItsLanguage() {
        let kokoro = FakeState.sample.engines.ttsEngine(named: "kokoro")
        XCTAssertEqual(PreviewLine.text(for: "af_heart", of: kokoro), "Hi, I'm Heart.")
        XCTAssertEqual(PreviewLine.text(name: "Léa", language: "fr-FR"), "Bonjour, je m'appelle Léa.")
        XCTAssertEqual(PreviewLine.text(name: "Hans", language: "de"), "Hallo, ich bin Hans.")
        XCTAssertEqual(PreviewLine.text(name: "Xiaoxiao", language: "zh_CN"), "你好，我是Xiaoxiao。")
        XCTAssertEqual(PreviewLine.text(name: "Ming", language: "yue"), "你好，我是Ming。")
        XCTAssertEqual(PreviewLine.text(name: nil, language: "apc"), "مرحبا، هذا صوتي.")
    }

    func testEveryLanguageOfTheSampleVoicesHasALine() {
        for engine in FakeState.sample.engines.tts {
            for (voice, language) in engine.voiceLanguages {
                let base = String(language.prefix { $0 != "-" && $0 != "_" }).lowercased()
                XCTAssertNotNil(PreviewLine.lines[PreviewLine.aliases[base] ?? base], "\(engine.name) \(voice)")
            }
        }
        // The languages of the shipped voices beyond the sample (OmniVoice, Marvis, Kokoro's).
        for language in ["de", "fr", "es", "zh", "apc", "ja", "hi", "it", "pt"] {
            XCTAssertNotEqual(PreviewLine.text(name: nil, language: language), "Hi, this is my voice.", language)
        }
    }

    func testALanguageWithoutALineSpeaksEnglish() {
        XCTAssertEqual(PreviewLine.text(name: "Olga", language: "ru-RU"), "Hi, I'm Olga.")
        XCTAssertEqual(PreviewLine.text(name: "Olga", language: nil), "Hi, I'm Olga.")
    }

    func testAVoiceWithoutAPersonalNameIsNotCalledByIt() {
        let engines = FakeState.sample.engines
        XCTAssertEqual(PreviewLine.text(for: "default", of: engines.ttsEngine(named: "soprano")), "Hi, this is my voice.")
        XCTAssertEqual(PreviewLine.text(for: "default", of: engines.ttsEngine(named: "phonon")), "Hi, this is my voice.")
        XCTAssertEqual(PreviewLine.text(for: "fake", of: engines.ttsEngine(named: "fake")), "Hi, this is my voice.")
        XCTAssertEqual(PreviewLine.text(for: "The Snow Queen", of: engines.ttsEngine(named: "phonon")), "Hi, I'm The Snow Queen.")
        // OmniVoice and Marvis label voices with descriptions; the server sends no names for them.
        let omnivoice = TTSEngineInfo(
            name: "omnivoice", models: [], voices: ["de-DE-male"],
            voiceLabels: ["de-DE-male": "Middle-aged (German, male)"], voiceLanguages: ["de-DE-male": "de-DE"])
        XCTAssertEqual(PreviewLine.text(for: "de-DE-male", of: omnivoice), "Hallo, das ist meine Stimme.")
        let marvis = TTSEngineInfo(
            name: "marvis", models: [], voices: ["conversational_fr"],
            voiceLabels: ["conversational_fr": "Conversational FR (French, male)"],
            voiceLanguages: ["conversational_fr": "fr"])
        XCTAssertEqual(PreviewLine.text(for: "conversational_fr", of: marvis), "Bonjour, voici ma voix.")
    }

    func testANameTooLongForTheServersLimitIsLeftOut() {
        let long = String(repeating: "x", count: PreviewLine.maxLength)
        XCTAssertEqual(PreviewLine.text(name: long, language: "en"), "Hi, this is my voice.")
        // Counted in code points, as the server counts: "मैं" is one or two graphemes, three scalars.
        let hindi = String(repeating: "मैं", count: 15)
        XCTAssertLessThan(hindi.count, PreviewLine.maxLength)
        XCTAssertEqual(PreviewLine.text(name: hindi, language: "hi"), "नमस्ते, यह मेरी आवाज़ है।")
    }
}
