import Foundation
import Observation
import VoicedControl

/// The model rows' side of ADR 0022: lists, downloads and deletes models by running
/// `voiced models` as the app's child, as the installer runs `voiced install`, so it works
/// while the services are down and outlives the Settings window. One download at a time;
/// Use switches through `settings.set`, then restarts the service.
@MainActor
@Observable
public final class ModelManager {
    /// The download in flight: the model asked for, and the repository it is on (the
    /// model's, then its engine's shared ones).
    public struct Download: Sendable, Equatable {
        public var model: String
        public var progress: InstallProgress.Download?
        /// The command reported `done`: the model is on disk.
        public var isDone = false
        /// The command reported `done` or an `error`: a cancel now changes nothing.
        public var hasEnded = false
    }

    public private(set) var list: ModelList?
    /// Why the list could not be read, shown in place of the rows.
    public private(set) var listError: String?
    public private(set) var download: Download?
    /// The model a delete runs for.
    public private(set) var deleting: String?
    /// The last failure per model id, shown on its row until the next try.
    public private(set) var messages: [String: ModelMessage] = [:]
    /// The engine the last text to speech Use switched to, for the Home Assistant hint.
    public private(set) var switchedVoiceEngine: String?

    public var isDownloading: Bool { download != nil }
    /// A download or a delete runs: Update and Repair wait for it.
    public var isBusy: Bool { download != nil || deleting != nil }

    public let executable: String
    private let runner: InstallRunner
    private let app: AppModel
    private var downloadTask: Task<Bool, Never>?

    public init(
        app: AppModel, runner: InstallRunner,
        executable: String = DevelopmentOverride.voicedExecutable() ?? VoicedPaths().entryPoint.path
    ) {
        self.app = app
        self.runner = runner
        self.executable = executable
    }

    /// `voiced models list --json`.
    public func refresh() async {
        guard FileManager.default.isExecutableFile(atPath: executable) else {
            listError = "voiced is not installed at \(executable), so its models cannot be listed."
            return
        }
        let ran = await collect(["models", "list", "--json"])
        // A pane left while the list ran: the next one lists again.
        guard !Task.isCancelled else { return }
        guard ran.status == 0, ran.failure == nil else {
            let detail = ran.stderr.last ?? ran.failure ?? "it exited with status \(ran.status)"
            listError = "Cannot list the models (\(detail)). An older voiced has no `voiced models`; update voiced."
            return
        }
        do {
            list = try JSONDecoder().decode(ModelList.self, from: Data(ran.stdout.joined(separator: "\n").utf8))
            listError = nil
        } catch {
            listError = "voiced models list printed a list this app cannot read (\(error.localizedDescription)). Update the app."
        }
    }

    /// `voiced models download <id> --json-progress`. True once the model is on disk; false
    /// when it failed (the row says why), was cancelled, or another download or a delete
    /// runs. A service that reports `missing_model` for this model is restarted into it, from
    /// the pane and the menu window alike: the watchdog leaves that state alone.
    @discardableResult
    public func download(_ id: String) async -> Bool {
        guard download == nil, deleting == nil else { return false }
        messages[id] = nil
        download = Download(model: id)
        let task = Task { await runDownload(id) }
        downloadTask = task
        let done = await task.value
        download = nil
        downloadTask = nil
        await refresh()
        if done {
            for kind in ServiceKind.allCases {
                guard let status = app.status?[kind], status.state == .missingModel, status.model == id else { continue }
                await app.restart(.service(kind))
            }
        }
        return done
    }

    /// Cancel: the runner sends the download's process group SIGTERM; the next download
    /// resumes the partial files.
    public func cancelDownload() {
        downloadTask?.cancel()
    }

    /// `voiced models delete <id> [--previous] --yes --json`.
    public func delete(_ id: String, previous: Bool = false) async {
        guard deleting == nil, download == nil else { return }
        messages[id] = nil
        deleting = id
        let ran = await collect(["models", "delete", id] + (previous ? ["--previous"] : []) + ["--yes", "--json"])
        deleting = nil
        let result = try? JSONDecoder().decode(ModelDeleteResult.self, from: Data(ran.stdout.joined(separator: "\n").utf8))
        switch result {
        case .deleted? where ran.status == 0:
            break
        case .failed(let code, let message)?:
            messages[id] = ModelMessage.failure(code: code, message: message, model: model(id))
        default:
            messages[id] = ModelMessage(text: ran.stderr.last ?? ran.failure ?? "The delete exited with status \(ran.status).")
        }
        await refresh()
    }

    /// Use: downloads first if needed, sends engine and model together, then restarts the
    /// service, whose row shows it loading until `status` says ready.
    public func use(_ row: ModelRow, for kind: ServiceKind) async {
        if let model = row.model, !model.downloaded {
            guard await download(model.id) else { return }
        }
        let edit: SettingsEdit =
            kind == .stt
            ? .sttEngine(row.engine.name, model: row.model?.id) : .ttsEngine(row.engine.name, model: row.model?.id)
        guard await app.apply(edit) else { return }
        if kind == .tts { switchedVoiceEngine = row.engine.label }
        await app.restart(.service(kind))
    }

    public func dismissVoiceHint() {
        switchedVoiceEngine = nil
    }

    private func model(_ id: String) -> CatalogueModel? { list?.model(id)?.model }

    private func runDownload(_ id: String) async -> Bool {
        let command = InstallCommand(executable, ["models", "download", id, "--json-progress"])
        var session = InstallSession(command: "voiced models download", reports: "download progress")
        var status: Int32 = -1
        do {
            reading: for try await output in runner.run(command) {
                switch output {
                case .stdout(let line):
                    // A refused hello stops the command; `finish` says why.
                    guard session.receive(line: line, at: .now) else { break reading }
                    let downloads = session.progress.downloads
                    download?.progress = downloads.last { !$0.isFinished } ?? downloads.last
                    download?.isDone = session.isDone
                    download?.hasEnded = session.isDone || session.reportedError != nil
                case .stderr(let text): session.receive(stderr: text)
                case .exit(let code): status = code
                }
            }
        } catch {
            messages[id] = ModelMessage(text: "Cannot run \(executable): \(error.localizedDescription)")
            return false
        }
        // A cancel racing the end: `done` still counts and an error still shows; only a
        // download cut short before either is a silent stop.
        if Task.isCancelled {
            if session.isDone { return true }
            if let error = session.reportedError {
                messages[id] = ModelMessage.failure(code: error.code, message: error.message, model: model(id))
            }
            return false
        }
        switch session.finish(status: status) {
        case .done:
            return true
        case .failed(_, let cause, let code):
            messages[id] = ModelMessage.failure(code: code, message: cause, model: model(id))
            return false
        }
    }

    private struct Ran {
        var stdout: [String] = []
        var stderr: [String] = []
        var status: Int32 = -1
        /// Set when the command could not be started.
        var failure: String?
    }

    private func collect(_ arguments: [String]) async -> Ran {
        var ran = Ran()
        do {
            for try await output in runner.run(InstallCommand(executable, arguments)) {
                switch output {
                case .stdout(let line): ran.stdout.append(line)
                case .stderr(let line): ran.stderr.append(line)
                case .exit(let code): ran.status = code
                }
            }
        } catch {
            ran.failure = "cannot run \(executable): \(error.localizedDescription)"
        }
        return ran
    }
}
