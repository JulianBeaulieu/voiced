import Darwin
import Foundation
import SystemConfiguration
import VoicedControl

public struct InstallerConfiguration: Sendable {
    public var resources: InstallResources
    public var home: URL
    public var userName: String
    public var installProbe: InstallProbe
    /// How long Starting waits for the watchdog's hello; model loading is slow on a cold disk.
    public var startupTimeout: Duration = .seconds(120)
    /// No new bytes for this long while a download is unfinished is a stall.
    public var stallTimeout: Duration = .seconds(60)
    public var pollInterval: Duration = .milliseconds(500)
    /// How long the old LaunchDaemons may take to go after the prompt removed them.
    public var unloadTimeout: Duration = .seconds(30)
    /// The development override (`DevelopmentOverride`): a wheel or checkout to install
    /// instead of the bundled wheel. Nil in release builds.
    public var installSource: URL?
    /// The suite of the installer's own user default (whether the setup opens at launch);
    /// nil for the app's.
    public var defaultsSuite: String?
    /// The name and address Home Assistant reaches this Mac by.
    public var hostIdentity: @Sendable () -> HostIdentity = HostIdentity.current
    /// Free space on the volume holding a path.
    public var availableCapacity: @Sendable (URL) -> Int64? = { url in
        let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }

    public init(
        resources: InstallResources,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        userName: String = NSUserName(),
        installProbe: InstallProbe = InstallProbe()
    ) {
        self.resources = resources
        self.home = home
        self.userName = userName
        self.installProbe = installProbe
    }

    public var paths: VoicedPaths { VoicedPaths(home: home) }

    var defaults: UserDefaults { defaultsSuite.flatMap(UserDefaults.init(suiteName:)) ?? .standard }
}

/// This Mac as Home Assistant on the network sees it.
public struct HostIdentity: Sendable, Equatable {
    public var host: String
    public var address: String?

    public init(host: String, address: String?) {
        self.host = host
        self.address = address
    }

    /// The Bonjour name (`<name>.local`) and the first IPv4 address of a running `en`
    /// interface.
    public static func current() -> HostIdentity {
        let name = SCDynamicStoreCopyLocalHostName(nil).map { "\($0 as String).local" } ?? ProcessInfo.processInfo.hostName
        var address: String?
        var list: UnsafeMutablePointer<ifaddrs>?
        if getifaddrs(&list) == 0, let first = list {
            defer { freeifaddrs(list) }
            for entry in sequence(first: first, next: { $0.pointee.ifa_next }) {
                let flags = Int32(entry.pointee.ifa_flags)
                guard let socket = entry.pointee.ifa_addr, socket.pointee.sa_family == UInt8(AF_INET),
                    String(cString: entry.pointee.ifa_name).hasPrefix("en"),
                    flags & (IFF_UP | IFF_RUNNING) == (IFF_UP | IFF_RUNNING), flags & IFF_LOOPBACK == 0
                else { continue }
                var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                var inet = socket.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                if inet_ntop(AF_INET, &inet, &buffer, socklen_t(buffer.count)) != nil {
                    address = String(decoding: buffer.prefix { $0 != 0 }.map(UInt8.init(bitPattern:)), as: UTF8.self)
                    break
                }
            }
        }
        return HostIdentity(host: name, address: address)
    }
}

