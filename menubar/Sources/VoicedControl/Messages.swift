import Foundation

/// The first message the server sends on every connection.
public struct Hello: Codable, Sendable, Equatable {
    public var `protocol`: Int
    public var version: String

    public init(protocol: Int, version: String) {
        self.protocol = `protocol`
        self.version = version
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

/// Everything that can go wrong on the client side.
public enum ControlError: Error, Sendable, Equatable {
    /// The socket could not be opened: voiced is not running or not installed.
    case cannotConnect(String)
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
        case .cannotConnect(let reason):
            "Cannot connect to voiced: \(reason)"
        case .unsupportedProtocol(let server, let supported) where server > supported:
            "This voiced uses control protocol \(server); this menu bar app only knows "
                + "protocol \(supported). Update the menu bar app."
        case .unsupportedProtocol(let server, let supported):
            "This voiced uses control protocol \(server), older than protocol \(supported) "
                + "which this app needs. Update voiced."
        case .protocolViolation(let reason):
            "voiced sent an unexpected message: \(reason)"
        case .timedOut(let operation):
            "voiced did not answer \(operation) in time."
        case .notConnected:
            "Not connected to voiced."
        case .connectionClosed:
            "The connection to voiced closed."
        case .remote(let error):
            error.message.isEmpty ? error.code : error.message
        }
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
