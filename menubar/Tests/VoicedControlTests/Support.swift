import FakeControlServerKit
import Foundation
import VoicedControl

/// A short, unique socket path; `sun_path` holds 104 bytes.
func temporarySocketPath() -> String {
    let name = "vt-\(UUID().uuidString.prefix(8)).sock"
    return FileManager.default.temporaryDirectory.appendingPathComponent(name).path
}

func startServer(
    _ scenario: FakeScenario = .running, configure: (inout FakeControlServer.Options) -> Void = { _ in }
) throws -> FakeControlServer {
    var options = FakeControlServer.Options()
    options.restartDuration = 0.2
    configure(&options)
    let server = FakeControlServer(socketPath: temporarySocketPath(), scenario: scenario, options: options)
    try server.start()
    return server
}

func connectedClient(_ server: FakeControlServer) async throws -> ControlClient {
    let client = ControlClient(socketPath: server.socketPath, requestTimeout: .seconds(2))
    try await client.connect()
    return client
}

/// Waits for the next event matching `predicate`, failing after `timeout`.
func nextEvent(
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
func eventually(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(20))
    }
    return condition()
}

func decodeJSON<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
    try JSONDecoder().decode(type, from: Data(text.utf8))
}
