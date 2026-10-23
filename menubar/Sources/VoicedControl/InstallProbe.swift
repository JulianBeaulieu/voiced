import Foundation

/// Tells "not installed" from "installed but not running" when the socket is unreachable.
/// voiced is installed when its watchdog LaunchAgent is in `~/Library/LaunchAgents` (ADR
/// 0035). The label prefix is configurable (`[launchd] label_prefix`), so the check reads each
/// `*.watchdog.plist` instead of matching a name: the job runs `<path>/voiced watchdog`,
/// exactly as `voiced install` writes it. An install from before ADR 0035 is a watchdog
/// LaunchDaemon in `/Library/LaunchDaemons` that runs as this user; it counts as installed
/// until the installer moves it to an agent. File checks only; both directories are readable
/// as the user.
public struct InstallProbe: Sendable {
    public var launchAgentsDirectory: URL
    public var launchDaemonsDirectory: URL
    public var userName: String

    public init(
        launchAgentsDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true),
        launchDaemonsDirectory: URL = URL(fileURLWithPath: "/Library/LaunchDaemons"),
        userName: String = NSUserName()
    ) {
        self.launchAgentsDirectory = launchAgentsDirectory
        self.launchDaemonsDirectory = launchDaemonsDirectory
        self.userName = userName
    }

    public func isInstalled() -> Bool {
        !Self.watchdogs(in: launchAgentsDirectory).isEmpty || hasLegacyDaemons()
    }

    /// This user's LaunchDaemons from before ADR 0035 are still installed: they have to go,
    /// behind one administrator prompt, before the agents can run.
    public func hasLegacyDaemons() -> Bool {
        legacyDaemonUsers().contains(userName)
    }

    /// The accounts a voiced watchdog LaunchDaemon from before ADR 0035 runs as, this user's
    /// or not.
    public func legacyDaemonUsers() -> [String] {
        Self.watchdogs(in: launchDaemonsDirectory).compactMap { $0["UserName"] as? String }
    }

    /// The plists in `directory` of a job that runs `<path>/voiced watchdog`.
    private static func watchdogs(in directory: URL) -> [[String: Any]] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        return names.filter { $0.hasSuffix(".watchdog.plist") }.sorted().compactMap { name in
            guard let data = try? Data(contentsOf: directory.appendingPathComponent(name)),
                let plist = try? PropertyListSerialization.propertyList(from: data, format: nil)
                    as? [String: Any],
                let arguments = plist["ProgramArguments"] as? [String],
                arguments.count == 2,
                URL(fileURLWithPath: arguments[0]).lastPathComponent == "voiced",
                arguments[1] == "watchdog"
            else { return nil }
            return plist
        }
    }
}
