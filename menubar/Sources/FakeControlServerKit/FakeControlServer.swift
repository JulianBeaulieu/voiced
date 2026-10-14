import Darwin
import Foundation
import VoicedControl

/// A control server that speaks protocol 1 over a Unix socket with scripted state, so the app
/// and the tests run without voiced. It follows the contract closely (peer uid check, hello
/// first, error codes, `settings.set` validation, `restart_required`, warnings, events) but
/// keeps everything in memory.
public final class FakeControlServer: @unchecked Sendable {
    public struct Options: Sendable {
        /// How often `status` is pushed without a change; the contract says at most 10 s.
        public var statusInterval: TimeInterval = 10
        /// How long a restarted service stays `starting`.
        public var restartDuration: TimeInterval = 2
        /// Seconds between state changes in the `cycle` scenario.
        public var cycleInterval: TimeInterval = 6
        public var version = "0.0.1"
        /// Sent verbatim instead of `hello` when set, to test bad servers.
        public var firstLine: String?
        /// Requests with these ops are recorded but never answered, to test timeouts.
        public var unansweredOps: Set<String> = []

        public init() {}
    }

    /// One request as the server received it.
    public struct Received: Sendable, Equatable {
        public var id: Int
        public var op: String
        public var args: [String: JSONValue]
    }

    public let socketPath: String
    public let scenario: FakeScenario
    public let options: Options

    private let queue = DispatchQueue(label: "voiced.fake-control")
    private var listenFD: Int32 = -1
    private var acceptSource: DispatchSourceRead?
    private var clients: [Int32: Client] = [:]
    private var timers: [DispatchSourceTimer] = []
    private var state: FakeState
    private var uptimeReference: [String: Date] = [:]
    private var received: [Received] = []
    private var accepted = 0
    private var cycleStep = 0
    private var nextPID = 5000

    private final class Client {
        let fd: Int32
        let source: DispatchSourceRead
        var framer = LineFramer()

        init(fd: Int32, source: DispatchSourceRead) {
            self.fd = fd
            self.source = source
        }
    }

    public init(socketPath: String, scenario: FakeScenario = .running, options: Options = Options()) {
        self.socketPath = socketPath
        self.scenario = scenario
        self.options = options
        self.state = scenario.initialState
    }

    deinit {
        if listenFD >= 0 { close(listenFD) }
    }

    // MARK: - Public controls

    public func start() throws {
        try queue.sync { try listen() }
    }

    public func stop() {
        queue.sync { shutdown() }
    }

    /// Every request received so far, oldest first.
    public var requests: [Received] { queue.sync { received } }

    /// How many connections were accepted since start.
    public var acceptedConnections: Int { queue.sync { accepted } }

    public var currentState: FakeState { queue.sync { state } }

    /// Changes the state and pushes a `status` event to every client.
    public func update(_ change: @Sendable (inout FakeState) -> Void) {
        queue.sync {
            change(&state)
            broadcastStatus()
        }
    }

    /// Closes every client connection but keeps listening.
    public func dropConnections() {
        queue.sync {
            for client in clients.values { disconnect(client) }
        }
    }

    // MARK: - Socket

    private func listen() throws {
        guard listenFD < 0 else { return }
        unlink(socketPath)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard socketPath.utf8.count < capacity else {
            close(fd)
            throw POSIXError(.ENAMETOOLONG)
        }
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: socketPath.utf8)
            buffer[socketPath.utf8.count] = 0
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0, chmod(socketPath, 0o600) == 0, Darwin.listen(fd, 16) == 0 else {
            let code = errno
            close(fd)
            throw POSIXError(.init(rawValue: code) ?? .EIO)
        }
        _ = fcntl(fd, F_SETFL, O_NONBLOCK)
        listenFD = fd

        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setEventHandler { [weak self] in self?.acceptPending() }
        source.resume()
        acceptSource = source

