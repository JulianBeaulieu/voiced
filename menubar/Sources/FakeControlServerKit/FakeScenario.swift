import Foundation
import VoicedControl

/// Everything the fake server knows. The API key is kept to answer `api_key_set` and is
/// never sent, as on the real server.
public struct FakeState: Sendable {
    public var installed: Bool
    public var services: [String: ServiceStatus]
    public var settings: SettingsPayload
    public var engines: EnginesPayload
    public var apiKey: String

    public subscript(kind: ServiceKind) -> ServiceStatus {
        get { services[kind.rawValue] ?? ServiceStatus(state: .unknown) }
        set { services[kind.rawValue] = newValue }
    }

    public var status: StatusPayload { StatusPayload(installed: installed, services: services) }
}

/// Scripted states for tests and for driving the app by hand.
public enum FakeScenario: String, CaseIterable, Sendable {
    /// Both services running and warm.
    case running
    /// Speech to text warm, text to speech still loading its model.
    case starting
    /// Text to speech degraded after a failed inference, with a restart behind it.
    case degraded
    /// Both services stopped; the watchdog is up.
    case stopped
    /// The watchdog answers (run by hand) but the LaunchDaemons are not installed.
    case notInstalled = "not-installed"
    /// Running, bound to the LAN, no API key: the settings warning.
    case lanWithoutKey = "lan-without-key"
    /// Says hello with protocol 2, which this app must refuse.
    case newerProtocol = "newer-protocol"
    /// Moves through running, starting, degraded and stopped every few seconds.
    case cycle
    /// Text to speech up but its model (Kokoro) not downloaded: `missing_model` (ADR 0022).
    case missingModel = "missing-model"

    public var helloProtocol: Int { self == .newerProtocol ? 2 : 1 }

    public var summary: String {
        switch self {
        case .running: "both services running and warm"
        case .starting: "text to speech loading its model"
        case .degraded: "text to speech degraded, restarted once"
        case .stopped: "both services stopped"
        case .notInstalled: "watchdog run by hand: installed = false, services running"
        case .lanWithoutKey: "bound to the LAN with no API key"
        case .newerProtocol: "hello with protocol 2"
        case .cycle: "cycles through the states every few seconds"
        case .missingModel: "text to speech up, its model not downloaded"
        }
    }

    public var initialState: FakeState {
        var state = FakeState.sample
        switch self {
        case .running, .newerProtocol, .cycle:
            break
        case .missingModel:
            state[.tts] = ServiceStatus(
                state: .missingModel, pid: 4243,
                health: [
                    "status": "missing_model",
                    "last_error": "mlx-community/Kokoro-82M-bf16 is not downloaded; run voiced models download mlx-community/Kokoro-82M-bf16",
                ], engine: "kokoro", model: "mlx-community/Kokoro-82M-bf16", uptime: 30)
        case .starting:
            state[.tts] = ServiceStatus(
                state: .starting, pid: 4243, engine: "kokoro",
                model: "mlx-community/Kokoro-82M-bf16", warm: false, uptime: 4)
        case .degraded:
            state[.tts].state = .degraded
            state[.tts].restarts = 1
            state[.tts].health = [
                "status": "degraded",
                "last_error": "RuntimeError: [METAL] Command buffer execution failed",
            ]
        case .stopped:
            for kind in ServiceKind.allCases {
                state[kind] = ServiceStatus(
                    state: .stopped, engine: state[kind].engine, model: state[kind].model)
            }
        case .notInstalled:
            state.installed = false
        case .lanWithoutKey:
            state.settings.bind = .lan
            state.settings.apiKeySet = false
            state.apiKey = ""
        }
        return state
    }

    /// What the fake's cache holds at the start: every row state in `FakeModelState.sample`,
    /// and for `missing-model` the configured Kokoro gone with its old version.
    public var initialModels: FakeModelState {
        var models = FakeModelState.sample
        if self == .missingModel {
            models.setDownloaded("mlx-community/Kokoro-82M-bf16", false)
            for e in models.list.engines.indices where models.list.engines[e].name == "kokoro" {
                models.list.engines[e].models[0].previous = []
            }
            models.recount()
        }
        return models
    }
}

