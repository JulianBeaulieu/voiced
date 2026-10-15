import Foundation

/// What the app agrees to open for each `open` target. The server only says where things
/// are; the app checks that the answer is the kind of thing it asked for before handing it
/// to the browser or Finder, so a wrong answer cannot launch an app or a script.
public enum OpenAction: Sendable, Equatable {
    /// A web page served by this Mac, for the browser.
    case webPage(URL)
    /// A folder, for Finder.
    case folder(URL)
    /// A regular file, opened as text.
    case textFile(URL)
}

public enum OpenPolicy {
    /// `localHosts` are this Mac's own addresses: with `bind` set to one interface address the
    /// server reports the status page there rather than on loopback.
    public static func action(
        for target: OpenTarget, location: String, fileManager: FileManager = .default,
        localHosts: @autoclosure () -> Set<String> = interfaceAddresses()
    ) -> OpenAction? {
        switch target {
        case .statusPage:
            guard let url = URL(string: location),
                let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
                let host = url.host(percentEncoded: false),
                isLoopback(host) || localHosts().contains(bare(host))
            else { return nil }
            return .webPage(url)
        case .logsDirectory, .configFile:
            let path = location.hasPrefix("file://") ? URL(string: location)?.path ?? "" : location
            guard path.hasPrefix("/") else { return nil }
            let url = URL(fileURLWithPath: path).resolvingSymlinksInPath()
            var isDirectory: ObjCBool = false
            guard fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory) else { return nil }
            if target == .logsDirectory {
                // A bundle is a directory too; Finder would launch it.
                guard isDirectory.boolValue, url.pathExtension.isEmpty else { return nil }
                return .folder(url)
            }
            guard !isDirectory.boolValue,
                (try? url.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true
            else { return nil }
            return .textFile(url)
        }
    }

    static func bare(_ host: String) -> String {
        host.trimmingCharacters(in: CharacterSet(charactersIn: "[]")).lowercased()
    }

    /// `localhost`, `::1` or an IPv4 address in 127.0.0.0/8. A name that merely starts with
    /// "127." (127.example.com) is not one.
    static func isLoopback(_ host: String) -> Bool {
        let host = bare(host)
        if host == "localhost" { return true }
        var v6 = in6_addr()
        if inet_pton(AF_INET6, host, &v6) == 1 {
            return withUnsafeBytes(of: &v6) { Array($0) } == Array(repeating: 0, count: 15) + [1]
        }
        var v4 = in_addr()
        guard inet_pton(AF_INET, host, &v4) == 1 else { return false }
        return UInt32(bigEndian: v4.s_addr) >> 24 == 127
    }

    /// The IPv4 and IPv6 addresses of this Mac's interfaces. Reads the interface list; opens
    /// no connection.
    public static func interfaceAddresses() -> Set<String> {
        var addresses = Set<String>()
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return addresses }
        defer { freeifaddrs(list) }
        for entry in sequence(first: first, next: { $0.pointee.ifa_next }) {
            guard let address = entry.pointee.ifa_addr else { continue }
            let family = Int32(address.pointee.sa_family)
            guard family == AF_INET || family == AF_INET6 else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let length = socklen_t(family == AF_INET ? MemoryLayout<sockaddr_in>.size : MemoryLayout<sockaddr_in6>.size)
            if getnameinfo(address, length, &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                // Drop an IPv6 zone such as "%en0".
                let text = String(decoding: host.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
                addresses.insert(String(text.split(separator: "%").first ?? Substring(text)).lowercased())
            }
        }
        return addresses
    }
}
