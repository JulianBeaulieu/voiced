import Foundation

// What `voiced models list --json` and `voiced models delete --yes --json` print (ADR 0022,
// docs/operations.md "Models"). The app runs the command rather than asking the control
// socket, so the list is there while the services are down. Decoding is lenient, as for the
// control protocol's payloads: unknown fields are ignored and missing ones default.

/// `voiced models list --json`.
public struct ModelList: Codable, Sendable, Equatable {
    /// The Hugging Face cache the models live in, shared with other tools.
    public var cache: String
    /// What voiced's models take in it, each revision counted once.
    public var totalBytes: Int64
    public var engines: [ModelEngine]

    public init(cache: String, totalBytes: Int64, engines: [ModelEngine]) {
        self.cache = cache
        self.totalBytes = totalBytes
        self.engines = engines
    }

    enum CodingKeys: String, CodingKey {
        case cache, engines
        case totalBytes = "total_bytes"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cache = try c.decodeIfPresent(String.self, forKey: .cache) ?? ""
        totalBytes = try c.decodeIfPresent(Int64.self, forKey: .totalBytes) ?? 0
        engines = try c.decodeIfPresent([ModelEngine].self, forKey: .engines) ?? []
    }

    public func engines(of kind: ServiceKind) -> [ModelEngine] { engines.filter { $0.kind == kind.rawValue } }

    /// The model with this id, and its engine.
    public func model(_ id: String) -> (engine: ModelEngine, model: CatalogueModel)? {
        for engine in engines {
            if let model = engine.models.first(where: { $0.id == id }) { return (engine, model) }
        }
        return nil
    }
}

public struct ModelEngine: Codable, Sendable, Equatable, Identifiable {
    /// The entry point name, what `stt.engine` and `tts.engine` hold.
    public var name: String
    public var label: String
    /// `stt` or `tts`.
    public var kind: String
    /// Whether the engine's entry point is in the environment; Use needs it.
    public var installed: Bool
    public var voices: [String]
    /// Empty for a plugin engine the catalogue does not know: it has nothing to manage.
    public var models: [CatalogueModel]
    /// Repositories every model of the engine needs, downloaded and deleted with them.
    public var shared: [CatalogueModel]

    public var id: String { "\(kind).\(name)" }

    public init(
        name: String, label: String, kind: String, installed: Bool = true, voices: [String] = [],
        models: [CatalogueModel], shared: [CatalogueModel] = []
    ) {
        self.name = name
        self.label = label
        self.kind = kind
        self.installed = installed
        self.voices = voices
        self.models = models
        self.shared = shared
    }

    enum CodingKeys: String, CodingKey {
        case name, label, kind, installed, voices, models, shared
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? name
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        installed = try c.decodeIfPresent(Bool.self, forKey: .installed) ?? true
        voices = (try? c.decodeIfPresent([String].self, forKey: .voices)) ?? []
        models = try c.decodeIfPresent([CatalogueModel].self, forKey: .models) ?? []
        shared = try c.decodeIfPresent([CatalogueModel].self, forKey: .shared) ?? []
    }
}

public struct CatalogueModel: Codable, Sendable, Equatable, Identifiable {
    public struct Revision: Codable, Sendable, Equatable {
        public var revision: String
        public var bytes: Int64

        public init(revision: String, bytes: Int64) {
            self.revision = revision
            self.bytes = bytes
        }
    }

    /// What `stt.model` and `tts.model` hold and what `download` and `delete` take.
    public var id: String
    public var repo: String
    public var license: String
    /// The model card.
    public var url: String?
    public var gated: Bool
    public var downloadBytes: Int64
    public var onDiskBytes: Int64
    /// Every locked file is in the snapshot at the pin, at its locked size.
    public var downloaded: Bool
    /// Older pinned revisions still in the cache.
    public var previous: [Revision]
    /// Branches pointing at the pin: another tool fetched it by name too.
    public var refs: [String]

    public init(
        id: String, repo: String? = nil, license: String, url: String? = nil, gated: Bool = false,
        downloadBytes: Int64, onDiskBytes: Int64 = 0, downloaded: Bool, previous: [Revision] = [],
        refs: [String] = []
    ) {
        self.id = id
        self.repo = repo ?? id
        self.license = license
        self.url = url ?? "https://huggingface.co/\(repo ?? id)"
        self.gated = gated
        self.downloadBytes = downloadBytes
        self.onDiskBytes = onDiskBytes
        self.downloaded = downloaded
        self.previous = previous
        self.refs = refs
    }

