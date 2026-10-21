import Foundation

// Payloads of the control protocol (protocol 1). Decoding is lenient: unknown fields are
// ignored, unknown enum values map to a fallback, and optional extras default sensibly, so a
// newer server with additions inside the same protocol version still decodes.

public enum ServiceKind: String, Codable, Sendable, CaseIterable, Identifiable {
    case stt, tts

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .stt: "Speech to text"
        case .tts: "Text to speech"
        }
    }

    public var shortTitle: String { rawValue.uppercased() }
}

public enum ServiceState: String, Codable, Sendable, Equatable {
    case running, starting, stopped, degraded, unknown
    /// The service is up but its model is not downloaded; `model` names it (ADR 0022).
    case missingModel = "missing_model"

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ServiceState(rawValue: raw) ?? .unknown
    }
}

public struct ServiceStatus: Codable, Sendable, Equatable {
    public var state: ServiceState
    public var pid: Int?
    public var health: JSONValue?
    public var engine: String?
    public var model: String?
    public var warm: Bool
    public var uptime: Double?
    public var restarts: Int

    public init(
        state: ServiceState, pid: Int? = nil, health: JSONValue? = nil, engine: String? = nil,
        model: String? = nil, warm: Bool = false, uptime: Double? = nil, restarts: Int = 0
    ) {
        self.state = state
        self.pid = pid
        self.health = health
        self.engine = engine
        self.model = model
        self.warm = warm
        self.uptime = uptime
        self.restarts = restarts
    }

    enum CodingKeys: String, CodingKey {
        case state, pid, health, engine, model, warm, restarts
        case uptime = "uptime_s"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try c.decodeIfPresent(ServiceState.self, forKey: .state) ?? .unknown
        pid = try c.decodeIfPresent(Int.self, forKey: .pid)
        health = try c.decodeIfPresent(JSONValue.self, forKey: .health)
        if health == .null { health = nil }
        engine = try c.decodeIfPresent(String.self, forKey: .engine)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        warm = try c.decodeIfPresent(Bool.self, forKey: .warm) ?? false
        uptime = try c.decodeIfPresent(Double.self, forKey: .uptime)
        restarts = try c.decodeIfPresent(Int.self, forKey: .restarts) ?? 0
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(state, forKey: .state)
        try c.encode(pid, forKey: .pid)
        try c.encode(health ?? .null, forKey: .health)
        try c.encode(engine, forKey: .engine)
        try c.encode(model, forKey: .model)
        try c.encode(warm, forKey: .warm)
        try c.encode(uptime, forKey: .uptime)
        try c.encode(restarts, forKey: .restarts)
    }
}

/// The `status` result and the `status` event.
public struct StatusPayload: Codable, Sendable, Equatable {
    public var installed: Bool
    public var services: [String: ServiceStatus]

    public init(installed: Bool, services: [String: ServiceStatus]) {
        self.installed = installed
        self.services = services
    }

    public subscript(kind: ServiceKind) -> ServiceStatus? { services[kind.rawValue] }
    public var watchdog: ServiceStatus? { services["watchdog"] }
}

public enum BindMode: String, Codable, Sendable, Equatable, CaseIterable {
    case loopback, lan
}

public struct STTSettings: Codable, Sendable, Equatable {
    public var engine: String
    public var model: String
    public var language: String

    public init(engine: String, model: String, language: String) {
        self.engine = engine
        self.model = model
        self.language = language
    }
}

public struct TTSSettings: Codable, Sendable, Equatable {
    public var engine: String
    public var model: String
    public var voice: String
    public var speed: Double

    public init(engine: String, model: String, voice: String, speed: Double) {
        self.engine = engine
        self.model = model
        self.voice = voice
        self.speed = speed
    }
}

public struct Ports: Codable, Sendable, Equatable {
    public var sttHTTP: Int
    public var ttsHTTP: Int
    public var sttWyoming: Int
    public var ttsWyoming: Int

    public init(sttHTTP: Int, ttsHTTP: Int, sttWyoming: Int, ttsWyoming: Int) {
        self.sttHTTP = sttHTTP
        self.ttsHTTP = ttsHTTP
        self.sttWyoming = sttWyoming
        self.ttsWyoming = ttsWyoming
    }

    enum CodingKeys: String, CodingKey {
        case sttHTTP = "stt_http"
        case ttsHTTP = "tts_http"
        case sttWyoming = "stt_wyoming"
        case ttsWyoming = "tts_wyoming"
    }
}

public struct Paths: Codable, Sendable, Equatable {
    public var config: String
    public var logs: String
    public var state: String

    public init(config: String, logs: String, state: String) {
        self.config = config
        self.logs = logs
        self.state = state
    }
}

