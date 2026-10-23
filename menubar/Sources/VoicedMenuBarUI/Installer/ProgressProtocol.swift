import Foundation
import VoicedControl

/// One line of `voiced install --json-progress` or `voiced models download --json-progress`
/// (ADR 0018, protocol 1). Unknown events and unknown fields are ignored, so either side can
/// add them without a new protocol number.
public enum InstallEvent: Sendable, Equatable {
    case hello(protocol: Int)
    case step(id: String, state: StepState, shown: String)
    case download(model: String, bytes: Int64, total: Int64)
    case log(String)
    case done
    /// `code` is ADR 0022's (`token_missing`, `network`, `busy`, ...), sent by `voiced models
    /// download`; `voiced install` sends none.
    case error(step: String?, message: String, code: String? = nil)

    public enum StepState: String, Sendable {
        case start, done
    }

    /// The protocol this app speaks.
    public static let supportedProtocol = 1

    /// Parses one stdout line. Nil for an event this app does not know or one missing the
    /// fields it needs. A line that is not a JSON object is kept as log text: stdout is the
    /// protocol's, but a library printing to it should not end an install.
    public init?(line: String) {
        guard let value = try? JSONValue(data: Data(line.utf8)), let fields = value.objectValue else {
            self = .log(line)
            return
        }
        switch fields["event"]?.stringValue {
        case "hello":
            guard let number = fields["protocol"]?.intValue else { return nil }
            self = .hello(protocol: number)
        case "step":
            guard let id = fields["step"]?.stringValue,
                let state = fields["state"]?.stringValue.flatMap(StepState.init(rawValue:))
            else { return nil }
            self = .step(id: id, state: state, shown: fields["shown"]?.stringValue ?? id)
        case "download":
            guard let model = fields["model"]?.stringValue, let bytes = fields["bytes"]?.intValue,
                let total = fields["total"]?.intValue
            else { return nil }
            self = .download(model: model, bytes: Int64(bytes), total: Int64(total))
        case "log":
            guard let text = fields["line"]?.stringValue else { return nil }
            self = .log(text)
        case "done":
            self = .done
        case "error":
            // An empty message is worded by the session, which knows the command.
            self = .error(
                step: fields["step"]?.stringValue, message: fields["message"]?.stringValue ?? "",
                code: fields["code"]?.stringValue)
        default:
            return nil
        }
    }

    /// Why a hello is refused, or nil when its protocol is this app's. The side with the
    /// older protocol is the one to update.
    public static func refusal(protocol number: Int) -> String? {
        if number == supportedProtocol { return nil }
        let update = number > supportedProtocol ? "Update the app." : "Update voiced."
        return "This voiced reports install progress with protocol \(number); this app speaks "
            + "protocol \(supportedProtocol). \(update)"
    }
}

/// What the progress screen shows while `voiced install` runs.
public struct InstallProgress: Sendable, Equatable {
    public struct Step: Sendable, Equatable, Identifiable {
        public var id: String
        /// What the screen calls it: the app's name for a step it knows, else what
        /// `voiced install` showed, which is a description of the command.
        public var title: String
        public var isDone: Bool

        static let titles = [
            "config": "Writing the configuration",
            "settings": "Writing your choices to the configuration",
            "claim": "Preparing ~/.voiced",
            "check": "Checking the speech engines",
            "ports": "Checking the ports",
            "models": "Downloading the models",
            "link": "Switching to the new environment",
            "manifest": "Recording the install",
            "legacy": "Removing the previous install",
            "prune": "Removing old environments",
            "unstage": "Removing files an earlier version left",
        ]

        static func title(id: String, shown: String) -> String {
            titles[id] ?? shown
        }
    }

    public struct Download: Sendable, Equatable, Identifiable {
        public var model: String
        /// Capped at `total`: the manifest's size is the most a download may take.
        public var bytes: Int64
        public var total: Int64
        public var bytesPerSecond: Double?

        public var id: String { model }
        public var fraction: Double { total > 0 ? Double(bytes) / Double(total) : 0 }
        public var isFinished: Bool { bytes >= total }
        public var secondsRemaining: Double? {
            guard let bytesPerSecond, bytesPerSecond > 0 else { return nil }
            return Double(total - bytes) / bytesPerSecond
        }
    }

    public var steps: [Step] = []
    public var downloads: [Download] = []

    public init(steps: [Step] = [], downloads: [Download] = []) {
        self.steps = steps
        self.downloads = downloads
    }

    /// The step in progress, for the screen's one line.
    public var current: Step? { steps.last { !$0.isDone } }
}

/// Reads one run of `voiced install --json-progress` (or `voiced models download`, which
/// speaks the same protocol): checks the hello, folds the events into `progress`, keeps the
/// last lines of output for a failure, and decides the outcome from the last event and the
/// exit status, which must agree.
struct InstallSession {
    enum Outcome: Equatable {
        case done
        case failed(step: String?, cause: String, code: String? = nil)
    }

    /// The command and what it reports, for the outcome's wording.
    let command: String
    let reports: String

    init(command: String = "voiced install", reports: String = "install progress") {
        self.command = command
        self.reports = reports
    }

