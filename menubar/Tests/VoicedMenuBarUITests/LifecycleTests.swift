import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

/// ADR 0018's Lifecycle: Update, Roll back, Repair and Uninstall of an install the app made,
/// with the fake runner, a temporary home and the fake control server. The fake `voiced
/// install` does what the real one does to the layout: it links its environment and rewrites
/// `install.json` with the one it replaced as `previous`.
@MainActor
final class LifecycleTests: XCTestCase {
    private var home: URL!
    private var daemons: URL!
    private var socket: String!
    private var runner: FakeInstallRunner!
    private var resources: InstallResources!
    private var servers: [FakeControlServer] = []
    private var models: [AppModel] = []
    /// The environment the app's previous update made: what the services run at the start.
    private var current: URL!

    private var state: URL { home.appendingPathComponent(".voiced") }
    private var envs: URL { state.appendingPathComponent("envs") }
    private var user: String { NSUserName() }

    override func setUp() async throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        daemons = try launchDaemonsDirectory(watchdogUser: NSUserName())
        socket = temporarySocketPath()
        runner = FakeInstallRunner()
        resources = try installResources(in: home.appendingPathComponent("bundle"))
        current = try makeEnvironment("20261001T120000", version: "0.0.9")
        try writeRecord(environment: current, version: "0.0.9")
        runner.respond = { [unowned self] command in respond(command) }
    }

    override func tearDown() async throws {
        models.forEach { $0.stop() }
        servers.forEach { $0.stop() }
        try? FileManager.default.removeItem(at: home)
        try? FileManager.default.removeItem(at: daemons)
    }

    // MARK: - The fakes

    /// What the next `voiced install` links: its environment gets this version and Python.
    private var installedVersion = "0.1.0"
    private var installedPython = "3.12.15"
    /// The lock hash the next `voiced install` records; by default the app's for its version.
    private var installedLock: String?
    /// The watchdog restarts itself when the entry point moves; off, it keeps its old hello
    /// until `voiced restart all`.
    private var watchdogFollowsTheLink = true
    /// `voiced restart all` brings up services that answer with the current install.json.
    private var restartBringsServicesUp = true
    private var checkResponse = FakeInstallRunner.Response.success
    private var installResponse: FakeInstallRunner.Response?
    private var uninstallResponse = FakeInstallRunner.Response.success

    private func respond(_ command: InstallCommand) -> FakeInstallRunner.Response {
        let name = URL(fileURLWithPath: command.executable).lastPathComponent
        switch (name, command.arguments.first) {
        case ("bash", _):
            return checkResponse
        case ("voiced", "install"):
            if let installResponse { return installResponse }
            let environment = URL(fileURLWithPath: command.executable).deletingLastPathComponent().deletingLastPathComponent()
            try? populate(environment, version: installedVersion, python: installedPython)
            let before = InstallRecord.read(stateDirectory: state)
            let previous = before?.environment == environment.path ? before?.previous : before?.environment
            try? writeRecord(
                environment: environment, version: installedVersion, previous: previous.map(URL.init(fileURLWithPath:)),
                lock: installedLock)
            if watchdogFollowsTheLink { servers.forEach { $0.dropConnections() } }
            return .progress(RecordedProgress.success)
        case ("voiced", "restart"):
            if restartBringsServicesUp {
                servers.forEach { $0.stop() }
                _ = try? startServer(following: true)
                return .success
            }
            return .init([.stderr("restart failed: no control socket"), .exit(1)])
        case ("voiced", "uninstall"):
            return uninstallResponse
        default:
            return FakeInstallRunner.defaultResponse(command)
        }
    }

    /// The services' control socket. `following` reads install.json for each hello, as the
    /// watchdog does; otherwise the hello keeps what install.json said when it started.
    @discardableResult
    private func startServer(following: Bool = true) throws -> FakeControlServer {
        var options = FakeControlServer.Options()
        options.restartDuration = 0.1
        if following {
            options.installFile = state.appendingPathComponent("install.json")
        } else {
            options.install = try JSONValue(data: Data(contentsOf: state.appendingPathComponent("install.json")))
        }
        let server = FakeControlServer(socketPath: socket, options: options)
        try server.start()
        servers.append(server)
        return server
    }

    private func makeInstaller(startupTimeout: Duration = .seconds(3)) -> Installer {
        let probe = InstallProbe(launchDaemonsDirectory: daemons)
        var configuration = InstallerConfiguration(resources: resources, home: home, installProbe: probe)
        configuration.startupTimeout = startupTimeout
        configuration.pollInterval = .milliseconds(20)
        configuration.availableCapacity = { _ in 100_000_000_000 }
        let model = AppModel(
            socketPath: socket, installProbe: probe, backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)),
            opener: { _ in true })
        models.append(model)
        return Installer(configuration: configuration, runner: runner, model: model)
    }

    /// An environment as uv builds it: `bin/voiced`, `bin/python` leading to a uv-managed
    /// Python under `~/.voiced/python`, voiced's dist-info and `pyvenv.cfg`.
    @discardableResult
    private func makeEnvironment(_ name: String, version: String, python: String = "3.12.15") throws -> URL {
        let environment = envs.appendingPathComponent(name, isDirectory: true)
        try populate(environment, version: version, python: python)
        return environment
    }

    private func populate(_ environment: URL, version: String, python: String) throws {
        let fileManager = FileManager.default
        let bin = environment.appendingPathComponent("bin")
        try fileManager.createDirectory(at: bin, withIntermediateDirectories: true)
        let voiced = bin.appendingPathComponent("voiced")
        try Data("#!/bin/sh\n".utf8).write(to: voiced)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: voiced.path)
        let pythonHome = pythonDirectory(python).appendingPathComponent("bin")
        try fileManager.createDirectory(at: pythonHome, withIntermediateDirectories: true)
        let interpreter = pythonHome.appendingPathComponent("python3.12")
        try Data("python".utf8).write(to: interpreter)
        let link = bin.appendingPathComponent("python")
        try? fileManager.removeItem(at: link)
        try fileManager.createSymbolicLink(at: link, withDestinationURL: interpreter)
        try fileManager.createDirectory(
            at: environment.appendingPathComponent("lib/python3.12/site-packages/voiced-\(version).dist-info"),
            withIntermediateDirectories: true)
        try Data("home = \(pythonHome.path)\nversion_info = \(python)\n".utf8)
            .write(to: environment.appendingPathComponent("pyvenv.cfg"))
    }

    private func pythonDirectory(_ version: String) -> URL {
        state.appendingPathComponent("python/cpython-\(version)-macos-aarch64-none", isDirectory: true)
    }

    /// install.json for an app install of `environment`, with `bin/voiced` linked to it.
    private func writeRecord(
        environment: URL, version: String, previous: URL? = nil, source: String = "app", lock: String? = nil
    ) throws {
        let python = environment.appendingPathComponent("bin/python").resolvingSymlinksInPath().path
        var record: [String: Any] = [
            "source": source, "environment": environment.path, "version": version,
            "lock_sha256": lock ?? (version == resources.manifest.version ? resources.manifest.lockSHA256 : "old"),
            "python": python,
        ]
        record["previous"] = previous?.path ?? NSNull()
        try JSONSerialization.data(withJSONObject: record).write(to: state.appendingPathComponent("install.json"))
        let entry = state.appendingPathComponent("bin/voiced")
        try FileManager.default.createDirectory(at: entry.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: entry)
        try FileManager.default.createSymbolicLink(at: entry, withDestinationURL: environment.appendingPathComponent("bin/voiced"))
    }

    private var record: InstallRecord? { InstallRecord.read(stateDirectory: state) }

    private func reach(_ installer: Installer, _ description: String = "phase", _ condition: (InstallerPhase) -> Bool) async {
        let reached = await eventually(timeout: .seconds(10)) { condition(installer.phase) }
        XCTAssertTrue(reached, "\(description) not reached; phase is \(installer.phase)")
    }

    private func finished(_ installer: Installer) async {
        await reach(installer, "finished") { if case .finished = $0 { true } else { false } }
    }

    private func failure(_ installer: Installer) async -> InstallFailure? {
        await reach(installer, "failure") { if case .failed = $0 { true } else { false } }
        if case .failed(let failure) = installer.phase { return failure }
        return nil
    }

    private func environmentNames() -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: envs.path)) ?? []).sorted()
    }

    private var checks: [InstallCommand] { runner.commands(named: "bash") }

    // MARK: - Update

    func testUpdateBuildsBesideTheCurrentEnvironmentAndKeepsIt() async throws {
        try startServer()
        let installer = makeInstaller()
        XCTAssertTrue(installer.managesInstall)
        installer.update()
        await finished(installer)
        XCTAssertEqual(installer.phase, .finished(version: "0.1.0"))
        XCTAssertEqual(installer.task, .update)

        // Steps 1 and 2: a new environment from the bundle, and its own voiced install.
        let uv = runner.commands(named: "uv")
        XCTAssertEqual(uv.map { Array($0.arguments.prefix(2)) }.prefix(4), [["python", "install"], ["venv", "--managed-python"], ["pip", "install"], ["pip", "install"]])
        let names = environmentNames()
        XCTAssertEqual(names.count, 2)
        XCTAssertTrue(names.contains(current.lastPathComponent), "the previous environment stays for Roll back")
        let built = envs.appendingPathComponent(try XCTUnwrap(names.first { $0 != current.lastPathComponent }))
        let install = try XCTUnwrap(runner.commands(named: "voiced").first)
        XCTAssertEqual(install.executable, built.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(install.arguments, ["install", "--origin", "app", "--json-progress"])
        XCTAssertEqual(record?.environment, built.path)
        XCTAssertEqual(record?.previous, current.path)

        // Step 3: the check as the user with a fixed environment; the plists match, so no prompt.
        let check = try XCTUnwrap(checks.first)
        XCTAssertEqual(check.executable, "/bin/bash")
        XCTAssertEqual(Array(check.arguments.suffix(3)), ["voiced-privileged", "check", user])
        XCTAssertEqual(check.arguments[3], PrivilegedScript.text)
        XCTAssertFalse(check.inheritsEnvironment)
        XCTAssertEqual(check.environment, PrivilegedScript.environment)
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
        // Step 4 came without a restart; then the uv cache is cleared.
        XCTAssertFalse(runner.commands.contains { $0.arguments.first == "restart" })
        XCTAssertEqual(uv.last?.arguments, ["cache", "clean"])
        XCTAssertEqual(uv.last?.environment["UV_CACHE_DIR"], home.appendingPathComponent("Library/Application Support/voiced/uv-cache").path)

        XCTAssertEqual(installer.rollbackTarget(), RollbackTarget(environment: current, version: "0.0.9"))
        XCTAssertFalse(installer.offersRollback, "only a failed update offers it on the failure screen")
    }

    func testUpdateAsksForTheAdministratorOnlyWhenAPlistDiffers() async throws {
        try startServer()
        checkResponse = .init([.stdout("differs io.github.julianbeaulieu.voiced.tts"), .exit(1)])
        let installer = makeInstaller()
        installer.update()
        await reach(installer, "approval") { $0 == .approval(firewallOn: false) }
        XCTAssertTrue(runner.privilegedCalls.isEmpty, "the prompt waits for the button")
        installer.approve()
        await finished(installer)
        XCTAssertEqual(runner.privilegedCalls, ["install \(user)"])
    }

    func testANotLoadedLabelAlsoNeedsTheAdministrator() async throws {
        try startServer()
        checkResponse = .init([.stdout("not loaded io.github.julianbeaulieu.voiced.watchdog"), .exit(1)])
        let installer = makeInstaller()
        installer.repair()
        await reach(installer, "approval") { if case .approval = $0 { true } else { false } }
    }

    func testANewPythonWithTheFirewallOnFollowsTheException() async throws {
        try startServer()
        installedPython = "3.13.9"
        let base = runner.respond
        runner.respond = { command in
            if command.executable.hasSuffix("socketfilterfw") {
                return .init([.stdout("Firewall is enabled. (State = 1)"), .exit(0)])
            }
            return base(command)
        }
        let installer = makeInstaller()
        installer.update()
        await reach(installer, "approval") { $0 == .approval(firewallOn: true) }
        installer.approve()
        await finished(installer)
        XCTAssertEqual(runner.privilegedCalls, ["install \(user)"])
    }

    func testANewPythonWithTheFirewallOffNeedsNoPrompt() async throws {
        try startServer()
        installedPython = "3.13.9"
        let installer = makeInstaller()
        installer.update()
        await finished(installer)
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
        XCTAssertEqual(runner.commands(named: "socketfilterfw").count, 1)
    }

    func testWithoutAHelloFromTheNewEnvironmentItRestartsAll() async throws {
        try startServer(following: false)
        watchdogFollowsTheLink = false
        let installer = makeInstaller(startupTimeout: .milliseconds(500))
        installer.update()
        await finished(installer)
        let restart = try XCTUnwrap(runner.commands.first { $0.arguments.first == "restart" })
        XCTAssertEqual(restart.executable, state.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(restart.arguments, ["restart", "all"])
    }

    func testAnUpdateWhoseServicesDoNotComeUpOffersRollBack() async throws {
        try startServer(following: false)
        watchdogFollowsTheLink = false
        restartBringsServicesUp = false
        let installer = makeInstaller(startupTimeout: .milliseconds(300))
        installer.update()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .starting)
        XCTAssertEqual(failed?.output.last, "voiced restart all: restart failed: no control socket")
        XCTAssertTrue(installer.offersRollback)
        let built = try XCTUnwrap(record?.environment)

        // Roll back: the previous environment's own voiced install links it again.
        restartBringsServicesUp = true
        watchdogFollowsTheLink = true
        installedVersion = "0.0.9"
        installer.rollBack()
        await finished(installer)
        XCTAssertEqual(installer.task, .rollBack)
        XCTAssertEqual(installer.phase, .finished(version: "0.0.9"))
        XCTAssertEqual(runner.commands(named: "voiced").last { $0.arguments.first == "install" }?.executable,
            current.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(record?.environment, current.path)
        XCTAssertEqual(record?.previous, built, "rolling back again would go forward")
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 1, "nothing is built")
    }

    /// The new environment's hello must carry this app's lock too.
    func testAnUpdateWaitsForThePinsNotOnlyThePath() async throws {
        try startServer()
        installedLock = "something else"
        let installer = makeInstaller(startupTimeout: .milliseconds(300))
        installer.update()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .starting)
    }

    /// A command-line install pruned the environment the update built: Try Again builds again.
    func testTryAgainBuildsAgainWhenTheEnvironmentWasPruned() async throws {
        try startServer()
        installResponse = .progress([RecordedProgress.hello, RecordedProgress.error("models", "no network")], status: 1)
        let installer = makeInstaller()
        installer.update()
        _ = await failure(installer)
        for name in environmentNames() where name != current.lastPathComponent {
            try FileManager.default.removeItem(at: envs.appendingPathComponent(name))
        }
        installResponse = nil
        installer.retry()
        await finished(installer)
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 2)
    }

    func testAFailedInstallInAnUpdateOffersOnlyTryAgain() async throws {
        try startServer()
        installResponse = .progress([RecordedProgress.hello, RecordedProgress.error("models", "no network")], status: 1)
        let installer = makeInstaller()
        installer.update()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .install)
        XCTAssertEqual(record?.environment, current.path, "nothing the services run changed")
        XCTAssertFalse(installer.offersRollback)

        installResponse = nil
        installer.retry()
        await finished(installer)
        let installs = runner.commands(named: "voiced").filter { $0.arguments.first == "install" }
        XCTAssertEqual(installs.count, 2)
        XCTAssertEqual(installs[0].executable, installs[1].executable, "Try Again reuses the environment it built")
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 1)
    }

    func testAnUpdateWithoutTheNetworkStopsBeforeBuilding() async throws {
        runner.isOnline = false
        let installer = makeInstaller()
        installer.update()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.cause, "Can't reach the internet.")
        XCTAssertEqual(environmentNames(), [current.lastPathComponent])
    }

    func testACheckThatRefusesIsAFailure() async throws {
        try startServer()
        checkResponse = .init([.stderr("refused: bad home directory for \(user)"), .exit(1)])
        let installer = makeInstaller()
        installer.update()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .approval)
        XCTAssertEqual(failed?.cause, "Refused: bad home directory for \(user).")
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
        checkResponse = .success
        installer.retry()
        await finished(installer)
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 1, "the check again, not the build")
    }

    func testACancelledPromptInAnUpdateGoesBackToApproval() async throws {
        try startServer()
        checkResponse = .init([.stdout("differs io.github.julianbeaulieu.voiced.stt"), .exit(1)])
        runner.privilegedOutcomes = [.cancelled]
        let installer = makeInstaller()
        installer.update()
        await reach(installer, "approval") { if case .approval = $0 { true } else { false } }
        installer.approve()
        let asked = await eventually { runner.privilegedCalls.count == 1 }
        XCTAssertTrue(asked)
        await reach(installer, "approval again") { $0 == .approval(firewallOn: false) }
    }

    func testAFailedBootstrapInAnUpdateNamesTheService() async throws {
        try startServer()
        checkResponse = .init([.stdout("differs io.github.julianbeaulieu.voiced.stt"), .exit(1)])
        runner.privilegedOutcomes = [.finished(status: 3, output: "failed io.github.julianbeaulieu.voiced.tts")]
        let installer = makeInstaller()
        installer.update()
        await reach(installer, "approval") { if case .approval = $0 { true } else { false } }
        installer.approve()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.cause, "macOS did not start text to speech.")
    }

    func testUpdateRemovesThePythonsNoEnvironmentUses() async throws {
        try startServer()
        let unused = pythonDirectory("3.12.9")
        try FileManager.default.createDirectory(at: unused.appendingPathComponent("bin"), withIntermediateDirectories: true)
        installedPython = "3.13.9"
        let installer = makeInstaller()
        installer.update()
        await finished(installer)
        XCTAssertFalse(FileManager.default.fileExists(atPath: unused.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: pythonDirectory("3.12.15").path), "the previous environment's")
        XCTAssertTrue(FileManager.default.fileExists(atPath: pythonDirectory("3.13.9").path), "the new environment's")
    }

    // MARK: - Roll back

    func testRollBackNeedsAKeptAppEnvironment() throws {
        let installer = makeInstaller()
        XCTAssertNil(installer.rollbackTarget(), "no previous recorded")
        let elsewhere = home.appendingPathComponent("elsewhere/20250101T000000")
        try populate(elsewhere, version: "0.0.8", python: "3.12.15")
        try writeRecord(environment: current, version: "0.0.9", previous: elsewhere)
        XCTAssertNil(installer.rollbackTarget(), "a whole environment, but outside envs/")
        try writeRecord(environment: current, version: "0.0.9", previous: current)
        XCTAssertNil(installer.rollbackTarget(), "the active one")
        try writeRecord(environment: current, version: "0.0.9", previous: envs.appendingPathComponent("20250101T000000"))
        XCTAssertNil(installer.rollbackTarget(), "pruned")
        let older = try makeEnvironment("20250101T000000", version: "0.0.8", python: "3.12.8")
        XCTAssertEqual(installer.rollbackTarget(), RollbackTarget(environment: older, version: "0.0.8"))
        try FileManager.default.removeItem(at: pythonDirectory("3.12.8"))
        XCTAssertNil(installer.rollbackTarget(), "its Python is gone")
        installer.rollBack()
        XCTAssertTrue(runner.commands.isEmpty)
    }

    // MARK: - Repair

    func testRepairReinstallsIntoTheActiveEnvironment() async throws {
        // The services do not answer until restarted.
        let installer = makeInstaller()
        installer.repair()
        await finished(installer)
        XCTAssertEqual(installer.task, .repair)
        let uv = runner.commands(named: "uv")
        let interpreter = current.appendingPathComponent("bin/python").path
        XCTAssertEqual(uv.map(\.arguments), [
            ["pip", "install", "--python", interpreter, "--reinstall", "--no-deps", "--require-hashes", "-r", resources.lock.path],
            ["pip", "install", "--python", interpreter, "--reinstall", "--no-deps", resources.wheel.path],
        ])
        XCTAssertEqual(runner.commands(named: "voiced").map(\.arguments.first), ["install", "restart"])
        XCTAssertEqual(runner.commands(named: "voiced").first?.executable, current.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(checks.count, 1)
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
        XCTAssertEqual(environmentNames(), [current.lastPathComponent])
        XCTAssertNil(record?.previous, "a repair in place replaces nothing")
    }

    /// A repair in place moves no link, so nothing else would restart services that answer
    /// but run broken code: the restart comes before the wait.
    func testRepairRestartsBeforeItWaits() async throws {
        let server = try startServer()
        let installer = makeInstaller()
        installer.repair()
        await finished(installer)
        XCTAssertEqual(runner.commands(named: "voiced").map(\.arguments.first), ["install", "restart"])
        XCTAssertTrue(server.requests.isEmpty || !server.requests.contains { $0.op == "service.restart" })
    }

    /// A half-finished install whose only environment was moved aside builds a fresh one
    /// instead of resuming the broken one.
    func testRepairNeverResumesABrokenEnvironment() async throws {
        try Data(#"{"source": "app", "environment": null}"#.utf8).write(to: state.appendingPathComponent("install.json"))
        try FileManager.default.removeItem(at: state.appendingPathComponent("bin/voiced"))
        try FileManager.default.removeItem(at: current)
        try makeEnvironment("20261018T120000.broken", version: resources.manifest.version)
        let installer = makeInstaller()
        installer.repair()
        await finished(installer)
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 1)
        XCTAssertFalse(runner.commands(named: "voiced").contains { $0.executable.contains(".broken") })
    }

    func testRepairReinstallsMissingDaemons() async throws {
        checkResponse = .init([.stdout("differs io.github.julianbeaulieu.voiced.watchdog"), .exit(1)])
        let installer = makeInstaller()
        installer.repair()
        await reach(installer, "approval") { if case .approval = $0 { true } else { false } }
        installer.approve()
        await finished(installer)
        XCTAssertEqual(runner.privilegedCalls, ["install \(user)"])
    }

    func testRepairOfAnEnvironmentWhosePythonIsGoneBuildsAFreshOne() async throws {
        try FileManager.default.removeItem(at: pythonDirectory("3.12.15"))
        let installer = makeInstaller()
        installer.repair()
        await finished(installer)
        let names = environmentNames()
        XCTAssertTrue(names.contains(current.lastPathComponent + ".broken"), "\(names)")
        XCTAssertFalse(names.contains(current.lastPathComponent))
        XCTAssertEqual(runner.commands(named: "uv").filter { $0.arguments.first == "venv" }.count, 1)
        let built = try XCTUnwrap(record?.environment)
        XCTAssertTrue(built.hasPrefix(envs.path) && !built.hasSuffix(".broken"))
        XCTAssertNil(installer.rollbackTarget(), "the broken one is no rollback")
    }

    func testRepairOfAnInstallStoppedBeforeLinkingResumesItsEnvironment() async throws {
        try Data(#"{"source": "app", "environment": null}"#.utf8).write(to: state.appendingPathComponent("install.json"))
        try FileManager.default.removeItem(at: state.appendingPathComponent("bin/voiced"))
        let resumable = try makeEnvironment("20261018T120000", version: resources.manifest.version)
        let installer = makeInstaller()
        installer.repair()
        await finished(installer)
        XCTAssertTrue(runner.commands(named: "uv").isEmpty)
        XCTAssertEqual(runner.commands(named: "voiced").first?.executable, resumable.appendingPathComponent("bin/voiced").path)
    }

    func testAFailedReinstallIsAnEnvironmentFailure() async throws {
        let base = runner.respond
        runner.respond = { command in
            if command.arguments.contains("--reinstall") { return .init([.stderr("error: hash mismatch"), .exit(2)]) }
            return base(command)
        }
        let installer = makeInstaller()
        installer.repair()
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .environment)
        XCTAssertEqual(failed?.cause, "error: hash mismatch")
        XCTAssertTrue(runner.commands(named: "voiced").isEmpty)
    }

    func testRepairWaitsForTheInstallLock() async throws {
        guard case .acquired(let lock) = InstallLock.tryAcquire(state) else { return XCTFail("lock") }
        let installer = makeInstaller()
        installer.repair()
        await reach(installer, "waiting") { $0 == .environment(step: "Waiting for another voiced install to finish") }
        XCTAssertTrue(runner.commands.isEmpty)
        lock.release()
        await finished(installer)
    }

    // MARK: - Uninstall

    func testUninstallRemovesTheServicesThenTheFiles() async throws {
        let cache = home.appendingPathComponent("Library/Application Support/voiced/uv-cache")
        try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
        let installer = makeInstaller()
        installer.uninstall(UninstallChoices())
        XCTAssertTrue(runner.commands.isEmpty, "not without the confirmation")
        installer.confirmUninstall()
        XCTAssertEqual(installer.phase, .confirmUninstall)
        let choices = UninstallChoices(models: true, logs: false, config: true)
        installer.uninstall(choices)
        await reach(installer, "uninstalled") { $0 == .uninstalled(choices) }
        XCTAssertEqual(runner.privilegedCalls, ["uninstall \(user)"])
        let remove = try XCTUnwrap(runner.commands(named: "voiced").first)
        XCTAssertEqual(remove.executable, state.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(remove.arguments, ["uninstall", "--yes", "--remove-models", "--remove-config"])
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.path))

        installer.windowClosed()
        XCTAssertEqual(installer.phase, .welcome)
        XCTAssertEqual(installer.task, .install)
    }

    func testACancelledUninstallPromptGoesBack() async throws {
        runner.privilegedOutcomes = [.cancelled]
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        let asked = await eventually { runner.privilegedCalls.count == 1 }
        XCTAssertTrue(asked)
        await reach(installer, "confirmation") { $0 == .confirmUninstall }
        XCTAssertTrue(runner.commands.isEmpty)
    }

    func testAFailedPrivilegedUninstallKeepsTheFiles() async throws {
        runner.privilegedOutcomes = [.finished(status: 1, output: "refused: another voiced install or uninstall is running")]
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .uninstall)
        XCTAssertEqual(failed?.cause, "Refused: another voiced install or uninstall is running.")
        XCTAssertTrue(runner.commands.isEmpty)
        installer.retry()
        XCTAssertEqual(installer.phase, .confirmUninstall, "Try Again asks again")
    }

    func testAFailedFileRemovalShowsItsOutput() async throws {
        uninstallResponse = .init([.stderr("Daemons still loaded: stt. Run the sudo script first, then"), .exit(1)])
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        let failed = await failure(installer)
        XCTAssertEqual(failed?.stage, .uninstall)
        XCTAssertEqual(failed?.cause, "Daemons still loaded: stt. Run the sudo script first, then")
    }

    /// No voiced left to run `voiced uninstall`: the services go, then the app removes the
    /// state directory by the same rules and says the models, logs and config stayed.
    func testAnEnvironmentTooBrokenToUninstallIsStillRemoved() async throws {
        try FileManager.default.removeItem(at: pythonDirectory("3.12.15"))
        try makeEnvironment("20261018T120000.partial", version: "0.1.0", python: "3.12.16")
        let notVoiceds = envs.appendingPathComponent("notes")
        try FileManager.default.createDirectory(at: notVoiceds, withIntermediateDirectories: true)
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices(models: true, logs: true, config: true))
        await reach(installer, "uninstalled") { $0 == .uninstalled(UninstallChoices()) }
        XCTAssertEqual(runner.privilegedCalls, ["uninstall \(user)"])
        XCTAssertTrue(runner.commands(named: "voiced").isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: state.path), ["envs"])
        XCTAssertEqual(environmentNames(), ["notes"], "what voiced did not build stays")
    }

    /// Try Again after the services were removed: no second prompt.
    func testAnUninstallAfterTheServicesWentAsksNoAdministrator() async throws {
        try FileManager.default.removeItem(at: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.watchdog.plist"))
        try FileManager.default.removeItem(at: pythonDirectory("3.12.15"))
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        await reach(installer, "uninstalled") { if case .uninstalled = $0 { true } else { false } }
        XCTAssertTrue(runner.privilegedCalls.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: state.path))
    }

    /// The active environment is broken but the rollback one runs: it removes the files, with
    /// the boxes ticked.
    func testUninstallUsesAnyVoicedThatStillRuns() async throws {
        let older = try makeEnvironment("20250101T000000", version: "0.0.8", python: "3.12.8")
        try FileManager.default.removeItem(at: pythonDirectory("3.12.15"))
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices(models: true))
        await reach(installer, "uninstalled") { $0 == .uninstalled(UninstallChoices(models: true)) }
        let remove = try XCTUnwrap(runner.commands(named: "voiced").first)
        XCTAssertEqual(remove.executable, older.appendingPathComponent("bin/voiced").path)
        XCTAssertEqual(remove.arguments, ["uninstall", "--yes", "--remove-models"])
    }

    /// The app's own removal refuses a state directory that is a link, one without
    /// `install.json`, and one whose `install.json` does not parse, and deletes nothing.
    func testTheAppsRemovalRefusesWhatIsNotVoiceds() async throws {
        let fileManager = FileManager.default
        let stages = InstallStages(configuration: makeInstallerConfiguration(), runner: runner)
        let everything = try fileManager.subpathsOfDirectory(atPath: state.path).sorted()

        let target = home.appendingPathComponent("target", isDirectory: true)
        try fileManager.moveItem(at: state, to: target)
        try fileManager.createSymbolicLink(at: state, withDestinationURL: target)
        guard case .failure(let linked) = await stages.removeStateDirectory() else { return XCTFail("removed through a link") }
        XCTAssertTrue(linked.cause.contains("is not a folder"), linked.cause)
        XCTAssertEqual(try fileManager.subpathsOfDirectory(atPath: target.path).sorted(), everything)
        try fileManager.removeItem(at: state)
        try fileManager.moveItem(at: target, to: state)

        let record = state.appendingPathComponent("install.json")
        for contents in [nil, Data("{not json".utf8), Data(#"{"source": "someone"}"#.utf8)] {
            try? fileManager.removeItem(at: record)
            if let contents { try contents.write(to: record) }
            guard case .failure(let refused) = await stages.removeStateDirectory() else {
                return XCTFail("removed without a readable install.json")
            }
            XCTAssertTrue(refused.cause.contains("no readable install.json"), refused.cause)
            XCTAssertTrue(fileManager.fileExists(atPath: envs.appendingPathComponent(current.lastPathComponent).path))
            XCTAssertTrue(fileManager.fileExists(atPath: pythonDirectory("3.12.15").path))
        }
    }

    /// A new environment never takes an id whose `.partial` or `.broken` is there.
    func testANewEnvironmentNeverTakesAMovedOrUnfinishedID() throws {
        let stages = InstallStages(configuration: makeInstallerConfiguration(), runner: runner)
        let date = Date(timeIntervalSince1970: 1_792_368_000)  // 2026-10-19T00:00:00Z
        try FileManager.default.createDirectory(at: envs.appendingPathComponent("20261019T000000.broken"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: envs.appendingPathComponent("20261019T000000-2.partial"), withIntermediateDirectories: true)
        XCTAssertEqual(stages.newEnvironmentDirectory(at: date).lastPathComponent, "20261019T000000-3")
        try FileManager.default.createDirectory(at: envs.appendingPathComponent("20261019T000000-3"), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(
            at: envs.appendingPathComponent("20261019T000000-4"), withDestinationURL: home.appendingPathComponent("gone"))
        XCTAssertEqual(stages.newEnvironmentDirectory(at: date).lastPathComponent, "20261019T000000-5")
    }

    /// Two builds in the same second, the first's environment deleted since: the second must
    /// not take its name while `install.json` still names it, as the active environment or as
    /// the rollback target.
    func testANewEnvironmentNeverTakesAnIDInstallJSONStillNames() throws {
        let stages = InstallStages(configuration: makeInstallerConfiguration(), runner: runner)
        let date = Date(timeIntervalSince1970: 1_792_454_400)  // 2026-10-20T00:00:00Z
        let first = stages.newEnvironmentDirectory(at: date)
        XCTAssertEqual(first.lastPathComponent, "20261020T000000")
        try writeRecord(environment: first, version: "0.1.0", previous: current)
        XCTAssertEqual(stages.newEnvironmentDirectory(at: date).lastPathComponent, "20261020T000000-2")

        let second = envs.appendingPathComponent("20261020T000000-2", isDirectory: true)
        try writeRecord(environment: second, version: "0.1.0", previous: first)
        XCTAssertEqual(stages.newEnvironmentDirectory(at: date).lastPathComponent, "20261020T000000-3")

        // Unreadable, it names nothing, as for `install.plan_install`.
        try Data("{not json".utf8).write(to: state.appendingPathComponent("install.json"))
        XCTAssertEqual(stages.newEnvironmentDirectory(at: date).lastPathComponent, "20261020T000000")
    }

    /// The app's own removal never goes through a link.
    func testTheAppsRemovalDoesNotFollowLinks() async throws {
        let theirs = home.appendingPathComponent("theirs", isDirectory: true)
        try FileManager.default.createDirectory(at: theirs.appendingPathComponent("cpython-3.11.4/bin"), withIntermediateDirectories: true)
        try FileManager.default.removeItem(at: state.appendingPathComponent("python"))
        try FileManager.default.createSymbolicLink(at: state.appendingPathComponent("python"), withDestinationURL: theirs)
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        await reach(installer, "uninstalled") { if case .uninstalled = $0 { true } else { false } }
        XCTAssertTrue(FileManager.default.fileExists(atPath: theirs.appendingPathComponent("cpython-3.11.4").path))
    }

    func testAnotherAccountsUninstallIsRefused() async throws {
        runner.privilegedOutcomes = [.finished(status: 1, output: "refused: voiced is already installed for someone on this Mac")]
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        await reach(installer, "refused") { $0 == .refused("voiced is already installed for someone on this Mac.") }
    }

    // MARK: - Installs the app does not own

    func testHomebrewAndCommandLineInstallsAreOnlyShown() throws {
        for source in ["homebrew", "cli"] {
            try writeRecord(environment: current, version: "0.0.9", previous: current, source: source)
            let installer = makeInstaller()
            XCTAssertFalse(installer.managesInstall)
            installer.update()
            installer.repair()
            installer.rollBack()
            installer.confirmUninstall()
            XCTAssertEqual(installer.phase, .welcome, source)
            XCTAssertNil(installer.rollbackTarget())
            XCTAssertTrue(runner.commands.isEmpty && runner.privilegedCalls.isEmpty, source)
        }
        XCTAssertEqual(
            ExternalInstall.commands(for: .homebrew).map(\.command),
            ["brew upgrade voiced", "brew reinstall voiced", "voiced uninstall", "brew uninstall voiced"],
            "the services go before the formula")
        XCTAssertEqual(ExternalInstall.commands(for: .cli).map(\.command), ["voiced install --source <checkout>", "voiced uninstall"])
        XCTAssertTrue(ExternalInstall.commands(for: .app).isEmpty)
    }

    func testNothingStartsWhileARunIsWorking() async throws {
        runner.privilegedHangs = true
        let installer = makeInstaller()
        installer.confirmUninstall()
        installer.uninstall(UninstallChoices())
        await reach(installer, "uninstalling") { $0 == .uninstalling(step: "Removing the background services") }
        installer.update()
        installer.repair()
        XCTAssertEqual(installer.task, .uninstall)
        XCTAssertTrue(runner.commands.isEmpty)
        installer.cancel()
    }

    // MARK: - Pythons

    func testOnlyPythonsNoEnvironmentRunsOnAreRemoved() throws {
        let fileManager = FileManager.default
        let pythons = state.appendingPathComponent("python")
        let unused = pythonDirectory("3.12.9")
        try fileManager.createDirectory(at: unused, withIntermediateDirectories: true)
        // uv's minor-version links: one to a Python in use, one to the unused one.
        try fileManager.createSymbolicLink(
            at: pythons.appendingPathComponent("cpython-3.12-macos-aarch64-none"), withDestinationURL: pythonDirectory("3.12.15"))
        try fileManager.createSymbolicLink(
            at: pythons.appendingPathComponent("cpython-3.11-macos-aarch64-none"), withDestinationURL: unused)
        try fileManager.createDirectory(at: pythons.appendingPathComponent(".cache"), withIntermediateDirectories: true)
        let stages = InstallStages(configuration: makeInstallerConfiguration(), runner: runner)
        stages.removeUnusedPythons()
        let left = try fileManager.contentsOfDirectory(atPath: pythons.path).sorted()
        XCTAssertEqual(left, [".cache", "cpython-3.12-macos-aarch64-none", "cpython-3.12.15-macos-aarch64-none"])

        // An environment whose home cannot be read could use any of them: nothing goes.
        try makeEnvironment("20261018T120000", version: "0.1.0", python: "3.13.9")
        try fileManager.removeItem(at: envs.appendingPathComponent("20261018T120000/pyvenv.cfg"))
        try fileManager.createDirectory(at: unused, withIntermediateDirectories: true)
        stages.removeUnusedPythons()
        XCTAssertTrue(fileManager.fileExists(atPath: unused.path))
    }

    /// `~/.voiced/python`, `envs` or `~/.voiced` itself as a link: the Pythons there are not
    /// voiced's, so none is removed.
    func testPythonsBehindALinkAreNeverRemoved() throws {
        let fileManager = FileManager.default
        let stages = InstallStages(configuration: makeInstallerConfiguration(), runner: runner)
        let theirs = home.appendingPathComponent("uv-python", isDirectory: true)
        try fileManager.createDirectory(at: theirs.appendingPathComponent("cpython-3.11.4-macos-aarch64-none/bin"), withIntermediateDirectories: true)
        let pythons = state.appendingPathComponent("python")
        try fileManager.moveItem(at: pythons, to: theirs.appendingPathComponent("moved"))
        try fileManager.createSymbolicLink(at: pythons, withDestinationURL: theirs)
        stages.removeUnusedPythons()
        XCTAssertTrue(fileManager.fileExists(atPath: theirs.appendingPathComponent("cpython-3.11.4-macos-aarch64-none").path))

        try fileManager.removeItem(at: pythons)
        try fileManager.createDirectory(at: pythons.appendingPathComponent("cpython-3.11.4-macos-aarch64-none"), withIntermediateDirectories: true)
        let realEnvs = home.appendingPathComponent("real-envs")
        try fileManager.moveItem(at: envs, to: realEnvs)
        try fileManager.createSymbolicLink(at: envs, withDestinationURL: realEnvs)
        stages.removeUnusedPythons()
        XCTAssertTrue(fileManager.fileExists(atPath: pythons.appendingPathComponent("cpython-3.11.4-macos-aarch64-none").path))

        try fileManager.removeItem(at: envs)
        try fileManager.moveItem(at: realEnvs, to: envs)
        let linked = home.appendingPathComponent("linked-state")
        try fileManager.moveItem(at: state, to: linked)
        try fileManager.createSymbolicLink(at: state, withDestinationURL: linked)
        stages.removeUnusedPythons()
        XCTAssertTrue(fileManager.fileExists(atPath: linked.appendingPathComponent("python/cpython-3.11.4-macos-aarch64-none").path))
    }

    private func makeInstallerConfiguration() -> InstallerConfiguration {
        InstallerConfiguration(resources: resources, home: home, installProbe: InstallProbe(launchDaemonsDirectory: daemons))
    }
}
