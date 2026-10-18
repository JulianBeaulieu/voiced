import Foundation

/// The first message the server sends on every connection.
public struct Hello: Codable, Sendable, Equatable {
    public var `protocol`: Int
    /// The voiced version; nil when the server leaves it out.
    public var version: String?
    /// What the install's `install.json` records; nil without one, from an older watchdog,
    /// or when it is not the expected shape (protocol 1 ignores what it cannot use).
    public var install: InstallRecord?

    public init(protocol: Int, version: String?, install: InstallRecord? = nil) {
        self.protocol = `protocol`
        self.version = version
        self.install = install
    }

    enum CodingKeys: String, CodingKey {
        case `protocol`, version, install
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.protocol = try container.decode(Int.self, forKey: .protocol)
        version = try container.decodeIfPresent(String.self, forKey: .version)
        install = try? container.decodeIfPresent(InstallRecord.self, forKey: .install)
    }
}

/// Who made an install (`install.json`'s `source`, ADR 0017). A record with any other value is
/// not one this app understands and reads as no record.
public enum InstallSource: String, Codable, Sendable {
    case app, homebrew, cli
}

/// `~/.voiced/install.json` (ADR 0017): who installed voiced, which environment is active,
/// and the voiced version, lock hash and Python in it. The watchdog's `hello` carries it, and
/// without a watchdog the app reads the file. Fields the record lacks are nil.
public struct InstallRecord: Codable, Sendable, Equatable {
    public var source: InstallSource
    public var environment: String?
    public var version: String?
    public var lockSHA256: String?
    public var python: String?

    public init(
        source: InstallSource, environment: String? = nil, version: String? = nil, lockSHA256: String? = nil,
        python: String? = nil
    ) {
        self.source = source
        self.environment = environment
        self.version = version
        self.lockSHA256 = lockSHA256
        self.python = python
    }

    enum CodingKeys: String, CodingKey {
        case source, environment, version, python
        case lockSHA256 = "lock_sha256"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        source = try container.decode(InstallSource.self, forKey: .source)
        func text(_ key: CodingKeys) -> String? {
            // "" is what install.json holds before the first environment is linked.
            guard let value = try? container.decodeIfPresent(String.self, forKey: key), !value.isEmpty else { return nil }
            return value
        }
        environment = text(.environment)
        version = text(.version)
        lockSHA256 = text(.lockSHA256)
        python = text(.python)
    }

    /// Reads `install.json` in `stateDirectory`: nil when it is missing, unreadable or not
    /// voiced's. Opened without blocking and read up to 64 KiB, as the watchdog reads it
    /// (`layout.read_manifest`): a FIFO or a huge file there must not hang the app.
    public static func read(stateDirectory: URL) -> InstallRecord? {
        let fd = open(stateDirectory.appendingPathComponent("install.json").path, O_RDONLY | O_NONBLOCK | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        guard let data = try? handle.read(upToCount: 64 * 1024 + 1), data.count <= 64 * 1024 else { return nil }
        return try? JSONDecoder().decode(InstallRecord.self, from: data)
    }
}

/// An error answer from the server: `{"ok": false, "error": {...}}`.
public struct RemoteError: Error, Sendable, Equatable, Decodable {
    public var code: String
    public var message: String
    /// The offending setting for `invalid_setting`, when the server names it.
    public var field: String?

    public init(code: String, message: String, field: String? = nil) {
        self.code = code
        self.message = message
        self.field = field
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        code = try c.decodeIfPresent(String.self, forKey: .code) ?? "internal"
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
        field = try c.decodeIfPresent(String.self, forKey: .field)
    }

