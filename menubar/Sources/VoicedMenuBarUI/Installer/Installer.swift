import Darwin
import Foundation
import Observation
import SystemConfiguration
import VoicedControl

/// What the app pins for an install (ADR 0018): the Python patch version, the lock and the
/// wheel it ships, and the sizes the welcome screen shows.
public struct InstallManifest: Codable, Sendable, Equatable {
    public struct Wheel: Codable, Sendable, Equatable {
        public var name: String
        public var sha256: String

        public init(name: String, sha256: String) {
            self.name = name
            self.sha256 = sha256
        }
    }

    public struct Model: Codable, Sendable, Equatable {
        public var repo: String
        public var revision: String
        public var bytes: Int64

        public init(repo: String, revision: String, bytes: Int64) {
            self.repo = repo
            self.revision = revision
            self.bytes = bytes
        }
    }

    public var python: String
    public var lockSHA256: String
    public var wheel: Wheel
    /// The total download size of the locked wheels.
    public var wheelsBytes: Int64
    public var models: [Model]

    public init(python: String, lockSHA256: String, wheel: Wheel, wheelsBytes: Int64, models: [Model]) {
        self.python = python
        self.lockSHA256 = lockSHA256
        self.wheel = wheel
        self.wheelsBytes = wheelsBytes
        self.models = models
    }

    enum CodingKeys: String, CodingKey {
        case python, wheel, models
        case lockSHA256 = "lock_sha256"
        case wheelsBytes = "wheels_bytes"
    }

    public var downloadBytes: Int64 { wheelsBytes + models.reduce(0) { $0 + $1.bytes } }
}

/// The files the app carries for an install: the bundled uv, the lock, the wheel and the
/// manifest.
public struct InstallResources: Sendable, Equatable {
    public var uv: URL
    public var lock: URL
    public var wheel: URL
    public var manifest: InstallManifest

    public init(uv: URL, lock: URL, wheel: URL, manifest: InstallManifest) {
        self.uv = uv
        self.lock = lock
        self.wheel = wheel
        self.manifest = manifest
    }

    /// The resources in `bundle`: `Contents/Helpers/uv`, and the manifest, the lock and the
    /// wheel it names in `Contents/Resources`. Nil when any is missing, as in a bundle built
    /// without them, which keeps pointing at the installation guide.
    public static func inBundle(_ bundle: Bundle = .main) -> InstallResources? {
        let contents = bundle.bundleURL.appendingPathComponent("Contents")
        let resources = contents.appendingPathComponent("Resources")
        guard let data = try? Data(contentsOf: resources.appendingPathComponent("install-manifest.json")),
            let manifest = try? JSONDecoder().decode(InstallManifest.self, from: data)
        else { return nil }
        let found = InstallResources(
            uv: contents.appendingPathComponent("Helpers/uv"),
            lock: resources.appendingPathComponent("requirements.lock"),
            wheel: resources.appendingPathComponent(manifest.wheel.name),
            manifest: manifest)
        let files = [found.uv, found.lock, found.wheel]
        return files.allSatisfy { FileManager.default.fileExists(atPath: $0.path) } ? found : nil
    }
}

public struct InstallerConfiguration: Sendable {
    public var resources: InstallResources
    public var home: URL
    public var userName: String
    public var socketPath: String
    public var installProbe: InstallProbe
    /// How long Starting waits for the watchdog's hello; model loading is slow on a cold disk.
    public var startupTimeout: Duration = .seconds(120)
    /// No new bytes for this long while a download is unfinished is a stall.
    public var stallTimeout: Duration = .seconds(60)
    public var pollInterval: Duration = .milliseconds(500)
    /// The name and address Home Assistant reaches this Mac by.
    public var hostIdentity: @Sendable () -> HostIdentity = HostIdentity.current

    public init(
        resources: InstallResources,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        userName: String = NSUserName(),
        socketPath: String = SocketLocation.resolve(),
        installProbe: InstallProbe = InstallProbe()
    ) {
        self.resources = resources
        self.home = home
        self.userName = userName
        self.socketPath = socketPath
        self.installProbe = installProbe
    }

