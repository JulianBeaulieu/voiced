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
        XCTAssertEqual(defaults.string(forKey: VoicedUpdateContinuation.relaunchKey), "0.4.0", "the version it relaunches from")
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
        XCTAssertEqual(updates.state, .installing(update), "nothing to retry before the install asks to quit")
        updates.installing { retries += 1 }
        updates.quitCancelled()
        XCTAssertEqual(updates.state, .relaunchCancelled(update))
        XCTAssertTrue(updates.isBusy, "Update and Repair still wait")
        updates.relaunchNow()
        XCTAssertEqual(retries, 1)
        XCTAssertEqual(updates.state, .installing(update), "until the next cancelled quit")
        updates.relaunchNow()
        XCTAssertEqual(retries, 1)
        updates.quitCancelled()
        updates.finished()
        XCTAssertEqual(updates.state, .idle)
    }

    func testAQuitWhileTheInstallerWorksWaitsAndOffersRelaunchNow() {
        let updates = updates()
        var installerWorking = false
        updates.installBlocker = { AppUpdates.blocker(installing: installerWorking, modelsBusy: false) }
        var retries = 0
        updates.found(update) { _ in }
        updates.install()
        updates.extracting()
        XCTAssertFalse(updates.holdsQuit(), "no quit for an update asked yet")
        updates.installing { retries += 1 }
        XCTAssertFalse(updates.holdsQuit(), "nothing is busy: the app quits")

        // "Update voiced" started while the update downloaded.
        installerWorking = true
        XCTAssertTrue(updates.holdsQuit())
        XCTAssertEqual(updates.state, .relaunchCancelled(update))
        updates.relaunchNow()
        XCTAssertEqual(retries, 0, "Relaunch Now waits for the installer too")
        installerWorking = false
        updates.relaunchNow()
        XCTAssertEqual(retries, 1)
        XCTAssertEqual(updates.state, .installing(update))
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

    /// A continuation as the app makes it, counting the updates it starts.
    private final class Starts {
        var count = 0
        var canStart = true
    }

    private func continuation(
        version: String = "0.5.0", from: String? = "0.4.0", defaults: UserDefaults? = nil, starts: Starts
    ) -> VoicedUpdateContinuation {
        let defaults = defaults ?? scratchDefaults()
        if let from { defaults.set(from, forKey: VoicedUpdateContinuation.relaunchKey) }
        return VoicedUpdateContinuation(
            appVersion: version, defaults: defaults, canStart: { starts.canStart }, startUpdate: { starts.count += 1 })
    }

    func testTheRelaunchedAppUpdatesVoicedOnceWithoutAsking() {
        let defaults = scratchDefaults()
        let starts = Starts()
        let relaunched = continuation(defaults: defaults, starts: starts)
        XCTAssertNil(defaults.string(forKey: VoicedUpdateContinuation.relaunchKey), "read once")
        XCTAssertFalse(relaunched.run(nil), "not known yet")
        XCTAssertTrue(relaunched.run(olderVoiced))
        XCTAssertFalse(relaunched.run(olderVoiced), "once")
        XCTAssertEqual(starts.count, 1)
        XCTAssertFalse(continuation(from: nil, defaults: defaults, starts: starts).run(olderVoiced), "nor at the next launch")
    }

    func testTheFirstKnownComparisonDecides() {
        let starts = Starts()
        let relaunched = continuation(starts: starts)
        XCTAssertFalse(relaunched.run(.matches))
        XCTAssertFalse(relaunched.run(olderVoiced), "a later mismatch asks, as on any launch")
        XCTAssertEqual(starts.count, 0)
    }

    func testAnInstallAnotherToolManagesIsLeftToIt() {
        let starts = Starts()
        let homebrew = InstallMatch.differs(installed: "0.4.0", bundled: "0.5.0", source: .homebrew, sameVersion: false)
        XCTAssertFalse(continuation(starts: starts).run(homebrew))
        XCTAssertEqual(starts.count, 0)
    }

    func testAnUpdateThatDidNotHappenContinuesNothing() {
        let starts = Starts()
        XCTAssertFalse(continuation(version: "0.4.0", starts: starts).run(olderVoiced))
        XCTAssertFalse(continuation(from: nil, starts: starts).run(olderVoiced), "an ordinary launch asks")
        XCTAssertEqual(starts.count, 0)
    }

    func testABusyMomentKeepsTheDecisionForLater() {
        let starts = Starts()
        starts.canStart = false
        let relaunched = continuation(starts: starts)
        XCTAssertFalse(relaunched.run(olderVoiced), "the installer, a model download or translocation waits")
        XCTAssertFalse(relaunched.run(.matches), "nor is a comparison taken while it waits")
        starts.canStart = true
        XCTAssertTrue(relaunched.run(olderVoiced))
        XCTAssertEqual(starts.count, 1)
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
            { updates.installBlocker = { "Waits until voiced has finished installing." } },
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
