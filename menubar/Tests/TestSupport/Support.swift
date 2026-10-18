import FakeControlServerKit
import Foundation
import VoicedControl

/// A short, unique socket path; `sun_path` holds 104 bytes.
public func temporarySocketPath() -> String {
    "/tmp/vt-\(UUID().uuidString.prefix(8)).sock"
}

/// A stand-in /Library/LaunchDaemons. With `watchdogUser`, it holds a watchdog plist for
/// that user, shaped like the ones `voiced install` writes.
public func launchDaemonsDirectory(watchdogUser: String? = nil, label: String = "io.github.julianbeaulieu.voiced") throws -> URL {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    if let watchdogUser {
        try writePlist(
            ["ProgramArguments": ["/Users/someone/.voiced/bin/voiced", "watchdog"], "UserName": watchdogUser],
            to: directory.appendingPathComponent("\(label).watchdog.plist"))
    }
    return directory
}

public func writePlist(_ plist: [String: Any], to url: URL) throws {
    let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
    try data.write(to: url)
}

public func startServer(
    _ scenario: FakeScenario = .running, configure: (inout FakeControlServer.Options) -> Void = { _ in }
) throws -> FakeControlServer {
    var options = FakeControlServer.Options()
    options.restartDuration = 0.2
    configure(&options)
    let server = FakeControlServer(socketPath: temporarySocketPath(), scenario: scenario, options: options)
    try server.start()
    return server
}

public func connectedClient(_ server: FakeControlServer) async throws -> ControlClient {
    let client = ControlClient(socketPath: server.socketPath, requestTimeout: .seconds(2))
    try await client.connect()
    return client
}

/// Waits for the next event matching `predicate`, failing after `timeout`.
public func nextEvent(
    _ client: ControlClient, timeout: Duration = .seconds(3),
    where predicate: @escaping @Sendable (ControlEvent) -> Bool
) async -> ControlEvent? {
    await withTaskGroup(of: ControlEvent?.self) { group in
        group.addTask {
            for await event in client.events where predicate(event) { return event }
            return nil
        }
        group.addTask {
            try? await Task.sleep(for: timeout)
            return nil
        }
        let first = await group.next() ?? nil
        group.cancelAll()
        return first
    }
}

/// Polls `condition` on the main actor until it holds or `timeout` passes.
@MainActor
public func eventually(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(20))
    }
    return condition()
}

public func decodeJSON<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
    try JSONDecoder().decode(type, from: Data(text.utf8))
}

/// Resumes a continuation once, whichever of several racers comes first.
private final class ResumeOnce<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T?, Never>?

    init(_ continuation: CheckedContinuation<T?, Never>) {
        self.continuation = continuation
    }

    func resume(_ value: T?) {
        let pending = lock.withLock {
            defer { continuation = nil }
            return continuation
        }
        pending?.resume(returning: value)
    }
}

/// The task's value, or nil when it has none after `timeout`, so a test that would wait forever
/// fails instead. The task itself is left running.
public func finished<T: Sendable>(_ task: Task<T, Error>, within timeout: Duration = .seconds(5)) async -> T? {
    await withCheckedContinuation { continuation in
        let once = ResumeOnce(continuation)
        Task { once.resume(try? await task.value) }
        Task {
            try? await Task.sleep(for: timeout)
            once.resume(nil)
        }
    }
}