    var stateDirectory: URL { home.appendingPathComponent(".voiced", isDirectory: true) }
    var environments: URL { stateDirectory.appendingPathComponent("envs", isDirectory: true) }
}

/// This Mac as Home Assistant on the network sees it.
public struct HostIdentity: Sendable, Equatable {
    public var host: String
    public var address: String?

    public init(host: String, address: String?) {
        self.host = host
        self.address = address
    }

    /// The Bonjour name (`<name>.local`) and the first IPv4 address of a running `en`
    /// interface.
    public static func current() -> HostIdentity {
        let name = SCDynamicStoreCopyLocalHostName(nil).map { "\($0 as String).local" } ?? ProcessInfo.processInfo.hostName
        var address: String?
        var list: UnsafeMutablePointer<ifaddrs>?
        if getifaddrs(&list) == 0, let first = list {
            defer { freeifaddrs(list) }
            for entry in sequence(first: first, next: { $0.pointee.ifa_next }) {
                let flags = Int32(entry.pointee.ifa_flags)
                guard let socket = entry.pointee.ifa_addr, socket.pointee.sa_family == UInt8(AF_INET),
                    String(cString: entry.pointee.ifa_name).hasPrefix("en"),
                    flags & (IFF_UP | IFF_RUNNING) == (IFF_UP | IFF_RUNNING), flags & IFF_LOOPBACK == 0
                else { continue }
                var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                var inet = socket.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                if inet_ntop(AF_INET, &inet, &buffer, socklen_t(buffer.count)) != nil {
                    address = String(decoding: buffer.prefix { $0 != 0 }.map(UInt8.init(bitPattern:)), as: UTF8.self)
                    break
                }
            }
        }
        return HostIdentity(host: name, address: address)
    }
}

/// The screens of the installer, in the order of ADR 0018's Flow.
public enum InstallerPhase: Sendable, Equatable {
    case welcome
    /// Building the environment with the bundled uv: the current step's name, no bar.
    case environment(step: String)
    /// `voiced install --json-progress`: its steps and downloads.
    case installing(InstallProgress)
    /// "Install background services", before the administrator prompt.
    case approval(firewallOn: Bool)
    /// The administrator prompt and the privileged script.
    case approving
    /// Waiting for the watchdog's hello.
    case starting
    case homeAssistant(HomeAssistantStage)
    case done
    case failed(InstallFailure)
    /// voiced is installed for another account; nothing else is offered.
    case refused(String)
}

public enum HomeAssistantStage: Sendable, Equatable {
    case asking
    case applying
    case connected(HomeAssistantInfo)
}

/// What Home Assistant needs to find voiced, shown with copy buttons.
public struct HomeAssistantInfo: Sendable, Equatable {
    public var identity: HostIdentity
    public var sttPort: Int
    public var ttsPort: Int
    /// A key generated on this screen, shown once.
    public var apiKey: String?
}

/// A step that failed: which one, a one-line cause, the last lines of output. "Try again"
/// runs the stage again; every stage is safe to repeat.
public struct InstallFailure: Sendable, Equatable {
    public enum Stage: Sendable, Equatable {
        case environment, install, approval, starting
    }

    public var stage: Stage
    public var step: String
    public var cause: String
    public var output: [String]

    public init(stage: Stage, step: String, cause: String, output: [String] = []) {
        self.stage = stage
        self.step = step
        self.cause = cause
        self.output = output
    }
}

/// What the welcome screen says about room and what is already there.
public struct WelcomeInfo: Sendable, Equatable {
    public var downloadBytes: Int64
    public var requiredBytes: Int64
    public var availableBytes: Int64?
    /// An earlier install was interrupted: the button says "Continue".
    public var resuming: Bool

    /// Unknown free space does not block: the install fails on its own if the disk fills.
    public var hasRoom: Bool { availableBytes.map { $0 >= requiredBytes } ?? true }
}

/// The installer (ADR 0018): a state machine over the Flow, from the welcome screen to Done.
/// Every side effect goes through `runner`, and the services are reached through the control
/// socket like the rest of the app.
@MainActor
@Observable
public final class Installer {
    /// What a whole install needs free while it runs (ADR 0016's measurement): more than the
    /// sum of the downloads, because the uv cache, `.incomplete` files and the Python download
    /// take room meanwhile.
    public static let requiredBytes: Int64 = 4_400_000_000
    /// Checked before the first download.
    static let reachabilityChecks = ["https://pypi.org/simple/", "https://huggingface.co/"]
        .compactMap(URL.init(string:))

