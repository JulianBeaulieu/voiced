import Foundation

/// Tells "not installed" from "installed but not running" when the socket is unreachable.
/// voiced is installed when its watchdog LaunchAgent is in `~/Library/LaunchAgents` (ADR
/// 0035). The label prefix is configurable (`[launchd] label_prefix`), so the check reads each
/// `*.watchdog.plist` instead of matching a name: the job runs `<path>/voiced watchdog`,
/// exactly as `voiced install` writes it. An install from 0.4.0 or earlier is LaunchDaemons in
/// `/Library/LaunchDaemons` under the default labels, the only ones the old root step
/// installed (`voiced.launchd.control.LEGACY_LABELS`); this user's count as installed until the
/// installer moves them to agents. File checks only; both directories are readable as the
/// user. A daemon still loaded after its plist was deleted needs `launchctl print`, which the
/// installer adds (`InstallStages.hasLegacyDaemons`).
public struct InstallProbe: Sendable {
    /// The default labels, which the old root step installed and the app knows.
    public static let defaultLabels = ["stt", "tts", "watchdog"].map { "io.github.julianbeaulieu.voiced.\($0)" }
    /// `~/Library/LaunchAgents`, where `voiced install` writes the agents.
    public static var defaultLaunchAgentsDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents", isDirectory: true)
    }

    public var launchAgentsDirectory: URL
    public var launchDaemonsDirectory: URL
    public var userName: String

    public init(
        launchAgentsDirectory: URL = InstallProbe.defaultLaunchAgentsDirectory,
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

    /// This user's LaunchDaemons from 0.4.0 or earlier are still installed: they have to go,
    /// behind one administrator prompt, before the agents can run.
    public func hasLegacyDaemons() -> Bool {
        legacyDaemons().contains { $0.user == nil || $0.user == userName }
    }

    /// The accounts the LaunchDaemons from 0.4.0 or earlier run as, this user's or not.
    public func legacyDaemonUsers() -> [String] {
        Array(Set(legacyDaemons().compactMap(\.user))).sorted()
    }

    /// The default labels' plists in `/Library/LaunchDaemons`, with their `UserName`.
    private func legacyDaemons() -> [(label: String, user: String?)] {
        Self.defaultLabels.compactMap { label in
            let url = launchDaemonsDirectory.appendingPathComponent("\(label).plist")
            guard FileManager.default.fileExists(atPath: url.path) else { return nil }
            let plist = (try? Data(contentsOf: url)).flatMap {
                try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: Any]
            }
            return (label, plist?["UserName"] as? String)
        }
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
