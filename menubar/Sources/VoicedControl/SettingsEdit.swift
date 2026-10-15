import Foundation

/// One change the user makes in the settings window, and the `settings.set` arguments it
/// becomes. Engine switches carry a matching model and voice, because the server validates
/// the whole section and an engine with another engine's model is not a valid setting.
public enum SettingsEdit: Sendable, Equatable {
    case bind(BindMode)
    case apiKey(String)
    case clearAPIKey
    case zeroconf(Bool)
    case browserSettings(Bool)
    case sttEngine(String)
    case ttsEngine(String)
    case ttsVoice(String)

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
        case .sttEngine(let name):
            guard name != current.stt.engine else { break }
            args[.sttEngine] = .string(name)
            if let info = engines?.sttEngine(named: name),
                let model = pick(current.stt.model, from: info.models), model != current.stt.model
            {
                args[.sttModel] = .string(model)
            }
        case .ttsEngine(let name):
            guard name != current.tts.engine else { break }
            args[.ttsEngine] = .string(name)
            if let info = engines?.ttsEngine(named: name) {
                if let model = pick(current.tts.model, from: info.models), model != current.tts.model {
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
