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
        let entry = configuration.stateDirectory.appendingPathComponent("bin/voiced")
        var candidates: [URL] = []
        if let linked = try? FileManager.default.destinationOfSymbolicLink(atPath: entry.path) {
            candidates.append(URL(fileURLWithPath: linked).deletingLastPathComponent().deletingLastPathComponent())
        }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: configuration.environments.path)) ?? []
        candidates += names.filter { !$0.hasSuffix(".partial") }.sorted().reversed()
            .map { configuration.environments.appendingPathComponent($0, isDirectory: true) }
        return candidates.first(where: hasThisAppsVoiced)
    }

    /// Inside `envs/`, with a `bin/voiced` and this app's wheel's version installed.
    private func hasThisAppsVoiced(_ environment: URL) -> Bool {
        let environment = environment.standardizedFileURL
        guard environment.deletingLastPathComponent().path == configuration.environments.standardizedFileURL.path,
            FileManager.default.isExecutableFile(atPath: environment.appendingPathComponent("bin/voiced").path)
        else { return false }
        // voiced-0.1.0-py3-none-any.whl installs voiced-0.1.0.dist-info.
        let wheel = configuration.resources.manifest.wheel.name.split(separator: "-")
        guard wheel.count > 2 else { return false }
        let info = "voiced-\(wheel[1]).dist-info"
        let lib = environment.appendingPathComponent("lib")
        let pythons = (try? FileManager.default.contentsOfDirectory(atPath: lib.path)) ?? []
        return pythons.contains {
            FileManager.default.fileExists(atPath: lib.appendingPathComponent("\($0)/site-packages/\(info)").path)
        }
    }

    /// Claims `~/.voiced`, then builds `envs/<id>.partial` with the bundled uv and renames it
    /// once its packages are in, holding the install lock until then.
    func buildEnvironment(progress: (String) -> Void) async -> Result<URL, InstallFailure> {
        let state = configuration.stateDirectory
        let preparing = "Preparing \(state.path)"
        func failure(_ cause: String) -> Result<URL, InstallFailure> {
            .failure(InstallFailure(stage: .environment, step: preparing, cause: cause))
        }
        if let mismatch = configuration.resources.mismatch() { return failure(mismatch) }
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
            try fileManager.createDirectory(at: configuration.environments, withIntermediateDirectories: true)
        } catch {
            return failure(error.localizedDescription)
        }
        // A .partial is only ever this app's, left by a run that was stopped.
        let names = (try? fileManager.contentsOfDirectory(atPath: configuration.environments.path)) ?? []
        for name in names where name.hasSuffix(".partial") {
            try? fileManager.removeItem(at: configuration.environments.appendingPathComponent(name))
        }
        let target = newEnvironmentDirectory()
        let partial = target.appendingPathExtension("partial")
        for (shown, command) in environmentCommands(building: partial) {
            progress(shown)
            if case .failure(let failure) = await execute(command, step: shown) { return .failure(failure) }
        }
        do {
            try fileManager.moveItem(at: partial, to: target)
        } catch {
            return .failure(InstallFailure(stage: .environment, step: "Installing voiced", cause: error.localizedDescription))
        }
        return .success(target)
    }

    /// ADR 0018's environment table, with the bundled uv. `--relocatable` because the
    /// directory is renamed after the build: entry points then find their Python relative to
    /// themselves instead of at the `.partial` path.
    func environmentCommands(building partial: URL) -> [(String, InstallCommand)] {
        let resources = configuration.resources
        let python = resources.manifest.python
        let uv = resources.uv.path
        let interpreter = partial.appendingPathComponent("bin/python").path
        let environment = [
            "UV_NO_CONFIG": "1",
            "UV_LINK_MODE": "clone",
            "UV_CACHE_DIR": configuration.home.appendingPathComponent("Library/Application Support/voiced/uv-cache").path,
            "UV_PYTHON_INSTALL_DIR": configuration.stateDirectory.appendingPathComponent("python").path,
        ]
        let install = ["pip", "install", "--python", interpreter, "--no-deps"]
        return [
            ("Installing Python \(python)", InstallCommand(uv, ["python", "install", "--no-bin", python], environment: environment)),
            (
                "Creating the environment",
                InstallCommand(
                    uv, ["venv", "--managed-python", "--relocatable", "--python", python, partial.path],
                    environment: environment)
            ),
            ("Installing packages", InstallCommand(uv, install + ["--require-hashes", "-r", resources.lock.path], environment: environment)),
            ("Installing voiced", InstallCommand(uv, install + [resources.wheel.path], environment: environment)),
        ]
    }

    /// `envs/<UTC timestamp>`, as ADR 0017 names environments, with a suffix on a collision.
    private func newEnvironmentDirectory() -> URL {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyyMMdd'T'HHmmss"
        let base = formatter.string(from: Date())
        var name = base
        var n = 1
        let fileManager = FileManager.default
        while fileManager.fileExists(atPath: configuration.environments.appendingPathComponent(name).path)
            || fileManager.fileExists(atPath: configuration.environments.appendingPathComponent(name + ".partial").path)
        {
            n += 1
            name = "\(base)-\(n)"
        }
        return configuration.environments.appendingPathComponent(name, isDirectory: true)
    }

    /// Waits while another install holds the lock.
    private func acquireLock(progress: (String) -> Void) async -> InstallLock? {
        while !Task.isCancelled {
            switch InstallLock.tryAcquire(configuration.stateDirectory) {
            case .acquired(let lock): return lock
            case .busy:
                progress("Waiting for another voiced install to finish")
                try? await Task.sleep(for: configuration.pollInterval)
            case .failed: return nil
            }
        }
        return nil
    }

    /// Runs one uv command.
    private func execute(_ command: InstallCommand, step: String) async -> Result<Void, InstallFailure> {
        var output: [String] = []
        var errors: [String] = []
        var status: Int32 = -1
        do {
            for try await line in runner.run(command) {
                switch line {
                case .stdout(let text): output.append(text)
                case .stderr(let text):
                    output.append(text)
                    errors.append(text)
                case .exit(let code): status = code
                }
            }
        } catch {
            return .failure(InstallFailure(
                stage: .environment, step: step, cause: "Cannot run \(command.executable): \(error.localizedDescription)"))
        }
        guard status != 0 else { return .success(()) }
        let cause = errors.last { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? "Exited with status \(status)."
        return .failure(InstallFailure(stage: .environment, step: step, cause: cause, output: Array(output.suffix(InstallSession.tailLength))))
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
        case .failed(let step, let cause):
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
        var on = false
        do {
            for try await output in runner.run(command) {
                if case .stdout(let text) = output, text.contains("enabled") { on = true }
            }
        } catch {
            // Unknown counts as off: the privileged step checks again as root.
        }
        return on
    }

    func installDaemons() async -> PrivilegedScript.Result {
        PrivilegedScript.result(of: await runner.runPrivileged(.install, user: configuration.userName))
    }

    // MARK: - Starting

    /// Waits until `model`, the app's own connection, has the watchdog's hello and settings.
    func waitForServices(_ model: AppModel) async -> Result<Void, InstallFailure> {
        let step = "Starting voiced"
        model.start()
        let deadline = ContinuousClock.now + configuration.startupTimeout
        while !Task.isCancelled {
            switch model.connection {
            case .connected where model.settings != nil:
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
}
