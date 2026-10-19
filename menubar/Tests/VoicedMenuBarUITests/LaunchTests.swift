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
    /// Whether each call came on the main thread: `SMAppService` answers over XPC, which
    /// must not block the main actor.
    private(set) var onMainThread: [Bool] = []

    init(_ value: SMAppService.Status) {
        self.value = value
    }

    func set(_ new: SMAppService.Status) {
        lock.withLock { value = new }
    }

    func status(_ url: URL) -> SMAppService.Status {
        let main = Thread.isMainThread
        return lock.withLock {
            asked.append(url.lastPathComponent)
            onMainThread.append(main)
            return value
        }
    }
}

@MainActor
final class LaunchTests: XCTestCase {
    private var home: URL!
    private var daemons: URL!
    private var models: [AppModel] = []
    private var servers: [FakeControlServer] = []

    override func setUp() async throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".voiced"), withIntermediateDirectories: true)
        daemons = try launchDaemonsDirectory()
    }

    private var paths: VoicedPaths { VoicedPaths(home: home, bundle: URL(fileURLWithPath: "/Applications/voiced.app")) }

    override func tearDown() async throws {
        models.forEach { $0.stop() }
        servers.forEach { $0.stop() }
        try? FileManager.default.removeItem(at: home)
        try? FileManager.default.removeItem(at: daemons)
    }

    private let manifest = InstallManifest(
        version: "0.2.0", python: "3.12.15", lockSHA256: "ab12",
        wheel: .init(name: "voiced-0.2.0-py3-none-any.whl", sha256: "cd34"), wheelsBytes: 1, models: [])

    private func record(_ source: InstallSource = .app, version: String? = "0.2.0", lock: String? = "AB12") -> InstallRecord {
        InstallRecord(source: source, environment: "/env", version: version, lockSHA256: lock, python: "3.12.15")
    }

    private func makeChecks(
        socket: String = temporarySocketPath(), status: StatusBox = StatusBox(.enabled),
        translocation: (URL) -> URL? = { _ in nil }, installSource: URL? = nil
    ) -> (LaunchChecks, AppModel) {
        let model = AppModel(
            socketPath: socket, installProbe: InstallProbe(launchDaemonsDirectory: daemons),
            backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)), opener: { _ in true })
        models.append(model)
        let launch = LaunchChecks(
            model: model, manifest: manifest, installSource: installSource, paths: paths,
            backgroundItems: BackgroundItems(launchDaemonsDirectory: daemons, status: status.status),
            translocation: translocation, pollInterval: .milliseconds(20))
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
        XCTAssertNil(InstallMatch.compare(InstallRecord(source: .app), with: manifest), "claimed, nothing linked yet")
        XCTAssertEqual(InstallMatch.compare(record(), with: manifest), .matches, "the lock hash ignores case")
        XCTAssertEqual(
            InstallMatch.compare(record(version: "0.1.0"), with: manifest),
            .differs(installed: "0.1.0", bundled: "0.2.0", source: .app, sameVersion: false))
        XCTAssertEqual(
            InstallMatch.compare(record(version: "0.3.0"), with: manifest),
            .differs(installed: "0.3.0", bundled: "0.2.0", source: .app, sameVersion: false), "newer counts too")
        XCTAssertEqual(
            InstallMatch.compare(record(lock: "ffff"), with: manifest),
            .differs(installed: "0.2.0", bundled: "0.2.0", source: .app, sameVersion: true))
        XCTAssertEqual(
            InstallMatch.compare(record(version: nil, lock: nil), with: manifest),
            .differs(installed: "unknown", bundled: "0.2.0", source: .app, sameVersion: false))
    }

    func testTheCardDependsOnWhoManagesTheInstall() {
        XCTAssertNil(LaunchCards.notice(.matches))
        let app = LaunchCards.notice(.differs(installed: "0.1.0", bundled: "0.2.0", source: .app, sameVersion: false))
        XCTAssertEqual(app?.text, "The services run voiced 0.1.0; this app carries 0.2.0.")
        XCTAssertNil(app?.command)
        XCTAssertEqual(app?.managed, true, "the app offers to update its own install")
        let brew = LaunchCards.notice(.differs(installed: "0.3.0", bundled: "0.2.0", source: .homebrew, sameVersion: false))
        XCTAssertEqual(brew?.command, "brew upgrade voiced")
        XCTAssertEqual(brew?.managed, false)
        XCTAssertTrue(brew?.text.contains("Homebrew manages this install") == true)
        let cli = LaunchCards.notice(.differs(installed: "0.2.0", bundled: "0.2.0", source: .cli, sameVersion: true))
        XCTAssertEqual(
            cli?.text,
            "The services run voiced 0.2.0 with other dependencies than this app carries. "
                + "This install was made from the command line; update it from your checkout with:")
        XCTAssertEqual(cli?.command, "voiced install --source <checkout>")
        XCTAssertEqual(cli?.managed, false)
    }

    func testWithoutAWatchdogTheFileIsRead() async throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.1.0", "lock_sha256": "ab12"}"#)
        let (launch, _) = makeChecks()
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.1.0", bundled: "0.2.0", source: .app, sameVersion: false))

        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.2.0", "lock_sha256": "ab12"}"#)
        XCTAssertNotEqual(launch.installMatch, .matches, "read once per refresh, not per look")
        await launch.refresh()
        XCTAssertEqual(launch.installMatch, .matches)

        try FileManager.default.removeItem(at: home.appendingPathComponent(".voiced/install.json"))
        await launch.refresh()
        XCTAssertNil(launch.installMatch)
    }

    /// A source this app does not know reads as no record, not as a mismatch.
    func testAnUnknownSourceIsNoRecord() throws {
        try writeInstallJSON(#"{"source": "pipx", "environment": "/env", "version": "0.1.0"}"#)
        XCTAssertNil(makeChecks().0.installRecord)
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
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.3.0", bundled: "0.2.0", source: .homebrew, sameVersion: false))

        // The watchdog goes away: the file speaks again.
        server.stop()
        let gone = await eventually { model.installRecord == nil }
        XCTAssertTrue(gone)
        XCTAssertEqual(launch.installMatch, .differs(installed: "0.0.9", bundled: "0.2.0", source: .app, sameVersion: false))
    }

    func testABuildWithoutPinsComparesNothing() throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.1.0"}"#)
        let model = AppModel(socketPath: temporarySocketPath(), opener: { _ in true })
        let launch = LaunchChecks(
            model: model, manifest: nil, paths: paths, backgroundItems: BackgroundItems(launchDaemonsDirectory: daemons),
            translocation: { _ in nil })
        XCTAssertNil(launch.installMatch)
        XCTAssertNotNil(launch.installRecord)
    }

    /// With the development override set, the installed voiced is not the bundle's on purpose.
    func testTheOverrideSkipsTheComparison() throws {
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.3.0.dev1"}"#)
        let (launch, _) = makeChecks(installSource: URL(fileURLWithPath: "/src/voiced-0.3.0.dev1-py3-none-any.whl"))
        XCTAssertNil(launch.installMatch)
        XCTAssertNotNil(launch.installRecord)
    }

    // MARK: - Login Items

    func testNothingIsAskedUntilRefreshed() throws {
        try installPlists()
        let status = StatusBox(.requiresApproval)
        let (launch, _) = makeChecks(status: status)
        XCTAssertTrue(status.asked.isEmpty, "not at init, which runs on the main actor")
        XCTAssertFalse(launch.backgroundItemsOff)
    }

    func testServicesSwitchedOffInLoginItemsShowTheCardWhileWatchedUntilTurnedOn() async throws {
        try installPlists()
        let status = StatusBox(.requiresApproval)
        let (launch, _) = makeChecks(status: status)
        let watching = Task { await launch.watch() }
        defer { watching.cancel() }
        let off = await eventually { launch.backgroundItemsOff }
        XCTAssertTrue(off)
        status.set(.enabled)
        let cleared = await eventually { !launch.backgroundItemsOff }
        XCTAssertTrue(cleared, "polled while watched")
        XCTAssertEqual(launch.backgroundItems.count, 3)
    }

    func testLoginItemsAreNeverAskedOnTheMainThread() async throws {
        try installPlists()
        let status = StatusBox(.enabled)
        let (launch, _) = makeChecks(status: status)
        await launch.refresh()
        let watching = Task { await launch.watch() }
        let asked = await eventually { status.asked.count >= 6 }
        watching.cancel()
        await watching.value
        XCTAssertTrue(asked)
        XCTAssertFalse(status.onMainThread.isEmpty)
        XCTAssertEqual(status.onMainThread.filter { $0 }.count, 0, "SMAppService was asked on the main thread")
    }

    func testTheInstallButtonHonoursTranslocation() {
        let (launch, model) = makeChecks()
        let translocated = makeChecks(translocation: { _ in URL(fileURLWithPath: "/Users/someone/Downloads/voiced.app") }).0
        XCTAssertNotNil(PopoverView(model: model, canInstall: true, launch: launch).installAction)
        XCTAssertNotNil(PopoverView(model: model, canInstall: true, launch: nil).installAction)
        XCTAssertNil(PopoverView(model: model, canInstall: true, launch: translocated).installAction, "move it first")
        XCTAssertNil(PopoverView(model: model, canInstall: false, launch: launch).installAction, "no resources: the guide")
    }

    /// Repair is offered while an install the app made is in place but does not answer.
    func testRepairIsOfferedForTheAppsInstallThatDoesNotAnswer() async throws {
        try writePlist(
            ["ProgramArguments": ["/Users/someone/.voiced/bin/voiced", "watchdog"], "UserName": NSUserName()],
            to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        try writeInstallJSON(#"{"source": "app", "environment": "/env", "version": "0.2.0", "lock_sha256": "ab12"}"#)
        let (launch, model) = makeChecks()
        await launch.refresh()
        XCTAssertNil(PopoverView(model: model, launch: launch, repair: {}).repairAction, "still connecting")
        model.start()
        let unreachable = await eventually {
            if case .unreachable(installed: true, _) = model.connection { true } else { false }
        }
        XCTAssertTrue(unreachable)
        XCTAssertNotNil(PopoverView(model: model, launch: launch, repair: {}).repairAction)
        XCTAssertNil(PopoverView(model: model, launch: launch).repairAction, "no installer")
        try writeInstallJSON(#"{"source": "homebrew", "environment": "/opt/homebrew/opt/voiced/libexec"}"#)
        await launch.refresh()
        XCTAssertNil(PopoverView(model: model, launch: launch, repair: {}).repairAction, "Homebrew's to repair")
    }

    func testWatchingStopsWhenCancelled() async throws {
        try installPlists()
        let status = StatusBox(.enabled)
        let (launch, _) = makeChecks(status: status)
        let watching = Task { await launch.watch() }
        let asked = await eventually { status.asked.count >= 6 }
        XCTAssertTrue(asked)
        watching.cancel()
        await watching.value
        let count = status.asked.count
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(status.asked.count, count, "no polling once the window closes")
    }

    func testOnlyInstalledServicesAreAskedAbout() throws {
        let status = StatusBox(.requiresApproval)
        XCTAssertEqual(BackgroundItems(launchDaemonsDirectory: daemons, status: status.status).statuses(), [:])
        XCTAssertTrue(status.asked.isEmpty)

        try writePlist(["Label": "x"], to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.tts.plist"))
        XCTAssertEqual(
            BackgroundItems(launchDaemonsDirectory: daemons, status: status.status).statuses(),
            ["io.github.julianbeaulieu.voiced.tts": .requiresApproval])
        XCTAssertEqual(status.asked, ["io.github.julianbeaulieu.voiced.tts.plist"])
    }

    func testOnlyRequiresApprovalCountsAsOff() async throws {
        try installPlists()
        for other in [SMAppService.Status.enabled, .notRegistered, .notFound] {
            let (launch, _) = makeChecks(status: StatusBox(other))
            await launch.refresh()
            XCTAssertFalse(launch.backgroundItemsOff, "\(other)")
        }
    }

    // MARK: - Translocation

    func testATranslocatedAppNamesWhereItReallyIsAndOffersNoInstaller() {
        let original = URL(fileURLWithPath: "/Users/someone/Downloads/voiced.app")
        let (launch, _) = makeChecks(translocation: { _ in original })
        XCTAssertEqual(launch.translocatedFrom, original)
        XCTAssertFalse(launch.allowsInstall)
        let (normal, _) = makeChecks()
        XCTAssertNil(normal.translocatedFrom)
        XCTAssertTrue(normal.allowsInstall)
    }

    func testAnAppThatIsNotTranslocated() {
        XCTAssertNil(Translocation.originalURL(of: Bundle.main.bundleURL))
        XCTAssertNil(Translocation.originalURL(of: URL(fileURLWithPath: "/Applications/voiced.app")))
    }

    func testTranslocationDecisions() {
        let bundle = URL(fileURLWithPath: "/private/var/folders/x/AppTranslocation/ABCD/d/voiced.app")
        let original = URL(fileURLWithPath: "/Users/someone/Downloads/voiced.app")
        XCTAssertEqual(Translocation.originalURL(of: bundle, isTranslocated: { _ in true }, original: { _ in original }), original)
        XCTAssertEqual(
            Translocation.originalURL(of: bundle, isTranslocated: { _ in true }, original: { _ in nil }), bundle,
            "an original that cannot be found still asks to be moved")
        XCTAssertEqual(Translocation.originalURL(of: bundle, isTranslocated: { _ in true }, original: nil), bundle)
        XCTAssertNil(Translocation.originalURL(of: bundle, isTranslocated: { _ in false }, original: { _ in original }))
        XCTAssertEqual(Translocation.originalURL(of: bundle, isTranslocated: nil, original: nil), bundle, "by the path's shape")
        XCTAssertNil(Translocation.originalURL(of: original, isTranslocated: nil, original: nil))
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
        next.observe(.differs(installed: "0.1.0", bundled: "0.2.0", source: .app, sameVersion: false))
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
        let bundle = VoicedPaths(home: home, bundle: home.appendingPathComponent("voiced.app"))
        try FileManager.default.createDirectory(at: bundle.resources, withIntermediateDirectories: true)
        XCTAssertNil(WhatsNew.notes(in: bundle))
        try Data("# Notes\n".utf8).write(to: bundle.whatsNew)
        XCTAssertEqual(WhatsNew.notes(in: bundle), "# Notes\n")
    }

    // MARK: - Diagnostics

    func testDiagnosticsNameTheAppThePinsAndTheInstall() {
        let pinned = InstallManifest(
            version: "0.2.0", python: "3.12.15", lockSHA256: "ab12",
            wheel: .init(name: "voiced-0.2.0-py3-none-any.whl", sha256: "cd34"),
            wheelsBytes: 1, models: [.init(repo: "mlx-community/Kokoro-82M-bf16", revision: "a71e", bytes: 1)])
        let someone = VoicedPaths(home: URL(fileURLWithPath: "/Users/someone"), bundle: URL(fileURLWithPath: "/Applications/voiced.app"))
        let diagnostics = Diagnostics(
            appVersion: "0.2.0", paths: someone, translocatedFrom: nil, manifest: pinned,
            installSource: URL(fileURLWithPath: "/src/voiced-0.2.0-py3-none-any.whl"), record: record(),
            backgroundItems: ["io.github.julianbeaulieu.voiced.watchdog": .requiresApproval], installer: "welcome",
            connection: "Running", serverVersion: "0.2.0", socketPath: "/Users/someone/.voiced/control.sock")
        let report = diagnostics.report
        for expected in [
            "Menu bar app\n  Version: 0.2.0\n  Location: /Applications/voiced.app\n"
                + "  Install source override: /src/voiced-0.2.0-py3-none-any.whl\n  Installer: welcome",
            "  voiced: 0.2.0\n  Wheel: voiced-0.2.0-py3-none-any.whl\n  Wheel sha256: cd34",
            "  mlx-community/Kokoro-82M-bf16: revision a71e",
            "Installed environment\n  Source: app\n  Environment: /env\n  voiced: 0.2.0\n  Lock sha256: AB12",
            "  State: Running",
            "  io.github.julianbeaulieu.voiced.stt: Not installed",
            "  io.github.julianbeaulieu.voiced.watchdog: Login Items: requiresApproval",
            "  Logs: /Users/someone/Library/Logs/voiced",
            "  Config file: /Users/someone/.config/voiced/config.toml",
            "  uv cache: /Users/someone/Library/Application Support/voiced/uv-cache",
        ] {
            XCTAssertTrue(report.contains(expected), "\(expected) not in\n\(report)")
        }
        XCTAssertFalse(report.contains("Translocated"))

        let bare = Diagnostics(
            appVersion: "Development build", paths: someone,
            translocatedFrom: URL(fileURLWithPath: "/Users/someone/Downloads/voiced.app"), manifest: nil,
            installSource: nil, record: nil, backgroundItems: [:], installer: nil, connection: "Not installed",
            serverVersion: nil, socketPath: "/s").report
        for expected in [
            "  Translocated from: /Users/someone/Downloads/voiced.app",
            "  Pins: None: this build has no installer resources", "  install.json: Not found",
            "  Watchdog reports voiced: None",
        ] {
            XCTAssertTrue(bare.contains(expected), "\(expected) not in\n\(bare)")
        }
        XCTAssertFalse(bare.contains("Installer:"))
    }

    func testLoginItemsStatusesHaveNames() {
        XCTAssertEqual(
            [SMAppService.Status.notRegistered, .enabled, .requiresApproval, .notFound].map(\.name),
            ["notRegistered", "enabled", "requiresApproval", "notFound"])
    }

    // MARK: - Settings panes

    func testTheDebugPaneIsHiddenUntilTurnedOn() {
        XCTAssertFalse(SettingsPane.visible(showingDebug: false).contains(.debug))
        XCTAssertEqual(SettingsPane.visible(showingDebug: true).last, .debug)
    }

    // MARK: - The development override

    /// The tests are a debug build, where the override exists; release builds compile it out.
    func testTheOverrideInADebugBuild() {
        let defaults = scratchDefaults()
        XCTAssertNil(DevelopmentOverride.installSource(defaults: defaults), "unset")
        defaults.set("  ", forKey: DevelopmentOverride.key)
        XCTAssertNil(DevelopmentOverride.installSource(defaults: defaults), "blank")
        defaults.set("~/src/voiced/dist/voiced-0.2.0-py3-none-any.whl", forKey: DevelopmentOverride.key)
        XCTAssertEqual(
            DevelopmentOverride.installSource(defaults: defaults)?.path,
            FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("src/voiced/dist/voiced-0.2.0-py3-none-any.whl").path)
    }

    // MARK: - The icon

    func testTheIconsImagesAreTemplates() {
        for state in [MenuBarIconState.idle, .busy, .error] {
            let image = MenuBarIconImage.image(for: state)
            XCTAssertTrue(image.isTemplate, "\(state)")
            XCTAssertGreaterThan(image.size.width, 0)
        }
        XCTAssertGreaterThan(MenuBarIconImage.image(for: .busy).size.width, MenuBarIconImage.image(for: .idle).size.width)
    }
}
