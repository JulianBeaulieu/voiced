import Darwin
import Foundation
import Observation
import Security
import VoicedControl

/// The installer (ADR 0018): sequences the stages of the Flow, from the welcome screen to
/// Done, and those of the Lifecycle (Update, Roll back, Repair, Uninstall) for an install the
/// app manages, and turns their results into screens. The stages do the work through the
/// runner; the services are reached through the app's own `AppModel`, as everything else is.
@MainActor
@Observable
public final class Installer {
    /// What a whole install needs free while it runs (ADR 0016's measurement): more than the
    /// sum of the downloads, because the uv cache, `.incomplete` files and the Python download
    /// take room meanwhile.
    public static let requiredBytes: Int64 = 4_400_000_000

    public private(set) var phase: InstallerPhase
    /// What the current or last run is doing.
    public private(set) var task: InstallerTask = .install
    public private(set) var welcome: WelcomeInfo
    /// A warning from the privileged step (the firewall), shown until the end.
    public private(set) var warning: String?
    /// A Home Assistant change the watchdog refused.
    public private(set) var errorMessage: String?

    private let configuration: InstallerConfiguration
    private let stages: InstallStages
    private let model: AppModel
    private var work: Task<Void, Never>?
    /// The environment this run builds, resumes, repairs or rolls back to, once it is
    /// complete.
    private var environment: URL?
    /// `install.json` when the run began: its Python, to tell whether the firewall's
    /// exception has to follow a new one.
    private var recordBefore: InstallRecord?

    public init(configuration: InstallerConfiguration, runner: InstallRunner, model: AppModel) {
        self.configuration = configuration
        stages = InstallStages(configuration: configuration, runner: runner)
        self.model = model
        welcome = WelcomeInfo(
            downloadBytes: configuration.resources.manifest.downloadBytes, requiredBytes: Self.requiredBytes,
            availableBytes: nil, resuming: false)
        phase = .welcome
        if let owner = otherAccountsInstall() {
            phase = .refused("voiced is already installed for \(owner) on this Mac.")
        }
        refreshWelcome()
    }

    /// What the pinned models take in the Hugging Face cache, for the uninstall's box.
    public var modelBytes: Int64 { configuration.resources.manifest.models.reduce(0) { $0 + $1.bytes } }

    private var isInstalled: Bool { configuration.installProbe.isInstalled() }

    /// Building, downloading, installing the services or waiting for them: the menu bar icon
    /// shows it as busy.
    public var isWorking: Bool {
        switch phase {
        case .environment, .installing, .approving, .checking, .starting, .uninstalling, .homeAssistant(.applying): true
        default: false
        }
    }

    /// `install.json` as it is on disk now.
    private var record: InstallRecord? { InstallRecord.read(stateDirectory: configuration.paths.stateDirectory) }

    /// The app made this install, so it updates, repairs, rolls back and uninstalls it.
    /// Homebrew's and the command line's are only shown (ADR 0018, "An install that already
    /// exists").
    public var managesInstall: Bool { record?.source == .app && otherAccountsInstall() == nil }

    /// The environment `install.json` says was active before this one, while it is still in
    /// `envs/` and runnable; nil for an install the app does not manage.
    public func rollbackTarget() -> RollbackTarget? {
        guard managesInstall, let record, let previous = record.previous.map(URL.init(fileURLWithPath:)),
            previous.standardizedFileURL.path != record.environment.map({ URL(fileURLWithPath: $0).standardizedFileURL.path }),
            stages.isAppEnvironment(previous), InstallStages.hasPython(previous)
        else { return nil }
        return RollbackTarget(environment: previous, version: InstallStages.version(in: previous))
    }

    /// An update failed after it linked its new environment, the services do not come up on
    /// it, and the previous one is still there: the failure offers "Roll back".
    public var offersRollback: Bool {
        guard task == .update, case .failed = phase, let environment,
            record?.environment.map({ URL(fileURLWithPath: $0).standardizedFileURL }) == environment.standardizedFileURL
        else { return false }
        return rollbackTarget() != nil
    }

    /// A live account other than this one whose watchdog is installed. An install whose
    /// account was deleted is orphaned, and the privileged step takes it over.
    private func otherAccountsInstall() -> String? {
        configuration.installProbe.watchdogUsers().first { name in
            guard let account = getpwnam(name) else { return false }
            return account.pointee.pw_uid != getuid()
        }
    }

    private func refreshWelcome() {
        let fileManager = FileManager.default
        let environments = (try? fileManager.contentsOfDirectory(atPath: configuration.paths.environments.path)) ?? []
        welcome.availableBytes = configuration.availableCapacity(configuration.home)
        welcome.resuming = isInstalled || !environments.isEmpty || fileManager.fileExists(atPath: configuration.paths.installRecord.path)
    }

    // MARK: - Actions

    /// "Install" or "Continue" on the welcome screen.
    public func install() {
        guard phase == .welcome else { return }
        refreshWelcome()
        guard welcome.hasRoom else { return }
        begin { [self] in await firstInstall() }
    }