    enum CodingKeys: String, CodingKey {
        case id, repo, license, url, gated, downloaded, previous, refs
        case downloadBytes = "download_bytes"
        case onDiskBytes = "on_disk_bytes"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        repo = try c.decodeIfPresent(String.self, forKey: .repo) ?? id
        license = try c.decodeIfPresent(String.self, forKey: .license) ?? ""
        url = try c.decodeIfPresent(String.self, forKey: .url)
        gated = try c.decodeIfPresent(Bool.self, forKey: .gated) ?? false
        downloadBytes = try c.decodeIfPresent(Int64.self, forKey: .downloadBytes) ?? 0
        onDiskBytes = try c.decodeIfPresent(Int64.self, forKey: .onDiskBytes) ?? 0
        downloaded = try c.decodeIfPresent(Bool.self, forKey: .downloaded) ?? false
        previous = (try? c.decodeIfPresent([Revision].self, forKey: .previous)) ?? []
        refs = (try? c.decodeIfPresent([String].self, forKey: .refs)) ?? []
    }

    /// "parakeet-tdt-0.6b-v3": the last part of the id.
    public var name: String { id.split(separator: "/").last.map(String.init) ?? id }
    public var cardURL: URL? { url.flatMap(URL.init(string:)) }
    public var previousBytes: Int64 { previous.reduce(0) { $0 + $1.bytes } }
}

/// `voiced models delete <model> --yes --json` on success.
public struct ModelDeletion: Codable, Sendable, Equatable {
    public var model: String
    public var freed: Int64
    public var notes: [String]

    enum CodingKeys: String, CodingKey {
        case model, freed, notes
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        freed = try c.decodeIfPresent(Int64.self, forKey: .freed) ?? 0
        notes = (try? c.decodeIfPresent([String].self, forKey: .notes)) ?? []
    }
}

/// How a failed `voiced models` command is shown: the error codes of docs/operations.md
/// decoded into what to do, with the model card when the user must go there.
public struct ModelMessage: Sendable, Equatable {
    public var text: String
    public var link: URL?
    /// A command to run in Terminal, shown copyable.
    public var command: String?

    public init(text: String, link: URL? = nil, command: String? = nil) {
        self.text = text
        self.link = link
        self.command = command
    }

    /// A gated model before the token sheet exists (ADR 0022, "First implementation"):
    /// `huggingface_hub` finds the token `hf auth login` stores.
    public static func needsToken(_ model: CatalogueModel?) -> ModelMessage {
        ModelMessage(
            text: "Needs a Hugging Face token: accept the terms on the model's page, run this in "
                + "Terminal, then Download.",
            link: model?.cardURL, command: "hf auth login")
    }

    /// The message for an `error` code from `download --json-progress` or `delete --json`.
    /// Unknown codes show the command's own message.
    public static func failure(code: String?, message: String, model: CatalogueModel?) -> ModelMessage {
        switch code {
        case "token_missing", "gated_terms":
            return needsToken(model)
        case "token_invalid":
            return ModelMessage(
                text: "Hugging Face rejected the token. Log in again in Terminal, then Download.",
                link: model?.cardURL, command: "hf auth login")
        case "network":
            return ModelMessage(text: "Hugging Face could not be reached. Check the connection and try again.")
        case "disk":
            return ModelMessage(text: "Not enough free disk space. \(message)")
        case "busy":
            return ModelMessage(text: "voiced is installing or updating; try again when it ends.")
        case "in_use":
            return ModelMessage(text: "In use. Choose another model first.")
        default:
            return ModelMessage(text: message.isEmpty ? "The command failed." : message)
        }
    }
}

/// What a model row shows (ADR 0022, section 5).
public enum ModelRowState: Sendable, Equatable {
    /// The configured model, downloaded.
    case inUse
    /// The configured model is not downloaded: the service reports `missing_model`.
    case inUseMissing
    case downloaded
    case notDownloaded
    /// Not downloaded at the pin, an older pinned revision cached: Update fetches the pin.
    case oldVersion
    /// Not downloaded, and Hugging Face shares it only with accounts that accepted its terms.
    case gated
    case downloading
}

