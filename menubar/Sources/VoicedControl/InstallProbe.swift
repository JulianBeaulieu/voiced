import Foundation

/// Tells "not installed" from "installed but not running" when the socket is unreachable.
/// voiced is installed when a watchdog LaunchDaemon for this user is in place. The label
/// prefix is configurable (`[launchd] label_prefix`), so the check reads each
/// `*.watchdog.plist` instead of matching a name: the job runs `... watchdog` as this user.
/// A file check only; LaunchDaemon plists are world-readable.
public struct InstallProbe: Sendable {
    public var launchDaemonsDirectory: URL
    public var userName: String

    public init(
        launchDaemonsDirectory: URL = URL(fileURLWithPath: "/Library/LaunchDaemons"),
        userName: String = NSUserName()
    ) {
        self.launchDaemonsDirectory = launchDaemonsDirectory
        self.userName = userName
    }

    public func isInstalled() -> Bool {
        let fileManager = FileManager.default
        let names = (try? fileManager.contentsOfDirectory(atPath: launchDaemonsDirectory.path)) ?? []
        return names.filter { $0.hasSuffix(".watchdog.plist") }.contains { name in
            let url = launchDaemonsDirectory.appendingPathComponent(name)
            guard let data = try? Data(contentsOf: url),
                let plist = try? PropertyListSerialization.propertyList(from: data, format: nil)
                    as? [String: Any],
                let arguments = plist["ProgramArguments"] as? [String]
            else { return false }
            return arguments.last == "watchdog" && plist["UserName"] as? String == userName
        }
    }
}