    enum CodingKeys: String, CodingKey { case code, message, field }
}

/// Why a connection could not be set up. Each case has its own explanation in the UI.
public enum ConnectFailure: Sendable, Equatable {
    /// No file at the socket path: the watchdog is not running (or voiced is not installed).
    case noSocket
    /// A socket file nobody listens on: left over from a watchdog that stopped.
    case notListening
    /// The socket belongs to another user.
    case permissionDenied
    /// Something that is not a socket sits at the path.
    case notASocket
    /// The path does not fit in a Unix socket address (104 bytes on macOS).
    case pathTooLong
    /// Something accepted the connection but never said hello.
    case noHello
    /// The server closed the connection before hello, as its peer check does for another user.
    case closedBeforeHello
    case other(String)
}

extension ConnectFailure {
    /// A short description for error messages; the window explains each case at length.
    public var summary: String {
        switch self {
        case .noSocket: "the control socket does not exist"
        case .notListening: "nothing is listening on the control socket"
        case .permissionDenied: "the control socket belongs to another user"
        case .notASocket: "the path is not a socket"
        case .pathTooLong: "the socket path is too long"
        case .noHello: "the server did not say hello"
        case .closedBeforeHello: "the server closed the connection before saying hello"
        case .other(let detail): detail
        }
    }
}

/// Everything that can go wrong on the client side.
public enum ControlError: Error, Sendable, Equatable {
    case cannotConnect(ConnectFailure)
    /// The server speaks a protocol version this app does not.
    case unsupportedProtocol(server: Int, supported: Int)
    /// The server sent something that is not the protocol.
    case protocolViolation(String)
    case timedOut(operation: String)
    case notConnected
    case connectionClosed
    case remote(RemoteError)
}

extension ControlError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .cannotConnect(let failure):
            "Cannot connect to voiced: \(failure.summary)."
        case .unsupportedProtocol(let server, let supported) where server > supported:
            "This voiced uses control protocol \(server); this menu bar app only knows "
                + "protocol \(supported). Update the menu bar app."
        case .unsupportedProtocol(let server, let supported):
            "This voiced uses control protocol \(server), older than protocol \(supported) "
                + "which this app needs. Update voiced."
        case .protocolViolation(let reason):
            "voiced sent an unexpected message: \(reason)."
        case .timedOut(let operation):
            "voiced did not answer \(operation) in time."
        case .notConnected:
            "Not connected to voiced."
        case .connectionClosed:
            "The connection to voiced closed."
        case .remote(let error):
            error.displayMessage
        }
    }
}

extension RemoteError {
    /// The message, led by the setting it is about when the server names one and the
    /// message does not already start with it.
    public var displayMessage: String {
        let text = message.isEmpty ? code : message
        guard let field, !field.isEmpty, !text.hasPrefix(field) else { return text }
        return "\(field): \(text)"
    }
}

/// A message received after `hello`.
public enum IncomingMessage: Sendable, Equatable {
    case response(id: Int, result: Result<JSONValue, RemoteError>)
    case event(name: String, data: JSONValue)

    /// Classifies one decoded line. Returns nil for anything that is neither a response
    /// with an integer id nor an event, which the client ignores.
    public init?(_ message: JSONValue) {
        guard case .object(let fields) = message else { return nil }
        if case .string(let name)? = fields["event"] {
            self = .event(name: name, data: fields["data"] ?? .null)
            return
        }
        guard let id = fields["id"]?.intValue else { return nil }
        if fields["ok"] == .bool(true) {
            self = .response(id: id, result: .success(fields["result"] ?? .object([:])))
        } else {
            let error = (try? fields["error"]?.decode(RemoteError.self))
                ?? RemoteError(code: "internal", message: "Malformed error response.")
            self = .response(id: id, result: .failure(error))
        }
    }
}

/// A server-pushed event, typed where the app cares about it.
public enum ControlEvent: Sendable, Equatable {
    case status(StatusPayload)
    case settings(SettingsPayload)
    /// An event this app does not know, or a known one whose payload did not decode.
    case other(name: String, data: JSONValue)

    public init(name: String, data: JSONValue) {
        switch name {
        case "status":
            if let payload = try? data.decode(StatusPayload.self) {
                self = .status(payload)
                return
            }
        case "settings":
            if let payload = try? data.decode(SettingsPayload.self) {
                self = .settings(payload)
                return
            }
        default:
            break
        }
        self = .other(name: name, data: data)
    }
}
