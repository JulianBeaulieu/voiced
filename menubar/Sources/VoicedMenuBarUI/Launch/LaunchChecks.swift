import Foundation
import Observation
import ServiceManagement
import VoicedControl

/// What each launch of the app re-checks, without root (ADR 0018, "On every launch" and Flow
/// step 0): whether the app runs translocated, whether the services run the voiced this app
/// carries, and whether the services are allowed in Login Items.
@MainActor
@Observable
public final class LaunchChecks {
    /// Where the app really is when macOS runs it from a translocated copy, which is
    /// read-only and at a random path; nil when it is not translocated.
    public private(set) var translocatedFrom: URL?
    /// The Login Items status of each installed service's plist, by label; empty until first
    /// asked, and for services that are not installed.
    public private(set) var backgroundItems: [String: SMAppService.Status] = [:]

    /// What the app pins, from its bundle; nil in a build without the installer's resources.
    public let manifest: InstallManifest?
    /// The development override in effect (`installSource`); nil outside debug builds.
    public let installSource: URL?
    public let paths: VoicedPaths

    private let model: AppModel
    private let backgroundItemsSource: BackgroundItems
    private let pollInterval: Duration
    /// `install.json` as last read from disk, for when no watchdog answers.
    private var recordOnDisk: InstallRecord?

    public init(
        model: AppModel, manifest: InstallManifest?, installSource: URL? = nil, paths: VoicedPaths = VoicedPaths(),
        backgroundItems: BackgroundItems = BackgroundItems(),
        translocation: (URL) -> URL? = { Translocation.originalURL(of: $0) },
        pollInterval: Duration = .seconds(2)
    ) {
        self.model = model
        self.manifest = manifest
        self.installSource = installSource
        self.paths = paths
        backgroundItemsSource = backgroundItems
        self.pollInterval = pollInterval
        translocatedFrom = translocation(paths.bundle)
        recordOnDisk = InstallRecord.read(stateDirectory: paths.stateDirectory)
    }

    /// The services are switched off in System Settings > Login Items, so launchd will not
    /// start them at login. That macOS reports a switched-off agent as `requiresApproval` is
    /// still to be confirmed on a real install (ADR 0018, "Open items"); the Debug pane shows
    /// the raw statuses meanwhile.
    public var backgroundItemsOff: Bool { backgroundItems.values.contains(.requiresApproval) }

    /// The installer is offered only from a copy of the app that is not translocated.
    public var allowsInstall: Bool { translocatedFrom == nil }

    /// What `install.json` records: the watchdog's word while it answers, else the file.
    public var installRecord: InstallRecord? {
        if case .connected = model.connection { return model.installRecord }
        return recordOnDisk
    }

    /// How the installed voiced compares with the one this app carries. Nil with nothing
    /// installed, without pins, or while the development override installs another wheel.
    public var installMatch: InstallMatch? {
        guard let manifest, installSource == nil else { return nil }
        return InstallMatch.compare(installRecord, with: manifest)
    }

    /// Reads `install.json` again and asks for the Login Items statuses, off the main actor:
    /// `SMAppService` answers over XPC.
    public func refresh() async {
        recordOnDisk = InstallRecord.read(stateDirectory: paths.stateDirectory)
        let source = backgroundItemsSource
        let statuses = await Task.detached { source.statuses() }.value
        if statuses != backgroundItems { backgroundItems = statuses }
    }

    /// Refreshes until cancelled: run by the menu bar window while it is open, so the Login
    /// Items card goes away soon after the user turns the services back on.
    public func watch() async {
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(for: pollInterval)
        }
    }
}

/// The installed voiced against the app's pins: the version and the lock's hash
/// (ADR 0018, "Updates, with rollback"). Any difference, in either direction, is a mismatch.
public enum InstallMatch: Sendable, Equatable {
    case matches
    case differs(installed: String, bundled: String, source: InstallSource, sameVersion: Bool)

    /// Nil when nothing is installed yet: no record, or one with no environment linked.
    public static func compare(_ record: InstallRecord?, with manifest: InstallManifest) -> InstallMatch? {
        guard let record, record.environment != nil else { return nil }
        let sameVersion = record.version == manifest.version
        let sameLock = record.lockSHA256?.lowercased() == manifest.lockSHA256.lowercased()
        if sameVersion && sameLock { return .matches }
        return .differs(
            installed: record.version ?? "unknown", bundled: manifest.version, source: record.source,
            sameVersion: sameVersion)
    }
}

/// What the user runs for an install another tool manages (ADR 0018, "An install that already
/// exists"): the app shows these and never acts on such an install itself.
public enum ExternalInstall {
    public struct Command: Sendable, Equatable {
        public var purpose: String
        public var command: String
        /// What to do with what it prints, or what has to come first.
        public var note: String?
    }

