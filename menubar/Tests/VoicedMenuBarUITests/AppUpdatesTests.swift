import Foundation
import SwiftUI
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

/// The app's own updates (ADR 0034), with the fake updater standing in for Sparkle: the
/// tests call what Sparkle's user driver calls, in the order Sparkle calls it.
@MainActor
final class AppUpdatesTests: XCTestCase {
    private let update = AvailableUpdate(
        version: "0.5.0", releasePage: URL(string: "https://github.com/JulianBeaulieu/voiced/releases/tag/v0.5.0"))

    private func scratchDefaults() -> UserDefaults {
        let name = "voiced-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        addTeardownBlock { UserDefaults().removePersistentDomain(forName: name) }
        return defaults
    }

    private func updates(
        _ updater: FakeAppUpdater = FakeAppUpdater(), version: String = "0.4.0", defaults: UserDefaults? = nil
    ) -> AppUpdates {
        AppUpdates(updater: updater, appVersion: version, defaults: defaults ?? scratchDefaults())
    }

    func testTheSwitchIsTheUpdatersSetting() {
        let updater = FakeAppUpdater(automaticallyChecks: false)
        let updates = updates(updater)
        XCTAssertFalse(updates.automaticallyChecks)
        updates.automaticallyChecks = true
        XCTAssertTrue(updater.automaticallyChecks)
    }

    func testOneClickDownloadsInstallsAndRelaunches() {
        let defaults = scratchDefaults()
        let updates = updates(defaults: defaults)
        var choices: [AppUpdateChoice] = []
        updates.found(update) { choices.append($0) }
        XCTAssertEqual(updates.state, .available(update))

        updates.install()
        XCTAssertEqual(choices, [.install])
        XCTAssertEqual(updates.state, .downloading(update, fraction: nil))
        updates.downloadStarted {}
        updates.downloadExpects(1000)
        updates.downloadReceived(250)
        XCTAssertEqual(updates.state, .downloading(update, fraction: 0.25))
        updates.downloadReceived(750)
        XCTAssertEqual(updates.state, .downloading(update, fraction: 1))
        updates.extracting()
        XCTAssertEqual(updates.state, .installing(update))

        updates.installing {}
        XCTAssertEqual(updates.state, .installing(update))
        XCTAssertEqual(defaults.string(forKey: AppUpdates.relaunchKey), "0.4.0", "the version it relaunches from")
    }

    func testLaterDismissesUntilTheNextCheck() {
        let updates = updates()
        var choices: [AppUpdateChoice] = []
        updates.found(update) { choices.append($0) }
        updates.later()
        XCTAssertEqual(choices, [.later])
        XCTAssertEqual(updates.state, .idle)
        updates.install()
        XCTAssertEqual(choices, [.later], "nothing to install any more")
    }

    func testInstallWaitsWhileVoicedOrAModelIsBusy() {
        let updates = updates()
        var busy: String? = AppUpdates.blocker(installing: true, modelsBusy: false)
        updates.installBlocker = { busy }
        var choices: [AppUpdateChoice] = []
        updates.found(update) { choices.append($0) }
        updates.install()
        XCTAssertEqual(choices, [], "quitting now would stop the installer")
        XCTAssertEqual(updates.state, .available(update))
        XCTAssertTrue(updates.isAvailable)
        busy = AppUpdates.blocker(installing: false, modelsBusy: true)
        XCTAssertNotNil(busy)
        updates.install()
        XCTAssertEqual(choices, [])
        busy = AppUpdates.blocker(installing: false, modelsBusy: false)
        XCTAssertNil(busy)
        updates.install()
        XCTAssertEqual(choices, [.install])
    }

