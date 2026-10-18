import AppKit
import Foundation
import VoicedControl

/// A program the installer runs as the user.
public struct InstallCommand: Sendable, Equatable {
    public var executable: String
    public var arguments: [String]
    /// Added to the app's environment.
    public var environment: [String: String]

    public init(_ executable: String, _ arguments: [String], environment: [String: String] = [:]) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
    }
}

/// What a running command produces: its lines, then its exit status, last.
public enum ProcessOutput: Sendable, Equatable {
    case stdout(String)
    case stderr(String)
    case exit(Int32)
}

/// The privileged script's modes that need root. Its `check` mode does not, and runs as the
/// user.
public enum PrivilegedMode: String, Sendable {
    case install, uninstall
}

/// How a run of the privileged script ended.
public enum PrivilegedOutcome: Sendable, Equatable {
    /// The script ran; `output` is its stdout on success and its stderr otherwise, as
    /// `do shell script` reports them.
    case finished(status: Int32, output: String)
    /// The administrator prompt was cancelled (-128).
    case cancelled
}

/// Everything the installer does to the Mac, so the state machine runs in tests without
/// starting a process, prompting or touching the network.
@MainActor
public protocol InstallRunner: AnyObject {
    /// Runs `command`. Cancelling the iteration terminates it: SIGTERM, then SIGKILL after
    /// 5 s. Throws only when it cannot be started.
    func run(_ command: InstallCommand) -> AsyncThrowingStream<ProcessOutput, Error>
    /// Runs the compiled-in privileged script behind one administrator prompt.
    func runPrivileged(_ mode: PrivilegedMode, user: String) async -> PrivilegedOutcome
    /// Whether `url` answers at all, for the network check.
    func isReachable(_ url: URL) async -> Bool
    /// Free space on the volume holding `url`, for what the install needs.
    func availableCapacity(at url: URL) -> Int64?
}

/// The real runner: `Process` for commands, `NSAppleScript` for the prompt.
@MainActor
public final class SystemInstallRunner: InstallRunner {
    private let running: RunningProcesses

    /// `terminationGrace`: how long a command has between SIGTERM and SIGKILL.
    public init(terminationGrace: TimeInterval = 5) {
        running = RunningProcesses(grace: terminationGrace)
    }

    public func run(_ command: InstallCommand) -> AsyncThrowingStream<ProcessOutput, Error> {
        Self.stream(command, running: running)
    }

    /// Nonisolated: the pipes are read and the exit is awaited on other queues.
    private nonisolated static func stream(
        _ command: InstallCommand, running: RunningProcesses
    ) -> AsyncThrowingStream<ProcessOutput, Error> {
        AsyncThrowingStream { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: command.executable)
            process.arguments = command.arguments
            process.environment = ProcessInfo.processInfo.environment.merging(command.environment) { $1 }
            let out = Pipe()
            let err = Pipe()
            process.standardOutput = out
            process.standardError = err
            process.standardInput = FileHandle.nullDevice
            continuation.onTermination = { termination in
                if case .cancelled = termination { running.terminate(process) }
            }
            do {
                try process.run()
            } catch {
                continuation.finish(throwing: error)
                return
            }
            running.insert(process)
            // Both pipes are read to the end before the exit status is reported, so no line
            // is lost to a race with termination.
            let group = DispatchGroup()
            for (pipe, wrap) in [(out, ProcessOutput.stdout), (err, ProcessOutput.stderr)] {
                group.enter()
                DispatchQueue.global().async {
                    Self.readLines(pipe.fileHandleForReading) { continuation.yield(wrap($0)) }
                    group.leave()
                }
            }
            group.notify(queue: .global()) {
                process.waitUntilExit()
                running.remove(process)
                continuation.yield(.exit(process.terminationStatus))
                continuation.finish()
            }
        }
    }

    private nonisolated static func readLines(_ handle: FileHandle, _ yield: (String) -> Void) {
        var framer = LineFramer()
        while true {
            let data = handle.availableData
            if data.isEmpty { break }
            // A line too long for the framer is dropped, not fatal: it is only shown.
            for line in (try? framer.append(data)) ?? [] {
                yield(String(decoding: line, as: UTF8.self))
            }
        }
    }

    /// Ends every command still running, for the app quitting mid-install: SIGTERM, then
    /// SIGKILL after 5 s. Blocks until they are gone.
    public func terminateAll() {
        running.terminateAll()
    }

    public func runPrivileged(_ mode: PrivilegedMode, user: String) async -> PrivilegedOutcome {
        // NSAppleScript belongs on the main thread. The window does not redraw while the
        // prompt is up and the script runs; that is a few seconds.
        PrivilegedScript.run(mode, user: user)
    }

    public func isReachable(_ url: URL) async -> Bool {
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = "HEAD"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = false
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }
        return (try? await session.data(for: request).1 as? HTTPURLResponse) != nil
    }

    public func availableCapacity(at url: URL) -> Int64? {
        let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }
}

/// The processes `SystemInstallRunner` started and has not seen exit.
private final class RunningProcesses: @unchecked Sendable {
    private let lock = NSLock()
    private var processes: [ObjectIdentifier: Process] = [:]
    private let grace: TimeInterval

    init(grace: TimeInterval) {
        self.grace = grace
    }

    func insert(_ process: Process) {
        lock.withLock { processes[ObjectIdentifier(process)] = process }
    }

    func remove(_ process: Process) {
        lock.withLock { _ = processes.removeValue(forKey: ObjectIdentifier(process)) }
    }

    func terminate(_ process: Process) {
        guard process.isRunning else { return }
        process.terminate()
        let pid = process.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
            if process.isRunning { kill(pid, SIGKILL) }
        }
    }

    func terminateAll() {
        let all = lock.withLock { Array(processes.values) }
        all.filter(\.isRunning).forEach { $0.terminate() }
        let deadline = Date().addingTimeInterval(grace)
        while all.contains(where: \.isRunning), Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        for process in all where process.isRunning { kill(process.processIdentifier, SIGKILL) }
    }
}