public struct ModelRow: Sendable, Equatable, Identifiable {
    public var engine: ModelEngine
    /// Nil for a plugin engine the catalogue does not know: one row with Use only.
    public var model: CatalogueModel?
    public var state: ModelRowState

    public var id: String { model.map { "\(engine.id)/\($0.id)" } ?? engine.id }
    public var title: String { model?.name ?? engine.label }

    /// "2.5 GB on disk" or "65 MB download".
    public var sizeLine: String? {
        guard let model else { return nil }
        if model.downloaded { return "\(Formatting.bytes(model.onDiskBytes)) on disk" }
        return "\(Formatting.bytes(model.downloadBytes)) download"
    }

    public var stateLabel: String {
        if model == nil, state != .inUse { return "Manages its own model" }
        return switch state {
        case .inUse: "In use"
        case .inUseMissing: "In use, not downloaded"
        case .downloaded: "Downloaded"
        case .notDownloaded: "Not downloaded"
        case .oldVersion: "Update available"
        case .gated: "Needs a Hugging Face token"
        case .downloading: "Downloading"
        }
    }

    /// Download, or Update for an old version, while the pin is not on disk.
    public var downloadTitle: String? {
        guard model != nil else { return nil }
        switch state {
        case .notDownloaded, .gated, .inUseMissing: return "Download"
        case .oldVersion: return "Update (\(Formatting.bytes(model?.downloadBytes ?? 0)))"
        case .inUse, .downloaded, .downloading: return nil
        }
    }

    public var canUse: Bool {
        guard engine.installed else { return false }
        switch state {
        case .downloaded, .notDownloaded, .oldVersion, .gated: return true
        case .inUse, .inUseMissing, .downloading: return false
        }
    }

    public var canDelete: Bool { model?.downloaded == true && state != .inUse }

    /// "Delete old version (2.1 GB)", on any row with an older pinned revision cached.
    public var deleteOldTitle: String? {
        guard let model, !model.previous.isEmpty else { return nil }
        return "Delete Old Version (\(Formatting.bytes(model.previousBytes)))"
    }

    /// The rows of one service's pane, grouped by engine in the list's order. `engine` and
    /// `model` are the configured ones, from the settings; `downloading` the id in flight.
    public static func rows(
        _ list: ModelList, kind: ServiceKind, engine current: String, model configured: String,
        downloading: String?
    ) -> [ModelRow] {
        list.engines(of: kind).flatMap { engine -> [ModelRow] in
            guard !engine.models.isEmpty else {
                return [ModelRow(engine: engine, model: nil, state: engine.name == current ? .inUse : .downloaded)]
            }
            return engine.models.map { model in
                let inUse = engine.name == current && model.id == configured
                let state: ModelRowState
                if model.id == downloading {
                    state = .downloading
                } else if inUse {
                    state = model.downloaded ? .inUse : .inUseMissing
                } else if model.downloaded {
                    state = .downloaded
                } else if model.gated {
                    state = .gated
                } else {
                    state = model.previous.isEmpty ? .notDownloaded : .oldVersion
                }
                return ModelRow(engine: engine, model: model, state: state)
            }
        }
    }

    /// "Delete <model>? This frees <freed>. Downloading it again takes <size>.", with the
    /// notes the delete itself prints: another tool's branch on the pin, and the engine's
    /// shared files going with its last downloaded model.
    public func deleteConfirmation() -> (title: String, message: String)? {
        guard let model else { return nil }
        let isLast = engine.models.allSatisfy { $0.id == model.id || !$0.downloaded }
        let shared = isLast ? engine.shared.filter(\.downloaded) : []
        let freed = model.onDiskBytes + shared.reduce(0) { $0 + $1.onDiskBytes }
        var message = "This frees \(Formatting.bytes(freed)). Downloading it again takes "
            + "\(Formatting.bytes(model.downloadBytes))."
        if let ref = model.refs.first {
            message += " This model is also cached as \(ref), perhaps by another app; that app "
                + "downloads it again if it needs it."
        }
        if !shared.isEmpty {
            message += " \(engine.label)'s shared files (\(shared.map(\.id).joined(separator: ", "))) are deleted with it."
        }
        return ("Delete \(model.name)?", message)
    }
}

extension Formatting {
    /// "2.5 GB", as Finder counts.
    public static func bytes(_ count: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: count, countStyle: .file)
    }
}
