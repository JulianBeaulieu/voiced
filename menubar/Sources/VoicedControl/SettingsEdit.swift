import Foundation

/// One change the user makes in the settings window, and the `settings.set` arguments it
/// becomes. Engine switches carry a matching model and voice, because the server validates
/// the whole section and an engine with another engine's model is not a valid setting. A
/// model row's Use names its model, sent with the engine so the server does not pick the
/// engine's default (ADR 0022).
public enum SettingsEdit: Sendable, Equatable {
    case bind(BindMode)
    case apiKey(String)
    case clearAPIKey
    case zeroconf(Bool)
    case browserSettings(Bool)
    case sttEngine(String, model: String? = nil)
    case ttsEngine(String, model: String? = nil)
    case ttsVoice(String)
    /// On or off (ADR 0033).
    case enabled(ServiceKind, Bool)
    /// Seconds unused before the model is unloaded; 0 is never.
    case idleUnload(ServiceKind, seconds: Int)

    /// The arguments for `settings.set`, or an empty dictionary when the edit changes nothing.
    public func arguments(current: SettingsPayload, engines: EnginesPayload?) -> [String: JSONValue] {
        var args: [SettingKey: JSONValue] = [:]
        switch self {
        case .bind(let mode):
            if mode != current.bind { args[.bind] = .string(mode.rawValue) }
        case .apiKey(let key):
            // An empty key would clear it; clearing is its own explicit action.
            let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { args[.apiKey] = .string(trimmed) }
        case .clearAPIKey:
            if current.apiKeySet { args[.apiKey] = "" }
        case .zeroconf(let on):
            if on != current.zeroconf { args[.zeroconf] = .bool(on) }
        case .browserSettings(let on):
            if on != current.browserSettings { args[.browserSettings] = .bool(on) }
        case .ttsVoice(let voice):
            if voice != current.tts.voice { args[.ttsVoice] = .string(voice) }
        case .enabled(let kind, let on):
            if on != current.residency(kind).enabled { args[.enabled(kind)] = .bool(on) }
        case .idleUnload(let kind, let seconds):
            if seconds != current.residency(kind).idleUnloadSeconds {
                args[.idleUnload(kind)] = .number(Double(seconds))
            }
        case .sttEngine(let name, let chosen):
            guard name != current.stt.engine || (chosen != nil && chosen != current.stt.model) else { break }
            args[.sttEngine] = .string(name)
            if let chosen {
                args[.sttModel] = .string(chosen)
            } else if let info = engines?.sttEngine(named: name),
                let model = pick(current.stt.model, from: info.models), model != current.stt.model
            {
                args[.sttModel] = .string(model)
            }
        case .ttsEngine(let name, let chosen):
            guard name != current.tts.engine || (chosen != nil && chosen != current.tts.model) else { break }
            args[.ttsEngine] = .string(name)
            if let chosen {
                args[.ttsModel] = .string(chosen)
            }
            if let info = engines?.ttsEngine(named: name) {
                if chosen == nil, let model = pick(current.tts.model, from: info.models), model != current.tts.model {
                    args[.ttsModel] = .string(model)
                }
                if let voice = pick(current.tts.voice, from: info.voices), voice != current.tts.voice {
                    args[.ttsVoice] = .string(voice)
                }
            }
        }
        return Dictionary(uniqueKeysWithValues: args.map { ($0.key.rawValue, $0.value) })
    }

    /// Keeps the current value when the new engine offers it, else takes the engine's first.
    private func pick(_ current: String, from options: [String]) -> String? {
        options.contains(current) ? current : options.first
    }
}

/// The choices for when a service unloads its model (ADR 0033).
public enum IdleUnload {
    /// Never, then 1, 5, 15 and 60 minutes unused.
    public static let standard = [0, 60, 300, 900, 3_600]

    /// The standard choices, plus `current` when the config file holds another value.
    public static func choices(including current: Int) -> [Int] {
        standard.contains(current) ? standard : (standard + [current]).sorted()
    }

    /// "Never", "After 1 minute unused", "After 90 seconds unused".
    public static func label(_ seconds: Int) -> String {
        if seconds <= 0 { return "Never" }
        let span: String
        if seconds % 3_600 == 0 {
            span = seconds == 3_600 ? "1 hour" : "\(seconds / 3_600) hours"
        } else if seconds % 60 == 0 {
            span = seconds == 60 ? "1 minute" : "\(seconds / 60) minutes"
        } else {
            span = seconds == 1 ? "1 second" : "\(seconds) seconds"
        }
        return "After \(span) unused"
    }
}
