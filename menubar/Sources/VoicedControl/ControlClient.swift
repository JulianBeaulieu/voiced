import Foundation
import Network

/// One connection to the voiced control socket.
///
/// A client is single use: `connect()` opens the Unix socket and waits for `hello`, requests
/// are matched to answers by id, and server events arrive on `events` until the connection
/// ends, when the stream finishes. Reconnecting means making a new client; `AppModel` does
/// that with a backoff. All mutable state is confined to `queue`. Cancelling the task that
/// awaits `connect()` or `request()` closes the connection.
public final class ControlClient: @unchecked Sendable {
    /// The control protocol version this client speaks.
    public static let supportedProtocol = 1
    /// `service.restart` may take a while when the service it restarts is hung, which is
    /// exactly when people press Restart.
    public static let restartTimeout: Duration = .seconds(30)

    public let socketPath: String
    public let requestTimeout: Duration
    /// Server events, in order. Finishes when the connection ends for any reason.
    public let events: AsyncStream<ControlEvent>

    private let eventSink: AsyncStream<ControlEvent>.Continuation
    private let queue = DispatchQueue(label: "voiced.control.client")
    private var connection: NWConnection?
    private var framer = LineFramer()
    private var nextID = 1
    private var pending: [Int: CheckedContinuation<JSONValue, Error>] = [:]
    private var helloWaiter: CheckedContinuation<Hello, Error>?
    private var hello: Hello?
    private var finished = false

    public init(socketPath: String, requestTimeout: Duration = .seconds(10)) {
        self.socketPath = socketPath
        self.requestTimeout = requestTimeout
        (events, eventSink) = AsyncStream.makeStream(bufferingPolicy: .bufferingNewest(64))
    }

    deinit {
        connection?.cancel()
        eventSink.finish()
    }

