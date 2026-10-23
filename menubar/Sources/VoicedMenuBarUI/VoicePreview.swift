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
    /// The request in flight; cancelling it closes its connection, and the service stops.
    private var request: Task<Void, Never>?

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
            start(voice, line: line)
        }
    }

    /// Speaks `line` in `voice`, after stopping whatever came before. The task ends when the
    /// line plays or fails.
    @discardableResult
    public func start(_ voice: String, line: String) -> Task<Void, Never> {
        stop()
        let current = attempt
        state = .loading(voice: voice)
        let request = Task { await fetchAndPlay(voice, line: line, attempt: current) }
        self.request = request
        return request
    }

    /// `start`, waiting until the line plays or fails.
    public func play(_ voice: String, line: String) async {
        await start(voice, line: line).value
    }

    private func fetchAndPlay(_ voice: String, line: String, attempt current: Int) async {
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

    /// Stops what plays, and cancels a request in flight.
    public func stop() {
        attempt += 1
        request?.cancel()
        request = nil
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

/// The line a voice preview speaks: the voice saying its name in its own language, "Hi, I'm
/// Heart." A voice without a personal name (`voice_names` from the server: "Default", or one
/// whose name only describes it, such as OmniVoice's "Middle-aged") says "Hi, this is my
/// voice." A language without a line here speaks English.
public enum PreviewLine {
    /// The server's `tts.speak` limit; a name that would make the line longer is left out.
    static let maxLength = 60

    /// Primary language subtag -> (with a name, where `%@` is the name; without one). The
    /// languages of the shipped voices, and Kokoro's beyond English.
    static let lines: [String: (named: String, unnamed: String)] = [
        "en": ("Hi, I'm %@.", "Hi, this is my voice."),
        "fr": ("Bonjour, je m'appelle %@.", "Bonjour, voici ma voix."),
        "de": ("Hallo, ich bin %@.", "Hallo, das ist meine Stimme."),
        "es": ("Hola, soy %@.", "Hola, esta es mi voz."),
        "it": ("Ciao, sono %@.", "Ciao, questa è la mia voce."),
        "pt": ("Olá, eu sou %@.", "Olá, esta é a minha voz."),
        "ja": ("こんにちは、%@です。", "こんにちは、これが私の声です。"),
        "zh": ("你好，我是%@。", "你好，这是我的声音。"),
        "hi": ("नमस्ते, मैं %@ हूँ।", "नमस्ते, यह मेरी आवाज़ है।"),
        "ar": ("مرحبا، أنا %@.", "مرحبا، هذا صوتي."),
    ]

    /// Tags that speak another tag's line: Levantine Arabic (OmniVoice's `apc`), Cantonese.
    static let aliases = ["apc": "ar", "yue": "zh"]

    /// The line for `voice` of `engine`, from its name and language in `engines.list`.
    public static func text(for voice: String, of engine: TTSEngineInfo?) -> String {
        text(name: engine?.voiceNames[voice], language: engine?.voiceLanguages[voice])
    }

    /// The line in `language` (a BCP-47 tag) naming `name`, or the unnamed line without one.
    static func text(name: String?, language: String?) -> String {
        let base = language.flatMap { $0.split(whereSeparator: { $0 == "-" || $0 == "_" }).first }
            .map { $0.lowercased() }
        let line = base.flatMap { lines[aliases[$0] ?? $0] } ?? lines["en"]!
        guard let name, !name.isEmpty else { return line.unnamed }
        let named = line.named.replacingOccurrences(of: "%@", with: name)
        return named.count <= maxLength ? named : line.unnamed
    }
}
