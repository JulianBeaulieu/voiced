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
    /// The services are switched off in System Settings > Login Items, so launchd will not
    /// start them at boot.
    public private(set) var backgroundItemsOff = false

    /// What the app pins, from its bundle; nil in a build without the installer's resources.
    public let manifest: InstallManifest?
    /// The development override in effect (`installSource`), nil in release builds.
    public let installSource: URL?
    public let bundleURL: URL
    /// The home whose `~/.voiced` is checked.
    public let home: URL

    private let model: AppModel
    private let stateDirectory: URL
    private let backgroundItems: BackgroundItems
    private let pollInterval: Duration
    /// `install.json` as last read from disk, for when no watchdog answers.
    private var recordOnDisk: InstallRecord?
    private var polling: Task<Void, Never>?

    public init(
        model: AppModel, manifest: InstallManifest?, installSource: URL? = nil,
        bundleURL: URL = Bundle.main.bundleURL,
        stateDirectory: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".voiced"),
        backgroundItems: BackgroundItems = BackgroundItems(),
        translocation: (URL) -> URL? = Translocation.originalURL,
        pollInterval: Duration = .seconds(2)
    ) {
        self.model = model
        self.manifest = manifest
        self.installSource = installSource
        self.bundleURL = bundleURL
        home = stateDirectory.deletingLastPathComponent()
        self.stateDirectory = stateDirectory
        self.backgroundItems = backgroundItems
        self.pollInterval = pollInterval
        translocatedFrom = translocation(bundleURL)
        refresh()
    }

    /// What `install.json` records: the watchdog's word while it answers, else the file.
    public var installRecord: InstallRecord? {
        if case .connected = model.connection { return model.installRecord }
        return recordOnDisk
    }

    /// How the installed voiced compares with the one this app carries.
    public var installMatch: InstallMatch? {
        guard let manifest else { return nil }
        return InstallMatch.compare(installRecord, with: manifest)
    }

    /// Reads the file and asks for the background items' state again. Called at launch and
    /// whenever a window opens; while the services are off it keeps asking until they are on.
    public func refresh() {
        recordOnDisk = InstallRecord.read(stateDirectory: stateDirectory)
        backgroundItemsOff = backgroundItems.areOff()
        if backgroundItemsOff, polling == nil {
            polling = Task { [weak self, pollInterval] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: pollInterval)
                    guard let self else { return }
                    backgroundItemsOff = backgroundItems.areOff()
                    if !backgroundItemsOff { break }
                }
                self?.polling = nil
            }
        }
    }

    public func stop() {
        polling?.cancel()
        polling = nil
    }

    public func openLoginItems() {
        SMAppService.openSystemSettingsLoginItems()
    }
}

/// The installed voiced against the app's pins: the version and the lock's hash
/// (ADR 0018, "Updates, with rollback"). Any difference, in either direction, is a mismatch.
public enum InstallMatch: Sendable, Equatable {
    case matches
    case differs(installed: String, bundled: String, source: String, sameVersion: Bool)

    /// Nil when nothing is installed yet: no record, or one with no environment linked.
    public static func compare(_ record: InstallRecord?, with manifest: InstallManifest) -> InstallMatch? {
        guard let record, record.environment != nil else { return nil }
        let installed = record.version ?? "unknown"
        let bundled = manifest.wheel.version ?? "unknown"
        let sameVersion = record.version != nil && installed == bundled
        let sameLock = record.lockSHA256?.lowercased() == manifest.lockSHA256.lowercased()
        if sameVersion && sameLock { return .matches }
        return .differs(installed: installed, bundled: bundled, source: record.source, sameVersion: sameVersion)
    }

    /// What the menu shows for a mismatch, and a command that resolves it when the app does
    /// not manage the install.
    public var notice: (text: String, command: String?)? {
        guard case .differs(let installed, let bundled, let source, let sameVersion) = self else { return nil }
        let running = sameVersion
            ? "The services run voiced \(installed) with other dependencies than this app carries."
            : "The services run voiced \(installed); this app carries \(bundled)."
        switch source {
        case "app":
            return (running, nil)
        case "homebrew":
            return ("\(running) Homebrew manages this install; update it with:", "brew upgrade voiced")
        default:
            return ("\(running) This install was made from the command line, which also updates it.", nil)
        }
    }
}

extension InstallManifest.Wheel {
    /// "0.1.0" from "voiced-0.1.0-py3-none-any.whl".
    public var version: String? {
        let parts = name.split(separator: "-")
        return parts.count > 2 ? String(parts[1]) : nil
    }
}

/// The services' entries in System Settings > General > Login Items > Allow in the
/// Background. The plists name the app in `AssociatedBundleIdentifiers`, so the entries show
/// as voiced; when the user turns them off, launchd will not run the services.
public struct BackgroundItems: Sendable {
    public static let labels = ["stt", "tts", "watchdog"].map { "io.github.julianbeaulieu.voiced.\($0)" }

    public var launchDaemonsDirectory: URL
    public var status: @Sendable (URL) -> SMAppService.Status

    public init(
        launchDaemonsDirectory: URL = URL(fileURLWithPath: "/Library/LaunchDaemons"),
        status: @escaping @Sendable (URL) -> SMAppService.Status = { SMAppService.statusForLegacyPlist(at: $0) }
    ) {
        self.launchDaemonsDirectory = launchDaemonsDirectory
        self.status = status
    }

    /// True when any installed service waits for approval. Plists that are not there are not
    /// asked about: an install that is not there is the installer's business.
    public func areOff() -> Bool {
        Self.labels.contains { label in
            let plist = launchDaemonsDirectory.appendingPathComponent("\(label).plist")
            return FileManager.default.fileExists(atPath: plist.path) && status(plist) == .requiresApproval
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

    /// Where `bundle` really is when it runs translocated, or nil when it does not. A
    /// translocated bundle whose original cannot be found answers with itself.
    public static func originalURL(of bundle: URL) -> URL? {
        guard let security = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY),
            let isSymbol = dlsym(security, "SecTranslocateIsTranslocatedURL")
        else { return bundle.path.contains("/AppTranslocation/") ? bundle : nil }
        var translocated = false
        let isTranslocated = unsafeBitCast(isSymbol, to: IsTranslocated.self)
        guard isTranslocated(bundle as CFURL, &translocated, nil), translocated else { return nil }
        guard let originalSymbol = dlsym(security, "SecTranslocateCreateOriginalPathForURL"),
            let original = unsafeBitCast(originalSymbol, to: OriginalPath.self)(bundle as CFURL, nil)
        else { return bundle }
        return original.takeRetainedValue() as URL
    }
}
