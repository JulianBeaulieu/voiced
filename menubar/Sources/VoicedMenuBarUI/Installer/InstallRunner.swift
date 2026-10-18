import Darwin
import Foundation

/// A program the installer runs as the user.
public struct InstallCommand: Sendable, Equatable {
    public var executable: String
    public var arguments: [String]
    /// Added to the app's environment, or, without `inheritsEnvironment`, all of it.
    public var environment: [String: String]
    /// False for the privileged run: nothing of the app's environment (`SHELLOPTS`, `PS4`,
    /// `BASH_ENV`, ...) may reach the shells that run as root.
    public var inheritsEnvironment: Bool

    public init(
        _ executable: String, _ arguments: [String], environment: [String: String] = [:],
        inheritsEnvironment: Bool = true
    ) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.inheritsEnvironment = inheritsEnvironment
    }
}

/// What a running command produces: its lines, then its exit status, last. A command ended by
/// a signal exits with 128 plus the signal number, as a shell reports it.
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

/// How a run of the privileged script ended, before it is interpreted
/// (`PrivilegedScript.result`).
public enum PrivilegedOutcome: Sendable, Equatable {
    /// The script ran; `output` is its stdout on success and its stderr otherwise, as
    /// `do shell script` reports them.
    case finished(status: Int32, output: String)
    /// The administrator prompt was cancelled (-128).
    case cancelled
    /// The app's own signature does not check out, so it does not ask for the password.
    case damaged(String)
}

/// The installer's only ways out of the process: commands, the privileged run and the
/// network. Tests replace it; everything else runs against a temporary home.
@MainActor
public protocol InstallRunner: AnyObject {
    /// Runs `command` in a process group of its own. Cancelling the iteration terminates the
    /// group: SIGTERM, then SIGKILL after the grace. Throws only when it cannot be started.
    func run(_ command: InstallCommand) -> AsyncThrowingStream<ProcessOutput, Error>
    /// Runs the compiled-in privileged script behind one administrator prompt.
    func runPrivileged(_ mode: PrivilegedMode, user: String) async -> PrivilegedOutcome
    /// Whether `url` answers at all, for the network check.
    func isReachable(_ url: URL) async -> Bool
}

/// The real runner: `posix_spawn` for commands, `osascript` for the prompt.
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

    /// Nonisolated: the pipes are read and the exit is awaited on other threads.
    private nonisolated static func stream(
        _ command: InstallCommand, running: RunningProcesses
    ) -> AsyncThrowingStream<ProcessOutput, Error> {
        AsyncThrowingStream { continuation in
            let child: Spawned
            do {
                child = try Spawned(command)
            } catch {
                continuation.finish(throwing: error)
                return
            }
            running.insert(child.pid)
            continuation.onTermination = { termination in
                if case .cancelled = termination { running.terminate(child.pid) }
            }
            // Both pipes are read to the end before the exit status is reported, so no line
            // is lost to a race with termination.
            let group = DispatchGroup()
            for (fd, wrap) in [(child.stdout, ProcessOutput.stdout), (child.stderr, ProcessOutput.stderr)] {
                group.enter()
                Thread.detachNewThread {
                    readLines(fd) { continuation.yield(wrap($0)) }
                    group.leave()
                }
            }
            group.notify(queue: .global()) {
                let status = child.waitForExit()
                // Reaped only once a termination in flight is done with the group's id.
                running.exited(child.pid)
                child.reap()
                running.remove(child.pid)
                continuation.yield(.exit(status))
                continuation.finish()
            }
        }
    }

    private nonisolated static func readLines(_ fd: Int32, _ yield: (String) -> Void) {
        defer { close(fd) }
        var lines = OutputLines()
        var buffer = [UInt8](repeating: 0, count: 16 * 1024)
        while true {
            let count = read(fd, &buffer, buffer.count)
            if count < 0, errno == EINTR { continue }
            if count <= 0 { break }
            lines.append(Data(buffer[0..<count])).forEach(yield)
        }
        lines.finish().map(yield)
    }

    /// Ends every command still running, for the app quitting mid-install: SIGTERM to each
    /// group, then SIGKILL after the grace. Blocks until the commands are gone.
    public func terminateAll() {
        running.terminateAll()
    }

    public func runPrivileged(_ mode: PrivilegedMode, user: String) async -> PrivilegedOutcome {
        #if !DEBUG
            // It hashes the whole bundle; not on the main actor.
            let bundle = Bundle.main.bundleURL
            if let problem = await Task.detached(operation: { BundleCheck.problem(at: bundle) }).value {
                return .damaged(problem)
            }
        #endif
        var stdout: [String] = []
        var stderr: [String] = []
        var status: Int32 = -1
        do {
            for try await output in run(PrivilegedScript.command(mode, user: user)) {
                switch output {
                case .stdout(let line): stdout.append(line)
                case .stderr(let line): stderr.append(line)
                case .exit(let code): status = code
                }
            }
        } catch {
            return .finished(status: -1, output: "Cannot run osascript: \(error.localizedDescription)")
        }
        return PrivilegedScript.outcome(status: status, stdout: stdout, stderr: stderr)
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
}

/// Splits a command's output into lines. A last line without a newline is kept, and a line
/// longer than `maximum` is passed on in pieces rather than dropped: it is only shown.
struct OutputLines {
    static let maximum = 64 * 1024
    private var buffer = Data()