/// The `settings.get` result and the `settings` event. Never contains the API key itself.
public struct SettingsPayload: Codable, Sendable, Equatable {
    public var bind: BindMode
    public var apiKeySet: Bool
    public var zeroconf: Bool
    public var stt: STTSettings
    public var tts: TTSSettings
    public var browserSettings: Bool
    public var ports: Ports?
    public var paths: Paths?

    public init(
        bind: BindMode, apiKeySet: Bool, zeroconf: Bool, stt: STTSettings, tts: TTSSettings,
        browserSettings: Bool, ports: Ports? = nil, paths: Paths? = nil
    ) {
        self.bind = bind
        self.apiKeySet = apiKeySet
        self.zeroconf = zeroconf
        self.stt = stt
        self.tts = tts
        self.browserSettings = browserSettings
        self.ports = ports
        self.paths = paths
    }

    enum CodingKeys: String, CodingKey {
        case bind, zeroconf, stt, tts, ports, paths
        case apiKeySet = "api_key_set"
        case browserSettings = "browser_settings"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // An unrecognised bind value is shown as LAN: the cautious reading.
        let rawBind = try c.decode(String.self, forKey: .bind)
        bind = BindMode(rawValue: rawBind) ?? .lan
        apiKeySet = try c.decode(Bool.self, forKey: .apiKeySet)
        zeroconf = try c.decodeIfPresent(Bool.self, forKey: .zeroconf) ?? false
        stt = try c.decode(STTSettings.self, forKey: .stt)
        tts = try c.decode(TTSSettings.self, forKey: .tts)
        browserSettings = try c.decodeIfPresent(Bool.self, forKey: .browserSettings) ?? false
        ports = try? c.decodeIfPresent(Ports.self, forKey: .ports)
        paths = try? c.decodeIfPresent(Paths.self, forKey: .paths)
    }

    /// The condition the contract asks the UI to warn about.
    public var isLANWithoutAPIKey: Bool { bind == .lan && !apiKeySet }
}

/// The `settings.set` result: the new settings plus what still needs a restart.
public struct SettingsSetResult: Decodable, Sendable, Equatable {
    public var settings: SettingsPayload
    public var restartRequired: [ServiceKind]
    public var warnings: [String]

    enum CodingKeys: String, CodingKey {
        case restartRequired = "restart_required"
        case warnings
    }

    public init(from decoder: Decoder) throws {
        settings = try SettingsPayload(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = try c.decodeIfPresent([String].self, forKey: .restartRequired) ?? []
        restartRequired = raw.compactMap(ServiceKind.init(rawValue:))
        warnings = try c.decodeIfPresent([String].self, forKey: .warnings) ?? []
    }
}

public struct STTEngineInfo: Codable, Sendable, Equatable {
    public var name: String
    /// A human name for the engine; added to the contract later, so it may be missing.
    public var label: String?
    public var models: [String]
    public var languages: [String]
    /// Each model's own languages, where they differ from the engine's (Nemotron's English
    /// model serves only `en-US`); may be missing or partial.
    public var modelLanguages: [String: [String]]

    public init(
        name: String, label: String? = nil, models: [String], languages: [String],
        modelLanguages: [String: [String]] = [:]
    ) {
        self.name = name
        self.label = label
        self.models = models
        self.languages = languages
        self.modelLanguages = modelLanguages
    }

    enum CodingKeys: String, CodingKey {
        case name, label, models, languages
        case modelLanguages = "model_languages"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        models = try c.decodeIfPresent([String].self, forKey: .models) ?? []
        languages = try c.decodeIfPresent([String].self, forKey: .languages) ?? []
        modelLanguages = (try? c.decodeIfPresent([String: [String]].self, forKey: .modelLanguages)) ?? [:]
    }

    public var displayName: String { label.flatMap { $0.isEmpty ? nil : $0 } ?? name }

    /// The languages `model` serves: its own, else the engine's.
    public func languages(of model: String) -> [String] {
        if let own = modelLanguages[model], !own.isEmpty { return own }
        return languages
    }

    /// Whether `model` serves `language`, matched on the primary subtag as the engines match
    /// (`en` is served by a model of `en-US`). A model with no languages listed serves any.
    public func serves(_ language: String, model: String) -> Bool {
        let served = languages(of: model)
        return served.isEmpty || served.contains { Self.primarySubtag($0) == Self.primarySubtag(language) }
    }

