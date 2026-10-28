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
        /// The hello's `install`: what `install.json` records, null for none.
        public var install: JSONValue = .null
        /// Read for each hello instead of `install` when set, as the watchdog reads
        /// `install.json`: an install that rewrites it shows on the next connection.
        public var installFile: URL?
        /// Sent verbatim instead of `hello` when set, to test bad servers.
        public var firstLine: String?
        /// Requests with these ops are recorded but never answered, to test timeouts.
        public var unansweredOps: Set<String> = []
        /// Requests with these ops are answered with an `internal` error.
        public var failingOps: Set<String> = []
        /// Close every connection right after accepting it, as the peer check does for
        /// another user.
        public var closeOnAccept = false
        /// Close a connection when its first request arrives: the client has read hello by
        /// then, so this is a watchdog that says hello and goes away, with no race.
        public var closeOnFirstRequest = false
        /// Settings a `VOICED_*` environment variable overrides, reported as
        /// `env_override:<key>` warnings when they are set, as the real server does.
        public var environmentOverrides: Set<SettingKey> = []
        /// Services `service.restart` refuses as `not_installed`, with the message. `all`
        /// is still accepted while one service can restart, with the refusal in `detail`,
        /// as the real server does.
        public var refusedRestarts: [ServiceKind: String] = [:]
        /// The fake's Hugging Face cache, written with the scenario's models at start. With it,
        /// `settings.set` refuses a model that is not downloaded and a restarted service
        /// without its model reports `missing_model`, as the real ones do (ADR 0022).
        public var modelsFile: URL?
        /// How long `tts.speak` takes to answer, as a model loading again would.
        public var speakDelay: TimeInterval = 0.2

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
        if let file = options.modelsFile { FakeModels(file: file).write(scenario.initialModels) }
        try queue.sync { try listen() }
    }

    public func stop() {
        queue.sync { shutdown() }
    }

    /// Every request received so far, oldest first.
    public var requests: [Received] { queue.sync { received } }

    /// How many connections were accepted since start.
    public var acceptedConnections: Int { queue.sync { accepted } }

    /// Connections open now; one closes when its client hangs up.
    public var openConnections: Int { queue.sync { clients.count } }

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
        try removeStaleSocket()
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

    /// Removes a socket file nobody listens on, like the real server, and refuses to touch
    /// anything else: a live socket or a file that is not a socket.
    private func removeStaleSocket() throws {
        var info = stat()
        guard lstat(socketPath, &info) == 0 else { return }
        guard info.st_mode & S_IFMT == S_IFSOCK else { throw POSIXError(.EEXIST) }
        let probe = socket(AF_UNIX, SOCK_STREAM, 0)
        defer { close(probe) }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: socketPath.utf8.prefix(buffer.count - 1))
        }
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(probe, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected != 0 else { throw POSIXError(.EADDRINUSE) }
        unlink(socketPath)
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
                credentials.cr_uid == getuid(), !options.closeOnAccept
            else {
                close(fd)
                continue
            }
            // Without SO_NOSIGPIPE a write to a peer that left kills the process. macOS
            // refuses the option (EINVAL) on a socket whose peer is already gone, such as a
            // stale-socket probe; such a connection has nobody to talk to anyway.
            var on: Int32 = 1
            guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size)) == 0 else {
                close(fd)
                continue
            }
            _ = fcntl(fd, F_SETFL, O_NONBLOCK)

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
                            "install": installValue,
                        ],
                    ], to: client)
            }
        }
    }

    private var installValue: JSONValue {
        guard let file = options.installFile else { return options.install }
        return (try? Data(contentsOf: file)).flatMap { try? JSONValue(data: $0) } ?? .null
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
            guard let base = bytes.baseAddress else { return }
            while offset < bytes.count {
                let written = Darwin.write(client.fd, base + offset, bytes.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && (errno == EAGAIN || errno == EINTR) {
                    // The client is slow to read: wait until the socket can take more.
                    var ready = pollfd(fd: client.fd, events: Int16(POLLOUT), revents: 0)
                    if poll(&ready, 1, 1_000) <= 0 { break }
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
            send(failure(.null, RemoteError(code: "bad_request", message: "not a JSON object")), to: client)
            return
        }
        let id = fields["id"] ?? .null
        guard let number = id.intValue, let op = fields["op"]?.stringValue else {
            send(failure(id, RemoteError(code: "bad_request", message: "a request needs an integer id and an op")), to: client)
            return
        }
        if options.closeOnFirstRequest {
            disconnect(client)
            return
        }
        let args = fields["args"]?.objectValue ?? [:]
        received.append(Received(id: number, op: op, args: args))
        if options.unansweredOps.contains(op) { return }
        if options.failingOps.contains(op) {
            send(failure(id, RemoteError(code: "internal", message: "\(op) failed")), to: client)
            return
        }

        let answer: Result<JSONValue, RemoteError>
        switch op {
        case "status": answer = .success(statusJSON())
        case "settings.get": answer = .success(json(state.settings))
        case "settings.set": answer = setSettings(args)
        case "engines.list": answer = .success(json(state.engines))
        case "service.restart": answer = restart(args)
        case "logs.tail": answer = tail(args)
        case "open": answer = open(args)
        case "tts.speak":
            // Answered later, as the real one waits for the audio; the client may be gone by then.
            let answer = speak(args)
            let (fd, asker) = (client.fd, ObjectIdentifier(client))
            queue.asyncAfter(deadline: .now() + options.speakDelay) { [weak self] in
                guard let self, let client = self.clients[fd], ObjectIdentifier(client) == asker else { return }
                switch answer {
                case .success(let result): self.send(["id": id, "ok": true, "result": result], to: client)
                case .failure(let error): self.send(self.failure(id, error), to: client)
                }
            }
            return
        default: answer = .failure(RemoteError(code: "unknown_op", message: "Unknown op \(op)."))
        }
        switch answer {
        case .success(let result):
            send(["id": id, "ok": true, "result": result], to: client)
            if op == "settings.set" { broadcast(event: "settings", data: json(state.settings)) }
        case .failure(let error):
            send(failure(id, error), to: client)
        }
    }

    private func failure(_ id: JSONValue, _ error: RemoteError) -> JSONValue {
        var body: [String: JSONValue] = ["code": .string(error.code), "message": .string(error.message)]
        if let field = error.field { body["field"] = .string(field) }
        return ["id": id, "ok": false, "error": .object(body)]
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

    private func setSettings(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        // Like the real server: the message does not repeat the field, `field` names it.
        func invalid(_ field: String, _ message: String) -> Result<JSONValue, RemoteError> {
            .failure(RemoteError(code: "invalid_setting", message: message, field: field))
        }
        var next = state
        var keys: [SettingKey] = []
        for (name, value) in args.sorted(by: { $0.key < $1.key }) {
            guard let key = SettingKey(rawValue: name) else { return invalid(name, "not a settable key") }
            keys.append(key)
            switch (key, value) {
            case (.bind, .string(let raw)):
                guard let mode = BindMode(rawValue: raw) else { return invalid(name, "must be loopback or lan") }
                next.settings.bind = mode
            case (.apiKey, .string(let apiKey)):
                next.apiKey = apiKey
                next.settings.apiKeySet = !apiKey.isEmpty
            case (.zeroconf, .bool(let on)): next.settings.zeroconf = on
            case (.browserSettings, .bool(let on)): next.settings.browserSettings = on
            case (.sttEngine, .string(let engine)): next.settings.stt.engine = engine
            case (.sttModel, .string(let model)): next.settings.stt.model = model
            case (.ttsEngine, .string(let engine)): next.settings.tts.engine = engine
            case (.ttsModel, .string(let model)): next.settings.tts.model = model
            case (.ttsVoice, .string(let voice)): next.settings.tts.voice = voice
            // The bounds are voiced.config's MIN_TTS_SPEED, MAX_TTS_SPEED and
            // MAX_IDLE_UNLOAD_S, the messages voiced.settings_store's, and the
            // idle_unload_s hint that field's description in voiced.config.
            case (.ttsSpeed, .number(let speed)):
                guard speed >= 0.25 else { return invalid(name, "must be at least 0.25") }
                guard speed <= 4 else { return invalid(name, "must be at most 4") }
                next.settings.tts.speed = speed
            case (.sttEnabled, .bool(let on)): next.settings.stt.enabled = on
            case (.ttsEnabled, .bool(let on)): next.settings.tts.enabled = on
            case (.sttIdleUnload, .number(let seconds)), (.ttsIdleUnload, .number(let seconds)):
                let hint = "(seconds; 0 is never, 604800 one week)"
                guard seconds.rounded() == seconds else {
                    return invalid(name, "must be a whole number, got \(seconds) \(hint)")
                }
                guard seconds >= 0 else { return invalid(name, "must be at least 0 \(hint)") }
                guard seconds <= 604_800 else { return invalid(name, "must be at most 604800 \(hint)") }
                if key == .sttIdleUnload {
                    next.settings.stt.idleUnloadS = Int(seconds)
                } else {
                    next.settings.tts.idleUnloadS = Int(seconds)
                }
            default:
                return invalid(name, "wrong type")
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
        for (field, model, changed) in [
            ("stt.model", next.settings.stt.model, keys.contains(.sttEngine) || keys.contains(.sttModel)),
            ("tts.model", next.settings.tts.model, keys.contains(.ttsEngine) || keys.contains(.ttsModel)),
        ] where changed && isMissing(model) {
            return .failure(RemoteError(
                code: "model_not_downloaded",
                message: "\(model) is not downloaded; run voiced models download \(model)", field: field))
        }
        state = next
        // The fake `voiced models delete` refuses the configured models, as the real one reads
        // the config file.
        if let file = options.modelsFile {
            FakeModels(file: file).update { $0.inUse = [next.settings.stt.model, next.settings.tts.model] }
        }

        var restart = Set<String>()
        for key in keys {
            switch key {
            case .sttEngine, .sttModel, .sttEnabled, .sttIdleUnload: restart.insert("stt")
            case .ttsEngine, .ttsModel, .ttsVoice, .ttsEnabled, .ttsIdleUnload: restart.insert("tts")
            case .bind, .apiKey, .zeroconf: restart.formUnion(["stt", "tts"])
            case .browserSettings: break  // read live by the status page
            case .ttsSpeed: break  // read live by the tts service, for each request
            }
        }
        guard case .object(var result) = json(state.settings) else { return .success(.null) }
        result["restart_required"] = .array(restart.sorted().map { .string($0) })
        var warnings: [JSONValue] = []
        if keys.contains(.bind) || keys.contains(.apiKey), state.settings.isLANWithoutAPIKey {
            warnings.append("lan_without_api_key")
        }
        for key in keys where options.environmentOverrides.contains(key) {
            warnings.append(.string("env_override:\(key.rawValue)"))
        }
        result["warnings"] = .array(warnings)
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
        var detail: [String: JSONValue] = [:]
        for kind in kinds {
            detail[kind.rawValue] = options.refusedRestarts[kind].map { .string("not_installed: \($0)") } ?? "restarting"
        }
        let refused = kinds.filter { options.refusedRestarts[$0] != nil }
        if refused.count == kinds.count {
            let message = refused.compactMap { options.refusedRestarts[$0] }.joined(separator: "; ")
            return .failure(RemoteError(code: "not_installed", message: message))
        }
        let accepted = kinds.filter { options.refusedRestarts[$0] == nil }
        let now = Date()
        for kind in accepted {
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
            for kind in accepted where self.state[kind].state == .starting {
                if !self.state.settings.isEnabled(kind) {
                    // Switched off: the process parks (ADR 0033).
                    self.state[kind].state = .off
                    continue
                }
                let missing = self.isMissing(kind == .stt ? self.state.settings.stt.model : self.state.settings.tts.model)
                self.state[kind].state = missing ? .missingModel : .running
                self.state[kind].warm = !missing
                self.state[kind].engine = kind == .stt ? self.state.settings.stt.engine : self.state.settings.tts.engine
                self.state[kind].model = kind == .stt ? self.state.settings.stt.model : self.state.settings.tts.model
            }
            self.broadcastStatus()
        }
        return .success(["accepted": true, "detail": .object(detail)])
    }

    /// A catalogued model that the fake cache does not hold.
    private func isMissing(_ model: String) -> Bool {
        options.modelsFile.flatMap { FakeModels(file: $0).read()?.isMissing(model) } ?? false
    }

    /// Like the real `tts.speak`: refused while text to speech is not running, else a WAV
    /// file whose length follows the text, a quiet tone in place of speech.
    private func speak(_ args: [String: JSONValue]) -> Result<JSONValue, RemoteError> {
        guard let text = args["text"]?.stringValue, !text.isEmpty, text.unicodeScalars.count <= 60,
            let voice = args["voice"]?.stringValue
        else {
            return .failure(RemoteError(code: "bad_request", message: "text and voice are required."))
        }
        guard [.running, .degraded].contains(state[.tts].state) else {
            return .failure(RemoteError(code: "service_unavailable", message: "the tts service is not answering"))
        }
        let tts = state.engines.ttsEngine(named: state.settings.tts.engine)
        guard tts?.voices.contains(voice) ?? true else {
            return .failure(RemoteError(
                code: "bad_request", message: "the running tts service does not serve the voice '\(voice)'"))
        }
        let seconds = Double(text.count) / 15 / state.settings.tts.speed
        return .success(["format": "wav", "audio": .string(Self.tone(seconds: seconds).base64EncodedString())])
    }

    /// A mono int16 WAV file at 24 kHz holding a quiet 440 Hz tone.
    static func tone(seconds: Double) -> Data {
        let rate = 24_000
        let frames = Int(seconds * Double(rate))
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: Array("RIFF".utf8))
        append(UInt32(36 + frames * 2))
        data.append(contentsOf: Array("WAVEfmt ".utf8))
        append(UInt32(16))
        append(UInt16(1))  // PCM
        append(UInt16(1))  // mono
        append(UInt32(rate))
        append(UInt32(rate * 2))
        append(UInt16(2))
        append(UInt16(16))
        data.append(contentsOf: Array("data".utf8))
        append(UInt32(frames * 2))
        for frame in 0..<frames {
            append(Int16(sin(Double(frame) * 2 * .pi * 440 / Double(rate)) * 2_000))
        }
        return data
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