        let now = Date()
        for key in state.services.keys { uptimeReference[key] = now }
        schedule(every: options.statusInterval) { [weak self] in self?.broadcastStatus() }
        if scenario == .cycle {
            schedule(every: options.cycleInterval) { [weak self] in self?.advanceCycle() }
        }
    }

    private func shutdown() {
        timers.forEach { $0.cancel() }
        timers.removeAll()
        for client in clients.values { disconnect(client) }
        acceptSource?.cancel()
        acceptSource = nil
        if listenFD >= 0 {
            close(listenFD)
            listenFD = -1
            unlink(socketPath)
        }
    }

    private func schedule(every interval: TimeInterval, _ action: @escaping () -> Void) {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + interval, repeating: interval)
        timer.setEventHandler(handler: action)
        timer.resume()
        timers.append(timer)
    }

    private func acceptPending() {
        while true {
            let fd = accept(listenFD, nil, nil)
            guard fd >= 0 else { return }
            // The contract's peer check: only the same user may talk to the socket.
            var credentials = xucred()
            var length = socklen_t(MemoryLayout<xucred>.size)
            guard getsockopt(fd, SOL_LOCAL, LOCAL_PEERCRED, &credentials, &length) == 0,
                credentials.cr_uid == getuid()
            else {
                close(fd)
                continue
            }
            _ = fcntl(fd, F_SETFL, O_NONBLOCK)
            var on: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))

            let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
            let client = Client(fd: fd, source: source)
            source.setEventHandler { [weak self, weak client] in
                guard let self, let client else { return }
                self.read(client)
            }
            source.setCancelHandler { close(fd) }
            clients[fd] = client
            accepted += 1
            source.resume()

            if let line = options.firstLine {
                write(Data((line + "\n").utf8), to: client)
            } else {
                send(
                    [
                        "event": "hello",
                        "data": [
                            "protocol": .number(Double(scenario.helloProtocol)),
                            "version": .string(options.version),
                        ],
                    ], to: client)
            }
        }
    }

    private func read(_ client: Client) {
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        let count = Darwin.read(client.fd, &buffer, buffer.count)
        if count == 0 || (count < 0 && errno != EAGAIN && errno != EINTR) {
            disconnect(client)
            return
        }
        guard count > 0 else { return }
        do {
            for line in try client.framer.append(Data(buffer[0..<count])) {
                handle(line, from: client)
            }
        } catch {
            disconnect(client)
        }
    }

    private func disconnect(_ client: Client) {
        guard clients.removeValue(forKey: client.fd) != nil else { return }
        client.source.cancel()
    }

    private func send(_ message: JSONValue, to client: Client) {
        guard let data = try? LineFramer.frame(message) else { return }
        write(data, to: client)
    }

    private func write(_ data: Data, to client: Client) {
        var offset = 0
        data.withUnsafeBytes { bytes in
            while offset < bytes.count {
                let written = Darwin.write(client.fd, bytes.baseAddress! + offset, bytes.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && (errno == EAGAIN || errno == EINTR) {
                    usleep(1_000)
                } else {
                    break
                }
            }
        }
        if offset < data.count { disconnect(client) }
    }

    private func broadcast(event name: String, data: JSONValue) {
        for client in clients.values { send(["event": .string(name), "data": data], to: client) }
    }

    private func broadcastStatus() {
        broadcast(event: "status", data: statusJSON())
    }

    // MARK: - Protocol

    private func handle(_ line: Data, from client: Client) {
        guard let message = try? JSONValue(data: line), let fields = message.objectValue else {
            send(error(nil, "bad_request", "Not a JSON object."), to: client)
            return
        }
        let id = fields["id"] ?? .null
        guard let number = id.intValue, let op = fields["op"]?.stringValue else {
            send(error(id, "bad_request", "A request needs an integer id and an op."), to: client)
            return
        }
        let args = fields["args"]?.objectValue ?? [:]
        received.append(Received(id: number, op: op, args: args))
        if options.unansweredOps.contains(op) { return }

        let answer: Result<JSONValue, RemoteError>
        switch op {
        case "status": answer = .success(statusJSON())
        case "settings.get": answer = .success(json(state.settings))
        case "settings.set": answer = setSettings(args)
        case "engines.list": answer = .success(json(state.engines))
        case "service.restart": answer = restart(args)
        case "logs.tail": answer = tail(args)
        case "open": answer = open(args)
        default: answer = .failure(RemoteError(code: "unknown_op", message: "Unknown op \(op)."))
        }
        switch answer {
        case .success(let result):
            send(["id": id, "ok": true, "result": result], to: client)
            if op == "settings.set" { broadcast(event: "settings", data: json(state.settings)) }
        case .failure(let failure):
            var body: [String: JSONValue] = ["code": .string(failure.code), "message": .string(failure.message)]
            if let field = failure.field { body["field"] = .string(field) }
            send(["id": id, "ok": false, "error": .object(body)], to: client)
        }
    }

    private func error(_ id: JSONValue?, _ code: String, _ message: String) -> JSONValue {
        ["id": id ?? .null, "ok": false, "error": ["code": .string(code), "message": .string(message)]]
    }

    private func json<T: Encodable>(_ value: T) -> JSONValue {
        (try? JSONValue(encoding: value)) ?? .null
    }

    private func statusJSON() -> JSONValue {
        var status = state.status
        let now = Date()
        for (key, service) in status.services {
            guard let base = service.uptime, service.state != .stopped else { continue }
            let since = uptimeReference[key] ?? now
            status.services[key]?.uptime = base + now.timeIntervalSince(since)
        }
        return json(status)
    }

    private static let settingKeys: Set<String> = [
        "bind", "api_key", "zeroconf", "stt.engine", "stt.model", "tts.engine", "tts.model",
        "tts.voice", "tts.speed", "browser_settings",
    ]

    private func setSettings(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        func invalid(_ field: String, _ message: String) -> Result<JSONValue, RemoteError> {
            .failure(RemoteError(code: "invalid_setting", message: "\(field): \(message)", field: field))
        }
        var next = state
        for key in args.keys.sorted() {
            let value = args[key]!
            guard Self.settingKeys.contains(key) else { return invalid(key, "not a setting") }
            switch (key, value) {
            case ("bind", .string(let raw)):
                guard let mode = BindMode(rawValue: raw) else { return invalid(key, "loopback or lan") }
                next.settings.bind = mode
            case ("api_key", .string(let key)):
                next.apiKey = key
                next.settings.apiKeySet = !key.isEmpty
            case ("zeroconf", .bool(let on)): next.settings.zeroconf = on
            case ("browser_settings", .bool(let on)): next.settings.browserSettings = on
            case ("stt.engine", .string(let name)): next.settings.stt.engine = name
            case ("stt.model", .string(let model)): next.settings.stt.model = model
            case ("tts.engine", .string(let name)): next.settings.tts.engine = name
            case ("tts.model", .string(let model)): next.settings.tts.model = model
            case ("tts.voice", .string(let voice)): next.settings.tts.voice = voice
            case ("tts.speed", .number(let speed)):
                guard (0.5...2.0).contains(speed) else { return invalid(key, "between 0.5 and 2.0") }
                next.settings.tts.speed = speed
            default:
                return invalid(key, "wrong type")
            }
        }
        // Validate the result as a whole, as the real server does through `Settings`.
        guard let stt = next.engines.sttEngine(named: next.settings.stt.engine) else {
            return invalid("stt.engine", "unknown engine \(next.settings.stt.engine)")
        }
        guard stt.models.isEmpty || stt.models.contains(next.settings.stt.model) else {
            return invalid("stt.model", "\(next.settings.stt.model) is not a \(stt.name) model")
        }
        guard let tts = next.engines.ttsEngine(named: next.settings.tts.engine) else {
            return invalid("tts.engine", "unknown engine \(next.settings.tts.engine)")
        }
        guard tts.models.isEmpty || tts.models.contains(next.settings.tts.model) else {
            return invalid("tts.model", "\(next.settings.tts.model) is not a \(tts.name) model")
        }
        guard tts.voices.isEmpty || tts.voices.contains(next.settings.tts.voice) else {
            return invalid("tts.voice", "\(next.settings.tts.voice) is not a \(tts.name) voice")
        }
        state = next

        var restart = Set<String>()
        for key in args.keys {
            if key.hasPrefix("stt.") { restart.insert("stt") }
            if key.hasPrefix("tts.") { restart.insert("tts") }
            if ["bind", "api_key", "zeroconf"].contains(key) { restart.formUnion(["stt", "tts"]) }
        }
        guard case .object(var result) = json(state.settings) else { return .success(.null) }
        result["restart_required"] = .array(restart.sorted().map { .string($0) })
        if state.settings.isLANWithoutAPIKey {
            result["warnings"] = ["lan_without_api_key"]
        }
        return .success(.object(result))
    }

    private func restart(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        let kinds: [ServiceKind]
        switch args["service"]?.stringValue {
        case "all": kinds = ServiceKind.allCases
        case let name?: 
            guard let kind = ServiceKind(rawValue: name) else {
                return .failure(RemoteError(code: "bad_request", message: "Unknown service \(name)."))
            }
            kinds = [kind]
        case nil:
            return .failure(RemoteError(code: "bad_request", message: "service is required."))
        }
        guard state.installed else {
            return .failure(RemoteError(code: "not_installed", message: "voiced is not installed."))
        }
        let now = Date()
        for kind in kinds {
            nextPID += 1
            state[kind].state = .starting
            state[kind].warm = false
            state[kind].pid = nextPID
            state[kind].uptime = 0
            state[kind].health = nil
            uptimeReference[kind.rawValue] = now
        }
        // Answer first, then report progress as events, as the contract describes.
        queue.async { [weak self] in self?.broadcastStatus() }
        queue.asyncAfter(deadline: .now() + options.restartDuration) { [weak self] in
            guard let self else { return }
            for kind in kinds where self.state[kind].state == .starting {
                self.state[kind].state = .running
                self.state[kind].warm = true
            }
            self.broadcastStatus()
        }
        return .success(["accepted": true])
    }

    private func tail(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        guard let service = args["service"]?.stringValue, ["stt", "tts", "watchdog"].contains(service) else {
            return .failure(RemoteError(code: "bad_request", message: "service must be stt, tts or watchdog."))
        }
        let count = args["lines"]?.intValue ?? 100
        guard (1...1000).contains(count) else {
            return .failure(RemoteError(code: "bad_request", message: "lines must be 1 to 1000."))
        }
        let lines = (1...min(count, 3)).map { JSONValue.string("\(service): sample log line \($0)") }
        return .success(["lines": .array(lines)])
    }

    private func open(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        let paths = state.settings.paths
        switch args["what"]?.stringValue.flatMap(OpenTarget.init(rawValue:)) {
        case .statusPage?:
            let port = state.settings.ports?.ttsHTTP ?? 8880
            return .success(["target": .string("http://127.0.0.1:\(port)/")])
        case .logsDirectory?:
            return .success(["target": .string(paths?.logs ?? "")])
        case .configFile?:
            return .success(["target": .string(paths?.config ?? "")])
        case nil:
            return .failure(RemoteError(code: "bad_request", message: "what must be status_page, logs_dir or config_file."))
        }
    }

    private func advanceCycle() {
        cycleStep += 1
        let order: [FakeScenario] = [.running, .starting, .degraded, .stopped]
        let next = order[cycleStep % order.count].initialState
        state.installed = next.installed
        state.services = next.services
        let now = Date()
        for key in state.services.keys { uptimeReference[key] = now }
        broadcastStatus()
    }
}