    public private(set) var phase: InstallerPhase
    public private(set) var welcome: WelcomeInfo
    /// A warning from the privileged step (the firewall), shown until the end.
    public private(set) var warning: String?
    /// A Home Assistant change the watchdog refused.
    public private(set) var errorMessage: String?

    private let configuration: InstallerConfiguration
    private let runner: InstallRunner
    private var work: Task<Void, Never>?
    /// The environment this run built, once it is complete.
    private var environment: URL?
    private var firewallOn = false
    private var client: ControlClient?
    private var settings: SettingsPayload?

    public init(configuration: InstallerConfiguration, runner: InstallRunner) {
        self.configuration = configuration
        self.runner = runner
        welcome = WelcomeInfo(
            downloadBytes: configuration.resources.manifest.downloadBytes, requiredBytes: Self.requiredBytes,
            availableBytes: nil, resuming: false)
        phase = .welcome
        if let owner = otherAccountsInstall() {
            phase = .refused("voiced is already installed for \(owner) on this Mac.")
        }
        refreshWelcome()
    }

    private var isInstalled: Bool { configuration.installProbe.isInstalled() }

    /// A live account other than this one whose watchdog is installed. An install whose
    /// account was deleted is orphaned, and the privileged step takes it over.
    private func otherAccountsInstall() -> String? {
        configuration.installProbe.watchdogUsers().first { name in
            guard let account = getpwnam(name) else { return false }
            return account.pointee.pw_uid != getuid()
        }
    }

    private func refreshWelcome() {
        let environments = (try? FileManager.default.contentsOfDirectory(atPath: configuration.environments.path)) ?? []
        welcome.availableBytes = runner.availableCapacity(at: configuration.home)
        welcome.resuming = isInstalled || !environments.isEmpty
    }

    // MARK: - Actions

    /// "Install" or "Continue" on the welcome screen. Installed plists mean only Starting is
    /// left; anything else runs every stage, each safe to repeat.
    public func install() {
        guard phase == .welcome else { return }
        refreshWelcome()
        guard welcome.hasRoom else { return }
        begin { [self] in
            if isInstalled { await startServices() } else { await buildEnvironment() }
        }
    }

    /// "Install Background Services": the one administrator prompt.
    public func approve() {
        guard case .approval = phase else { return }
        begin { [self] in await installDaemons() }
    }

    /// "Try Again" on a failure: the failed stage again.
    public func retry() {
        guard case .failed(let failure) = phase else { return }
        begin { [self] in
            switch failure.stage {
            case .environment: await buildEnvironment()
            case .install:
                if let environment { await runInstall(in: environment) } else { await buildEnvironment() }
            case .approval: await showApproval()
            case .starting: await startServices()
            }
        }
    }

    /// The Connect Home Assistant answer. Yes listens on the LAN with discovery on.
    public func connectHomeAssistant(_ yes: Bool) {
        guard case .homeAssistant(.asking) = phase else { return }
        guard yes else {
            phase = .done
            return
        }
        begin { [self] in
            phase = .homeAssistant(.applying)
            if await change([.bind(.lan), .zeroconf(true)]) {
                phase = .homeAssistant(.connected(homeAssistantInfo()))
            } else {
                phase = .homeAssistant(.asking)
            }
        }
    }

    /// Sets a new random API key and shows it once.
    public func generateAPIKey() {
        guard case .homeAssistant(.connected(var info)) = phase else { return }
        let key = Self.randomKey()
        begin { [self] in
            if await change([.apiKey(key)]) {
                info.apiKey = key
                phase = .homeAssistant(.connected(info))
            }
        }
    }

    public func continueToDone() {
        guard case .homeAssistant(.connected) = phase else { return }
        phase = .done
    }

    /// Stops whatever runs and lets go of the socket; children are terminated.
    public func cancel() {
        work?.cancel()
        work = nil
        client?.close()
        client = nil
    }

    private func begin(_ body: @escaping @MainActor () async -> Void) {
        work?.cancel()
        work = Task { await body() }
    }

