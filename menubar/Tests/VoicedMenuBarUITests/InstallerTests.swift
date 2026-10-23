import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

@MainActor
final class InstallerTests: XCTestCase {
    private var home: URL!
    private var agents: URL!
    private var daemons: URL!
    private var socket: String!
    private var runner: FakeInstallRunner!
    private var servers: [FakeControlServer] = []

    override func setUp() async throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        agents = try launchAgentsDirectory()
        daemons = try launchDaemonsDirectory()
        socket = temporarySocketPath()
        runner = FakeInstallRunner()
        resources = try installResources(in: home.appendingPathComponent("bundle"))
    }

    override func tearDown() async throws {
        models.forEach { $0.stop() }
        servers.forEach { $0.stop() }
        try? FileManager.default.removeItem(at: home)
        try? FileManager.default.removeItem(at: agents)
        try? FileManager.default.removeItem(at: daemons)
        UserDefaults.standard.removePersistentDomain(forName: defaultsSuite)
    }

    private var resources: InstallResources!
    /// The installer's own user defaults, thrown away after each test.
    private let defaultsSuite = "voiced-installer-tests-\(UUID().uuidString)"
    private let recommendedFallback =
        "To download the models by hand, fetch \(RecordedProgress.parakeet) at revision 0a1b2c3 and "
        + "\(RecordedProgress.kokoro) at revision 4d5e6f7 into the Hugging Face cache, then try again."
    private var capacity: Int64? = 100_000_000_000
    private var models: [AppModel] = []

    private func makeInstaller(
        startupTimeout: Duration = .seconds(3), stallTimeout: Duration = .seconds(60), installSource: URL? = nil
    ) -> Installer {
        let probe = InstallProbe(launchAgentsDirectory: agents, launchDaemonsDirectory: daemons)
        var configuration = InstallerConfiguration(resources: resources, home: home, installProbe: probe)
        configuration.installSource = installSource
        configuration.defaultsSuite = defaultsSuite
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

    /// `voiced install` is done and loaded the agents: the installer is on to Starting.
    private func reachInstalled(_ installer: Installer) async {
        await reach(installer, "starting") { [.starting, .homeAssistant(.asking)].contains($0) }
    }

    private func environmentNames() -> [String] {
        let envs = home.appendingPathComponent(".voiced/envs")
        return ((try? FileManager.default.contentsOfDirectory(atPath: envs.path)) ?? []).sorted()
    }

    /// A watchdog LaunchDaemon from before ADR 0035, run as `user`.
    private func legacyDaemon(for user: String) throws {
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
        XCTAssertEqual(installer.downloadBytes, 3_068_800_000)
        XCTAssertEqual(installer.welcome.availableBytes, 100_000_000_000)

        installer.install()
        await reach(installer, "Connect Home Assistant") { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.prompts, 0, "no administrator prompt anywhere")

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
        // The setup's choice, the recommended models: downloaded, then written at the switch.
        XCTAssertEqual(
            voiced.arguments,
            [
                "install", "--origin", "app", "--json-progress",
                "--set", "stt.enabled=true", "--set", "stt.engine=parakeet", "--set", "stt.model=\(RecordedProgress.parakeet)",
                "--set", "tts.enabled=true", "--set", "tts.engine=kokoro", "--set", "tts.model=\(RecordedProgress.kokoro)",
            ])
        // The install lock is released for voiced install, which takes it itself.
        guard case .acquired(let lock) = InstallLock.tryAcquire(home.appendingPathComponent(".voiced")) else {
            return XCTFail("the install lock is still held")
        }
        lock.release()

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
    }

    func testNoToHomeAssistantChangesNothing() async throws {
        let server = try startServer()
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
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

    func testAnotherAccountsOldInstallIsRefused() throws {
        try legacyDaemon(for: "root")
        let installer = makeInstaller()
        XCTAssertEqual(installer.phase, .refused("voiced is already installed for root on this Mac."))
        installer.install()
        XCTAssertTrue(runner.commands.isEmpty)
    }

    func testAnOrphanedInstallIsNotARefusal() throws {
        try legacyDaemon(for: "voiced-deleted-account")
        XCTAssertEqual(makeInstaller().phase, .welcome)
    }

    // MARK: - The setup

    /// The `--set` arguments of the `voiced install` the installer ran.
    private func installSettings() -> [String] {
        let arguments = runner.commands(named: "voiced").first?.arguments ?? []
        return zip(arguments, arguments.dropFirst()).filter { $0.0 == "--set" }.map(\.1)
    }

    func testTheSetupPreselectsTheRecommendedModelsAndOffersNoGatedOne() {
        let installer = makeInstaller()
        XCTAssertEqual(
            installer.setup,
            SetupChoice(
                stt: ServiceChoice(isOn: true, model: RecordedProgress.parakeet),
                tts: ServiceChoice(isOn: true, model: RecordedProgress.kokoro)))
        XCTAssertEqual(
            SetupChoice.options(in: installer.catalogue, for: .stt).flatMap(\.models).map(\.id),
            [RecordedProgress.parakeet, RecordedProgress.whisper])
        // The gated Phonon needs a token first; Settings offers it once voiced is installed.
        XCTAssertEqual(
            SetupChoice.options(in: installer.catalogue, for: .tts).flatMap(\.models).map(\.id),
            [RecordedProgress.kokoro, RecordedProgress.phonon])
    }

    func testAnotherModelAndAServiceOffAreWhatTheInstallWritesAndDownloads() async throws {
        let installer = makeInstaller()
        installer.setup.stt.model = RecordedProgress.whisper
        installer.setup.tts.isOn = false
        // The wheels and Whisper; nothing for text to speech.
        XCTAssertEqual(installer.downloadBytes, 205_000_000 + 885_000_000)
        installer.install()
        await reachInstalled(installer)
        XCTAssertEqual(
            installSettings(),
            ["stt.enabled=true", "stt.engine=whisper", "stt.model=\(RecordedProgress.whisper)", "tts.enabled=false"])
    }

    func testAnEnginesSharedFilesAreCountedOnce() {
        let installer = makeInstaller()
        installer.setup.tts.model = RecordedProgress.phonon
        XCTAssertEqual(
            installer.setup.downloads(in: installer.catalogue).map(\.id),
            [RecordedProgress.parakeet, RecordedProgress.phonon, "gradium/phonon-voices"])
        XCTAssertEqual(installer.downloadBytes, 205_000_000 + 2_508_500_000 + 65_000_000 + 9_500_000)
        XCTAssertEqual(
            installer.setup.settings(in: installer.catalogue).suffix(3),
            ["tts.enabled=true", "tts.engine=phonon", "tts.model=\(RecordedProgress.phonon)"])
    }

    func testBothServicesOffInstallsNothing() {
        let installer = makeInstaller()
        installer.setup.stt.isOn = false
        installer.setup.tts.isOn = false
        XCTAssertTrue(installer.setup.isEmpty)
        XCTAssertEqual(installer.downloadBytes, 205_000_000)
        installer.install()
        XCTAssertEqual(installer.phase, .welcome)
        XCTAssertTrue(runner.commands.isEmpty)
    }

    func testAFailedDownloadNamesTheChosenModelsForTheManualFallback() async throws {
        let installer = installerWithProgress(
            .progress(RecordedProgress.downloading + [RecordedProgress.error("models", "Connection reset.")], status: 1))
        installer.setup.stt.model = RecordedProgress.whisper
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(
            failure?.fallback,
            "To download the models by hand, fetch \(RecordedProgress.whisper) at revision 8e9f0a1 and "
                + "\(RecordedProgress.kokoro) at revision 4d5e6f7 into the Hugging Face cache, then try again.")
        // Try again keeps the choice.
        installer.retry()
        await reach(installer) { if case .failed = $0 { self.runner.commands(named: "voiced").count == 2 } else { false } }
        XCTAssertEqual(runner.commands(named: "voiced").map(\.arguments).last, runner.commands(named: "voiced").first?.arguments)
    }

    /// install-manifest.json as bundle.sh writes it, its catalogue cut from what
    /// `python -I -m voiced.models` printed (two engines of it).
    private static let writtenManifest = #"""
        {"version": "0.4.0", "python": "3.12.15", "lock_sha256": "ab",
         "wheel": {"name": "voiced-0.4.0-py3-none-any.whl", "sha256": "cd"}, "wheels_bytes": 1,
         "catalogue": {"engines": [
          {"name": "parakeet", "label": "Parakeet", "kind": "stt", "shared": [], "models": [
           {"id": "mlx-community/parakeet-tdt-0.6b-v3", "repo": "mlx-community/parakeet-tdt-0.6b-v3",
            "license": "CC-BY-4.0", "url": "https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3",
            "gated": false, "revision": "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15", "download_bytes": 2508532829,
            "quality": 5, "speed": 5, "note": "The default: accurate and fast, English and 24 European languages",
            "recommended": true}]},
          {"name": "phonon", "label": "Phonon", "kind": "tts", "models": [
           {"id": "gradium/phonon-31m-en", "repo": "gradium/phonon-31m-en", "license": "Apache-2.0",
            "url": "https://huggingface.co/gradium/phonon-31m-en", "gated": false,
            "revision": "8c64b55ec301728e2d1695bfea87a08a267d0c2d", "download_bytes": 65049306, "quality": 5,
            "speed": 5, "note": "Runs on the CPU, leaving the GPU to speech to text; 38 English voices",
            "recommended": false},
           {"id": "gradium/phonon-40m-en", "repo": "gradium/phonon-40m-en", "license": "CC-BY-NC-4.0",
            "url": "https://huggingface.co/gradium/phonon-40m-en", "gated": true,
            "revision": "9b95393286eaeeebf1330b2ae90a14a2074c415d", "download_bytes": 75090522, "quality": null,
            "speed": null, "note": "The larger English model, for non-commercial use; not benchmarked yet",
            "recommended": false}],
           "shared": [
           {"id": "gradium/phonon-voices", "repo": "gradium/phonon-voices", "license": "Apache-2.0",
            "url": "https://huggingface.co/gradium/phonon-voices", "gated": false,
            "revision": "da6c9ec0d47be8df5c87c730a3aa97187f3229c3", "download_bytes": 9510439, "quality": null,
            "speed": null, "note": "", "recommended": false}]}]}}
        """#

    func testTheManifestBundleShWritesDecodesIntoTheSetup() throws {
        let manifest = try JSONDecoder().decode(InstallManifest.self, from: Data(Self.writtenManifest.utf8))
        let choice = SetupChoice.recommended(in: manifest.catalogue)
        XCTAssertEqual(choice.stt, ServiceChoice(isOn: true, model: "mlx-community/parakeet-tdt-0.6b-v3"))
        // No recommended Phonon here: the first model that needs no token.
        XCTAssertEqual(choice.tts, ServiceChoice(isOn: true, model: "gradium/phonon-31m-en"))
        let request = InstallRequest(settings: [], models: choice.downloads(in: manifest.catalogue))
        XCTAssertEqual(
            request.manualDownload,
            "To download the models by hand, fetch mlx-community/parakeet-tdt-0.6b-v3 at revision "
                + "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15 and gradium/phonon-31m-en at revision "
                + "8c64b55ec301728e2d1695bfea87a08a267d0c2d and gradium/phonon-voices at revision "
                + "da6c9ec0d47be8df5c87c730a3aa97187f3229c3 into the Hugging Face cache, then try again.")
        XCTAssertEqual(manifest.catalogue.model("gradium/phonon-31m-en")?.model.ratings.map(\.value), [5, 5])
    }

    func testAFailedChoiceGoesBackToTheSetup() async throws {
        let installer = installerWithProgress(
            .progress([RecordedProgress.hello, RecordedProgress.error("models", "moonshine needs macOS 15 or later.")], status: 1))
        installer.setup.stt.model = RecordedProgress.whisper
        installer.install()
        _ = await failure(installer)
        XCTAssertTrue(installer.offersSetup)
        installer.changeChoices()
        XCTAssertEqual(installer.phase, .welcome)
        XCTAssertEqual(installer.setup.stt.model, RecordedProgress.whisper, "the choice is kept to change")

        // Closing the window on the failure goes back too.
        installer.install()
        _ = await failure(installer)
        installer.windowClosed()
        XCTAssertEqual(installer.phase, .welcome)
    }

    func testOtherFailuresDoNotOfferTheSetup() async throws {
        runner.isOnline = false
        let installer = makeInstaller()
        installer.install()
        _ = await failure(installer)
        XCTAssertFalse(installer.offersSetup)
    }

    func testClosingTheSetupUnusedKeepsItFromOpeningAtLaunch() {
        let installer = makeInstaller()
        XCTAssertTrue(installer.opensAtLaunch)
        installer.windowClosed()
        XCTAssertFalse(makeInstaller().opensAtLaunch, "the menu still offers it")
        // Another version of the app opens it again.
        UserDefaults(suiteName: defaultsSuite)?.set("0.0.9", forKey: "setupClosedBy")
        XCTAssertTrue(makeInstaller().opensAtLaunch)
    }

    func testNoPinnedModelMeansNoManualDownload() {
        let unpinned = CatalogueModel(id: "org/model", license: "MIT", downloadBytes: 1, downloaded: false)
        XCTAssertNil(InstallRequest(settings: [], models: [unpinned]).manualDownload)
    }

    func testHomeAssistantNamesOnlyTheServicesThatAreOn() async throws {
        let server = try startServer()
        server.update { $0.settings.tts.enabled = false }
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
        await reach(installer) { $0 == .homeAssistant(.asking) }
        installer.connectHomeAssistant(true)
        await reach(installer, "connected") { if case .homeAssistant(.connected) = $0 { true } else { false } }
        guard case .homeAssistant(.connected(let info)) = installer.phase else { return XCTFail() }
        XCTAssertEqual(info.sttPort, 10300)
        XCTAssertNil(info.ttsPort)
    }

    func testTheSetupOpensAtLaunchOnlyWhenNothingIsInstalled() throws {
        XCTAssertTrue(makeInstaller().opensAtLaunch)
        // The command line or an older app installed the services: the menu shows them.
        try writePlist(
            ["ProgramArguments": ["/Users/someone/.voiced/bin/voiced", "watchdog"]],
            to: agents.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        XCTAssertFalse(makeInstaller().opensAtLaunch)
        try FileManager.default.removeItem(at: agents.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        try legacyDaemon(for: NSUserName())
        XCTAssertFalse(makeInstaller().opensAtLaunch, "an install from 0.4.0 or earlier")
        try legacyDaemon(for: "root")
        XCTAssertFalse(makeInstaller().opensAtLaunch)
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
        await reachInstalled(installer)
        let names = environmentNames()
        XCTAssertFalse(names.contains("20260101T000000.partial"))
        // A complete environment is kept; voiced install prunes what it no longer needs.
        XCTAssertTrue(names.contains("20250101T000000"))
        XCTAssertEqual(names.count, 2)
    }

    func testInstalledAgentsLeaveOnlyStarting() async throws {
        agents = try launchAgentsDirectory(watchdog: true)
        try startServer()
        let installer = makeInstaller()
        XCTAssertTrue(installer.welcome.resuming)
        installer.install()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertTrue(runner.commands.isEmpty)
        XCTAssertEqual(runner.prompts, 0)
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
        await reachInstalled(installer)
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
        await reachInstalled(installer)
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
        await reachInstalled(installer)
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
        await reachInstalled(installer)
        XCTAssertTrue(FileManager.default.fileExists(atPath: manifest.path))
    }

    func testAnExistingManifestIsLeftAsItIs() async throws {
        let state = home.appendingPathComponent(".voiced")
        try FileManager.default.createDirectory(at: state.appendingPathComponent("envs/20250101T000000"), withIntermediateDirectories: true)
        let existing = Data(#"{"source": "homebrew", "environment": "/opt/homebrew/opt/voiced/libexec"}"#.utf8)
        try existing.write(to: manifest)
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
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
        await reachInstalled(installer)
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
        await reachInstalled(installer)
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
        await reachInstalled(installer)
        XCTAssertEqual(runner.commands(named: "voiced").map(\.executable), [linked.appendingPathComponent("bin/voiced").path])
    }

    func testAnotherVersionsEnvironmentIsNotContinued() async throws {
        try completeEnvironment("20250101T000000", version: "0.0.9")
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
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
        await reachInstalled(installer)
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
        XCTAssertEqual(runner.prompts, 0)
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
        XCTAssertEqual(failure?.fallback, recommendedFallback)
        let built = runner.commands(named: "uv").count

        installer.retry()
        await reachInstalled(installer)
        XCTAssertEqual(runner.commands(named: "uv").count, built)
        XCTAssertEqual(Set(runner.commands(named: "voiced").map(\.executable)).count, 1)
    }

    func testAStalledDownloadIsStoppedAndOffersTryAgain() async throws {
        let installer = installerWithProgress(
            .init(RecordedProgress.downloading.map(ProcessOutput.stdout), hangs: true), stallTimeout: .milliseconds(200))
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.step, "Downloading the models")
        XCTAssertEqual(
            failure?.cause,
            "The download of \(RecordedProgress.kokoro) stalled at \(ByteText.size(177_650_000)) of "
                + "\(ByteText.size(355_300_000)): no data for 0 seconds. Try again to resume it.")
        XCTAssertEqual(failure?.fallback, recommendedFallback)
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

    // MARK: - Migration from LaunchDaemons (ADR 0035)

    /// The services still run as this user's LaunchDaemons: the environment is built, then the
    /// one prompt left, then `voiced install`, which loads the agents.
    func testOldDaemonsAreRemovedBehindOnePromptBeforeVoicedInstall() async throws {
        try legacyDaemon(for: NSUserName())
        runner.onLegacyDaemonsRemoved = { [daemons] in
            try? FileManager.default.removeItem(at: daemons!.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        }
        try startServer()
        let installer = makeInstaller()
        installer.install()
        await reach(installer, "migration") { $0 == .migration }
        XCTAssertEqual(runner.prompts, 0, "the prompt waits for the button")
        XCTAssertTrue(runner.commands(named: "voiced").isEmpty, "voiced install waits for the daemons to go")
        XCTAssertFalse(environmentNames().isEmpty, "the environment is built first")
        XCTAssertFalse(installer.isWorking)

        installer.removeOldServices()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.prompts, 1)
        XCTAssertEqual(runner.commands(named: "voiced").count, 1)
    }

    func testACancelledPromptLeavesTheOldServicesRunning() async throws {
        try legacyDaemon(for: NSUserName())
        runner.privilegedOutcomes = [.cancelled]
        let installer = makeInstaller()
        installer.install()
        await reach(installer, "migration") { $0 == .migration }
        installer.removeOldServices()
        await reach(installer) { $0 == .migration && self.runner.prompts == 1 }
        XCTAssertTrue(runner.commands(named: "voiced").isEmpty)
        XCTAssertNil(installer.errorMessage)
    }

    func testAFailedRemovalIsAFailureAndTryAgainAsksAgain() async throws {
        try legacyDaemon(for: NSUserName())
        runner.privilegedOutcomes = [.finished(status: 1, output: "rm: Operation not permitted")]
        let installer = makeInstaller()
        installer.install()
        await reach(installer, "migration") { $0 == .migration }
        installer.removeOldServices()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .migration)
        XCTAssertEqual(failed?.output, ["rm: Operation not permitted"])
        installer.retry()
        await reach(installer) { _ in self.runner.prompts == 2 }
    }

    func testTheRemovalIsFixedCommandsWithNothingOfTheAppsEnvironment() {
        let command = LegacyDaemons.command
        XCTAssertEqual(command.executable, "/usr/bin/osascript")
        XCTAssertFalse(command.inheritsEnvironment)
        XCTAssertEqual(command.environment, ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        XCTAssertEqual(Array(command.arguments.suffix(2)), [LegacyDaemons.removal, LegacyDaemons.prompt])
        XCTAssertTrue(LegacyDaemons.prompt.contains("remove the old background services (the last time a password is needed)"))
        for label in BackgroundItems.labels {
            XCTAssertTrue(LegacyDaemons.removal.contains("/bin/launchctl bootout system/\(label) 2>/dev/null"))
            XCTAssertTrue(LegacyDaemons.removal.contains("/Library/LaunchDaemons/\(label).plist"))
        }
    }

    // MARK: - Starting

    func testNoHelloInTimeFailsAndTryAgainWaitsAgain() async throws {
        let installer = makeInstaller(startupTimeout: .milliseconds(300))
        installer.install()
        await reachInstalled(installer)
        let failure = await failure(installer)
        XCTAssertEqual(failure?.stage, .starting)
        XCTAssertTrue(failure?.cause.hasPrefix("voiced did not answer within") == true)
        XCTAssertEqual(failure?.output.count, 1)

        try startServer()
        installer.retry()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertEqual(runner.prompts, 0)
    }

    func testAnIncompatibleWatchdogIsAFailure() async throws {
        try startServer(.newerProtocol)
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
        let failure = await failure(installer)
        XCTAssertTrue(failure?.cause.hasSuffix("Update the menu bar app.") == true)
    }

    // MARK: - Home Assistant

    func testARefusedChangeStaysOnTheQuestion() async throws {
        try startServer { $0.failingOps = ["settings.set"] }
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
        await reach(installer) { $0 == .homeAssistant(.asking) }
        installer.connectHomeAssistant(true)
        let refused = await eventually { installer.errorMessage != nil && installer.phase == .homeAssistant(.asking) }
        XCTAssertTrue(refused)
    }

    func testARefusedRestartIsShown() async throws {
        try startServer { $0.refusedRestarts = [.stt: "stt is not installed", .tts: "tts is not installed"] }
        let installer = makeInstaller()
        installer.install()
        await reachInstalled(installer)
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

    // MARK: - The development override

    /// `unzip` as the fake runner plays it: the lock appears where it is asked to put it.
    private func playOverride(lock: Bool = true) {
        runner.respond = { command in
            guard command.executable == "/usr/bin/unzip" else { return FakeInstallRunner.defaultResponse(command) }
            let args = command.arguments
            guard lock, let i = args.firstIndex(of: "-d") else {
                return .init([.stderr("caution: filename not matched:  voiced/requirements.lock"), .exit(11)])
            }
            let target = URL(fileURLWithPath: args[i + 1]).appendingPathComponent("voiced")
            try? FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
            try? Data("numpy==2.0.0 --hash=sha256:11\n".utf8).write(to: target.appendingPathComponent("requirements.lock"))
            return .success
        }
    }

    private func devWheel(_ name: String = "voiced-0.3.0.dev1-py3-none-any.whl") throws -> URL {
        let wheel = home.appendingPathComponent("dist/\(name)")
        try FileManager.default.createDirectory(at: wheel.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("dev wheel".utf8).write(to: wheel)
        return wheel
    }

    func testAWheelOverrideIsInstalledWithTheLockItCarries() async throws {
        let wheel = try devWheel()
        // The bundled wheel no longer matching its manifest does not matter while overridden.
        try Data("another wheel".utf8).write(to: resources.wheel)
        playOverride()
        let installer = makeInstaller(installSource: wheel)
        installer.install()
        await reachInstalled(installer)

        let work = home.appendingPathComponent("Library/Application Support/voiced/install-source")
        XCTAssertEqual(
            runner.commands(named: "unzip").map(\.arguments),
            [["-o", "-q", wheel.path, "voiced/requirements.lock", "-d", work.path]])
        let pip = runner.commands(named: "uv").filter { $0.arguments.first == "pip" }
        XCTAssertEqual(pip.first?.arguments.last, work.appendingPathComponent("voiced/requirements.lock").path)
        XCTAssertEqual(pip.last?.arguments.last, wheel.path)
    }

    func testAnOverrideThatIsNotAVoicedWheelIsRefused() async throws {
        let checkout = home.appendingPathComponent("src/voiced")
        try FileManager.default.createDirectory(at: checkout, withIntermediateDirectories: true)
        let other = try devWheel("other-1.0-py3-none-any.whl")
        for source in [checkout, home.appendingPathComponent("missing-0.1.0-py3-none-any.whl"), other] {
            runner = FakeInstallRunner()
            let installer = makeInstaller(installSource: source)
            installer.install()
            let failure = await failure(installer)
            XCTAssertEqual(failure?.step, "Using the install source \(source.path)")
            XCTAssertEqual(failure?.cause, "It is not a voiced wheel. Build one with `uv build --wheel` in the checkout.")
            XCTAssertTrue(runner.commands.isEmpty)
            installer.cancel()
        }
    }

    func testAWheelWithoutALockIsRefused() async throws {
        let wheel = try devWheel()
        playOverride(lock: false)
        let installer = makeInstaller(installSource: wheel)
        installer.install()
        let failure = await failure(installer)
        XCTAssertEqual(failure?.cause, "caution: filename not matched:  voiced/requirements.lock")
        XCTAssertTrue(runner.commands(named: "uv").isEmpty)
        XCTAssertEqual(environmentNames(), [], "nothing built in ~/.voiced")
    }

    // MARK: - Busy

    func testTheInstallerIsBusyWhileItWorks() async throws {
        runner.respond = { $0.arguments.first == "python" ? .init([], hangs: true) : FakeInstallRunner.defaultResponse($0) }
        let installer = makeInstaller()
        XCTAssertFalse(installer.isWorking)
        installer.install()
        await reach(installer, "environment") { if case .environment = $0 { true } else { false } }
        XCTAssertTrue(installer.isWorking)
        installer.cancel()
    }

    func testWaitingOnTheUserIsNotBusy() async throws {
        try startServer()
        let installer = makeInstaller()
        installer.install()
        await reach(installer) { $0 == .homeAssistant(.asking) }
        XCTAssertFalse(installer.isWorking)
    }
}
