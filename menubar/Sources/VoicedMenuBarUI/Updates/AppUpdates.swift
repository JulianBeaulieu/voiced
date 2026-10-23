import Foundation
import Observation

/// What the app asks of the updater that replaces the app itself (ADR 0034): Sparkle in the
/// bundle, a fake in the tests and the snapshots. What the updater finds comes back through
/// the `AppUpdates` methods under "From the updater".
@MainActor
public protocol AppUpdater: AnyObject {
    /// Checks in the background, at launch and then daily; on unless the user turned it off.
    var automaticallyChecks: Bool { get set }
    /// A check the user asked for, which reports "up to date" too.
    func checkForUpdates()
}

/// A newer app in the feed.
public struct AvailableUpdate: Sendable, Equatable {
    /// The version as people read it, for example "0.5.0".
    public var version: String
    /// The release's page, with its notes.
    public var releasePage: URL?

    public init(version: String, releasePage: URL? = nil) {
        self.version = version
        self.releasePage = releasePage
    }
}

/// Where an update of the app stands. A found update is news, not a problem: the service
/// rows do not change for it, and the icon gets a neutral badge, never the error one.
public enum AppUpdateState: Sendable, Equatable {
    case idle
    /// A check the user asked for is running.
    case checking
    /// Install and Relaunch downloads, installs and relaunches.
    case available(AvailableUpdate)
    /// The fraction downloaded, nil until the size is known.
    case downloading(AvailableUpdate, fraction: Double?)
    /// Unpacking, checking the signature and replacing the app; it then relaunches.
    case installing(AvailableUpdate)
    /// The answer to a check the user asked for.
    case upToDate
    case failed(String)
}

/// What the user chose for a found update, as the updater takes it.
public enum AppUpdateChoice: Sendable, Equatable {
    case install
    /// Not now; the next check offers it again.
    case later
}

/// The app's own updates, for the menu bar window and Settings. The updater calls the methods
/// under "From the updater" as it goes; the views call the ones under "From the user".
@MainActor
@Observable
public final class AppUpdates {
    /// Set while the app relaunches into an update: the version it relaunches from.
    public static let relaunchKey = "appUpdateRelaunchedFrom"

    public private(set) var state: AppUpdateState = .idle
    public var automaticallyChecks: Bool {
        didSet { updater.automaticallyChecks = automaticallyChecks }
    }
    /// Why the update cannot be installed now, or nil when it can. Installing quits the app,
    /// which would stop an install of voiced or a model download (`applicationWillTerminate`),
    /// so the app sets this; it is read where the views read it, so they follow it.
    public var installBlocker: @MainActor () -> String? = { nil }

    /// Whether a check, a download or an install is under way.
    public var isBusy: Bool {
        switch state {
        case .checking, .downloading, .installing: true
        case .idle, .available, .upToDate, .failed: false
        }
    }

    /// A found update waits for the user: the icon's badge.
    public var isAvailable: Bool {
        if case .available = state { return true }
        return false
    }

    /// The quit for the install was cancelled: Relaunch Now asks again.
    public private(set) var canRetryRelaunch = false

    /// This app's version, as "up to date" names it.
    public let appVersion: String
    private let updater: AppUpdater
    private let defaults: UserDefaults
    private var choose: ((AppUpdateChoice) -> Void)?
    private var cancel: (() -> Void)?
    private var retryRelaunch: (() -> Void)?
    private var expectedLength: UInt64?
    private var receivedLength: UInt64 = 0
    /// The app was relaunched by its own update and has not yet seen how the installed voiced
    /// compares with the one it carries.
    private var relaunchedIntoUpdate: Bool

    public init(updater: AppUpdater, appVersion: String = VoicedPaths.appVersion, defaults: UserDefaults = .standard) {
        self.updater = updater
        self.defaults = defaults
        self.appVersion = appVersion
        automaticallyChecks = updater.automaticallyChecks
        // Read once and cleared: a later launch is an ordinary one. The same version means the
        // install did not happen.
        let from = defaults.string(forKey: Self.relaunchKey)
        defaults.removeObject(forKey: Self.relaunchKey)
        relaunchedIntoUpdate = from != nil && from != appVersion
    }