    /// "Install Background Services": the one administrator prompt.
    public func approve() {
        guard case .approval(let firewallOn) = phase else { return }
        begin { [self] in await installDaemons(firewallOn: firewallOn) }
    }

    /// "Try Again" on a failure: the failed stage again, in the task that failed.
    public func retry() {
        guard case .failed(let failure) = phase else { return }
        if failure.stage == .uninstall {
            phase = .confirmUninstall
            return
        }
        begin { [self] in
            switch failure.stage {
            case .environment, .uninstall: await runTask()
            case .install:
                if let environment { await installAndContinue(in: environment) } else { await runTask() }
            case .approval: await reconcileDaemons()
            case .starting: await startServices()
            }
        }
    }

    // MARK: - Lifecycle actions

    /// "Update voiced": a new environment from this app's wheel and lock beside the current
    /// one, which stays for "Roll back".
    public func update() {
        startTask(.update)
    }

    /// "Roll back": the previous environment's own `voiced install`, which links it again.
    public func rollBack() {
        guard let target = rollbackTarget() else { return }
        startTask(.rollBack, environment: target.environment)
    }

    /// "Repair": the install steps again against this app's bundle, in the active
    /// environment, or in a fresh one when its Python is gone.
    public func repair() {
        startTask(.repair)
    }

    /// "Uninstall voiced…": the confirmation, with what else to remove.
    public func confirmUninstall() {
        guard !isWorking, managesInstall else { return }
        task = .uninstall
        phase = .confirmUninstall
    }

    /// Removes the services (one administrator prompt), then voiced's files.
    public func uninstall(_ choices: UninstallChoices) {
        guard phase == .confirmUninstall, managesInstall else { return }
        begin { [self] in await runUninstall(choices) }
    }

    /// The window was closed: a run that ended starts from the welcome screen next time, so
    /// "Install voiced…" after an uninstall installs.
    public func windowClosed() {
        switch phase {
        case .done, .finished, .uninstalled, .confirmUninstall:
            task = .install
            environment = nil
            phase = .welcome
            refreshWelcome()
        default:
            break
        }
    }

    private func startTask(_ task: InstallerTask, environment: URL? = nil) {
        guard !isWorking, managesInstall else { return }
        self.task = task
        self.environment = environment
        recordBefore = record
        warning = nil
        begin { [self] in await runTask() }
    }

    /// The current task from its start.
    private func runTask() async {
        switch task {
        case .install: await firstInstall()
        case .update: await runUpdate()
        case .rollBack:
            if let environment { await installAndContinue(in: environment) }
        case .repair: await runRepair()
        case .uninstall: phase = .confirmUninstall
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
            phase = await apply([.bind(.lan), .zeroconf(true)])
                ? .homeAssistant(.connected(homeAssistantInfo())) : .homeAssistant(.asking)
        }
    }

    /// Sets a new random API key and shows it once.
    public func generateAPIKey() {
        guard case .homeAssistant(.connected(var info)) = phase else { return }
        let key = Self.randomKey()
        begin { [self] in
            if await apply([.apiKey(key)]) {
                info.apiKey = key
                phase = .homeAssistant(.connected(info))
            }
        }
    }

    public func continueToDone() {
        guard case .homeAssistant(.connected) = phase else { return }
        phase = .done
    }

    /// Stops whatever runs; the runner terminates its children.
    public func cancel() {
        work?.cancel()
        work = nil
    }

    private func begin(_ body: @escaping @MainActor () async -> Void) {
        work?.cancel()
        work = Task { await body() }
    }

    /// The value of a stage that succeeded; a failure becomes the failure screen, unless the
    /// run was cancelled, which leaves the screen alone.
    private func value<T>(_ result: Result<T, InstallFailure>) -> T? {
        switch result {
        case .success(let value): return value
        case .failure(let failure):
            fail(failure)
            return nil
        }
    }

    private func fail(_ failure: InstallFailure) {
        if !Task.isCancelled { phase = .failed(failure) }
    }

    // MARK: - The Flow

    /// Installed plists mean only Starting is left; an environment an interrupted run built
    /// gets `voiced install` again; anything else starts from the environment. Every stage is
    /// safe to repeat.
    private func firstInstall() async {
        if isInstalled { return await startServices() }
        phase = .environment(step: "Checking the connection")
        guard value(await stages.checkNetwork()) != nil else { return }
        let environment: URL
        if let resumed = stages.resumableEnvironment() {
            environment = resumed
        } else {
            guard let built = value(await stages.buildEnvironment(progress: { self.phase = .environment(step: $0) }))
            else { return }
            environment = built
        }
        self.environment = environment
        await installAndContinue(in: environment)
    }

    /// Steps 1 and 2 of an update: a new environment, and its `voiced install`.
    private func runUpdate() async {
        guard let built = await freshEnvironment() else { return }
        await installAndContinue(in: built)
    }

