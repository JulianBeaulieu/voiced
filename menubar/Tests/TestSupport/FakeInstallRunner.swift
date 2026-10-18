import CryptoKit
import Foundation
import VoicedControl
import VoicedMenuBarUI

/// An `InstallRunner` with scripted answers, so the installer runs without a process, a
/// prompt or the network. Records what it was asked to do.
@MainActor
public final class FakeInstallRunner: InstallRunner {
    /// What one command does.
    public struct Response: Sendable {
        public var outputs: [ProcessOutput]
        /// Between outputs, so a download has a speed.
        public var delay: Duration = .zero
        /// Never exits after its outputs; only cancelling ends it.
        public var hangs = false
        public var cannotStart = false

        public init(_ outputs: [ProcessOutput], delay: Duration = .zero, hangs: Bool = false, cannotStart: Bool = false) {
            self.outputs = outputs
            self.delay = delay
            self.hangs = hangs
            self.cannotStart = cannotStart
        }

        public static let success = Response([.exit(0)])

        /// `voiced install --json-progress` printing `lines`, then exiting with `status`.
        public static func progress(_ lines: [String], status: Int32 = 0, delay: Duration = .zero) -> Response {
            Response(lines.map(ProcessOutput.stdout) + [.exit(status)], delay: delay)
        }
    }

    public struct CannotStart: Error {}

    /// The answer for each command; by default every step succeeds with the recorded lines.
    public var respond: @MainActor (InstallCommand) -> Response = FakeInstallRunner.defaultResponse
    /// Answers for the privileged step, in order; success once they run out.
    public var privilegedOutcomes: [PrivilegedOutcome] = []
    /// The privileged step never returns, as while the prompt is up.
    public var privilegedHangs = false
    public var isOnline = true

    public private(set) var commands: [InstallCommand] = []
    public private(set) var privilegedCalls: [String] = []
    /// Commands whose run was cancelled before they exited: the real runner terminates them.
    public private(set) var terminated: [InstallCommand] = []

    public init() {}

    public static func defaultResponse(_ command: InstallCommand) -> Response {
        switch (URL(fileURLWithPath: command.executable).lastPathComponent, command.arguments.first) {
        case ("uv", "venv"):
            // uv creates the directory; the installer renames it afterwards.
            if let path = command.arguments.last {
                try? FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true)
            }
            return .success
        case ("socketfilterfw", _): return Response([.stdout("Firewall is disabled. (State = 0)"), .exit(0)])
        case ("voiced", _): return .progress(RecordedProgress.success)
        default: return .success
        }
    }

    /// The commands whose executable is named `name`.
    public func commands(named name: String) -> [InstallCommand] {
        commands.filter { URL(fileURLWithPath: $0.executable).lastPathComponent == name }
    }

    public func run(_ command: InstallCommand) -> AsyncThrowingStream<ProcessOutput, Error> {
        commands.append(command)
        let response = respond(command)
        return AsyncThrowingStream { continuation in
            guard !response.cannotStart else {
                continuation.finish(throwing: CannotStart())
                return
            }
            let feeder = Task { @MainActor in
                for output in response.outputs {
                    if response.delay > .zero { try? await Task.sleep(for: response.delay) }
                    if Task.isCancelled { return }
                    continuation.yield(output)
                }
                if !response.hangs { continuation.finish() }
            }
            continuation.onTermination = { termination in
                feeder.cancel()
                guard case .cancelled = termination else { return }
                Task { @MainActor in self.terminated.append(command) }
            }
        }
    }

    public func runPrivileged(_ mode: PrivilegedMode, user: String) async -> PrivilegedOutcome {
        privilegedCalls.append("\(mode.rawValue) \(user)")
        if privilegedHangs {
            while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(20)) }
        }
        return privilegedOutcomes.isEmpty ? .finished(status: 0, output: "") : privilegedOutcomes.removeFirst()
    }

    public func isReachable(_ url: URL) async -> Bool { isOnline }
}