    /// Whether to update the installed voiced without asking, after the user installed this
    /// app's update: they already asked for the new version, so "Update voiced" is not asked
    /// again. True once, and only for an install the app manages that runs another voiced
    /// than the app carries. Nothing is decided while `canStart` is false (the installer or a
    /// model download is busy, the app is translocated) or the comparison is unknown; the
    /// first comparison known while it can start decides.
    public func continuesWithVoicedUpdate(_ match: InstallMatch?, canStart: Bool) -> Bool {
        guard relaunchedIntoUpdate, canStart, let match else { return false }
        relaunchedIntoUpdate = false
        guard case .differs(_, _, .app, _) = match else { return false }
        return true
    }

    // MARK: - From the user

    public func check() {
        guard !isBusy else { return }
        updater.checkForUpdates()
    }

    public func install() {
        guard case .available(let update) = state, installBlocker() == nil else { return }
        answer(.install)
        state = .downloading(update, fraction: nil)
    }

    public func later() {
        guard case .available = state else { return }
        answer(.later)
        state = .idle
    }

    /// Stops a download; the update stays offered at the next check.
    public func cancelDownload() {
        guard case .downloading = state else { return }
        cancel?()
        cancel = nil
        state = .idle
    }

    /// Asks the app to quit again for the install, after an earlier quit was cancelled.
    public func relaunchNow() {
        guard canRetryRelaunch, let retryRelaunch else { return }
        canRetryRelaunch = false
        retryRelaunch()
    }

    /// Clears "up to date" and an error.
    public func dismiss() {
        switch state {
        case .upToDate, .failed: state = .idle
        default: break
        }
    }

    // MARK: - From the updater

    public func checkStarted() {
        state = .checking
    }

    public func found(_ update: AvailableUpdate, choose: @escaping (AppUpdateChoice) -> Void) {
        self.choose = choose
        cancel = nil
        state = .available(update)
    }

    public func notFound() {
        state = .upToDate
    }

    public func failed(_ message: String) {
        choose = nil
        cancel = nil
        retryRelaunch = nil
        canRetryRelaunch = false
        state = .failed(message)
    }

    public func downloadStarted(cancel: @escaping () -> Void) {
        self.cancel = cancel
        expectedLength = nil
        receivedLength = 0
        if let update { state = .downloading(update, fraction: nil) }
    }

    public func downloadExpects(_ length: UInt64) {
        expectedLength = length > 0 ? length : nil
        receivedLength = 0
        updateFraction()
    }

    public func downloadReceived(_ length: UInt64) {
        receivedLength += length
        updateFraction()
    }

    /// The download is in; what follows is the install.
    public func extracting() {
        cancel = nil
        if let update { state = .installing(update) }
    }

    /// The app is about to quit so the update can replace it; the next launch continues with
    /// the installed voiced (`continuesWithVoicedUpdate`). `retry` asks the app to quit again,
    /// for when this quit is cancelled (`quitCancelled`).
    public func installing(retry: @escaping () -> Void) {
        defaults.set(appVersion, forKey: Self.relaunchKey)
        retryRelaunch = retry
        if let update { state = .installing(update) }
    }

    /// The app refused to quit (the user kept a model download running): while the update
    /// waits, Relaunch Now asks again. Called by the app delegate for every cancelled quit.
    public func quitCancelled() {
        guard retryRelaunch != nil, case .installing = state else { return }
        canRetryRelaunch = true
    }

    /// The updater ended its session: whatever was in progress is over, and only an answer the
    /// user has not read stays.
    public func finished() {
        choose = nil
        cancel = nil
        retryRelaunch = nil
        canRetryRelaunch = false
        switch state {
        case .upToDate, .failed: break
        default: state = .idle
        }
    }

    private var update: AvailableUpdate? {
        switch state {
        case .available(let update), .downloading(let update, _), .installing(let update): update
        case .idle, .checking, .upToDate, .failed: nil
        }
    }

    private func answer(_ choice: AppUpdateChoice) {
        let choose = self.choose
        self.choose = nil
        choose?(choice)
    }

    private func updateFraction() {
        guard case .downloading(let update, _) = state else { return }
        let fraction = expectedLength.map { min(1, Double(receivedLength) / Double($0)) }
        state = .downloading(update, fraction: fraction)
    }
}
