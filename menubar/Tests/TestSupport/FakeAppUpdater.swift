import VoicedMenuBarUI

/// An updater that only counts what it is asked; tests and snapshots play Sparkle's part by
/// calling the `AppUpdates` methods it would call.
@MainActor
public final class FakeAppUpdater: AppUpdater {
    public var automaticallyChecks: Bool
    public private(set) var checks = 0

    public init(automaticallyChecks: Bool = true) {
        self.automaticallyChecks = automaticallyChecks
    }

    public func checkForUpdates() {
        checks += 1
    }
}
