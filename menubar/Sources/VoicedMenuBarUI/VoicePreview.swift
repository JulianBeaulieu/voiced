import AVFoundation
import Foundation
import Observation
import VoicedControl

/// Where a preview's audio comes from: `AppModel` asks the watchdog (`tts.speak`), which asks
/// the running text to speech service with the API key it holds.
@MainActor
public protocol VoiceSpeaker: AnyObject {
    /// `text` in `voice` at the service's default speed, as a WAV file.
    func speak(_ text: String, voice: String) async throws -> Data
}

/// Plays a WAV file. `finished` is called when it ends by itself, never after `stop()`.
@MainActor
public protocol PreviewPlayer: AnyObject {
    func play(_ wav: Data, finished: @escaping @MainActor () -> Void) throws
    func stop()
}

extension AppModel: VoiceSpeaker {}

/// Hearing a voice before choosing it: the play button beside the Voice picker. One preview
/// at a time; starting another, or stopping, drops the one before.
@MainActor
@Observable
public final class VoicePreview {
    public enum State: Equatable, Sendable {
        case idle
        /// Waiting for the audio, which includes a model loading again after it was unloaded.
        case loading(voice: String)
        case playing(voice: String)
        /// One line for the pane; the next preview or stop clears it.
        case failed(voice: String, message: String)
    }

    public private(set) var state: State = .idle
    private let speaker: VoiceSpeaker
    private let player: PreviewPlayer
    /// Counts previews, so the answer to one that was stopped or replaced is dropped.
    private var attempt = 0

    public init(speaker: VoiceSpeaker, player: PreviewPlayer = SystemPreviewPlayer()) {
        self.speaker = speaker
        self.player = player
    }

    /// `voice` is being fetched or played, so its button stops it.
    public func isActive(_ voice: String) -> Bool {
        switch state {
        case .loading(let current), .playing(let current): current == voice
        case .idle, .failed: false
        }
    }

    /// The button: stops `voice` when it is active, else speaks `line` in it.
    public func toggle(_ voice: String, line: String) {
        if isActive(voice) {
            stop()
        } else {
            Task { await play(voice, line: line) }
        }
    }

    public func play(_ voice: String, line: String) async {
        stop()
        let current = attempt
        state = .loading(voice: voice)
        do {
            let wav = try await speaker.speak(line, voice: voice)
            guard current == attempt else { return }
            try player.play(wav) { [weak self] in
                guard let self, current == self.attempt else { return }
                self.state = .idle
            }
            state = .playing(voice: voice)
        } catch {
            guard current == attempt else { return }
            state = .failed(voice: voice, message: "Not played: " + AppModel.describe(error))
        }
    }

    /// Stops what plays and forgets a request in flight. The request itself runs on: cancelling
    /// it would close the control connection.
    public func stop() {
        attempt += 1
        player.stop()
        state = .idle
    }

    /// Why a preview cannot be asked for now, or nil when it can. A model unloaded while idle
    /// is fine: the request waits while it loads (ADR 0033).
    public nonisolated static func unavailableReason(_ status: ServiceStatus?) -> String? {
        switch status?.state {
        case .running?, .degraded?: nil
        case .off?: "Turn text to speech on to hear a voice."
        case .starting?: "Text to speech is starting; a voice can be heard once it runs."
        case .stopped?: "Text to speech is stopped; restart it to hear a voice."
        case .missingModel?: "Download the model to hear its voices."
        case .unknown?, nil: "Text to speech is not running, so no voice can be heard."
        }
    }
}

/// Plays through AVFoundation, on the default output.
@MainActor
public final class SystemPreviewPlayer: NSObject, PreviewPlayer, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    private var finished: (@MainActor () -> Void)?

    public override init() {}

    public func play(_ wav: Data, finished: @escaping @MainActor () -> Void) throws {
        stop()
        let player = try AVAudioPlayer(data: wav)
        player.delegate = self
        guard player.play() else { throw PreviewPlayerError.cannotPlay }
        self.player = player
        self.finished = finished
    }

    public func stop() {
        player?.stop()
        player = nil
        finished = nil
    }

    public nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let id = ObjectIdentifier(player)
        Task { @MainActor in self.didFinish(id) }
    }

    private func didFinish(_ id: ObjectIdentifier) {
        guard let player, ObjectIdentifier(player) == id else { return }
        let finished = self.finished
        self.player = nil
        self.finished = nil
        finished?()
    }
}

enum PreviewPlayerError: LocalizedError {
    case cannotPlay

    var errorDescription: String? { "The audio could not be played." }
}