    /// How far back the download speed is averaged.
    static let speedWindow: Duration = .seconds(5)
    static let tailLength = 8
    /// The most steps and downloads kept: the CLI has ten steps and two models, and a
    /// misbehaving one cannot grow the screen without bound.
    static let maximumEntries = 32
    /// The most speed samples kept per download, however fast events come: the CLI sends
    /// four a second, which is 20 in the window.
    static let maximumSamples = 256

    private(set) var progress = InstallProgress()
    private(set) var tail: [String] = []
    /// Set when the hello is refused or missing; the run is stopped.
    private(set) var refusal: String?
    /// When the bytes of an unfinished download last grew, for the stall check.
    private(set) var lastGrowth: ContinuousClock.Instant?
    private var greeted = false
    /// The first `done` or `error`; anything after it changes nothing.
    private var terminal: InstallEvent?

    /// `done` arrived, whatever the exit status turns out to be.
    var isDone: Bool { terminal == .done }

    /// The `error` event that arrived, if any.
    var reportedError: (message: String, code: String?)? {
        guard case .error(_, let message, let code)? = terminal else { return nil }
        return (message.isEmpty ? "\(command) failed." : message, code)
    }
    private(set) var samples: [String: [(at: ContinuousClock.Instant, bytes: Int64)]] = [:]

    /// A download with bytes to go whose bytes have not grown for `timeout`.
    func isStalled(at now: ContinuousClock.Instant, after timeout: Duration) -> Bool {
        guard progress.downloads.contains(where: { !$0.isFinished }), let lastGrowth else { return false }
        return now - lastGrowth > timeout
    }

    /// Takes one stdout line. Returns false once the run must be stopped (a refused hello).
    mutating func receive(line: String, at now: ContinuousClock.Instant) -> Bool {
        guard refusal == nil else { return false }
        guard let event = InstallEvent(line: line) else { return true }
        if !greeted {
            // Something on stdout before the hello, such as a library's warning, is shown,
            // not taken for the protocol; `finish` still notices a hello that never comes.
            if case .log(let text) = event {
                keep(text)
                return true
            }
            guard case .hello(let number) = event else {
                refusal = "\(command) did not start with a progress hello. Update voiced."
                return false
            }
            if let reason = InstallEvent.refusal(protocol: number) {
                refusal = reason
                return false
            }
            greeted = true
            return true
        }
        switch event {
        case .hello:
            break
        case .step(let id, let state, let shown):
            let title = InstallProgress.Step.title(id: id, shown: shown)
            if let index = progress.steps.firstIndex(where: { $0.id == id }) {
                progress.steps[index].isDone = state == .done
                progress.steps[index].title = title
            } else if progress.steps.count < Self.maximumEntries {
                progress.steps.append(.init(id: id, title: title, isDone: state == .done))
            }
        case .download(let model, let bytes, let total):
            record(model: model, bytes: min(max(bytes, 0), max(total, 0)), total: max(total, 0), at: now)
        case .log(let text):
            keep(text)
        case .done, .error:
            if terminal == nil { terminal = event }
        }
        return true
    }

    mutating func receive(stderr text: String) {
        keep(text)
    }

    /// The outcome once the process has exited with `status`.
    func finish(status: Int32) -> Outcome {
        if let refusal { return .failed(step: nil, cause: refusal) }
        if !greeted {
            return .failed(step: nil, cause: "This voiced does not report \(reports). Update voiced.")
        }
        switch terminal {
        case .error(let step, let message, let code)?:
            return .failed(step: step, cause: message.isEmpty ? "\(command) failed." : message, code: code)
        case .done? where status == 0:
            return .done
        case .done?:
            return .failed(step: nil, cause: "\(command) reported done but exited with status \(status).")
        default:
            let cause = status == 0 ? "\(command) ended without reporting done." : "\(command) exited with status \(status)."
            return .failed(step: progress.current?.id, cause: cause)
        }
    }

    private mutating func record(model: String, bytes: Int64, total: Int64, at now: ContinuousClock.Instant) {
        let known = progress.downloads.firstIndex { $0.model == model }
        if known == nil, progress.downloads.count >= Self.maximumEntries { return }
        let index = known ?? progress.downloads.count
        if known == nil { progress.downloads.append(.init(model: model, bytes: bytes, total: total)) }
        // A new download counts as progress: the previous one may have finished long ago.
        if known == nil || bytes > progress.downloads[index].bytes { lastGrowth = now }
        progress.downloads[index].bytes = bytes
        progress.downloads[index].total = total
        var window = samples[model, default: []]
        window.append((now, bytes))
        // Samples arrive in order, so the old ones are at the front.
        let fresh = window.firstIndex { now - $0.at <= Self.speedWindow } ?? window.endIndex
        window.removeFirst(max(fresh, window.count - Self.maximumSamples))
        samples[model] = window
        if let first = window.first, now > first.at {
            let seconds = (now - first.at) / .seconds(1)
            progress.downloads[index].bytesPerSecond = Double(bytes - first.bytes) / seconds
        }
    }

    private mutating func keep(_ text: String) {
        tail.append(text)
        if tail.count > Self.tailLength { tail.removeFirst(tail.count - Self.tailLength) }
    }
}