    mutating func append(_ data: Data) -> [String] {
        buffer.append(data)
        var lines: [String] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            lines.append(Self.text(buffer[buffer.startIndex..<newline]))
            buffer = Data(buffer[buffer.index(after: newline)...])
        }
        if buffer.count > Self.maximum {
            lines.append(Self.text(buffer))
            buffer.removeAll()
        }
        return lines.filter { !$0.isEmpty }
    }

    mutating func finish() -> String? {
        defer { buffer.removeAll() }
        let last = Self.text(buffer)
        return last.isEmpty ? nil : last
    }

    private static func text(_ data: Data) -> String {
        var line = String(decoding: data, as: UTF8.self)
        if line.hasSuffix("\r") { line.removeLast() }
        return line
    }
}

/// A child started with `posix_spawn` in its own process group, so signals reach whatever it
/// starts too, in `/`, with only stdin (/dev/null), stdout and stderr open.
private struct Spawned {
    let pid: pid_t
    let stdout: Int32
    let stderr: Int32

    init(_ command: InstallCommand) throws {
        var out: [Int32] = [0, 0]
        var err: [Int32] = [0, 0]
        guard pipe(&out) == 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        guard pipe(&err) == 0 else {
            close(out[0])
            close(out[1])
            throw POSIXError(.init(rawValue: errno) ?? .EIO)
        }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, out[1], 1)
        posix_spawn_file_actions_adddup2(&actions, err[1], 2)
        // Not the app's working directory, which the privileged run's root shell would start in.
        posix_spawn_file_actions_addchdir_np(&actions, "/")
        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        // A group of its own; every other descriptor of the app closed in the child.
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT))
        posix_spawnattr_setpgroup(&attributes, 0)

        let base = command.inheritsEnvironment ? ProcessInfo.processInfo.environment : [:]
        let environment = base.merging(command.environment) { $1 }.map { "\($0.key)=\($0.value)" }.sorted()
        var pid: pid_t = 0
        let result = withCStrings([command.executable] + command.arguments) { argv in
            withCStrings(environment) { envp in
                posix_spawn(&pid, command.executable, &actions, &attributes, argv, envp)
            }
        }
        close(out[1])
        close(err[1])
        guard result == 0 else {
            close(out[0])
            close(err[0])
            throw POSIXError(.init(rawValue: result) ?? .EIO)
        }
        self.pid = pid
        stdout = out[0]
        stderr = err[0]
    }

    /// Waits for the child to exit without reaping it, so its pid, the group's id, is not
    /// reused yet. Returns its exit status, or 128 plus the signal that ended it.
    func waitForExit() -> Int32 {
        var info = siginfo_t()
        while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) < 0 {
            if errno != EINTR { return -1 }
        }
        return info.si_code == CLD_EXITED ? info.si_status : 128 + info.si_status
    }

    func reap() {
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0, errno == EINTR {}
    }
}

private func withCStrings<R>(_ strings: [String], _ body: ([UnsafeMutablePointer<CChar>?]) -> R) -> R {
    let pointers = strings.map { strdup($0) } + [nil]
    defer { pointers.forEach { free($0) } }
    return body(pointers)
}

/// The process groups `SystemInstallRunner` started. A group's id is its leader's pid, which
/// the system does not reuse while the leader is unreaped; the reader reaps it only after a
/// termination's SIGKILL went out, so the signal never reaches another group.
private final class RunningProcesses: @unchecked Sendable {
    private struct Entry {
        var exited = false
        /// Held while a termination may still signal the group.
        var hold: DispatchSemaphore?
    }

    private let lock = NSLock()
    private var entries: [pid_t: Entry] = [:]
    private let grace: TimeInterval

    init(grace: TimeInterval) {
        self.grace = grace
    }

    func insert(_ pid: pid_t) {
        lock.withLock { entries[pid] = Entry() }
    }

    /// The leader has exited. Blocks while a termination still needs its group id.
    func exited(_ pid: pid_t) {
        let hold = lock.withLock {
            entries[pid]?.exited = true
            return entries[pid]?.hold
        }
        hold?.wait()
    }

    func remove(_ pid: pid_t) {
        lock.withLock { _ = entries.removeValue(forKey: pid) }
    }

    /// Starts a termination, unless the group is gone or one is already under way.
    private func hold(_ pid: pid_t) -> DispatchSemaphore? {
        lock.withLock {
            guard var entry = entries[pid], entry.hold == nil else { return nil }
            let hold = DispatchSemaphore(value: 0)
            entry.hold = hold
            entries[pid] = entry
            return hold
        }
    }

    func terminate(_ pid: pid_t) {
        guard let hold = hold(pid) else { return }
        kill(-pid, SIGTERM)
        // The whole group, even if its leader is gone: what it started may still run.
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
            kill(-pid, SIGKILL)
            hold.signal()
        }
    }

    func terminateAll() {
        let held = lock.withLock { Array(entries.keys) }.compactMap { pid in hold(pid).map { (pid, $0) } }
        held.forEach { kill(-$0.0, SIGTERM) }
        let deadline = Date().addingTimeInterval(grace)
        while Date() < deadline, lock.withLock({ held.contains { entries[$0.0]?.exited == false } }) {
            Thread.sleep(forTimeInterval: 0.05)
        }
        for (pid, hold) in held {
            kill(-pid, SIGKILL)
            hold.signal()
        }
    }
}
