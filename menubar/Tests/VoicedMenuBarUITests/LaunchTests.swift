import FakeControlServerKit
import Foundation
import ServiceManagement
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

/// A Login Items status that tests can change while it is being polled.
private final class StatusBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: SMAppService.Status
    private(set) var asked: [String] = []

    init(_ value: SMAppService.Status) {
        self.value = value
    }

    func set(_ new: SMAppService.Status) {
        lock.withLock { value = new }
    }

    func status(_ url: URL) -> SMAppService.Status {
        lock.withLock {
            asked.append(url.lastPathComponent)
            return value
        }
    }
}

@MainActor
final class LaunchTests: XCTestCase {
    private var home: URL!
    private var daemons: URL!
    private var models: [AppModel] = []
    private var checks: [LaunchChecks] = []
    private var servers: [FakeControlServer] = []

    override func setUp() async throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".voiced"), withIntermediateDirectories: true)
        daemons = try launchDaemonsDirectory()
    }

    override func tearDown() async throws {
        checks.forEach { $0.stop() }
        models.forEach { $0.stop() }
        servers.forEach { $0.stop() }
        try? FileManager.default.removeItem(at: home)
        try? FileManager.default.removeItem(at: daemons)
    }

    private let manifest = InstallManifest(
        python: "3.12.15", lockSHA256: "ab12",
        wheel: .init(name: "voiced-0.2.0-py3-none-any.whl", sha256: "cd34"), wheelsBytes: 1, models: [])

    private func record(_ source: String = "app", version: String? = "0.2.0", lock: String? = "AB12") -> InstallRecord {
        InstallRecord(source: source, environment: "/env", version: version, lockSHA256: lock, python: "3.12.15")
    }

    private func makeChecks(
        socket: String = temporarySocketPath(), status: StatusBox = StatusBox(.enabled),
        translocation: (URL) -> URL? = { _ in nil }
    ) -> (LaunchChecks, AppModel) {
        let model = AppModel(
            socketPath: socket, installProbe: InstallProbe(launchDaemonsDirectory: daemons),
            backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)), opener: { _ in true })
        models.append(model)
        let launch = LaunchChecks(
            model: model, manifest: manifest, bundleURL: URL(fileURLWithPath: "/Applications/voiced.app"),
            stateDirectory: home.appendingPathComponent(".voiced"),
            backgroundItems: BackgroundItems(launchDaemonsDirectory: daemons, status: status.status),
            translocation: translocation, pollInterval: .milliseconds(20))
        checks.append(launch)
        return (launch, model)
    }

    private func writeInstallJSON(_ text: String) throws {
        try Data(text.utf8).write(to: home.appendingPathComponent(".voiced/install.json"))
    }

    private func installPlists() throws {
        for label in BackgroundItems.labels {
            try writePlist(["Label": label], to: daemons.appendingPathComponent("\(label).plist"))
        }
    }

    // MARK: - The installed version against the app's pins

    func testComparesVersionAndLock() {
        XCTAssertNil(InstallMatch.compare(nil, with: manifest), "nothing installed")
        XCTAssertNil(InstallMatch.compare(InstallRecord(source: "app"), with: manifest), "claimed, nothing linked yet")
        XCTAssertEqual(InstallMatch.compare(record(), with: manifest), .matches, "the lock hash ignores case")
        XCTAssertEqual(
            InstallMatch.compare(record(version: "0.1.0"), with: manifest),
            .differs(installed: "0.1.0", bundled: "0.2.0", source: "app", sameVersion: false))
        XCTAssertEqual(
            InstallMatch.compare(record(version: "0.3.0"), with: manifest),
            .differs(installed: "0.3.0", bundled: "0.2.0", source: "app", sameVersion: false), "newer counts too")
        XCTAssertEqual(
            InstallMatch.compare(record(lock: "ffff"), with: manifest),
            .differs(installed: "0.2.0", bundled: "0.2.0", source: "app", sameVersion: true))
        XCTAssertEqual(
            InstallMatch.compare(record(version: nil, lock: nil), with: manifest),
            .differs(installed: "unknown", bundled: "0.2.0", source: "app", sameVersion: false))
    }

    func testTheNoticeDependsOnWhoManagesTheInstall() {
        XCTAssertNil(InstallMatch.matches.notice)
        let app = InstallMatch.differs(installed: "0.1.0", bundled: "0.2.0", source: "app", sameVersion: false).notice
        XCTAssertEqual(app?.text, "The services run voiced 0.1.0; this app carries 0.2.0.")
        XCTAssertNil(app?.command)
        let brew = InstallMatch.differs(installed: "0.3.0", bundled: "0.2.0", source: "homebrew", sameVersion: false).notice
        XCTAssertEqual(brew?.command, "brew upgrade voiced")
        XCTAssertTrue(brew?.text.contains("Homebrew manages this install") == true)
        let cli = InstallMatch.differs(installed: "0.2.0", bundled: "0.2.0", source: "cli", sameVersion: true).notice
        XCTAssertEqual(
            cli?.text,
            "The services run voiced 0.2.0 with other dependencies than this app carries. "
                + "This install was made from the command line, which also updates it.")
        XCTAssertNil(cli?.command)
    }

    func testTheWheelNamesItsVersion() {
        XCTAssertEqual(InstallManifest.Wheel(name: "voiced-0.1.0rc1-py3-none-any.whl", sha256: "").version, "0.1.0rc1")
        XCTAssertEqual(InstallManifest.Wheel(name: "voiced-1.2.3+dev-py3-none-any.whl", sha256: "").version, "1.2.3+dev")
        XCTAssertNil(InstallManifest.Wheel(name: "voiced.whl", sha256: "").version)
    }

    func testWithoutAWatchdogTheFileIsRead() throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.1.0", "lock_sha256": "ab12"}"#)
        let (launch, _) = makeChecks()
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.1.0", bundled: "0.2.0", source: "app", sameVersion: false))

        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.2.0", "lock_sha256": "ab12"}"#)
        XCTAssertNotEqual(launch.installMatch, .matches, "read once per refresh, not per look")
        launch.refresh()
        XCTAssertEqual(launch.installMatch, .matches)

        try FileManager.default.removeItem(at: home.appendingPathComponent(".voiced/install.json"))
        launch.refresh()
        XCTAssertNil(launch.installMatch)
    }

    /// While the watchdog answers, its hello is what counts, not the file.
    func testTheWatchdogsHelloWins() async throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.0.9", "lock_sha256": "ab12"}"#)
        var options = FakeControlServer.Options()
        options.install = .object(["source": "homebrew", "environment": "/keg", "version": "0.3.0", "lock_sha256": "ee"])
        let socket = temporarySocketPath()
        let server = FakeControlServer(socketPath: socket, scenario: .running, options: options)
        try server.start()
        servers.append(server)
        let (launch, model) = makeChecks(socket: socket)
        model.start()
        let connected = await eventually { model.installRecord != nil }
        XCTAssertTrue(connected)
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.3.0", bundled: "0.2.0", source: "homebrew", sameVersion: false))

        // The watchdog goes away: the file speaks again.
        server.stop()
        let gone = await eventually { model.installRecord == nil }
        XCTAssertTrue(gone)
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.0.9", bundled: "0.2.0", source: "app", sameVersion: false))
    }

    func testABuildWithoutPinsComparesNothing() throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.1.0"}"#)
        let model = AppModel(socketPath: temporarySocketPath(), opener: { _ in true })
        let launch = LaunchChecks(
            model: model, manifest: nil, stateDirectory: home.appendingPathComponent(".voiced"),
            backgroundItems: BackgroundItems(launchDaemonsDirectory: daemons), translocation: { _ in nil })
        XCTAssertNil(launch.installMatch)
        XCTAssertNotNil(launch.installRecord)
    }

    // MARK: - Login Items

    func testServicesSwitchedOffInLoginItemsShowTheCardUntilTurnedOn() async throws {
        try installPlists()
        let status = StatusBox(.requiresApproval)
        let (launch, _) = makeChecks(status: status)
        XCTAssertTrue(launch.backgroundItemsOff)
        status.set(.enabled)
        let cleared = await eventually { !launch.backgroundItemsOff }
        XCTAssertTrue(cleared, "polled while the card is shown")
    }

    func testPollingStopsOnceOnAndStartsAgainOnRefresh() async throws {
        try installPlists()
        let status = StatusBox(.enabled)
        let (launch, _) = makeChecks(status: status)
        XCTAssertFalse(launch.backgroundItemsOff)
        let asked = status.asked.count
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(status.asked.count, asked, "no polling while on")

        status.set(.requiresApproval)
        launch.refresh()
        XCTAssertTrue(launch.backgroundItemsOff)
        status.set(.enabled)
        let cleared = await eventually { !launch.backgroundItemsOff }
        XCTAssertTrue(cleared)
    }

    func testOnlyInstalledServicesAreAskedAbout() throws {
        let status = StatusBox(.requiresApproval)
        XCTAssertFalse(BackgroundItems(launchDaemonsDirectory: daemons, status: status.status).areOff(), "nothing installed")
        XCTAssertTrue(status.asked.isEmpty)

        try writePlist(["Label": "x"], to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.tts.plist"))
        XCTAssertTrue(BackgroundItems(launchDaemonsDirectory: daemons, status: status.status).areOff())
        XCTAssertEqual(status.asked, ["io.github.julianbeaulieu.voiced.tts.plist"])

        for other in [SMAppService.Status.enabled, .notRegistered, .notFound] {
            status.set(other)
            XCTAssertFalse(BackgroundItems(launchDaemonsDirectory: daemons, status: status.status).areOff(), "\(other)")
        }
    }

    // MARK: - Translocation

    func testATranslocatedAppNamesWhereItReallyIs() {
        let original = URL(fileURLWithPath: "/Users/someone/Downloads/voiced.app")
        let (launch, _) = makeChecks(translocation: { _ in original })
        XCTAssertEqual(launch.translocatedFrom, original)
        XCTAssertNil(makeChecks().0.translocatedFrom)
    }

    func testAnAppThatIsNotTranslocated() {
        XCTAssertNil(Translocation.originalURL(of: Bundle.main.bundleURL))
        XCTAssertNil(Translocation.originalURL(of: URL(fileURLWithPath: "/Applications/voiced.app")))
    }

    // MARK: - What's new

    private func whatsNew(_ defaults: UserDefaults, notes: String? = "# What's new", version: String? = "0.2.0") -> WhatsNew {
        WhatsNew(notes: notes, version: version, defaults: defaults)
    }

    private func scratchDefaults() -> UserDefaults {
        let name = "voiced-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        addTeardownBlock { UserDefaults().removePersistentDomain(forName: name) }
        return defaults
    }

    func testAFirstInstallShowsNothingAndAnUpdateShowsTheNotesOnce() {
        let defaults = scratchDefaults()
        let first = whatsNew(defaults, version: "0.1.0")
        first.observe(.matches)
        XCTAssertFalse(first.isPending, "nothing to compare with")
        XCTAssertEqual(defaults.string(forKey: WhatsNew.seenKey), "0.1.0")

        // The next app version, before and after the services were updated.
        let next = whatsNew(defaults)
        next.observe(.differs(installed: "0.1.0", bundled: "0.2.0", source: "app", sameVersion: false))
        next.observe(nil)
        XCTAssertFalse(next.isPending)
        next.observe(.matches)
        XCTAssertTrue(next.isPending)
        next.shown()
        next.observe(.matches)
        XCTAssertFalse(next.isPending, "once")
        XCTAssertFalse(whatsNew(defaults).isPending, "nor at the next launch")
        whatsNew(defaults).observe(.matches)
        XCTAssertEqual(defaults.string(forKey: WhatsNew.seenKey), "0.2.0")
    }

    func testWithoutNotesNothingOpensButTheVersionIsRemembered() {
        let defaults = scratchDefaults()
        defaults.set("0.1.0", forKey: WhatsNew.seenKey)
        let new = whatsNew(defaults, notes: nil)
        new.observe(.matches)
        XCTAssertFalse(new.isPending)
        XCTAssertEqual(defaults.string(forKey: WhatsNew.seenKey), "0.2.0")
    }

    func testABuildWithoutPinsHasNoVersionToCompare() {
        let defaults = scratchDefaults()
        whatsNew(defaults, version: nil).observe(.matches)
        XCTAssertNil(defaults.string(forKey: WhatsNew.seenKey))
    }

    func testTheNotesAreReadBlockByBlock() {
        let markdown = """
            # What's new in voiced 0.2.0

            ### Added

            - `voiced install --json-progress`: one JSON event
              per line.
            - Another item.

            A paragraph
            on two lines.
            """
        XCTAssertEqual(
            MarkdownBlock.parse(markdown),
            [
                .heading(level: 1, text: "What's new in voiced 0.2.0"),
                .heading(level: 3, text: "Added"),
                .item("`voiced install --json-progress`: one JSON event per line."),
                .item("Another item."),
                .paragraph("A paragraph on two lines."),
            ])
        XCTAssertEqual(String(WhatsNewView.inline("`code` and **bold**").characters), "code and bold")
    }

    func testTheBundlesNotesAreFound() throws {
        let bundle = home.appendingPathComponent("voiced.app")
        let resources = bundle.appendingPathComponent("Contents/Resources")
        try FileManager.default.createDirectory(at: resources, withIntermediateDirectories: true)
        XCTAssertNil(WhatsNew.notes(in: Bundle(url: bundle)!))
        try Data("# Notes\n".utf8).write(to: resources.appendingPathComponent("WhatsNew.md"))
        XCTAssertEqual(WhatsNew.notes(in: Bundle(url: bundle)!), "# Notes\n")
    }

    // MARK: - Diagnostics

    func testDiagnosticsNameTheAppThePinsAndTheInstall() {
        let pinned = InstallManifest(
            python: "3.12.15", lockSHA256: "ab12", wheel: .init(name: "voiced-0.2.0-py3-none-any.whl", sha256: "cd34"),
            wheelsBytes: 1, models: [.init(repo: "mlx-community/Kokoro-82M-bf16", revision: "a71e", bytes: 1)])
        let diagnostics = Diagnostics(
            appVersion: "0.2.0", bundlePath: "/Applications/voiced.app", translocatedFrom: nil, manifest: pinned,
            installSource: "/src/voiced", record: record(), connection: "Running", serverVersion: "0.2.0",
            socketPath: "/Users/someone/.voiced/control.sock", home: URL(fileURLWithPath: "/Users/someone"))
        let report = diagnostics.report
        for expected in [
            "Menu bar app\n  Version: 0.2.0\n  Location: /Applications/voiced.app\n  Install source override: /src/voiced",
            "  Wheel sha256: cd34", "  mlx-community/Kokoro-82M-bf16: revision a71e",
            "Installed environment\n  Source: app\n  Environment: /env\n  voiced: 0.2.0\n  Lock sha256: AB12",
            "  State: Running", "  Logs: /Users/someone/Library/Logs/voiced",
            "  Config file: /Users/someone/.config/voiced/config.toml",
        ] {
            XCTAssertTrue(report.contains(expected), "\(expected) not in\n\(report)")
        }
        XCTAssertFalse(report.contains("Translocated"))

        let bare = Diagnostics(
            appVersion: "Development build", bundlePath: "/x", translocatedFrom: "/Users/someone/Downloads/voiced.app",
            manifest: nil, installSource: nil, record: nil, connection: "Not installed", serverVersion: nil,
            socketPath: "/s", home: URL(fileURLWithPath: "/Users/someone")).report
        for expected in [
            "  Translocated from: /Users/someone/Downloads/voiced.app",
            "  Pins: None: this build has no installer resources", "  install.json: Not found",
            "  Watchdog reports voiced: None",
        ] {
            XCTAssertTrue(bare.contains(expected), "\(expected) not in\n\(bare)")
        }
    }

    // MARK: - Settings panes

    func testTheDebugPaneIsHiddenUntilTurnedOn() {
        XCTAssertFalse(SettingsPane.visible(showingDebug: false).contains(.debug))
        XCTAssertEqual(SettingsPane.visible(showingDebug: true).last, .debug)
    }

    // MARK: - The development override

    func testTheOverrideIsIgnoredInReleaseBuilds() {
        let defaults = scratchDefaults()
        XCTAssertNil(DevelopmentOverride.installSource(defaults: defaults, isRelease: false), "unset")
        defaults.set("  ", forKey: DevelopmentOverride.key)
        XCTAssertNil(DevelopmentOverride.installSource(defaults: defaults, isRelease: false), "blank")
        defaults.set("~/src/voiced/", forKey: DevelopmentOverride.key)
        XCTAssertEqual(
            DevelopmentOverride.installSource(defaults: defaults, isRelease: false)?.path,
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("src/voiced").path)
        XCTAssertNil(DevelopmentOverride.installSource(defaults: defaults, isRelease: true))
    }

    /// The tests run unsigned or ad hoc signed, as development builds are.
    func testThisBuildIsNotARelease() {
        XCTAssertFalse(DevelopmentOverride.isReleaseBuild())
    }
}