    private func fail(_ stage: InstallFailure.Stage, _ step: String, _ cause: String, _ output: [String] = []) {
        guard !Task.isCancelled else { return }
        phase = .failed(InstallFailure(stage: stage, step: step, cause: cause, output: output))
    }

    // MARK: - Environment

    /// Builds `envs/<id>.partial` with the bundled uv and renames it once its packages are in,
    /// holding the install lock until then.
    private func buildEnvironment() async {
        let checking = "Checking the connection"
        phase = .environment(step: checking)
        for url in Self.reachabilityChecks where !(await runner.isReachable(url)) {
            return fail(.environment, checking, "Can't reach the internet.")
        }
        let state = configuration.stateDirectory
        let preparing = "Preparing \(state.path)"
        let fileManager = FileManager.default
        do {
            try fileManager.createDirectory(at: state, withIntermediateDirectories: true)
        } catch {
            return fail(.environment, preparing, error.localizedDescription)
        }
        guard let lock = await acquireLock() else {
            return fail(.environment, preparing, "Cannot lock \(state.path).")
        }
        defer { lock.release() }
        do {
            try claimStateDirectory()
            try fileManager.createDirectory(at: configuration.environments, withIntermediateDirectories: true)
        } catch {
            return fail(.environment, preparing, (error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
        // A .partial is only ever this app's, left by a run that was stopped.
        let names = (try? fileManager.contentsOfDirectory(atPath: configuration.environments.path)) ?? []
        for name in names where name.hasSuffix(".partial") {
            try? fileManager.removeItem(at: configuration.environments.appendingPathComponent(name))
        }
        let target = newEnvironmentDirectory()
        let partial = target.appendingPathExtension("partial")
        for (shown, command) in environmentCommands(building: partial) {
            phase = .environment(step: shown)
            guard await execute(command, step: shown) else { return }
        }
        do {
            try fileManager.moveItem(at: partial, to: target)
        } catch {
            return fail(.environment, "Installing voiced", error.localizedDescription)
        }
        environment = target
        lock.release()
        await runInstall(in: target)
    }

    /// Files a state directory may hold before anyone claimed it: what a watchdog run before
    /// any install leaves, and Finder's (`layout.UNCLAIMED_OK`).
    static let unclaimedOK: Set<String> = ["control.sock", ".DS_Store"]

    struct NotVoicedsFolder: LocalizedError {
        var path: String
        var others: [String]

        var errorDescription: String? {
            let shown = others.prefix(5).joined(separator: ", ") + (others.count > 5 ? ", ..." : "")
            return "\(path) is not voiced's folder: it has no install.json and holds \(shown). Move those elsewhere, then try again."
        }
    }

    /// Claims the state directory for the app before anything is built in it, under the
    /// install lock: writes `install.json` with no environment yet, which `voiced install`
    /// adopts. Only a directory that is empty, holds only `unclaimedOK` files, or already has
    /// an `install.json` (left as it is) can be claimed; anything else is someone else's.
    func claimStateDirectory() throws {
        let state = configuration.stateDirectory
        let manifest = state.appendingPathComponent("install.json")
        let fileManager = FileManager.default
        if fileManager.fileExists(atPath: manifest.path) { return }
        let others = try fileManager.contentsOfDirectory(atPath: state.path)
            .filter { !Self.unclaimedOK.contains($0) }.sorted()
        guard others.isEmpty else { throw NotVoicedsFolder(path: state.path, others: others) }
        try Data(#"{"source": "app", "environment": null}"#.utf8 + [0x0A]).write(to: manifest, options: .atomic)
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

    /// ADR 0017's install lock: an exclusive `flock` on the state directory. Waits while
    /// another install holds it.
    private func acquireLock() async -> InstallLock? {
        while !Task.isCancelled {
            switch InstallLock.tryAcquire(configuration.stateDirectory) {
            case .acquired(let lock): return lock
            case .busy:
                phase = .environment(step: "Waiting for another voiced install to finish")
                try? await Task.sleep(for: configuration.pollInterval)
            case .failed: return nil
            }
        }
        return nil
    }

    /// Runs one uv command. False when it failed (the phase says why) or was cancelled.
    private func execute(_ command: InstallCommand, step: String) async -> Bool {
        var output: [String] = []
        var errors: [String] = []
        var status: Int32?
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
            fail(.environment, step, "Cannot run \(command.executable): \(error.localizedDescription)")
            return false
        }
        guard let status else { return false }
        guard status == 0 else {
            let cause = errors.last { !$0.trimmingCharacters(in: .whitespaces).isEmpty } ?? "Exited with status \(status)."
            fail(.environment, step, cause, Array(output.suffix(InstallSession.tailLength)))
            return false
        }
        return true
    }

    // MARK: - voiced install

    private var session = InstallSession()

    /// `voiced install --origin app --json-progress` from the new environment, with a stall
    /// check while a download is unfinished.
    private func runInstall(in environment: URL) async {
        session = InstallSession()
        phase = .installing(session.progress)
        let command = InstallCommand(
            environment.appendingPathComponent("bin/voiced").path, ["install", "--origin", "app", "--json-progress"])
        var status: Int32?
        var cannotStart: String?
        var stalled = false
        let consume = Task {
            do {
                for try await output in runner.run(command) {
                    switch output {
                    case .stdout(let line):
                        guard session.receive(line: line, at: .now) else { return }
                        phase = .installing(session.progress)
                    case .stderr(let text): session.receive(stderr: text)
                    case .exit(let code): status = code
                    }
                }
            } catch {
                cannotStart = error.localizedDescription
            }
        }
        let watch = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: configuration.stallTimeout / 4)
                if session.isStalled(at: .now, after: configuration.stallTimeout) {
                    stalled = true
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
        guard !Task.isCancelled else { return }

        let models = InstallProgress.Step.title(id: "models", shown: "models")
        if stalled {
            let seconds = Int(configuration.stallTimeout / .seconds(1))
            return fail(
                .install, models, "The download stalled: no data for \(seconds) seconds. Try again to resume it.",
                session.tail)
        }
        if let cannotStart {
            return fail(.install, "Installing voiced", "Cannot run \(command.executable): \(cannotStart)")
        }
        switch session.finish(status: status ?? -1) {
        case .done:
            await showApproval()
        case .failed(let step, let cause):
            let title = session.progress.steps.first { $0.id == step }?.title ?? step ?? "Installing voiced"
            fail(.install, title, cause, session.tail)
        }
    }

    // MARK: - Approval

    private func showApproval() async {
        firewallOn = await isFirewallOn()
        phase = .approval(firewallOn: firewallOn)
    }

    /// Readable without root; on, the privileged step also allows the services' Python.
    private func isFirewallOn() async -> Bool {
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

    private func installDaemons() async {
        phase = .approving
        let step = "Installing background services"
        switch await runner.runPrivileged(.install, user: configuration.userName) {
        case .cancelled:
            phase = .approval(firewallOn: firewallOn)
        case .finished(0, _):
            warning = nil
            await startServices()
        case .finished(4, _):
            warning = "voiced could not allow its services through the firewall. Allow them in System Settings > Network > Firewall."
            await startServices()
        case .finished(5, _):
            warning = "Your firewall is on and voiced's Python is outside your home folder, so voiced left the firewall alone. Allow it in System Settings > Network > Firewall."
            await startServices()
        case .finished(3, let output):
            let lines = Self.lines(output)
            let services = lines.compactMap(Self.failedService)
            let names = services.isEmpty ? "the background services" : services.joined(separator: " and ")
            fail(.approval, step, "macOS did not start \(names).", lines)
        case .finished(let status, let output):
            let lines = Self.lines(output)
            let refusal = lines.lazy.compactMap { line -> String? in
                guard line.hasPrefix("refused: ") else { return nil }
                return String(line.dropFirst("refused: ".count))
            }.first
            if let refusal, refusal.hasPrefix("voiced is already installed for ") {
                phase = .refused(refusal + ".")
            } else {
                let cause = refusal.map { "Refused: \($0)." } ?? "The privileged step exited with status \(status)."
                fail(.approval, step, cause, lines)
            }
        }
    }

    /// `do shell script` ends lines with carriage returns.
    static func lines(_ text: String) -> [String] {
        text.split(whereSeparator: \.isNewline).map(String.init)
    }

    /// "failed io.github.julianbeaulieu.voiced.tts" -> "text to speech".
    static func failedService(_ line: String) -> String? {
        guard line.hasPrefix("failed "), let service = line.split(separator: ".").last else { return nil }
        if let kind = ServiceKind(rawValue: String(service)) { return kind.title.lowercased() }
        return service == "watchdog" ? "the watchdog" : String(service)
    }

    // MARK: - Starting

    /// Waits for the watchdog's hello, then reads the settings for the next screen.
    private func startServices() async {
        phase = .starting
        client?.close()
        client = nil
        let deadline = ContinuousClock.now + configuration.startupTimeout
        var last: Error?
        while !Task.isCancelled {
            let candidate = ControlClient(socketPath: configuration.socketPath)
            do {
                _ = try await candidate.connect()
                settings = try await candidate.settings()
                client = candidate
                phase = .homeAssistant(.asking)
                return
            } catch ControlError.unsupportedProtocol(let server, let supported) {
                candidate.close()
                let message = ControlError.unsupportedProtocol(server: server, supported: supported).localizedDescription
                return fail(.starting, "Starting voiced", message)
            } catch {
                candidate.close()
                last = error
            }
            if ContinuousClock.now >= deadline { break }
            try? await Task.sleep(for: configuration.pollInterval)
        }
        let waited = configuration.startupTimeout.formatted(.units(allowed: [.minutes, .seconds], width: .wide))
        fail(.starting, "Starting voiced", "voiced did not answer within \(waited).", last.map { [AppModel.describe($0)] } ?? [])
    }

    // MARK: - Home Assistant

    /// Applies `edits` in one `settings.set` and restarts what they need. False on a refusal,
    /// which `errorMessage` describes.
    private func change(_ edits: [SettingsEdit]) async -> Bool {
        guard let client, let current = settings else {
            errorMessage = "Not connected to voiced."
            return false
        }
        var args: [String: JSONValue] = [:]
        for edit in edits { args.merge(edit.arguments(current: current, engines: nil)) { $1 } }
        guard !args.isEmpty else { return true }
        let result: SettingsSetResult
        do {
            result = try await client.setSettings(args)
        } catch {
            errorMessage = AppModel.describe(error)
            return false
        }
        settings = result.settings
        let kinds = ServiceKind.allCases.filter(result.restartRequired.contains)
        if let first = kinds.first {
            do {
                let target: RestartTarget = kinds.count == ServiceKind.allCases.count ? .all : .service(first)
                let refused = try await client.restart(target, timeout: ControlClient.restartTimeout)
                if !refused.isEmpty {
                    errorMessage = AppModel.describe(refused: refused)
                    return false
                }
            } catch ControlError.timedOut {
                // The restart was asked for; the services come up on their own.
            } catch ControlError.remote(let remote) {
                errorMessage = "Not restarted: \(remote.displayMessage)"
                return false
            } catch {
                errorMessage = AppModel.describe(error)
                return false
            }
        }
        errorMessage = nil
        return true
    }

    private func homeAssistantInfo() -> HomeAssistantInfo {
        let ports = settings?.ports
        return HomeAssistantInfo(
            identity: configuration.hostIdentity(), sttPort: ports?.sttWyoming ?? 10300,
            ttsPort: ports?.ttsWyoming ?? 10200, apiKey: nil)
    }

    /// 32 random bytes, base64url without padding.
    static func randomKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

/// An exclusive `flock` on a directory, released by closing it (ADR 0017's install lock).
final class InstallLock {
    enum Attempt {
        case acquired(InstallLock)
        case busy
        case failed
    }

    private var fd: Int32

    private init(fd: Int32) {
        self.fd = fd
    }

    static func tryAcquire(_ directory: URL) -> Attempt {
        let fd = open(directory.path, O_RDONLY)
        guard fd >= 0 else { return .failed }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 { return .acquired(InstallLock(fd: fd)) }
        let busy = errno == EWOULDBLOCK
        close(fd)
        return busy ? .busy : .failed
    }

    func release() {
        guard fd >= 0 else { return }
        close(fd)
        fd = -1
    }

    deinit { release() }
}