    /// Opens the socket and waits for the server's `hello`. Throws `cannotConnect` with the
    /// reason when there is no usable server, and `unsupportedProtocol` when the server's
    /// protocol differs.
    @discardableResult
    public func connect(timeout: Duration = .seconds(3)) async throws -> Hello {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.async { self.start(continuation, timeout: timeout) }
            }
        } onCancel: {
            close()
        }
    }

    /// Sends one request and waits for its answer. Server errors throw `ControlError.remote`.
    public func request(
        _ op: String, _ args: [String: JSONValue] = [:], timeout: Duration? = nil
    ) async throws -> JSONValue {
        let limit = timeout ?? requestTimeout
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.async { self.send(op, args, limit, continuation) }
            }
        } onCancel: {
            close()
        }
    }

    /// Closes the connection. Pending requests fail with `connectionClosed`.
    public func close() {
        queue.async { self.finish(with: .connectionClosed) }
    }

    // MARK: - Queue-confined implementation

    private func start(_ continuation: CheckedContinuation<Hello, Error>, timeout: Duration) {
        guard connection == nil, !finished else {
            continuation.resume(throwing: finished ? ControlError.connectionClosed : ControlError.protocolViolation("client already used"))
            return
        }
        // NWEndpoint traps on a path that does not fit in sockaddr_un.
        guard socketPath.utf8.count <= SocketLocation.maximumPathLength else {
            finished = true
            eventSink.finish()
            continuation.resume(throwing: ControlError.cannotConnect(.pathTooLong))
            return
        }
        helloWaiter = continuation
        let parameters = NWParameters(tls: nil, tcp: NWProtocolTCP.Options())
        let connection = NWConnection(to: .unix(path: socketPath), using: parameters)
        self.connection = connection
        connection.stateUpdateHandler = { [weak self] state in
            self?.handle(state)
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + timeout.timeInterval) { [weak self] in
            guard let self, self.hello == nil, !self.finished else { return }
            self.finish(with: .cannotConnect(.noHello))
        }
    }

    private func handle(_ state: NWConnection.State) {
        switch state {
        case .ready:
            receive()
        case .waiting(let error), .failed(let error):
            // For a Unix socket, `waiting` means nothing usable is there; the app retries on
            // its own schedule, so give up on this attempt.
            finish(with: hello == nil ? .cannotConnect(Self.failure(error)) : .connectionClosed)
        case .cancelled:
            finish(with: .connectionClosed)
        default:
            break
        }
    }

    private func receive() {
        connection?.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
            [weak self] data, _, isComplete, error in
            guard let self, !self.finished else { return }
            if let data, !data.isEmpty {
                do {
                    for line in try self.framer.append(data) {
                        self.handle(line: line)
                        if self.finished { return }
                    }
                } catch {
                    self.finish(with: .protocolViolation("line too long"))
                    return
                }
            }
            if isComplete || error != nil {
                self.finish(with: self.hello == nil ? .cannotConnect(.closedBeforeHello) : .connectionClosed)
            } else {
                self.receive()
            }
        }
    }

    private func handle(line: Data) {
        // A line that is not JSON is skipped rather than fatal: one bad line from a buggy
        // server should not take the menu bar down with it.
        guard let message = try? JSONValue(data: line) else { return }
        guard hello != nil else {
            handleFirst(message)
            return
        }
        switch IncomingMessage(message) {
        case .response(let id, let result)?:
            guard let continuation = pending.removeValue(forKey: id) else { return }
            switch result {
            case .success(let value): continuation.resume(returning: value)
            case .failure(let error): continuation.resume(throwing: ControlError.remote(error))
            }
        case .event(let name, let data)?:
            eventSink.yield(ControlEvent(name: name, data: data))
        case nil:
            break
        }
    }

    private func handleFirst(_ message: JSONValue) {
        guard message["event"] == "hello" else {
            finish(with: .protocolViolation("the first message was not hello"))
            return
        }
        guard let greeting = try? message["data"]?.decode(Hello.self) else {
            finish(with: .protocolViolation("malformed hello"))
            return
        }
        guard greeting.protocol == Self.supportedProtocol else {
            finish(
                with: .unsupportedProtocol(
                    server: greeting.protocol, supported: Self.supportedProtocol))
            return
        }
        hello = greeting
        helloWaiter?.resume(returning: greeting)
        helloWaiter = nil
    }

    private func send(
        _ op: String, _ args: [String: JSONValue], _ timeout: Duration,
        _ continuation: CheckedContinuation<JSONValue, Error>
    ) {
        guard let connection, hello != nil, !finished else {
            continuation.resume(throwing: ControlError.notConnected)
            return
        }
        let id = nextID
        nextID += 1
        let line: Data
        do {
            line = try LineFramer.frame(["id": .number(Double(id)), "op": .string(op), "args": .object(args)])
        } catch {
            continuation.resume(throwing: ControlError.protocolViolation("cannot encode \(op)"))
            return
        }
        pending[id] = continuation
        connection.send(
            content: line,
            completion: .contentProcessed { [weak self] error in
                guard let self, error != nil else { return }
                self.finish(with: .connectionClosed)
            })
        queue.asyncAfter(deadline: .now() + timeout.timeInterval) { [weak self] in
            self?.pending.removeValue(forKey: id)?.resume(
                throwing: ControlError.timedOut(operation: op))
        }
    }

    private func finish(with error: ControlError) {
        guard !finished else { return }
        finished = true
        helloWaiter?.resume(throwing: error)
        helloWaiter = nil
        let waiting = pending
        pending.removeAll()
        // Requests in flight fail the same way whatever ended the connection.
        for continuation in waiting.values {
            continuation.resume(throwing: ControlError.connectionClosed)
        }
        connection?.stateUpdateHandler = nil
        connection?.cancel()
        eventSink.finish()
    }

    private static func failure(_ error: NWError) -> ConnectFailure {
        if case .posix(let code) = error {
            switch code {
            case .ENOENT: return .noSocket
            case .ECONNREFUSED: return .notListening
            case .EACCES, .EPERM: return .permissionDenied
            case .ENOTSOCK: return .notASocket
            case .ENAMETOOLONG: return .pathTooLong
            // The server hung up while the connection was being set up; Network reports a
            // Unix socket closed that early as "network is down".
            case .ENETDOWN, .ECONNRESET, .EPIPE: return .closedBeforeHello
            default: break
            }
        }
        return .other(error.localizedDescription)
    }
}

extension Duration {
    var timeInterval: TimeInterval {
        let (seconds, attoseconds) = components
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}

// MARK: - Typed operations

extension ControlClient {
    public func status() async throws -> StatusPayload {
        try await request("status").decode(StatusPayload.self)
    }

    public func settings() async throws -> SettingsPayload {
        try await request("settings.get").decode(SettingsPayload.self)
    }

    public func setSettings(_ args: [String: JSONValue]) async throws -> SettingsSetResult {
        try await request("settings.set", args).decode(SettingsSetResult.self)
    }

    public func engines() async throws -> EnginesPayload {
        try await request("engines.list").decode(EnginesPayload.self)
    }

    public func restart(_ target: RestartTarget, timeout: Duration = ControlClient.restartTimeout) async throws {
        _ = try await request("service.restart", ["service": .string(target.argument)], timeout: timeout)
    }

    /// Asks the server where something lives; the caller checks and opens it.
    public func openTarget(_ what: OpenTarget) async throws -> String {
        let result = try await request("open", ["what": .string(what.rawValue)])
        guard let target = result["target"]?.stringValue else {
            throw ControlError.protocolViolation("open returned no target")
        }
        return target
    }
}
