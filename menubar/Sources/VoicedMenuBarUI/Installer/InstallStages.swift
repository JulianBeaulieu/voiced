import Foundation
import VoicedControl

/// The steps of ADR 0018's Flow, each a function that does its work through the runner and
/// returns what it made or why it failed. `Installer` sequences them and turns their results
/// into screens; Update, Repair and Uninstall can sequence the same steps differently.
@MainActor
struct InstallStages {
    let configuration: InstallerConfiguration
    let runner: InstallRunner

    /// Checked before the first download.
    static let reachabilityChecks = ["https://pypi.org/simple/", "https://huggingface.co/"]
        .compactMap(URL.init(string:))

    // MARK: - Network

    func checkNetwork() async -> Result<Void, InstallFailure> {
        for url in Self.reachabilityChecks where !(await runner.isReachable(url)) {
            return .failure(InstallFailure(stage: .environment, step: "Checking the connection", cause: "Can't reach the internet."))
        }
        return .success(())
    }

    // MARK: - Environment

    /// An environment an interrupted run already built from this app's wheel: the one
    /// `bin/voiced` leads to, else the newest complete one in `envs/`. `voiced install` runs
    /// again there instead of building another.
    func resumableEnvironment() -> URL? {
        let entry = configuration.paths.entryPoint
        var candidates: [URL] = []
        if let linked = try? FileManager.default.destinationOfSymbolicLink(atPath: entry.path) {
            candidates.append(URL(fileURLWithPath: linked).deletingLastPathComponent().deletingLastPathComponent())
        }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: configuration.paths.environments.path)) ?? []
        candidates += names.filter { !$0.hasSuffix(".partial") && !$0.hasSuffix(".broken") }.sorted().reversed()
            .map { configuration.paths.environments.appendingPathComponent($0, isDirectory: true) }
        return candidates.first(where: hasThisAppsVoiced)
    }

    /// Inside `envs/`, with a `bin/voiced` and this app's wheel's version installed.
    private func hasThisAppsVoiced(_ environment: URL) -> Bool {
        isAppEnvironment(environment) && Self.version(in: environment) == configuration.resources.manifest.version
    }

    /// A directory in `envs/` with a `bin/voiced`: one the app may build in, repair or roll
    /// back to. Never the state directory itself (a legacy venv) or a Homebrew keg.
    func isAppEnvironment(_ environment: URL) -> Bool {
        let environment = environment.standardizedFileURL
        return environment.deletingLastPathComponent().path == configuration.paths.environments.standardizedFileURL.path
            && FileManager.default.isExecutableFile(atPath: environment.appendingPathComponent("bin/voiced").path)
    }

    /// Whether the environment's Python is still there: `bin/python` leads to a uv-managed
    /// interpreter, which can be removed under it.
    static func hasPython(_ environment: URL) -> Bool {
        FileManager.default.fileExists(atPath: environment.appendingPathComponent("bin/python").path)
    }

    /// voiced's version in `environment`, from its dist-info, as `layout.environment_version`
    /// reads it.
    static func version(in environment: URL) -> String? {
        let lib = environment.appendingPathComponent("lib")
        let fileManager = FileManager.default
        for python in ((try? fileManager.contentsOfDirectory(atPath: lib.path)) ?? []).sorted() {
            let site = lib.appendingPathComponent("\(python)/site-packages")
            let infos = (try? fileManager.contentsOfDirectory(atPath: site.path)) ?? []
            if let info = infos.sorted().first(where: { $0.hasPrefix("voiced-") && $0.hasSuffix(".dist-info") }) {
                return String(info.dropFirst("voiced-".count).dropLast(".dist-info".count))
            }
        }
        return nil
    }

    /// Claims `~/.voiced`, then builds `envs/<id>.partial` with the bundled uv and renames it
    /// once its packages are in, holding the install lock until then.
    func buildEnvironment(progress: (String) -> Void) async -> Result<URL, InstallFailure> {
        let state = configuration.paths.stateDirectory
        let preparing = "Preparing \(state.path)"
        func failure(_ cause: String) -> Result<URL, InstallFailure> {
            .failure(InstallFailure(stage: .environment, step: preparing, cause: cause))
        }
        let resources: InstallResources
        switch await installResources() {
        case .success(let found): resources = found
        case .failure(let failure): return .failure(failure)
        }
        let fileManager = FileManager.default
        do {
            try StateDirectory.checkNotSymlink(state)
            try fileManager.createDirectory(at: state, withIntermediateDirectories: true)
        } catch {
            return failure(error.localizedDescription)
        }
        guard let lock = await acquireLock(progress: progress) else { return failure("Cannot lock \(state.path).") }
        defer { lock.release() }
        do {
            try StateDirectory.claim(state)
            try fileManager.createDirectory(at: configuration.paths.environments, withIntermediateDirectories: true)
        } catch {
            return failure(error.localizedDescription)
        }
        // A .partial is only ever this app's, left by a run that was stopped.
        let names = (try? fileManager.contentsOfDirectory(atPath: configuration.paths.environments.path)) ?? []
        for name in names where name.hasSuffix(".partial") {
            try? fileManager.removeItem(at: configuration.paths.environments.appendingPathComponent(name))
        }
        let target = newEnvironmentDirectory()
        let partial = target.appendingPathExtension("partial")
        for (shown, command) in environmentCommands(building: partial, resources: resources) {
            progress(shown)
            if case .failure(let failure) = await execute(command, stage: .environment, step: shown) {
                return .failure(failure)
            }
        }
        do {
            try fileManager.moveItem(at: partial, to: target)
        } catch {
            return .failure(InstallFailure(stage: .environment, step: "Installing voiced", cause: error.localizedDescription))
        }
        return .success(target)
    }

    /// The wheel and lock to install: the app's own, checked against its manifest, or in a
    /// debug build the wheel the `installSource` override names (ADR 0018, "Open items"),
    /// with the lock it carries, so the two cannot disagree. Release builds never look at
    /// the override.
    func installResources() async -> Result<InstallResources, InstallFailure> {
        let bundled = configuration.resources
        #if DEBUG
            if let source = configuration.installSource { return await developmentResources(source) }
        #endif
        if let mismatch = bundled.mismatch() {
            return .failure(InstallFailure(stage: .environment, step: "Checking the app", cause: mismatch))
        }
        return .success(bundled)
    }

    #if DEBUG
        private func developmentResources(_ wheel: URL) async -> Result<InstallResources, InstallFailure> {
            let step = "Using the install source \(wheel.path)"
            func failure(_ cause: String) -> Result<InstallResources, InstallFailure> {
                .failure(InstallFailure(stage: .environment, step: step, cause: cause))
            }
            // voiced-0.3.0.dev1-py3-none-any.whl
            let parts = wheel.lastPathComponent.split(separator: "-")
            guard wheel.pathExtension == "whl", parts.count == 5, parts[0] == "voiced",
                FileManager.default.fileExists(atPath: wheel.path)
            else { return failure("It is not a voiced wheel. Build one with `uv build --wheel` in the checkout.") }
            let work = configuration.paths.installSourceWork
            try? FileManager.default.removeItem(at: work)
            do {
                try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
            } catch {
                return failure(error.localizedDescription)
            }
            let unzip = InstallCommand("/usr/bin/unzip", ["-o", "-q", wheel.path, "voiced/requirements.lock", "-d", work.path])
            if case .failure(let failure) = await execute(unzip, stage: .environment, step: step) { return .failure(failure) }
            let lock = work.appendingPathComponent("voiced/requirements.lock")
            guard let lockDigest = InstallResources.sha256(of: lock), let wheelDigest = InstallResources.sha256(of: wheel)
            else { return failure("\(wheel.lastPathComponent) carries no voiced/requirements.lock.") }
            var manifest = configuration.resources.manifest
            manifest.version = String(parts[1])
            manifest.wheel = .init(name: wheel.lastPathComponent, sha256: wheelDigest)
            manifest.lockSHA256 = lockDigest
            return .success(InstallResources(uv: configuration.resources.uv, lock: lock, wheel: wheel, manifest: manifest))
        }
    #endif

    /// What every uv command gets: no user configuration, clones from the app's own cache,
    /// Pythons under the state directory.
    private var uvEnvironment: [String: String] {
        [
            "UV_NO_CONFIG": "1",
            "UV_LINK_MODE": "clone",
            "UV_CACHE_DIR": configuration.paths.uvCache.path,
            "UV_PYTHON_INSTALL_DIR": configuration.paths.pythons.path,
        ]
    }

    /// ADR 0018's environment table, with the bundled uv. `--relocatable` because the
    /// directory is renamed after the build: entry points then find their Python relative to
    /// themselves instead of at the `.partial` path.
    func environmentCommands(building partial: URL, resources: InstallResources) -> [(String, InstallCommand)] {
        let python = resources.manifest.python
        let uv = resources.uv.path
        let environment = uvEnvironment
        return [
            ("Installing Python \(python)", InstallCommand(uv, ["python", "install", "--no-bin", python], environment: environment)),
            (
                "Creating the environment",
                InstallCommand(
                    uv, ["venv", "--managed-python", "--relocatable", "--python", python, partial.path],
                    environment: environment)
            ),
        ] + packageCommands(into: partial, resources: resources, reinstall: false)
    }

    /// The bundled lock, then the bundled wheel, into `environment`; with `reinstall`, over
    /// what is there (Repair).
    func packageCommands(into environment: URL, resources: InstallResources, reinstall: Bool) -> [(String, InstallCommand)] {
        let uv = resources.uv.path
        let interpreter = environment.appendingPathComponent("bin/python").path
        let install = ["pip", "install", "--python", interpreter] + (reinstall ? ["--reinstall"] : []) + ["--no-deps"]
        return [
            ("Installing packages", InstallCommand(uv, install + ["--require-hashes", "-r", resources.lock.path], environment: uvEnvironment)),
            ("Installing voiced", InstallCommand(uv, install + [resources.wheel.path], environment: uvEnvironment)),
        ]
    }

    // MARK: - Repair

    /// Reinstalls the bundled lock and wheel into `environment`, the active one, holding the
    /// install lock (ADR 0018, Repair).
    func repairEnvironment(_ environment: URL, progress: (String) -> Void) async -> Result<Void, InstallFailure> {
        let resources: InstallResources
        switch await installResources() {
        case .success(let found): resources = found
        case .failure(let failure): return .failure(failure)
        }
        guard let lock = await acquireLock(progress: progress) else {
            return .failure(InstallFailure(
                stage: .environment, step: "Repairing voiced", cause: "Cannot lock \(configuration.paths.stateDirectory.path)."))
        }
        defer { lock.release() }
        for (shown, command) in packageCommands(into: environment, resources: resources, reinstall: true) {
            progress(shown)
            if case .failure(let failure) = await execute(command, stage: .environment, step: shown) {
                return .failure(failure)
            }
        }
        return .success(())
    }

    /// Moves an environment whose Python is gone out of the way, to `envs/<id>.broken`, so a
    /// fresh one is built beside it; the next install's prune removes it.
    func moveAside(_ environment: URL) -> Result<Void, InstallFailure> {
        var target = environment.appendingPathExtension("broken")
        var n = 1
        while FileManager.default.fileExists(atPath: target.path) {
            n += 1
            target = environment.deletingLastPathComponent().appendingPathComponent("\(environment.lastPathComponent)-\(n).broken")
        }
        do {
            try FileManager.default.moveItem(at: environment, to: target)
            return .success(())
        } catch {
            return .failure(InstallFailure(stage: .environment, step: "Repairing voiced", cause: error.localizedDescription))
        }
    }

    // MARK: - After an update

    /// What an update leaves behind (ADR 0018, "Updates, with rollback"): the Pythons no
    /// remaining environment runs on, and the uv cache, which only grows. Under the install
    /// lock; anything that cannot be removed stays.
    func cleanUpAfterUpdate() async {
        guard let lock = await acquireLock(progress: { _ in }) else { return }
        defer { lock.release() }
        removeUnusedPythons()
        let clean = InstallCommand(configuration.resources.uv.path, ["cache", "clean"], environment: uvEnvironment)
        _ = await execute(clean, stage: .environment, step: "Clearing the uv cache")
    }

    /// Removes each uv-managed Python in `~/.voiced/python` that no environment in `envs/`
    /// leads to (`pyvenv.cfg`'s `home`), then the links left pointing at nothing. It removes
    /// nothing when `~/.voiced`, `envs` or `python` is a symbolic link (the Pythons there
    /// would be someone else's, as `layout.claim` refuses for the state directory), or when
    /// any environment's `home` cannot be read: that environment could use any of them.
    func removeUnusedPythons() {
        let fileManager = FileManager.default
        let paths = configuration.paths
        guard [paths.stateDirectory, paths.environments, paths.pythons].allSatisfy({ Self.fileType($0) == S_IFDIR })
        else { return }
        var used: [String] = []
        for name in (try? fileManager.contentsOfDirectory(atPath: paths.environments.path)) ?? [] {
            let environment = paths.environments.appendingPathComponent(name)
            switch Self.fileType(environment) {
            case S_IFDIR:
                // A .partial is a build that never finished; the next build deletes it.
                if name.hasSuffix(".partial") { continue }
                guard let home = Self.pythonHome(of: environment) else { return }
                used.append(home)
            case S_IFREG:
                continue
            default:
                return
            }
        }
        guard !used.isEmpty else { return }
        let names = ((try? fileManager.contentsOfDirectory(atPath: paths.pythons.path)) ?? []).filter { $0.hasPrefix("cpython-") }
        var links: [URL] = []
        for name in names {
            let python = paths.pythons.appendingPathComponent(name)
            switch Self.fileType(python) {
            case S_IFLNK:
                links.append(python)
            case S_IFDIR:
                let real = python.resolvingSymlinksInPath().path
                if !used.contains(where: { $0 == real || $0.hasPrefix(real + "/") }) { try? fileManager.removeItem(at: python) }
            default:
                break
            }
        }
        for link in links where !fileManager.fileExists(atPath: link.path) {
            try? fileManager.removeItem(at: link)
        }
    }

    /// `pyvenv.cfg`'s `home`, resolved; nil when it cannot be read.
    private static func pythonHome(of environment: URL) -> String? {
        let config = environment.appendingPathComponent("pyvenv.cfg")
        guard fileType(config) == S_IFREG, let text = try? String(contentsOf: config, encoding: .utf8) else { return nil }
        for line in text.split(whereSeparator: \.isNewline) {
            let parts = line.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            if parts.count == 2, parts[0] == "home", !parts[1].isEmpty {
                return URL(fileURLWithPath: parts[1]).resolvingSymlinksInPath().path
            }
        }
        return nil
    }

    /// The type bits of `url` itself, not of what a link leads to; nil when it is not there.
    static func fileType(_ url: URL) -> mode_t? {
        var info = stat()
        guard lstat(url.path, &info) == 0 else { return nil }
        return info.st_mode & S_IFMT
    }

    /// `envs/<UTC timestamp>`, as ADR 0017 names environments, with `-2`, `-3`... on a
    /// collision, as `install.new_environment_dir` names them: a build never writes into an
    /// environment a daemon may be running, and `install.json` never names a path that once
    /// was another environment.
    func newEnvironmentDirectory(at date: Date = Date()) -> URL {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyyMMdd'T'HHmmss"
        let base = formatter.string(from: date)
        let record = InstallRecord.read(stateDirectory: configuration.paths.stateDirectory)
        var name = base
        var n = 1
        while environmentTaken(configuration.paths.environments.appendingPathComponent(name, isDirectory: true), record: record) {
            n += 1
            name = "\(base)-\(n)"
        }
        return configuration.paths.environments.appendingPathComponent(name, isDirectory: true)
    }

    /// Whether a new environment must not be built at `candidate`: it, its `.partial` or its
    /// `.broken` is there (a link counts, even a dangling one), or `install.json` names it, as
    /// the active or the previous environment, even if it is gone (`install.environment_taken`).
    private func environmentTaken(_ candidate: URL, record: InstallRecord?) -> Bool {
        let name = candidate.lastPathComponent
        if ["", ".partial", ".broken"].contains(where: {
            Self.fileType(candidate.deletingLastPathComponent().appendingPathComponent(name + $0)) != nil
        }) {
            return true
        }
        let named = [record?.environment, record?.previous].compactMap { $0 }
        let path = Self.realParentPath(candidate)
        return named.contains { Self.realParentPath(URL(fileURLWithPath: $0)) == path }
    }

    /// `url` with its parent resolved through `realpath(3)`, as `os.path.realpath` resolves a
    /// path whose last component is gone; `resolvingSymlinksInPath()` leaves such a path as
    /// it is. Unresolved when the parent is not there either.
    private static func realParentPath(_ url: URL) -> String {
        let parent = url.deletingLastPathComponent()
        guard let resolved = realpath(parent.path, nil) else { return url.resolvingSymlinksInPath().path }
        defer { free(resolved) }
        return URL(fileURLWithPath: String(cString: resolved), isDirectory: true).appendingPathComponent(url.lastPathComponent).path
    }

    /// Waits while another install holds the lock.
    private func acquireLock(progress: (String) -> Void) async -> InstallLock? {
        while !Task.isCancelled {
            switch InstallLock.tryAcquire(configuration.paths.stateDirectory) {
            case .acquired(let lock): return lock
            case .busy:
                progress("Waiting for another voiced install to finish")
                try? await Task.sleep(for: configuration.pollInterval)
            case .failed: return nil
            }
        }
        return nil
    }

    /// Runs one command to its end; a failure carries its last lines.
    private func execute(_ command: InstallCommand, stage: InstallFailure.Stage, step: String) async -> Result<Void, InstallFailure> {
        let ran: Ran
        do {
            ran = try await collect(command)
        } catch {
            return .failure(InstallFailure(
                stage: stage, step: step, cause: "Cannot run \(command.executable): \(error.localizedDescription)"))
        }
        guard ran.status != 0 else { return .success(()) }
        let cause = ran.errors.last { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? "Exited with status \(ran.status)."
        return .failure(InstallFailure(stage: stage, step: step, cause: cause, output: Array(ran.lines.suffix(InstallSession.tailLength))))
    }

    /// What a command printed, both streams in order and stderr alone, and how it exited.
    private struct Ran {
        var lines: [String] = []
        var errors: [String] = []
        var status: Int32 = -1
    }

    /// Runs `command` to its end. Throws only when it cannot be started.
    private func collect(_ command: InstallCommand) async throws -> Ran {
        var ran = Ran()
        for try await output in runner.run(command) {
            switch output {
            case .stdout(let text): ran.lines.append(text)
            case .stderr(let text):
                ran.lines.append(text)
                ran.errors.append(text)
            case .exit(let code): ran.status = code
            }
        }
        return ran
    }

    // MARK: - voiced install

    /// `voiced install --origin app --json-progress` from `environment`, with a stall check
    /// while a download is unfinished.
    func runInstall(in environment: URL, progress: @escaping (InstallProgress) -> Void) async -> Result<Void, InstallFailure> {
        let command = InstallCommand(
            environment.appendingPathComponent("bin/voiced").path, ["install", "--origin", "app", "--json-progress"])
        let state = InstallRun()
        let runner = runner
        let consume = Task {
            do {
                for try await output in runner.run(command) {
                    switch output {
                    case .stdout(let line):
                        guard state.session.receive(line: line, at: .now) else { return }
                        progress(state.session.progress)
                    case .stderr(let text): state.session.receive(stderr: text)
                    case .exit(let code): state.status = code
                    }
                }
            } catch {
                state.cannotStart = error.localizedDescription
            }
        }
        let stallTimeout = configuration.stallTimeout
        let watch = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: stallTimeout / 4)
                if state.session.isStalled(at: .now, after: stallTimeout) {
                    state.stalled = true
                    consume.cancel()
                    return
                }
            }
        }
        await withTaskCancellationHandler {
            await consume.value
        } onCancel: {
            consume.cancel()
        }
        watch.cancel()

        let session = state.session
        let models = InstallProgress.Step.title(id: "models", shown: "models")
        let fallback = configuration.resources.manifest.manualDownload
        if state.stalled {
            let waited = stallTimeout.formatted(.units(allowed: [.seconds], width: .wide))
            return .failure(InstallFailure(
                stage: .install, step: models,
                cause: "The download stalled: no data for \(waited). Try again to resume it.",
                output: session.tail, fallback: fallback))
        }
        if let cannotStart = state.cannotStart {
            return .failure(InstallFailure(stage: .install, step: "Installing voiced", cause: "Cannot run \(command.executable): \(cannotStart)"))
        }
        switch session.finish(status: state.status ?? -1) {
        case .done:
            return .success(())
        case .failed(let step, let cause, _):
            let title = session.progress.steps.first { $0.id == step }?.title ?? step ?? "Installing voiced"
            return .failure(InstallFailure(
                stage: .install, step: title, cause: cause, output: session.tail,
                fallback: step == "models" ? fallback : nil))
        }
    }

    /// What the run's tasks share; they all run on the main actor.
    @MainActor
    private final class InstallRun {
        var session = InstallSession()
        var status: Int32?
        var cannotStart: String?
        var stalled = false
    }

    // MARK: - Approval

    /// Readable without root; on, the privileged step also allows the services' Python.
    func isFirewallOn() async -> Bool {
        let command = InstallCommand("/usr/libexec/ApplicationFirewall/socketfilterfw", ["--getglobalstate"])
        // Unknown counts as off: the privileged step checks again as root.
        let ran = try? await collect(command)
        return ran?.lines.contains { $0.contains("enabled") } ?? false
    }

    func installDaemons() async -> PrivilegedScript.Result {
        PrivilegedScript.result(of: await runner.runPrivileged(.install, user: configuration.userName))
    }

    /// `voiced-privileged check` as the user: true when the installed plists are what install
    /// would write and every label is loaded, so an update or repair asks for an
    /// administrator only when they are not.
    func checkDaemons() async -> Result<Bool, InstallFailure> {
        let step = "Checking the background services"
        let ran: Ran
        do {
            ran = try await collect(PrivilegedScript.checkCommand(user: configuration.userName))
        } catch {
            return .failure(InstallFailure(stage: .approval, step: step, cause: "Cannot run the check: \(error.localizedDescription)"))
        }
        switch PrivilegedScript.check(status: ran.status, lines: ran.lines) {
        case .upToDate: return .success(true)
        case .differs: return .success(false)
        case .failed(let cause): return .failure(InstallFailure(stage: .approval, step: step, cause: cause, output: ran.lines))
        }
    }

    // MARK: - Starting

    /// Waits until `model`, the app's own connection, has the watchdog's hello and settings.
    func waitForServices(_ model: AppModel) async -> Result<Void, InstallFailure> {
        await waitForHello(model) { _ in true }
    }

    /// Waits for a hello from services that run `environment` (ADR 0018, step 4 of an
    /// update). The watchdog restarts the services and itself when the entry point moves;
    /// if no such hello comes within the startup grace, `voiced restart all` and once more.
    /// With `pins`, the hello must also carry their version and lock hash.
    /// `restartFirst` restarts before the first wait: a repair in place does not move the
    /// entry point, so nothing else would restart the services.
    func waitForServices(
        _ model: AppModel, running environment: URL, pins: InstallManifest? = nil, restartFirst: Bool
    ) async -> Result<Void, InstallFailure> {
        let expected = environment.resolvingSymlinksInPath().path
        let runs: (AppModel) -> Bool = { model in
            guard let record = model.installRecord,
                record.environment.map({ URL(fileURLWithPath: $0).resolvingSymlinksInPath().path }) == expected
            else { return false }
            guard let pins else { return true }
            return InstallMatch.compare(record, with: pins) == .matches
        }
        if !restartFirst, case .success = await waitForHello(model, until: runs) { return .success(()) }
        if Task.isCancelled { return .success(()) }
        // Without a watchdog to ask, `restart all` falls back to launchctl, which needs root
        // here; launchd's KeepAlive restarts the services anyway, so wait regardless.
        let restart = InstallCommand(configuration.paths.entryPoint.path, ["restart", "all"])
        let restarted = await execute(restart, stage: .starting, step: "Restarting voiced")
        return await waitForHello(model, until: runs).mapError { failure in
            guard case .failure(let refused) = restarted else { return failure }
            var failure = failure
            failure.output += ["voiced restart all: \(refused.cause)"]
            return failure
        }
    }

    private func waitForHello(_ model: AppModel, until accepted: (AppModel) -> Bool) async -> Result<Void, InstallFailure> {
        let step = "Starting voiced"
        model.start()
        let deadline = ContinuousClock.now + configuration.startupTimeout
        while !Task.isCancelled {
            switch model.connection {
            case .connected where model.settings != nil && accepted(model):
                return .success(())
            case .incompatible(let message):
                return .failure(InstallFailure(stage: .starting, step: step, cause: message))
            default:
                break
            }
            if ContinuousClock.now >= deadline { break }
            try? await Task.sleep(for: configuration.pollInterval)
        }
        let waited = configuration.startupTimeout.formatted(.units(allowed: [.minutes, .seconds], width: .wide))
        let detail = [model.dashboard.message ?? model.dashboard.headline]
        return .failure(InstallFailure(stage: .starting, step: step, cause: "voiced did not answer within \(waited).", output: detail))
    }

    // MARK: - Uninstall

    func uninstallDaemons() async -> PrivilegedScript.Result {
        PrivilegedScript.result(of: await runner.runPrivileged(.uninstall, user: configuration.userName))
    }

    /// Whether any of the three plists is still in `/Library/LaunchDaemons`: an uninstall
    /// that already removed them does not ask for the administrator again.
    var daemonsInstalled: Bool {
        BackgroundItems.labels.contains {
            FileManager.default.fileExists(
                atPath: configuration.installProbe.launchDaemonsDirectory.appendingPathComponent("\($0).plist").path)
        }
    }

    /// A `voiced` that can still run `uninstall`: `~/.voiced/bin/voiced`, else any app
    /// environment's, newest first. Nil when an interrupted uninstall or a removed Python
    /// left none.
    func runnableVoiced() -> URL? {
        let entry = configuration.paths.entryPoint
        if let linked = try? FileManager.default.destinationOfSymbolicLink(atPath: entry.path),
            FileManager.default.isExecutableFile(atPath: entry.path),
            Self.hasPython(URL(fileURLWithPath: linked).deletingLastPathComponent().deletingLastPathComponent())
        {
            return entry
        }
        var environments: [URL] = []
        let names = (try? FileManager.default.contentsOfDirectory(atPath: configuration.paths.environments.path)) ?? []
        environments += names.filter { !$0.hasSuffix(".partial") && !$0.hasSuffix(".broken") }.sorted().reversed()
            .map { configuration.paths.environments.appendingPathComponent($0, isDirectory: true) }
        return environments.first { isAppEnvironment($0) && Self.hasPython($0) }?.appendingPathComponent("bin/voiced")
    }

    /// `voiced uninstall --yes` from `voiced`, which removes only what is voiced's in
    /// `~/.voiced` under the install lock and refuses symbolic links (ADR 0006, ADR 0017); the
    /// boxes ticked add the models, logs and config. Then the app's own uv cache.
    func removeFiles(with voiced: URL, _ choices: UninstallChoices) async -> Result<Void, InstallFailure> {
        var arguments = ["uninstall", "--yes"]
        if choices.models { arguments.append("--remove-models") }
        if choices.logs { arguments.append("--remove-logs") }
        if choices.config { arguments.append("--remove-config") }
        let command = InstallCommand(voiced.path, arguments)
        if case .failure(let failure) = await execute(command, stage: .uninstall, step: "Removing voiced's files") {
            return .failure(failure)
        }
        removeAppCache()
        return .success(())
    }

    /// The state directory's part of `voiced uninstall`, for when no `voiced` is left to run
    /// it (`install.state_targets`, the same rules): under the install lock, in a state
    /// directory that is not a link and has `install.json`, the environments voiced built or
    /// began (`pyvenv.cfg`, or `.partial`), `python/`, `bin/voiced`, `control.sock` and
    /// `install.json`, nothing through a link; then the folders left empty. The models, logs
    /// and config stay: only voiced knows where its config puts them.
    func removeStateDirectory() async -> Result<Void, InstallFailure> {
        let step = "Removing voiced's files"
        let paths = configuration.paths
        let fileManager = FileManager.default
        guard Self.fileType(paths.stateDirectory) == S_IFDIR else {
            removeAppCache()
            return Self.fileType(paths.stateDirectory) == nil
                ? .success(())
                : .failure(InstallFailure(stage: .uninstall, step: step, cause: "\(paths.stateDirectory.path) is not a folder; left as it is."))
        }
        // Read as the watchdog and `layout.read_manifest` read it: one that does not parse is
        // not voiced's to act on (Python refuses it with ManifestError).
        guard Self.fileType(paths.installRecord) == S_IFREG, InstallRecord.read(stateDirectory: paths.stateDirectory) != nil else {
            return .failure(InstallFailure(
                stage: .uninstall, step: step, cause: "\(paths.stateDirectory.path) has no readable install.json; left as it is."))
        }
        guard let lock = await acquireLock(progress: { _ in }) else {
            return .failure(InstallFailure(stage: .uninstall, step: step, cause: "Cannot lock \(paths.stateDirectory.path)."))
        }
        defer { lock.release() }
        var failed: [String] = []
        func remove(_ url: URL) {
            do { try fileManager.removeItem(at: url) } catch { failed.append(url.path) }
        }
        if Self.fileType(paths.environments) == S_IFDIR {
            for name in (try? fileManager.contentsOfDirectory(atPath: paths.environments.path)) ?? [] {
                let environment = paths.environments.appendingPathComponent(name)
                guard Self.fileType(environment) == S_IFDIR else { continue }
                if name.hasSuffix(".partial") || Self.fileType(environment.appendingPathComponent("pyvenv.cfg")) == S_IFREG {
                    remove(environment)
                }
            }
        }
        if Self.fileType(paths.pythons) == S_IFDIR { remove(paths.pythons) }
        for file in [paths.entryPoint, paths.stateDirectory.appendingPathComponent("control.sock"), paths.installRecord] {
            if let type = Self.fileType(file), [S_IFLNK, S_IFREG, S_IFSOCK].contains(type) { remove(file) }
        }
        for directory in [paths.entryPoint.deletingLastPathComponent(), paths.environments, paths.stateDirectory]
        where Self.fileType(directory) == S_IFDIR {
            rmdir(directory.path)  // only when empty
        }
        removeAppCache()
        guard failed.isEmpty else {
            return .failure(InstallFailure(stage: .uninstall, step: step, cause: "Could not remove \(failed.count) item(s).", output: failed))
        }
        return .success(())
    }

    /// The app's own: the uv cache and the development override's unpacked lock.
    private func removeAppCache() {
        for app in [configuration.paths.uvCache, configuration.paths.installSourceWork] {
            try? FileManager.default.removeItem(at: app)
        }
    }
}
