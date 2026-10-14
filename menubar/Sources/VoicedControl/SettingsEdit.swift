import Foundation

/// One change the user makes in the settings pane, and the `settings.set` arguments it
/// becomes. Engine switches carry a matching model and voice, because the server validates
/// the whole section and an engine with another engine's model is not a valid setting.
public enum SettingsEdit: Sendable, Equatable {
    case bind(BindMode)
    case apiKey(String)
    case clearAPIKey
    case zeroconf(Bool)
    case browserSettings(Bool)
    case sttEngine(String)
    case sttModel(String)
    case ttsEngine(String)
    case ttsModel(String)
    case ttsVoice(String)
    case ttsSpeed(Double)

    /// The arguments for `settings.set`, or an empty dictionary when the edit changes nothing.
    public func arguments(current: SettingsPayload, engines: EnginesPayload?) -> [String: JSONValue] {
        switch self {
        case .bind(let mode):
            return mode == current.bind ? [:] : ["bind": .string(mode.rawValue)]
        case .apiKey(let key):
            let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
            // An empty key would clear it; clearing is its own explicit action.
            return trimmed.isEmpty ? [:] : ["api_key": .string(trimmed)]
        case .clearAPIKey:
            return current.apiKeySet ? ["api_key": ""] : [:]
        case .zeroconf(let on):
            return on == current.zeroconf ? [:] : ["zeroconf": .bool(on)]
        case .browserSettings(let on):
            return on == current.browserSettings ? [:] : ["browser_settings": .bool(on)]
        case .sttModel(let model):
            return model == current.stt.model ? [:] : ["stt.model": .string(model)]
        case .ttsModel(let model):
            return model == current.tts.model ? [:] : ["tts.model": .string(model)]
        case .ttsVoice(let voice):
            return voice == current.tts.voice ? [:] : ["tts.voice": .string(voice)]
        case .ttsSpeed(let speed):
            return speed == current.tts.speed ? [:] : ["tts.speed": .number(speed)]
        case .sttEngine(let name):
            guard name != current.stt.engine else { return [:] }
            var args: [String: JSONValue] = ["stt.engine": .string(name)]
            if let info = engines?.sttEngine(named: name),
                let model = pick(current.stt.model, from: info.models), model != current.stt.model
            {
                args["stt.model"] = .string(model)
            }
            return args
        case .ttsEngine(let name):
            guard name != current.tts.engine else { return [:] }
            var args: [String: JSONValue] = ["tts.engine": .string(name)]
            if let info = engines?.ttsEngine(named: name) {
                if let model = pick(current.tts.model, from: info.models), model != current.tts.model {
                    args["tts.model"] = .string(model)
                }
                if let voice = pick(current.tts.voice, from: info.voices), voice != current.tts.voice {
                    args["tts.voice"] = .string(voice)
                }
            }
            return args
        }
    }

    /// Keeps the current value when the new engine offers it, else takes the engine's first.
    private func pick(_ current: String, from options: [String]) -> String? {
        options.contains(current) ? current : options.first
    }
}
