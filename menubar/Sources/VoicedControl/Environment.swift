import Foundation

/// Where the control socket is. Resolution order: the `VOICED_CONTROL_SOCKET` environment
/// variable, the `controlSocketPath` user default, then `~/.voiced/control.sock`, the default
/// state directory of the install layout (ADR 0006).
public enum SocketLocation {
    public static let environmentKey = "VOICED_CONTROL_SOCKET"
    public static let defaultsKey = "controlSocketPath"

    public static func resolve(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> String {
        if let path = environment[environmentKey], !path.isEmpty {
            return expand(path, home: home)
        }
        if let path = defaults.string(forKey: defaultsKey), !path.isEmpty {
            return expand(path, home: home)
        }
        return home.appendingPathComponent(".voiced/control.sock").path
    }

    static func expand(_ path: String, home: URL) -> String {
        if path == "~" { return home.path }
        if path.hasPrefix("~/") { return home.appendingPathComponent(String(path.dropFirst(2))).path }
        return path
    }
}

/// Tells "not installed" from "installed but not running" when the socket is unreachable.
/// voiced is installed when its watchdog LaunchDaemon plist is in place; this is a file
/// check only and needs no privileges.
public struct InstallProbe: Sendable {
    public var launchDaemonsDirectory: URL

    public init(launchDaemonsDirectory: URL = URL(fileURLWithPath: "/Library/LaunchDaemons")) {
        self.launchDaemonsDirectory = launchDaemonsDirectory
    }

    public func isInstalled() -> Bool {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: launchDaemonsDirectory.path)) ?? []
        // Labels are `<prefix>.watchdog`; the default prefix ends in `.voiced` and a custom
        // one is expected to name voiced as well.
        return names.contains { $0.hasSuffix(".watchdog.plist") && $0.lowercased().contains("voiced") }
    }
}

/// Reconnect delays: doubling from `initial` up to `maximum`, reset after a good connection.
public struct Backoff: Sendable, Equatable {
    public let initial: Duration
    public let maximum: Duration
    private var current: Duration?

    public init(initial: Duration = .milliseconds(500), maximum: Duration = .seconds(15)) {
        self.initial = initial
        self.maximum = maximum
    }

    public mutating func next() -> Duration {
        let delay = current.map { min($0 * 2, maximum) } ?? initial
        current = delay
        return delay
    }

    public mutating func reset() { current = nil }
}