    func testACancelledQuitOffersRelaunchNow() {
        let updates = updates()
        var retries = 0
        updates.found(update) { _ in }
        updates.install()
        updates.extracting()
        updates.quitCancelled()
        XCTAssertFalse(updates.canRetryRelaunch, "nothing to retry before the install asks to quit")
        updates.installing { retries += 1 }
        XCTAssertFalse(updates.canRetryRelaunch)
        updates.quitCancelled()
        XCTAssertTrue(updates.canRetryRelaunch)
        updates.relaunchNow()
        XCTAssertEqual(retries, 1)
        XCTAssertFalse(updates.canRetryRelaunch, "until the next cancelled quit")
        updates.relaunchNow()
        XCTAssertEqual(retries, 1)
        updates.quitCancelled()
        updates.finished()
        XCTAssertFalse(updates.canRetryRelaunch)
        XCTAssertEqual(updates.state, .idle)
    }

    func testADownloadThatFailsBeforeItStartsIsReported() {
        let updates = updates()
        updates.found(update) { _ in }
        updates.install()
        updates.failed("No network.")
        updates.downloadStarted {}
        XCTAssertEqual(updates.state, .failed("No network."), "a late callback does not revive it")
        updates.finished()
        XCTAssertEqual(updates.state, .failed("No network."))
    }

    func testAUserCheckAnswersEvenWhenThereIsNothingNew() {
        let updater = FakeAppUpdater()
        let updates = updates(updater)
        updates.check()
        XCTAssertEqual(updater.checks, 1)
        updates.checkStarted()
        XCTAssertEqual(updates.state, .checking)
        XCTAssertTrue(updates.isBusy)
        updates.check()
        XCTAssertEqual(updater.checks, 1, "not twice at once")
        updates.notFound()
        updates.finished()
        XCTAssertEqual(updates.state, .upToDate, "stays until read")
        updates.dismiss()
        XCTAssertEqual(updates.state, .idle)
    }

    func testAFailureStaysUntilDismissed() {
        let updates = updates()
        updates.found(update) { _ in }
        updates.install()
        updates.downloadStarted {}
        updates.failed("The update is improperly signed.")
        updates.finished()
        XCTAssertEqual(updates.state, .failed("The update is improperly signed."))
        XCTAssertFalse(updates.isBusy)
        updates.dismiss()
        XCTAssertEqual(updates.state, .idle)
    }

    func testCancellingADownloadStopsIt() {
        let updates = updates()
        var cancelled = 0
        updates.found(update) { _ in }
        updates.install()
        updates.downloadStarted { cancelled += 1 }
        updates.cancelDownload()
        XCTAssertEqual(cancelled, 1)
        XCTAssertEqual(updates.state, .idle)
    }

    func testTheSessionEndingClearsWhatWasInProgress() {
        let updates = updates()
        updates.found(update) { _ in }
        updates.finished()
        XCTAssertEqual(updates.state, .idle)
    }

    // MARK: - After the relaunch

    private let olderVoiced = InstallMatch.differs(installed: "0.4.0", bundled: "0.5.0", source: .app, sameVersion: false)

