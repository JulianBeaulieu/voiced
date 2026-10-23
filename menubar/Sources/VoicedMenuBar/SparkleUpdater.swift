import Foundation
import Sparkle
import VoicedMenuBarUI
import os

/// Sparkle behind `AppUpdater` (ADR 0034). The feed, the public key, the daily interval and
/// the checks being on by default are in the bundle's Info.plist (bundle.sh); this only
/// starts the updater and passes what Sparkle shows to `AppUpdates`, which the menu bar
/// window and Settings show in their own words. Sparkle's own windows never open.
@MainActor
final class SparkleUpdater: AppUpdater {
    private let updater: SPUUpdater
    /// Held for as long as the updater, whatever Sparkle keeps.
    private let driver: UserDriver

    var automaticallyChecks: Bool {
        get { updater.automaticallyChecksForUpdates }
        set { updater.automaticallyChecksForUpdates = newValue }
    }

    private init(updater: SPUUpdater, driver: UserDriver) {
        self.updater = updater
        self.driver = driver
    }

    func checkForUpdates() {
        updater.checkForUpdates()
    }

    /// The app's updates, checked once now when the checks are on (Sparkle's own schedule
    /// would wait a day from the last check, and skips the first launch); nil for a build
    /// without a feed, such as `swift run`, or when Sparkle refuses its configuration.
    static func start(bundle: Bundle = .main) -> AppUpdates? {
        guard bundle.object(forInfoDictionaryKey: "SUFeedURL") != nil else { return nil }
        let driver = UserDriver()
        let updater = SPUUpdater(hostBundle: bundle, applicationBundle: bundle, userDriver: driver, delegate: nil)
        do {
            try updater.start()
        } catch {
            Logger(subsystem: bundle.bundleIdentifier ?? "voiced-menubar", category: "updates")
                .error("The updater did not start: \(error.localizedDescription, privacy: .public)")
            return nil
        }
        let updates = AppUpdates(updater: SparkleUpdater(updater: updater, driver: driver))
        driver.updates = updates
        if updater.automaticallyChecksForUpdates { updater.checkForUpdatesInBackground() }
        return updates
    }
}

/// Sparkle's user interface, handed to `AppUpdates`. Every reply Sparkle waits for is
/// answered: by the user's choice, at once for what needs no choice.
@MainActor
private final class UserDriver: NSObject, SPUUserDriver {
    weak var updates: AppUpdates?

    /// Not asked: Info.plist turns the checks on (`SUEnableAutomaticChecks`) and the switch in
    /// Settings turns them off.
    func show(_ request: SPUUpdatePermissionRequest, reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: true, sendSystemProfile: false))
    }

    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {
        updates?.checkStarted()
    }

    func showUpdateFound(
        with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void
    ) {
        // The feed never announces a release without an archive (release.py writes none).
        guard let updates, !appcastItem.isInformationOnlyUpdate else { return reply(.dismiss) }
        let update = AvailableUpdate(version: appcastItem.displayVersionString, releasePage: appcastItem.infoURL)
        updates.found(update) { reply($0 == .install ? .install : .dismiss) }
    }

    /// The card links to the release page instead.
    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}

    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: any Error) {}

    func showUpdateNotFoundWithError(_ error: any Error, acknowledgement: @escaping () -> Void) {
        updates?.notFound()
        acknowledgement()
    }

    func showUpdaterError(_ error: any Error, acknowledgement: @escaping () -> Void) {
        updates?.failed(error.localizedDescription)
        acknowledgement()
    }

    func showDownloadInitiated(cancellation: @escaping () -> Void) {
        updates?.downloadStarted(cancel: cancellation)
    }

    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {
        updates?.downloadExpects(expectedContentLength)
    }

    func showDownloadDidReceiveData(ofLength length: UInt64) {
        updates?.downloadReceived(length)
    }

    func showDownloadDidStartExtractingUpdate() {
        updates?.extracting()
    }

    func showExtractionReceivedProgress(_ progress: Double) {}

    /// The user chose Install and Relaunch, the only way to a download here, so it goes ahead.
    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        reply(.install)
    }

    func showInstallingUpdate(
        withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void
    ) {
        updates?.installing(retry: retryTerminatingApplication)
    }

    func showUpdateInstalledAndRelaunched(_ relaunched: Bool, acknowledgement: @escaping () -> Void) {
        acknowledgement()
    }

    func dismissUpdateInstallation() {
        updates?.finished()
    }
}