/// What a bundle would carry, in `directory`: a lock and a wheel with the hashes the manifest
/// pins, and two models.
public func installResources(in directory: URL) throws -> InstallResources {
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let lock = directory.appendingPathComponent("requirements.lock")
    let wheel = directory.appendingPathComponent("voiced-0.1.0-py3-none-any.whl")
    try Data("numpy==2.0.0 --hash=sha256:00\n".utf8).write(to: lock)
    try Data("a wheel".utf8).write(to: wheel)
    func sha256(_ url: URL) throws -> String {
        SHA256.hash(data: try Data(contentsOf: url)).map { String(format: "%02x", $0) }.joined()
    }
    return InstallResources(
        uv: directory.appendingPathComponent("uv"), lock: lock, wheel: wheel,
        manifest: InstallManifest(
            version: "0.1.0", python: "3.12.15", lockSHA256: try sha256(lock),
            wheel: .init(name: wheel.lastPathComponent, sha256: try sha256(wheel)),
            wheelsBytes: 205_000_000,
            models: [
                .init(repo: RecordedProgress.parakeet, revision: "0a1b2c3", bytes: 2_508_500_000),
                .init(repo: RecordedProgress.kokoro, revision: "4d5e6f7", bytes: 355_300_000),
            ]))
}

/// Lines of `voiced install --json-progress` as ADR 0018's protocol 1 specifies them, `shown`
/// as the CLI writes it: a description of the step's command.
public enum RecordedProgress {
    public static let hello = #"{"event": "hello", "protocol": 1, "version": "0.1.0"}"#
    public static let parakeet = "mlx-community/parakeet-tdt-0.6b-v3"
    public static let kokoro = "mlx-community/Kokoro-82M-bf16"

    public static func step(_ id: String, _ state: String, _ shown: String) -> String {
        #"{"event": "step", "step": "\#(id)", "state": "\#(state)", "shown": "\#(shown)"}"#
    }

    public static func download(_ model: String, _ bytes: Int64, _ total: Int64) -> String {
        #"{"event": "download", "model": "\#(model)", "bytes": \#(bytes), "total": \#(total)}"#
    }

    public static func error(_ step: String, _ message: String) -> String {
        #"{"event": "error", "step": "\#(step)", "message": "\#(message)"}"#
    }

    public static let done = #"{"event": "done", "staging_dir": "/Users/someone/.config/voiced/launchd"}"#

    private static let env = "/Users/someone/.voiced/envs/20261018T120000"
    /// Each step's `shown`, as the CLI words them.
    public static let shown = [
        "config": "create /Users/someone/.config/voiced/config.toml from config.example.toml",
        "claim": "create log dir /Users/someone/Library/Logs/voiced, and claim /Users/someone/.voiced (install.json)",
        "check": "\(env)/bin/python -P -c <engine check>",
        "ports": "check that ports 10300, 10200, 8001 and 8002 are free",
        "models": "\(env)/bin/python -c <model download>",
        "link": "link /Users/someone/.voiced/bin/voiced -> \(env)/bin/voiced",
        "manifest": "write /Users/someone/.voiced/install.json",
        "prune": "remove environments in /Users/someone/.voiced/envs other than the new and the previous one",
        "stage": "write the plists and scripts to /Users/someone/.config/voiced/launchd",
    ]

    public static func step(_ id: String, _ state: String) -> String {
        step(id, state, shown[id] ?? id)
    }

    /// A step that starts and finishes.
    public static func steps(_ ids: String...) -> [String] {
        ids.flatMap { [step($0, "start"), step($0, "done")] }
    }

    /// Up to the models step, which then downloads both models halfway.
    public static let downloading: [String] =
        [hello] + steps("config", "claim", "check", "ports") + [
            step("models", "start"),
            download(parakeet, 0, 2_508_500_000), download(kokoro, 0, 355_300_000),
            download(parakeet, 627_125_000, 2_508_500_000), download(kokoro, 88_825_000, 355_300_000),
            download(parakeet, 1_254_250_000, 2_508_500_000), download(kokoro, 177_650_000, 355_300_000),
        ]

    /// A whole successful run, with an unknown event and unknown fields along the way.
    public static let success: [String] =
        downloading + [
            #"{"event": "future-event", "anything": 1}"#,
            download(parakeet, 2_508_500_000, 2_508_500_000),
            #"{"event": "download", "model": "\#(kokoro)", "bytes": 355300000, "total": 355300000, "file": "x"}"#,
            #"{"event": "log", "line": "loaded parakeet"}"#,
            step("models", "done"),
        ] + steps("link", "manifest", "prune", "stage") + [done]
}
