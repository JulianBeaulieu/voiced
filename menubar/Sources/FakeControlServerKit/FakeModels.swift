import Foundation
import VoicedControl

/// What the fake's Hugging Face cache holds: the catalogue as `voiced models list --json`
/// prints it, and the code each model's download fails with, if any.
public struct FakeModelState: Codable, Sendable, Equatable {
    public var list: ModelList
    /// Model id to an ADR 0022 error code (`token_missing`, `network`, ...).
    public var failures: [String: String]

    public init(list: ModelList, failures: [String: String] = [:]) {
        self.list = list
        self.failures = failures
    }

    /// Whether `id` is a catalogued model that is not downloaded; nil for one the catalogue
    /// does not know, which the real server does not check either.
    public func isMissing(_ id: String) -> Bool? {
        list.model(id).map { !$0.model.downloaded }
    }

    public mutating func setDownloaded(_ id: String, _ downloaded: Bool) {
        for e in list.engines.indices {
            for kind in [\ModelEngine.models, \ModelEngine.shared] {
                for m in list.engines[e][keyPath: kind].indices where list.engines[e][keyPath: kind][m].id == id {
                    list.engines[e][keyPath: kind][m].downloaded = downloaded
                    let model = list.engines[e][keyPath: kind][m]
                    list.engines[e][keyPath: kind][m].onDiskBytes = downloaded ? model.downloadBytes : 0
                }
            }
        }
        recount()
    }

    /// `total_bytes`: what is on disk, old versions included.
    public mutating func recount() {
        list.totalBytes = list.engines.flatMap { $0.models + $0.shared }
            .reduce(0) { $0 + ($1.downloaded ? $1.onDiskBytes : 0) + $1.previousBytes }
    }
}

/// The fake's cache on disk, a JSON file shared by the fake server (which refuses
/// `model_not_downloaded` and reports `missing_model` from it, as the real services read the
/// cache) and the fake `voiced models` command, which changes it, as the real CLI does.
public struct FakeModels: Sendable {
    public let file: URL

    public init(file: URL) {
        self.file = file
    }

    /// Next to the socket, so the fake command run by the app finds it through the app's
    /// `VOICED_CONTROL_SOCKET`.
    public init(socketPath: String) {
        self.init(file: URL(fileURLWithPath: socketPath + ".models.json"))
    }

    public func read() -> FakeModelState? {
        (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(FakeModelState.self, from: $0) }
    }

    public func write(_ state: FakeModelState) {
        try? JSONEncoder().encode(state).write(to: file, options: .atomic)
    }

    public func update(_ change: (inout FakeModelState) -> Void) {
        guard var state = read() else { return }
        change(&state)
        write(state)
    }

    // MARK: - The fake command

    /// `voiced models list --json`, `download <model> --json-progress` and `delete <model>
    /// [--previous] --yes --json`, with the real command's output shapes and exit statuses.
    /// `pause` runs between progress lines; a download takes ten of them.
    public func run(_ arguments: [String], output: (String) -> Void, pause: () -> Void) -> Int32 {
        guard let state = read() else {
            output(Self.object(["error": ["code": "failed", "message": "No fake models at \(file.path)."]]))
            return 1
        }
        let operands = arguments.filter { !$0.hasPrefix("--") }
        switch (operands.first, operands.dropFirst().first) {
        case ("list"?, nil):
            output(Self.encoded(state.list))
            return 0
        case ("download"?, let id?):
            return download(id, state: state, output: output, pause: pause)
        case ("delete"?, let id?):
            return delete(id, previous: arguments.contains("--previous"), state: state, output: output)
        default:
            FileHandle.standardError.write(Data("usage: voiced models list|download|delete\n".utf8))
            return 2
        }
    }