    /// ADR 0018's Repair: reinstall into the active environment; move it aside and build a
    /// fresh one when its Python is gone, or when none is linked yet (an install stopped
    /// half-way), resume or build one.
    private func runRepair() async {
        phase = .environment(step: "Checking the environment")
        let active = record?.environment.map(URL.init(fileURLWithPath:))
        if let active, stages.isAppEnvironment(active) {
            if InstallStages.hasPython(active) {
                guard value(await stages.repairEnvironment(active, progress: { self.phase = .environment(step: $0) })) != nil
                else { return }
                environment = active
                return await installAndContinue(in: active)
            }
            guard value(stages.moveAside(active)) != nil else { return }
        }
        if active == nil, let resumed = stages.resumableEnvironment() {
            environment = resumed
            return await installAndContinue(in: resumed)
        }
        guard let built = await freshEnvironment() else { return }
        await installAndContinue(in: built)
    }

    /// The network check, then a new environment from this app's wheel.
    private func freshEnvironment() async -> URL? {
        phase = .environment(step: "Checking the connection")
        guard value(await stages.checkNetwork()) != nil,
            let built = value(await stages.buildEnvironment(progress: { self.phase = .environment(step: $0) }))
        else { return nil }
        environment = built
        return built
    }

    /// `voiced install` in `environment`, then the first install's approval, or for the
    /// Lifecycle the check that asks for it only when needed.
    private func installAndContinue(in environment: URL) async {
        phase = .installing(InstallProgress())
        guard value(await stages.runInstall(in: environment, progress: { self.phase = .installing($0) })) != nil
        else { return }
        if task == .install {
            await showApproval()
        } else {
            await reconcileDaemons()
        }
    }

    /// Step 3 of an update: the prompt only if a plist differs or a label is not loaded, or
    /// when the firewall is on and the services' Python changed, so its exception follows.
    private func reconcileDaemons() async {
        guard task != .install else { return await showApproval() }
        phase = .checking
        guard let upToDate = value(await stages.checkDaemons()) else { return }
        let pythonChanged = record?.python != recordBefore?.python
        if upToDate, !pythonChanged {
            return await startServices()
        }
        let firewallOn = await stages.isFirewallOn()
        if upToDate, !firewallOn { return await startServices() }
        phase = .approval(firewallOn: firewallOn)
    }

    private func showApproval() async {
        phase = .approval(firewallOn: await stages.isFirewallOn())
    }

    private func installDaemons(firewallOn: Bool) async {
        phase = .approving(firewallOn: firewallOn)
        switch await stages.installDaemons() {
        case .done(let warning):
            self.warning = warning
            await startServices()
        case .cancelled:
            phase = .approval(firewallOn: firewallOn)
        case .anotherAccount(let message):
            phase = .refused(message)
        case .failed(let cause, let output):
            fail(InstallFailure(stage: .approval, step: "Installing background services", cause: cause, output: output))
        }
    }

    private func startServices() async {
        phase = .starting
        guard task != .install, let environment = environment ?? record?.environment.map(URL.init(fileURLWithPath:)) else {
            guard value(await stages.waitForServices(model)) != nil else { return }
            phase = .homeAssistant(.asking)
            return
        }
        let waited = await stages.waitForServices(model, running: environment, restartFirst: task == .repair)
        guard value(waited) != nil else { return }
        if task == .update { await stages.cleanUpAfterUpdate() }
        if !Task.isCancelled { phase = .finished(version: record?.version) }
    }

    // MARK: - Uninstall

    /// ADR 0018's Uninstall: the privileged script's `uninstall`, then `voiced uninstall
    /// --yes`. An environment too broken to run that is refused before the prompt: Repair
    /// first, then Uninstall.
    private func runUninstall(_ choices: UninstallChoices) async {
        let step = "Uninstalling voiced"
        let active = record?.environment.map(URL.init(fileURLWithPath:))
        if !FileManager.default.isExecutableFile(atPath: configuration.paths.entryPoint.path)
            || active.map(InstallStages.hasPython) == false
        {
            return fail(InstallFailure(
                stage: .uninstall, step: step,
                cause: "voiced's environment is too broken to remove its files. Repair it, then uninstall again."))
        }
        phase = .uninstalling(step: "Removing the background services")
        switch await stages.uninstallDaemons() {
        case .done:
            break
        case .cancelled:
            phase = .confirmUninstall
            return
        case .anotherAccount(let message):
            phase = .refused(message)
            return
        case .failed(let cause, let output):
            return fail(InstallFailure(stage: .uninstall, step: step, cause: cause, output: output))
        }
        phase = .uninstalling(step: "Removing voiced's files")
        guard value(await stages.removeFiles(choices)) != nil else { return }
        phase = .uninstalled(choices)
    }

    // MARK: - Home Assistant

    /// Applies `edits` through the app model, then restarts what they need. False on a
    /// refusal, which `errorMessage` describes.
    private func apply(_ edits: [SettingsEdit]) async -> Bool {
        model.errorMessage = nil
        for edit in edits where !(await model.apply(edit)) {
            errorMessage = model.errorMessage ?? "Not connected to voiced."
            return false
        }
        if !model.restartRequired.isEmpty { await model.restartPending() }
        errorMessage = model.errorMessage
        return errorMessage == nil
    }

    private func homeAssistantInfo() -> HomeAssistantInfo {
        let ports = model.settings?.ports
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