    func testTheRelaunchedAppUpdatesVoicedOnceWithoutAsking() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let relaunched = updates(version: "0.5.0", defaults: defaults)
        XCTAssertNil(defaults.string(forKey: AppUpdates.relaunchKey), "read once")
        XCTAssertFalse(relaunched.continuesWithVoicedUpdate(nil, canStart: true), "not known yet")
        XCTAssertTrue(relaunched.continuesWithVoicedUpdate(olderVoiced, canStart: true))
        XCTAssertFalse(relaunched.continuesWithVoicedUpdate(olderVoiced, canStart: true), "once")
        XCTAssertFalse(updates(version: "0.5.0", defaults: defaults).continuesWithVoicedUpdate(olderVoiced, canStart: true), "nor at the next launch")
    }

    func testTheFirstKnownComparisonDecides() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let relaunched = updates(version: "0.5.0", defaults: defaults)
        XCTAssertFalse(relaunched.continuesWithVoicedUpdate(.matches, canStart: true))
        XCTAssertFalse(relaunched.continuesWithVoicedUpdate(olderVoiced, canStart: true), "a later mismatch asks, as on any launch")
    }

    func testAnInstallAnotherToolManagesIsLeftToIt() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let homebrew = InstallMatch.differs(installed: "0.4.0", bundled: "0.5.0", source: .homebrew, sameVersion: false)
        XCTAssertFalse(updates(version: "0.5.0", defaults: defaults).continuesWithVoicedUpdate(homebrew, canStart: true))
    }

    func testAnUpdateThatDidNotHappenContinuesNothing() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        XCTAssertFalse(updates(version: "0.4.0", defaults: defaults).continuesWithVoicedUpdate(olderVoiced, canStart: true))
        XCTAssertFalse(updates(version: "0.5.0").continuesWithVoicedUpdate(olderVoiced, canStart: true), "an ordinary launch asks")
    }

    func testABusyMomentKeepsTheDecisionForLater() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let relaunched = updates(version: "0.5.0", defaults: defaults)
        XCTAssertFalse(relaunched.continuesWithVoicedUpdate(olderVoiced, canStart: false))
        XCTAssertTrue(relaunched.continuesWithVoicedUpdate(olderVoiced, canStart: true))
    }

    // MARK: - The continuation

    private final class FakeStarter: VoicedUpdateStarter {
        var isWorking = false
        var updates = 0
        func update() { updates += 1 }
    }

    func testTheContinuationStartsUpdateVoicedOnceWhenNothingIsBusy() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let updates = updates(version: "0.5.0", defaults: defaults)
        let installer = FakeStarter()
        var free = false
        let continuation = VoicedUpdateContinuation(updates: updates, installer: installer, canStart: { free })

        XCTAssertFalse(continuation.run(olderVoiced), "a model download or translocation waits")
        installer.isWorking = true
        free = true
        XCTAssertFalse(continuation.run(olderVoiced), "the installer is busy")
        installer.isWorking = false
        XCTAssertFalse(continuation.run(nil), "nothing known yet")
        XCTAssertEqual(installer.updates, 0)
        XCTAssertTrue(continuation.run(olderVoiced))
        XCTAssertEqual(installer.updates, 1)
        XCTAssertFalse(continuation.run(olderVoiced), "once")
        XCTAssertEqual(installer.updates, 1)
    }

    func testWithoutAnInstallerTheContinuationKeepsWaiting() {
        let defaults = scratchDefaults()
        defaults.set("0.4.0", forKey: AppUpdates.relaunchKey)
        let updates = updates(version: "0.5.0", defaults: defaults)
        XCTAssertFalse(VoicedUpdateContinuation(updates: updates, installer: nil, canStart: { true }).run(olderVoiced))
        XCTAssertTrue(updates.continuesWithVoicedUpdate(olderVoiced, canStart: true), "not consumed")
    }

    // MARK: - In the views

    func testAWaitingUpdateBadgesTheIconWithoutAnError() {
        let running = Dashboard(health: .running, headline: "Running")
        let waiting = MenuBarIconStatus(dashboard: running, installing: false, updateAvailable: true)
        XCTAssertEqual(waiting.state, .update)
        XCTAssertEqual(waiting.accessibilityLabel, "voiced: Running, update available")
        XCTAssertEqual(MenuBarIconStatus(dashboard: running, installing: true, updateAvailable: true).state, .busy)
        let stopped = Dashboard(health: .attention, headline: "Stopped")
        XCTAssertEqual(MenuBarIconStatus(dashboard: stopped, installing: false, updateAvailable: true).state, .error)
    }

    func testTheCardRendersInEveryState() {
        let updates = updates()
        let states: [() -> Void] = [
            { updates.checkStarted() },
            { updates.found(self.update) { _ in } },
            { updates.install(); updates.downloadStarted {}; updates.downloadExpects(10); updates.downloadReceived(5) },
            { updates.extracting() },
            { updates.installing {}; updates.quitCancelled() },
            { updates.failed("No network.") },
        ]
        for step in states {
            step()
            let host = NSHostingView(rootView: AppUpdateCard(updates: updates).frame(width: 340))
            XCTAssertGreaterThan(host.fittingSize.height, 20, "\(updates.state)")
        }
        updates.dismiss()
        XCTAssertEqual(NSHostingView(rootView: AppUpdateCard(updates: updates)).fittingSize.height, 0)
    }
}