    static func primarySubtag(_ tag: String) -> String {
        tag.replacingOccurrences(of: "_", with: "-").split(separator: "-").first.map { $0.lowercased() } ?? ""
    }
}

/// The Speech to text pane's language rows: the configured language, marked when the model
/// in use does not serve it, and the languages that model serves. `name` turns a BCP-47 tag
/// into a display name.
public struct SpeechLanguages: Sendable, Equatable {
    public var language: String
    public var isServed: Bool
    public var served: [String]

    public init(_ settings: STTSettings, engine: STTEngineInfo?, name: (String) -> String) {
        language = name(settings.language)
        isServed = engine?.serves(settings.language, model: settings.model) ?? true
        served = (engine?.languages(of: settings.model) ?? []).map(name)
    }

    /// One line, the mark in words: "English: not served by this model".
    public var line: String { isServed ? language : "\(language): not served by this model" }
}

public struct TTSEngineInfo: Codable, Sendable, Equatable {
    public var name: String
    /// A human name for the engine; may be missing.
    public var label: String?
    public var models: [String]
    public var voices: [String]
    /// Human names for voice ids, such as "Heart (American English, female)"; may be missing
    /// or partial.
    public var voiceLabels: [String: String]
    /// Each voice's BCP-47 tag, such as "en-GB", for grouping; may be missing or partial.
    public var voiceLanguages: [String: String]
    public var languages: [String]

    public init(
        name: String, label: String? = nil, models: [String], voices: [String],
        voiceLabels: [String: String] = [:], voiceLanguages: [String: String] = [:],
        languages: [String] = []
    ) {
        self.name = name
        self.label = label
        self.models = models
        self.voices = voices
        self.voiceLabels = voiceLabels
        self.voiceLanguages = voiceLanguages
        self.languages = languages
    }

    enum CodingKeys: String, CodingKey {
        case name, label, models, voices, languages
        case voiceLabels = "voice_labels"
        case voiceLanguages = "voice_languages"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        models = try c.decodeIfPresent([String].self, forKey: .models) ?? []
        voices = try c.decodeIfPresent([String].self, forKey: .voices) ?? []
        voiceLabels = (try? c.decodeIfPresent([String: String].self, forKey: .voiceLabels)) ?? [:]
        voiceLanguages = (try? c.decodeIfPresent([String: String].self, forKey: .voiceLanguages)) ?? [:]
        languages = (try? c.decodeIfPresent([String].self, forKey: .languages)) ?? []
    }

    public var displayName: String { label.flatMap { $0.isEmpty ? nil : $0 } ?? name }

    /// The voice's label, or its id when the server has none.
    public func voiceName(_ id: String) -> String {
        voiceLabels[id].flatMap { $0.isEmpty ? nil : $0 } ?? id
    }
}

/// The `engines.list` result.
public struct EnginesPayload: Codable, Sendable, Equatable {
    public var stt: [STTEngineInfo]
    public var tts: [TTSEngineInfo]

    public init(stt: [STTEngineInfo], tts: [TTSEngineInfo]) {
        self.stt = stt
        self.tts = tts
    }

    public func sttEngine(named name: String) -> STTEngineInfo? { stt.first { $0.name == name } }
    public func ttsEngine(named name: String) -> TTSEngineInfo? { tts.first { $0.name == name } }
}

/// What `open` can open.
public enum OpenTarget: String, Sendable, CaseIterable {
    case statusPage = "status_page"
    case logsDirectory = "logs_dir"
    case configFile = "config_file"
}

/// What `service.restart` can restart.
public enum RestartTarget: Sendable, Equatable {
    case service(ServiceKind)
    case all

    public var argument: String {
        switch self {
        case .service(let kind): kind.rawValue
        case .all: "all"
        }
    }
}

/// The per-service outcome in a `service.restart` answer: `"restarting"`, or the error code
/// and message of a service that was refused (`"not_installed: ..."`).
public enum RestartDetail {
    public static func refused(in result: JSONValue) -> [ServiceKind: String] {
        guard case .object(let detail)? = result["detail"] else { return [:] }
        var refused: [ServiceKind: String] = [:]
        for (name, outcome) in detail {
            guard let kind = ServiceKind(rawValue: name), let text = outcome.stringValue,
                text != "restarting"
            else { continue }
            refused[kind] = text
        }
        return refused
    }
}

/// The keys `settings.set` accepts (the contract's list). The app and the fake server both
/// use this enum, so neither can drift from the other.
public enum SettingKey: String, Sendable, CaseIterable {
    case bind
    case apiKey = "api_key"
    case zeroconf
    case sttEngine = "stt.engine"
    case sttModel = "stt.model"
    case ttsEngine = "tts.engine"
    case ttsModel = "tts.model"
    case ttsVoice = "tts.voice"
    case ttsSpeed = "tts.speed"
    case browserSettings = "browser_settings"
}