    private func download(_ id: String, state: FakeModelState, output: (String) -> Void, pause: () -> Void) -> Int32 {
        output(#"{"event": "hello", "protocol": 1, "version": "0.0.1"}"#)
        func fail(_ code: String, _ message: String) -> Int32 {
            output(Self.object(["event": "error", "code": code, "message": message, "repo": id]))
            return 1
        }
        guard let (engine, model) = state.list.model(id) else {
            return fail("failed", "\(id) is not in the catalogue.")
        }
        if let code = state.failures[id] {
            return fail(code, "The fake download of \(id) fails with \(code).")
        }
        for item in [model] + engine.shared where !item.downloaded {
            let steps: Int64 = 10
            for step in 0...steps {
                let bytes = item.downloadBytes * step / steps
                output(Self.object(["event": "download", "model": item.id, "bytes": bytes, "total": item.downloadBytes]))
                if step < steps { pause() }
            }
            update { $0.setDownloaded(item.id, true) }
        }
        output(#"{"event": "done"}"#)
        return 0
    }

    private func delete(_ id: String, previous: Bool, state: FakeModelState, output: (String) -> Void) -> Int32 {
        guard let (engine, model) = state.list.model(id) else {
            output(Self.object(["error": ["code": "failed", "message": "\(id) is not in the catalogue."]]))
            return 1
        }
        var deleted: [[String: Any]] = []
        var notes: [String] = []
        if previous {
            deleted = model.previous.map { ["repo": model.repo, "revision": $0.revision, "bytes": $0.bytes] }
            update { state in
                for e in state.list.engines.indices {
                    for m in state.list.engines[e].models.indices where state.list.engines[e].models[m].id == id {
                        state.list.engines[e].models[m].previous = []
                    }
                }
                state.setDownloaded(id, model.downloaded)
            }
        } else if model.downloaded {
            deleted = [["repo": model.repo, "revision": "fake", "bytes": model.onDiskBytes]]
            let isLast = engine.models.allSatisfy { $0.id == id || !$0.downloaded }
            let shared = isLast ? engine.shared.filter(\.downloaded) : []
            deleted += shared.map { ["repo": $0.repo, "revision": "fake", "bytes": $0.onDiskBytes] }
            if !shared.isEmpty {
                notes.append("\(engine.label)'s shared files (\(shared.map(\.id).joined(separator: ", "))) are deleted with it.")
            }
            update { state in ([id] + shared.map(\.id)).forEach { state.setDownloaded($0, false) } }
        }
        let freed = deleted.reduce(Int64(0)) { $0 + ($1["bytes"] as? Int64 ?? 0) }
        output(Self.object(["model": id, "deleted": deleted, "freed": freed, "notes": notes] as [String: Any]))
        return 0
    }

    private static func encoded(_ value: Encodable) -> String {
        (try? JSONEncoder().encode(value)).map { String(decoding: $0, as: UTF8.self) } ?? "{}"
    }

    private static func object(_ object: [String: Any]) -> String {
        (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]))
            .map { String(decoding: $0, as: UTF8.self) } ?? "{}"
    }
}

extension FakeModelState {
    /// One model in each state the rows show, matching `FakeState.sample`'s engines and
    /// settings: Parakeet and Kokoro downloaded and in use (Kokoro with an old version
    /// cached), Soprano with only an old version, Phonon's 31m not downloaded and its gated
    /// 40m failing without a token, and two plugin engines with nothing to manage.
    public static var sample: FakeModelState {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var state = FakeModelState(
            list: ModelList(
                cache: "\(home)/.cache/huggingface/hub", totalBytes: 0,
                engines: [
                    ModelEngine(
                        name: "parakeet", label: "Parakeet", kind: "stt",
                        models: [
                            CatalogueModel(
                                id: "mlx-community/parakeet-tdt-0.6b-v3", license: "CC-BY-4.0",
                                downloadBytes: 2_508_532_829, onDiskBytes: 2_509_044_141, downloaded: true,
                                refs: ["main"])
                        ]),
                    ModelEngine(name: "fake", label: "Fake", kind: "stt", models: []),
                    ModelEngine(
                        name: "kokoro", label: "Kokoro", kind: "tts", voices: ["af_heart"],
                        models: [
                            CatalogueModel(
                                id: "mlx-community/Kokoro-82M-bf16", license: "Apache-2.0",
                                downloadBytes: 355_322_783, onDiskBytes: 389_410_807, downloaded: true,
                                previous: [.init(revision: "0f1e2d3", bytes: 341_000_000)], refs: ["main"])
                        ]),
                    ModelEngine(
                        name: "soprano", label: "Soprano", kind: "tts", voices: ["default"],
                        models: [
                            CatalogueModel(
                                id: "mlx-community/Soprano-80M-bf16", license: "Apache-2.0",
                                downloadBytes: 220_642_837, downloaded: false,
                                previous: [.init(revision: "4c5b6a7", bytes: 219_000_000)])
                        ]),
                    ModelEngine(
                        name: "phonon", label: "Phonon", kind: "tts", voices: ["default"],
                        models: [
                            CatalogueModel(
                                id: "gradium/phonon-31m-en", license: "Apache-2.0", downloadBytes: 65_049_306,
                                downloaded: false),
                            CatalogueModel(
                                id: "gradium/phonon-40m-en", license: "CC-BY-NC-4.0", gated: true,
                                downloadBytes: 75_090_522, downloaded: false),
                        ],
                        shared: [
                            CatalogueModel(
                                id: "gradium/phonon-voices", license: "Apache-2.0", downloadBytes: 9_510_439,
                                downloaded: false)
                        ]),
                    ModelEngine(name: "fake", label: "Fake", kind: "tts", models: []),
                ]),
            failures: ["gradium/phonon-40m-en": "token_missing"])
        // Each revision's bytes counted once, as the real list does.
        state.recount()
        return state
    }
}
