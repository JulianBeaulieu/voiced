import Foundation

/// What the continuation needs of the installer: `Installer` in the app, a fake in the tests.
@MainActor
public protocol VoicedUpdateStarter: AnyObject {
    var isWorking: Bool { get }
    /// "Update voiced".
    func update()
}

extension Installer: VoicedUpdateStarter {}

/// After the app relaunched into its own update, "Update voiced" for the installed voiced,
/// without asking again (ADR 0034, "After the relaunch"). The app runs it whenever the
/// comparison, the installer or the model rows change, and opens the installer window when
/// it starts the update.
@MainActor
public struct VoicedUpdateContinuation {
    let updates: AppUpdates
    let installer: (any VoicedUpdateStarter)?
    /// Nothing else stands in the way: the app is not translocated, and no model downloads
    /// or is being deleted.
    let canStart: () -> Bool

    public init(updates: AppUpdates, installer: (any VoicedUpdateStarter)?, canStart: @escaping () -> Bool) {
        self.updates = updates
        self.installer = installer
        self.canStart = canStart
    }

    /// Starts "Update voiced" when it should; true when it did.
    public func run(_ match: InstallMatch?) -> Bool {
        guard let installer else { return false }
        let free = !installer.isWorking && canStart()
        guard updates.continuesWithVoicedUpdate(match, canStart: free) else { return false }
        installer.update()
        return true
    }
}

extension AppUpdates {
    /// Why Install and Relaunch waits: quitting would stop the installer or a model download.
    public static func blocker(installing: Bool, modelsBusy: Bool) -> String? {
        if installing { return "Install it once voiced has finished installing." }
        if modelsBusy { return "Install it once the model download has finished." }
        return nil
    }
}
