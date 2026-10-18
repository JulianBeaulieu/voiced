import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

@MainActor
final class InstallerTests: XCTestCase {
    private var home: URL!
    private var daemons: URL!
    private var socket: String!
    private var runner: FakeInstallRunner!
    private var servers: [FakeControlServer] = []

    override func setUp() async throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        daemons = try launchDaemonsDirectory()
        socket = temporarySocketPath()
        runner = FakeInstallRunner()
        resources = try installResources(in: home.appendingPathComponent("bundle"))
    }

    override func tearDown() async throws {
        models.forEach { $0.stop() }
        servers.forEach { $0.stop() }
        try? FileManager.default.removeItem(at: home)
        try? FileManager.default.removeItem(at: daemons)
    }

    private var resources: InstallResources!
    private var capacity: Int64? = 100_000_000_000
    private var models: [AppModel] = []

    private func makeInstaller(
        startupTimeout: Duration = .seconds(3), stallTimeout: Duration = .seconds(60)
    ) -> Installer {
        let probe = InstallProbe(launchDaemonsDirectory: daemons)
        var configuration = InstallerConfiguration(resources: resources, home: home, installProbe: probe)
        configuration.startupTimeout = startupTimeout
        configuration.stallTimeout = stallTimeout
        configuration.pollInterval = .milliseconds(20)
        configuration.hostIdentity = { HostIdentity(host: "studio.local", address: "192.0.2.10") }
        let capacity = capacity
        configuration.availableCapacity = { _ in capacity }
        let model = AppModel(
            socketPath: socket, installProbe: probe, backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)),
            opener: { _ in true })
        models.append(model)
        return Installer(configuration: configuration, runner: runner, model: model)
    }

    @discardableResult
    private func startServer(_ scenario: FakeScenario = .running, configure: (inout FakeControlServer.Options) -> Void = { _ in }) throws -> FakeControlServer {
        var options = FakeControlServer.Options()
        options.restartDuration = 0.1
        configure(&options)
        let server = FakeControlServer(socketPath: socket, scenario: scenario, options: options)
        try server.start()
        servers.append(server)
        return server
    }

    private func reach(_ installer: Installer, _ description: String = "phase", _ condition: (InstallerPhase) -> Bool) async {
        let reached = await eventually { condition(installer.phase) }
        XCTAssertTrue(reached, "\(description) not reached; phase is \(installer.phase)")
    }

    private func failure(_ installer: Installer) async -> InstallFailure? {
        await reach(installer, "failure") { if case .failed = $0 { true } else { false } }
        if case .failed(let failure) = installer.phase { return failure }
        return nil
    }

    private func reachApproval(_ installer: Installer) async {
        await reach(installer, "approval") { if case .approval = $0 { true } else { false } }
    }

    private func environmentNames() -> [String] {
        let envs = home.appendingPathComponent(".voiced/envs")
        return ((try? FileManager.default.contentsOfDirectory(atPath: envs.path)) ?? []).sorted()
    }

    private func installedPlist(for user: String) throws {
        try writePlist(
            ["ProgramArguments": ["/Users/\(user)/.voiced/bin/voiced", "watchdog"], "UserName": user],
            to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
    }

    // MARK: - The whole flow

    func testTheWholeFlow() async throws {
        let server = try startServer()
        // A fresh install listens on this Mac only, without discovery.
        server.update { $0.settings.zeroconf = false }
        let installer = makeInstaller()
        XCTAssertEqual(installer.phase, .welcome)
        XCTAssertFalse(installer.welcome.resuming)
        XCTAssertEqual(installer.welcome.downloadBytes, 3_068_800_000)
        XCTAssertEqual(installer.welcome.availableBytes, 100_000_000_000)

        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(installer.phase, .approval(firewallOn: false))

        // The environment, with the bundled uv and its pins, then voiced install from it.
        let uv = runner.commands(named: "uv")
        XCTAssertEqual(uv.map { Array($0.arguments.prefix(2)) }, [["python", "install"], ["venv", "--managed-python"], ["pip", "install"], ["pip", "install"]])
        XCTAssertEqual(uv[0].arguments, ["python", "install", "--no-bin", "3.12.15"])
        let partial = try XCTUnwrap(uv[1].arguments.last)
        XCTAssertTrue(partial.hasSuffix(".partial"))
        XCTAssertEqual(uv[1].arguments.dropLast(), ["venv", "--managed-python", "--relocatable", "--python", "3.12.15"])
        XCTAssertEqual(
            uv[2].arguments,
            ["pip", "install", "--python", partial + "/bin/python", "--no-deps", "--require-hashes", "-r", resources.lock.path])
        XCTAssertEqual(uv[3].arguments.last, resources.wheel.path)
        XCTAssertEqual(uv[0].environment["UV_NO_CONFIG"], "1")
        XCTAssertEqual(uv[0].environment["UV_LINK_MODE"], "clone")
        XCTAssertEqual(uv[0].environment["UV_PYTHON_INSTALL_DIR"], home.appendingPathComponent(".voiced/python").path)
        XCTAssertEqual(
            uv[0].environment["UV_CACHE_DIR"], home.appendingPathComponent("Library/Application Support/voiced/uv-cache").path)
        let id = URL(fileURLWithPath: partial).deletingPathExtension().lastPathComponent
        XCTAssertNotNil(id.range(of: #"^\d{8}T\d{6}$"#, options: .regularExpression))
        XCTAssertEqual(environmentNames(), [id])
        let voiced = try XCTUnwrap(runner.commands(named: "voiced").first)
        XCTAssertEqual(voiced.executable, home.appendingPathComponent(".voiced/envs/\(id)/bin/voiced").path)
        XCTAssertEqual(voiced.arguments, ["install", "--origin", "app", "--json-progress"])
        // The install lock is released for voiced install, which takes it itself.
        guard case .acquired(let lock) = InstallLock.tryAcquire(home.appendingPathComponent(".voiced")) else {
            return XCTFail("the install lock is still held")
        }
        lock.release()

        installer.approve()
        await reach(installer, "Connect Home Assistant") { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.privilegedCalls, ["install \(NSUserName())"])

        installer.connectHomeAssistant(true)
        await reach(installer, "connected") { if case .homeAssistant(.connected) = $0 { true } else { false } }
        let sets = server.requests.filter { $0.op == "settings.set" }.map(\.args)
        XCTAssertEqual(sets, [["bind": "lan"], ["zeroconf": true]])
        XCTAssertTrue(server.requests.contains { $0.op == "service.restart" })
        guard case .homeAssistant(.connected(let info)) = installer.phase else { return XCTFail() }
        XCTAssertEqual(info, HomeAssistantInfo(
            identity: HostIdentity(host: "studio.local", address: "192.0.2.10"), sttPort: 10300, ttsPort: 10200, apiKey: nil))

        installer.generateAPIKey()
        await reach(installer, "key") { if case .homeAssistant(.connected(let info)) = $0 { info.apiKey != nil } else { false } }
        guard case .homeAssistant(.connected(let keyed)) = installer.phase, let key = keyed.apiKey else { return XCTFail() }
        XCTAssertEqual(key.count, 43)
        XCTAssertEqual(server.requests.last { $0.op == "settings.set" }?.args, ["api_key": .string(key)])

        installer.continueToDone()
        XCTAssertEqual(installer.phase, .done)
        XCTAssertNil(installer.errorMessage)
        XCTAssertNil(installer.warning)
    }

    func testNoToHomeAssistantChangesNothing() async throws {
        let server = try startServer()
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        installer.connectHomeAssistant(false)
        XCTAssertEqual(installer.phase, .done)
        XCTAssertFalse(server.requests.contains { $0.op == "settings.set" })
    }

    // MARK: - Welcome

    func testNotEnoughDiskDisablesInstall() {
        capacity = 2_000_000_000
        let installer = makeInstaller()
        XCTAssertFalse(installer.welcome.hasRoom)
        XCTAssertEqual(installer.welcome.availableBytes, 2_000_000_000)
        installer.install()
        XCTAssertEqual(installer.phase, .welcome)
        XCTAssertTrue(runner.commands.isEmpty)
    }

    func testUnknownFreeSpaceDoesNotBlock() {
        capacity = nil
        XCTAssertTrue(makeInstaller().welcome.hasRoom)
    }

    func testAnotherAccountsInstallIsRefused() throws {
        try installedPlist(for: "root")
        let installer = makeInstaller()
        XCTAssertEqual(installer.phase, .refused("voiced is already installed for root on this Mac."))
        installer.install()
        XCTAssertTrue(runner.commands.isEmpty)
    }

    func testAnOrphanedInstallIsNotARefusal() throws {
        try installedPlist(for: "voiced-deleted-account")
        XCTAssertEqual(makeInstaller().phase, .welcome)
    }

    // MARK: - App quit mid-install

    func testAnInterruptedEnvironmentIsContinuedAndItsPartialRemoved() async throws {
        let envs = home.appendingPathComponent(".voiced/envs")
        try FileManager.default.createDirectory(at: envs.appendingPathComponent("20260101T000000.partial"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: envs.appendingPathComponent("20250101T000000"), withIntermediateDirectories: true)
        // The interrupted run claimed the folder before it built anything.
        try Data(#"{"source": "app", "environment": null}"#.utf8).write(to: manifest)
        let installer = makeInstaller()
        XCTAssertTrue(installer.welcome.resuming)
        installer.install()
        await reachApproval(installer)
        let names = environmentNames()
        XCTAssertFalse(names.contains("20260101T000000.partial"))
        // A complete environment is kept; voiced install prunes what it no longer needs.
        XCTAssertTrue(names.contains("20250101T000000"))
        XCTAssertEqual(names.count, 2)
    }

    func testInstalledPlistsLeaveOnlyStarting() async throws {
        try installedPlist(for: NSUserName())
        try startServer()
        let installer = makeInstaller()
        XCTAssertTrue(installer.welcome.resuming)
        installer.install()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertTrue(runner.commands.isEmpty)
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
    }

    func testCancelTerminatesTheRunningCommand() async throws {
        runner.respond = { command in
            command.arguments.first == "python" ? .init([], hangs: true) : FakeInstallRunner.defaultResponse(command)
        }
        let installer = makeInstaller()
        installer.install()
        await reach(installer) { $0 == .environment(step: "Installing Python 3.12.15") }
        installer.cancel()
        let terminated = await eventually { self.runner.terminated.count == 1 }
        XCTAssertTrue(terminated)
        XCTAssertEqual(runner.terminated.first?.arguments.first, "python")
    }

    // MARK: - Environment failures

    func testNoNetworkStopsBeforeTheFirstDownload() async throws {
        runner.isOnline = false
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure, InstallFailure(stage: .environment, step: "Checking the connection", cause: "Can't reach the internet."))
        XCTAssertTrue(runner.commands.isEmpty)

        runner.isOnline = true
        installer.retry()
        await reachApproval(installer)
    }

    func testAFailedUVStepShowsItsErrorAndRetriesCleanly() async throws {
        var fail = true
        runner.respond = { command in
            if command.arguments.prefix(2) == ["pip", "install"], command.arguments.contains("-r"), fail {
                return .init([.stdout("Resolved 83 packages"), .stderr("error: Failed to download `numpy`"), .exit(2)])
            }
            return FakeInstallRunner.defaultResponse(command)
        }
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(
            failure,
            InstallFailure(
                stage: .environment, step: "Installing packages", cause: "error: Failed to download `numpy`",
                output: ["Resolved 83 packages", "error: Failed to download `numpy`"]))
        let left = environmentNames()
        XCTAssertEqual(left.count, 1)
        XCTAssertTrue(left[0].hasSuffix(".partial"))

        fail = false
        installer.retry()
        await reachApproval(installer)
        // The stopped build's .partial is gone; only the new environment is left.
        XCTAssertEqual(environmentNames().count, 1)
        XCTAssertFalse(environmentNames()[0].hasSuffix(".partial"))
    }

    func testUVThatCannotStart() async throws {
        runner.respond = { _ in .init([], cannotStart: true) }
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.step, "Installing Python 3.12.15")
        XCTAssertTrue(failure?.cause.hasPrefix("Cannot run \(resources.uv.path)") == true)
    }

    // MARK: - Claiming ~/.voiced

    private var manifest: URL { home.appendingPathComponent(".voiced/install.json") }

    func testAFreshStateDirectoryIsClaimedBeforeAnythingIsBuilt() async throws {
        var claimedFirst: Bool?
        runner.respond = { command in
            if claimedFirst == nil { claimedFirst = FileManager.default.fileExists(atPath: self.manifest.path) }
            return FakeInstallRunner.defaultResponse(command)
        }
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(claimedFirst, true)
        let claimed = try JSONSerialization.jsonObject(with: Data(contentsOf: manifest)) as? [String: Any]
        XCTAssertEqual(claimed?["source"] as? String, "app")
        XCTAssertTrue(claimed?["environment"] is NSNull)
    }

    func testAWatchdogsSocketAndFindersFileDoNotBlockTheClaim() async throws {
        let state = home.appendingPathComponent(".voiced")
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        for name in ["control.sock", ".DS_Store"] {
            FileManager.default.createFile(atPath: state.appendingPathComponent(name).path, contents: Data())
        }
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertTrue(FileManager.default.fileExists(atPath: manifest.path))
    }

    func testAnExistingManifestIsLeftAsItIs() async throws {
        let state = home.appendingPathComponent(".voiced")
        try FileManager.default.createDirectory(at: state.appendingPathComponent("envs/20250101T000000"), withIntermediateDirectories: true)
        let existing = Data(#"{"source": "homebrew", "environment": "/opt/homebrew/opt/voiced/libexec"}"#.utf8)
        try existing.write(to: manifest)
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(try Data(contentsOf: manifest), existing)
    }

    func testSomeoneElsesFolderIsNotClaimed() async throws {
        let state = home.appendingPathComponent(".voiced")
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: state.appendingPathComponent("notes.txt").path, contents: Data())
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .environment)
        XCTAssertEqual(
            failure?.cause,
            "\(state.path) is not voiced's folder: it has no install.json and holds notes.txt. Move those elsewhere, then try again.")
        XCTAssertTrue(runner.commands.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: manifest.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: state.appendingPathComponent("envs").path))
    }

    func testASymlinkedStateDirectoryIsRefused() async throws {
        let elsewhere = home.appendingPathComponent("elsewhere")
        try FileManager.default.createDirectory(at: elsewhere, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: home.appendingPathComponent(".voiced"), withDestinationURL: elsewhere)
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        let target = elsewhere.resolvingSymlinksInPath().path
        XCTAssertEqual(
            failure?.cause,
            "\(home.appendingPathComponent(".voiced").path) is a symbolic link to \(target). voiced needs a real folder there; move \(target) to \(home.appendingPathComponent(".voiced").path), then try again.")
        XCTAssertTrue(runner.commands.isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: elsewhere.path), [])
    }

    /// A venv at `path` with voiced installed, as `voiced install` made one before ADR 0017.
    private func makeVoicedVenv(at path: URL, editable: Bool = false) throws {
        let info = path.appendingPathComponent("lib/python3.12/site-packages/voiced-0.1.0.dist-info")
        try FileManager.default.createDirectory(at: info, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: path.appendingPathComponent("pyvenv.cfg").path, contents: Data())
        if editable {
            try Data(#"{"url": "file:///src", "dir_info": {"editable": true}}"#.utf8)
                .write(to: info.appendingPathComponent("direct_url.json"))
        }
    }

    func testALegacyVenvIsLeftAsItIs() async throws {
        try makeVoicedVenv(at: home.appendingPathComponent(".voiced"))
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertFalse(FileManager.default.fileExists(atPath: manifest.path))
    }

    func testAnEditableVenvIsSomeoneElses() async throws {
        try makeVoicedVenv(at: home.appendingPathComponent(".voiced"), editable: true)
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertTrue(failure?.cause.contains("is not voiced's folder") == true)
    }

    func testAWheelThatIsNotThePinnedOneIsRefused() async throws {
        try Data("another wheel".utf8).write(to: resources.wheel)
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, "\(resources.wheel.lastPathComponent) does not match the app's manifest. Download the app again.")
        XCTAssertTrue(runner.commands.isEmpty)
    }

    // MARK: - Continuing

    /// A complete environment of this app's wheel, as an interrupted run leaves it.
    @discardableResult
    private func completeEnvironment(_ name: String, version: String = "0.1.0") throws -> URL {
        let environment = home.appendingPathComponent(".voiced/envs/\(name)")
        let bin = environment.appendingPathComponent("bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: bin.appendingPathComponent("voiced").path, contents: Data(), attributes: [.posixPermissions: 0o755])
        try FileManager.default.createDirectory(
            at: environment.appendingPathComponent("lib/python3.12/site-packages/voiced-\(version).dist-info"),
            withIntermediateDirectories: true)
        try Data(#"{"source": "app", "environment": null}"#.utf8).write(to: manifest)
        return environment
    }

    func testContinueRunsVoicedInstallInTheEnvironmentAlreadyBuilt() async throws {
        try completeEnvironment("20250101T000000", version: "0.0.9")
        let built = try completeEnvironment("20260101T000000")
        let installer = makeInstaller()
        XCTAssertTrue(installer.welcome.resuming)
        installer.install()
        await reachApproval(installer)
        XCTAssertTrue(runner.commands(named: "uv").isEmpty)
        XCTAssertEqual(runner.commands(named: "voiced").map(\.executable), [built.appendingPathComponent("bin/voiced").path])
    }

    func testContinueFollowsTheEntryPoint() async throws {
        let linked = try completeEnvironment("20250101T000000")
        try completeEnvironment("20260101T000000")
        let bin = home.appendingPathComponent(".voiced/bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            atPath: bin.appendingPathComponent("voiced").path, withDestinationPath: linked.appendingPathComponent("bin/voiced").path)
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(runner.commands(named: "voiced").map(\.executable), [linked.appendingPathComponent("bin/voiced").path])
    }

    func testAnotherVersionsEnvironmentIsNotContinued() async throws {
        try completeEnvironment("20250101T000000", version: "0.0.9")
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(runner.commands(named: "uv").count, 4)
    }

    func testWaitsWhileAnotherInstallHoldsTheLock() async throws {
        let state = home.appendingPathComponent(".voiced")
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
        guard case .acquired(let held) = InstallLock.tryAcquire(state) else { return XCTFail() }
        let installer = makeInstaller()
        installer.install()
        await reach(installer) { $0 == .environment(step: "Waiting for another voiced install to finish") }
        XCTAssertTrue(runner.commands.isEmpty)
        held.release()
        await reachApproval(installer)
    }

    // MARK: - voiced install failures

    private func installerWithProgress(_ response: FakeInstallRunner.Response, stallTimeout: Duration = .seconds(60)) -> Installer {
        runner.respond = { command in
            command.arguments.first == "install" ? response : FakeInstallRunner.defaultResponse(command)
        }
        return makeInstaller(stallTimeout: stallTimeout)
    }

    func testAPortInUseNamesTheStepAndTheHolder() async throws {
        let installer = installerWithProgress(.progress(
            [
                RecordedProgress.hello, RecordedProgress.step("ports", "start"),
                RecordedProgress.error("ports", "Port 10300 is in use by python3.12 (pid 4242)."),
            ], status: 1))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(
            failure,
            InstallFailure(stage: .install, step: "Checking the ports", cause: "Port 10300 is in use by python3.12 (pid 4242)."))
        // Only a download failure names the manual download.
        XCTAssertNil(failure?.fallback)
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
    }

    func testAnInterruptedDownloadResumesInTheSameEnvironment() async throws {
        var attempts = 0
        runner.respond = { command in
            guard command.arguments.first == "install" else { return FakeInstallRunner.defaultResponse(command) }
            attempts += 1
            if attempts == 1 {
                return .progress(RecordedProgress.downloading + [RecordedProgress.error("models", "Connection reset.")], status: 1)
            }
            return .progress(RecordedProgress.success)
        }
        let installer = makeInstaller()
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .install)
        XCTAssertEqual(failure?.step, "Downloading the models")
        XCTAssertEqual(failure?.cause, "Connection reset.")
        XCTAssertEqual(failure?.fallback, resources.manifest.manualDownload)
        let built = runner.commands(named: "uv").count

        installer.retry()
        await reachApproval(installer)
        XCTAssertEqual(runner.commands(named: "uv").count, built)
        XCTAssertEqual(Set(runner.commands(named: "voiced").map(\.executable)).count, 1)
    }

    func testAStalledDownloadIsStoppedAndOffersTryAgain() async throws {
        let installer = installerWithProgress(
            .init(RecordedProgress.downloading.map(ProcessOutput.stdout), hangs: true), stallTimeout: .milliseconds(200))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.step, "Downloading the models")
        XCTAssertTrue(failure?.cause.hasPrefix("The download stalled") == true)
        XCTAssertEqual(failure?.fallback, resources.manifest.manualDownload)
        XCTAssertTrue(failure?.fallback?.contains("\(RecordedProgress.parakeet) at revision 0a1b2c3") == true)
        let terminated = await eventually { self.runner.terminated.count == 1 }
        XCTAssertTrue(terminated)
    }

    func testANewerProgressProtocolIsRefused() async throws {
        let installer = installerWithProgress(
            .init([.stdout(#"{"event": "hello", "protocol": 2, "version": "9.0"}"#)], hangs: true))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, InstallEvent.refusal(protocol: 2))
        XCTAssertTrue(failure?.cause.hasSuffix("Update the app.") == true)
        let terminated = await eventually { self.runner.terminated.count == 1 }
        XCTAssertTrue(terminated)
    }

    func testAnOlderProgressProtocolIsRefused() async throws {
        let installer = installerWithProgress(.progress([#"{"event": "hello", "protocol": 0}"#, RecordedProgress.done]))
        installer.install()
        let failure = await failure(installer)
        XCTAssertTrue(failure?.cause.hasSuffix("Update voiced.") == true)
    }

    func testAVoicedWithoutJSONProgress() async throws {
        let installer = installerWithProgress(.init([.stderr("voiced: error: unrecognized arguments: --json-progress"), .exit(2)]))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, "This voiced does not report install progress. Update voiced.")
        XCTAssertEqual(failure?.output, ["voiced: error: unrecognized arguments: --json-progress"])
    }

    func testAnExitWithoutDone() async throws {
        let installer = installerWithProgress(.progress([RecordedProgress.hello], status: 0))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, "voiced install ended without reporting done.")
    }

    func testAVoicedThatCannotStart() async throws {
        let installer = installerWithProgress(.init([], cannotStart: true))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .install)
    }

    // MARK: - Approval

    func testTheFirewallStateIsShownBeforeThePrompt() async throws {
        runner.respond = { command in
            command.executable.hasSuffix("socketfilterfw")
                ? .init([.stdout("Firewall is enabled. (State = 1)"), .exit(0)]) : FakeInstallRunner.defaultResponse(command)
        }
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        XCTAssertEqual(installer.phase, .approval(firewallOn: true))
    }

    func testACancelledPromptGoesBackWithoutAnError() async throws {
        runner.privilegedOutcomes = [.cancelled]
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .approval(firewallOn: false) && self.runner.privilegedCalls.count == 1 }
        XCTAssertNil(installer.errorMessage)
    }

    func testAFailedBootstrapNamesTheService() async throws {
        runner.privilegedOutcomes = [
            .finished(status: 3, output: "failed io.github.julianbeaulieu.voiced.tts\rbootstrap failed: io.github.julianbeaulieu.voiced.tts")
        ]
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .approval)
        XCTAssertEqual(failure?.cause, "macOS did not start text to speech.")
        XCTAssertEqual(failure?.output.count, 2)

        // Try again asks again; the script replaces the plists.
        try startServer()
        installer.retry()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.privilegedCalls.count, 2)
    }

    func testAnotherAccountFoundByTheScriptIsARefusal() async throws {
        runner.privilegedOutcomes = [.finished(status: 1, output: "refused: voiced is already installed for alex on this Mac")]
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .refused("voiced is already installed for alex on this Mac.") }
    }

    func testOtherRefusalsAreFailures() async throws {
        runner.privilegedOutcomes = [.finished(status: 1, output: "refused: another voiced install or uninstall is running")]
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, "Refused: another voiced install or uninstall is running.")
    }

    func testFirewallProblemsAreWarningsNotFailures() async throws {
        try startServer()
        for (status, phrase) in [(Int32(4), "could not allow"), (5, "outside your home folder")] {
            runner.privilegedOutcomes = [.finished(status: status, output: "")]
            let installer = makeInstaller()
            installer.install()
            await reachApproval(installer)
            installer.approve()
            await reach(installer) { $0 == .homeAssistant(.asking) }
            XCTAssertTrue(installer.warning?.contains(phrase) == true, "\(status): \(installer.warning ?? "none")")
            installer.cancel()
        }
    }

    // MARK: - Starting

    func testNoHelloInTimeFailsAndTryAgainWaitsAgain() async throws {
        let installer = makeInstaller(startupTimeout: .milliseconds(300))
        installer.install()
        await reachApproval(installer)
        installer.approve()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .starting)
        XCTAssertTrue(failure?.cause.hasPrefix("voiced did not answer within") == true)
        XCTAssertEqual(failure?.output.count, 1)

        try startServer()
        installer.retry()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.privilegedCalls.count, 1)
    }

    func testAnIncompatibleWatchdogIsAFailure() async throws {
        try startServer(.newerProtocol)
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        let failure = await failure(installer)
        XCTAssertTrue(failure?.cause.hasSuffix("Update the menu bar app.") == true)
    }

    // MARK: - Home Assistant

    func testARefusedChangeStaysOnTheQuestion() async throws {
        try startServer { $0.failingOps = ["settings.set"] }
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        installer.connectHomeAssistant(true)
        let refused = await eventually { installer.errorMessage != nil && installer.phase == .homeAssistant(.asking) }
        XCTAssertTrue(refused)
    }

    func testARefusedRestartIsShown() async throws {
        try startServer { $0.refusedRestarts = [.stt: "stt is not installed", .tts: "tts is not installed"] }
        let installer = makeInstaller()
        installer.install()
        await reachApproval(installer)
        installer.approve()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        installer.connectHomeAssistant(true)
        let refused = await eventually { installer.errorMessage?.hasPrefix("Not restarted") == true }
        XCTAssertTrue(refused)
    }

    func testRandomKeysDiffer() {
        let key = Installer.randomKey()
        XCTAssertNotEqual(key, Installer.randomKey())
        XCTAssertNil(key.rangeOfCharacter(from: CharacterSet(charactersIn: "+/=")))
    }
}
