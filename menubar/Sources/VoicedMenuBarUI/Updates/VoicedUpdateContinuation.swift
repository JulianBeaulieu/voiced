import Foundation

/// After the app relaunched into its own update, "Update voiced" for the installed voiced,
/// without asking again (ADR 0034, "After the relaunch"): the user already asked for the new
/// version. The app runs it whenever the comparison, the installer or the model rows change,
/// and opens the installer window when it starts the update.
@MainActor
public final class VoicedUpdateContinuation {
    /// Set by `AppUpdates` as the app quits into an update: the version it relaunches from.
    public static let relaunchKey = "appUpdateRelaunchedFrom"

    private let canStart: () -> Bool
    private let startUpdate: () -> Void
    /// The app was relaunched by its own update and has not yet decided.
    private var pending: Bool

    /// `canStart`: nothing stands in the way (an installer that can update, idle; no model
    /// download or delete; the app not translocated). Reads and clears the key: a later
    /// launch is an ordinary one, and the same version means the install did not happen.
    public init(
        appVersion: String = VoicedPaths.appVersion, defaults: UserDefaults = .standard,
        canStart: @escaping () -> Bool, startUpdate: @escaping () -> Void
    ) {
        self.canStart = canStart
        self.startUpdate = startUpdate
        let from = defaults.string(forKey: Self.relaunchKey)
        defaults.removeObject(forKey: Self.relaunchKey)
        pending = from != nil && from != appVersion
    }

    /// Starts "Update voiced" when it should; true when it did. The first comparison known
    /// while it can start decides, once: only an install the app manages that runs another
    /// voiced than the app carries is updated.
    public func run(_ match: InstallMatch?) -> Bool {
        guard pending, let match, canStart() else { return false }
        pending = false
        guard case .differs(_, _, .app, _) = match else { return false }
        startUpdate()
        return true
    }
}
