import Foundation

/// Where the control socket is. The app does not read the config file, so it uses the
/// default state directory of the install layout (ADR 0006) unless told otherwise:
/// the `VOICED_CONTROL_SOCKET` environment variable, then the `controlSocketPath` user
/// default, then `~/.voiced/control.sock`.
public enum SocketLocation {
    public static let environmentKey = "VOICED_CONTROL_SOCKET"
    public static let defaultsKey = "controlSocketPath"
    /// `sun_path` holds 104 bytes including the terminating zero.
    public static let maximumPathLength = 103

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