    /// The commands that update, repair and uninstall an install made by `source`, in order;
    /// none for the app's own.
    public static func commands(for source: InstallSource) -> [Command] {
        switch source {
        case .app:
            []
        case .homebrew:
            [
                Command(purpose: "Update", command: "brew upgrade voiced"),
                Command(purpose: "Repair", command: "brew reinstall voiced"),
                Command(purpose: "Uninstall, first", command: "voiced uninstall --yes"),
                Command(
                    purpose: "Uninstall, then", command: "brew uninstall voiced",
                    note: "Only once the services are removed: before that they keep running on a binary that is gone."),
            ]
        case .cli:
            [
                Command(purpose: "Update or repair", command: "voiced install --source <checkout>"),
                Command(purpose: "Uninstall", command: "voiced uninstall --yes"),
            ]
        }
    }
}

/// The services' entries in System Settings > General > Login Items > Allow in the
/// Background. The plists name the app in `AssociatedBundleIdentifiers`, so the entries show
/// as voiced; when the user turns them off, launchd will not run the services.
public struct BackgroundItems: Sendable {
    public static let labels = InstallProbe.defaultLabels

    public var launchAgentsDirectory: URL
    public var status: @Sendable (URL) -> SMAppService.Status

    public init(
        launchAgentsDirectory: URL = InstallProbe.defaultLaunchAgentsDirectory,
        status: @escaping @Sendable (URL) -> SMAppService.Status = { SMAppService.statusForLegacyPlist(at: $0) }
    ) {
        self.launchAgentsDirectory = launchAgentsDirectory
        self.status = status
    }

    /// The status of each installed service, by label. Plists that are not there are not
    /// asked about: an install that is not there is the installer's business. Blocks on XPC.
    public func statuses() -> [String: SMAppService.Status] {
        var found: [String: SMAppService.Status] = [:]
        for label in Self.labels {
            let plist = launchAgentsDirectory.appendingPathComponent("\(label).plist")
            if FileManager.default.fileExists(atPath: plist.path) { found[label] = status(plist) }
        }
        return found
    }
}

extension SMAppService.Status {
    /// The case's name, for the Debug pane.
    public var name: String {
        switch self {
        case .notRegistered: "notRegistered"
        case .enabled: "enabled"
        case .requiresApproval: "requiresApproval"
        case .notFound: "notFound"
        @unknown default: "status \(rawValue)"
        }
    }
}

/// Gatekeeper's app translocation: an app opened from where it was downloaded, without being
/// moved, runs from a random read-only path. The Security functions that tell are not in the
/// SDK's headers, so they are looked up at run time, with the path's shape as the fallback.
public enum Translocation {
    private typealias IsTranslocated = @convention(c) (
        CFURL, UnsafeMutablePointer<Bool>, UnsafeMutablePointer<Unmanaged<CFError>?>?
    ) -> Bool
    private typealias OriginalPath = @convention(c) (
        CFURL, UnsafeMutablePointer<Unmanaged<CFError>?>?
    ) -> Unmanaged<CFURL>?

    /// Where `bundle` really is when it runs translocated, or nil when it does not.
    public static func originalURL(of bundle: URL) -> URL? {
        let security = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY)
        let isSymbol = security.flatMap { dlsym($0, "SecTranslocateIsTranslocatedURL") }
        let originalSymbol = security.flatMap { dlsym($0, "SecTranslocateCreateOriginalPathForURL") }
        let isTranslocated: ((URL) -> Bool)? = isSymbol.map { symbol in
            { url in
                var translocated = false
                let call = unsafeBitCast(symbol, to: IsTranslocated.self)
                return call(url as CFURL, &translocated, nil) && translocated
            }
        }
        let original: ((URL) -> URL?)? = originalSymbol.map { symbol in
            { url in
                let call = unsafeBitCast(symbol, to: OriginalPath.self)
                return call(url as CFURL, nil).map { $0.takeRetainedValue() as URL }
            }
        }
        return originalURL(of: bundle, isTranslocated: isTranslocated, original: original)
    }

    /// The decision, with the two lookups passed in. Without the first, the path's shape
    /// decides; a translocated bundle whose original cannot be found answers with itself, so
    /// the app still asks to be moved.
    static func originalURL(of bundle: URL, isTranslocated: ((URL) -> Bool)?, original: ((URL) -> URL?)?) -> URL? {
        let translocated = isTranslocated?(bundle) ?? bundle.path.contains("/AppTranslocation/")
        guard translocated else { return nil }
        return original?(bundle) ?? bundle
    }
}
