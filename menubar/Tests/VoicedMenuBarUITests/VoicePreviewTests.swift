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

    init(_ behaviour: Behaviour) {
        self.behaviour = behaviour
    }

    func speak(_ text: String, voice: String) async throws -> Data {
        asked.append((text, voice))
        switch behaviour {
        case .speaks: break
        case .fails(let error): throw error
        case .slow(let delay): try await Task.sleep(for: delay)
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

    func testWhileWaitingTheButtonStopsAndTheLateAnswerIsDropped() async {
        let speaker = FakeSpeaker(.slow(.milliseconds(200)))
        let player = FakePlayer()
        let preview = VoicePreview(speaker: speaker, player: player)

        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        let loading = await eventually { preview.state == .loading(voice: "af_heart") }
        XCTAssertTrue(loading)
        XCTAssertTrue(preview.isActive("af_heart"), "a second click stops")
        preview.toggle("af_heart", line: "Hi, I'm Heart.")
        XCTAssertEqual(preview.state, .idle)
        try? await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(preview.state, .idle)
        XCTAssertEqual(player.played, [], "the answer came after the stop")
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

    /// A new default speed needs no restart, so nothing is offered above the pane.
    func testSpeedSavesWithoutARestart() async throws {
        let server = try startServer()
        defer { server.stop() }
        let model = AppModel(socketPath: server.socketPath)
        model.start()
        defer { model.stop() }
        let ready = await eventually { model.settings != nil }
        XCTAssertTrue(ready)

        let saved = await model.apply(.ttsSpeed(SpeechSpeed.stepped(1, up: true)))
        XCTAssertTrue(saved)
        XCTAssertEqual(model.settings?.tts.speed, 1.1)
        XCTAssertEqual(model.restartRequired, [])
        XCTAssertEqual(server.currentState.settings.tts.speed, 1.1)
    }
}