extension FakeState {
    public static var sample: FakeState {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return FakeState(
            installed: true,
            services: [
                "stt": ServiceStatus(
                    state: .running, pid: 4242, engine: "parakeet",
                    model: "mlx-community/parakeet-tdt-0.6b-v3", warm: true, uptime: 11_520,
                    restarts: 0),
                "tts": ServiceStatus(
                    state: .running, pid: 4243, engine: "kokoro",
                    model: "mlx-community/Kokoro-82M-bf16", warm: true, uptime: 11_518,
                    restarts: 0),
                "watchdog": ServiceStatus(state: .running, pid: 4241, warm: true, uptime: 11_530),
            ],
            settings: SettingsPayload(
                bind: .loopback, apiKeySet: true, zeroconf: true,
                stt: STTSettings(
                    engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3", language: "en"),
                tts: TTSSettings(
                    engine: "kokoro", model: "mlx-community/Kokoro-82M-bf16", voice: "af_heart",
                    speed: 1.0),
                browserSettings: false,
                ports: Ports(sttHTTP: 8881, ttsHTTP: 8880, sttWyoming: 10300, ttsWyoming: 10200),
                paths: Paths(
                    config: "\(home)/.config/voiced/config.toml",
                    logs: "\(home)/Library/Logs/voiced", state: "\(home)/.voiced")),
            engines: EnginesPayload(
                stt: [
                    STTEngineInfo(
                        name: "parakeet", label: "Parakeet",
                        models: ["mlx-community/parakeet-tdt-0.6b-v3"], languages: ["en"]),
                    STTEngineInfo(name: "fake", label: "Fake", models: ["fake"], languages: ["en"]),
                ],
                tts: [
                    TTSEngineInfo(
                        name: "kokoro", label: "Kokoro", models: ["mlx-community/Kokoro-82M-bf16"],
                        voices: kokoroVoices, voiceLabels: kokoroLabels(kokoroVoices), languages: ["en"]),
                    TTSEngineInfo(
                        name: "soprano", label: "Soprano", models: ["mlx-community/Soprano-80M-bf16"],
                        voices: ["default"], voiceLabels: ["default": "Default"], languages: ["en"]),
                    TTSEngineInfo(
                        name: "phonon", label: "Phonon", models: ["gradium/phonon-31m-en", "gradium/phonon-40m-en"],
                        voices: ["default", "Easton", "Maeve", "The Snow Queen"],
                        voiceLabels: [
                            "default": "Default", "Easton": "Easton (American English, male)",
                            "Maeve": "Maeve (British English, female)", "The Snow Queen": "The Snow Queen",
                        ],
                        voiceLanguages: [
                            "default": "en", "Easton": "en-US", "Maeve": "en-GB", "The Snow Queen": "en",
                        ],
                        languages: ["en-US", "en-GB"]),
                    // No labels, as from an engine (or a server) that does not send them.
                    TTSEngineInfo(name: "fake", models: ["fake"], voices: ["fake"]),
                ]),
            apiKey: "sample-key")
    }
}

private let kokoroVoices = [
    "af_heart", "af_bella", "af_nicole", "af_sky", "am_adam", "am_michael",
    "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
]

/// Labels in the shape the real server derives from Kokoro's voice id convention.
private func kokoroLabels(_ ids: [String]) -> [String: String] {
    let accents: [Character: String] = ["a": "American English", "b": "British English"]
    var labels: [String: String] = [:]
    for id in ids {
        let parts = id.split(separator: "_", maxSplits: 1)
        guard parts.count == 2, let first = parts[0].first, let last = parts[0].last,
            let accent = accents[first]
        else { continue }
        labels[id] = "\(parts[1].capitalized) (\(accent), \(last == "f" ? "female" : "male"))"
    }
    return labels
}
