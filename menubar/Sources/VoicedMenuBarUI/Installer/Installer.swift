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
    /// The setup's choice, which the welcome screen edits; the first install writes it to
    /// the config.
    public var setup: SetupChoice
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
        welcome = WelcomeInfo(requiredBytes: Self.requiredBytes, availableBytes: nil, resuming: false)
        setup = .recommended(in: configuration.resources.manifest.catalogue)
        phase = .welcome
        if let owner = otherAccountsInstall() {
            phase = .refused("voiced is already installed for \(owner) on this Mac.")
        }
        refreshWelcome()
    }

    /// Repair installs this app's voiced: said when the services run another version (after a
    /// rollback, say), since repairing replaces it.
    public var repairNote: String? {
        let bundled = configuration.resources.manifest.version
        guard managesInstall, let installed = record?.version, installed != bundled else { return nil }
        return "Repair installs voiced \(bundled), the version this app carries, in place of \(installed)."
    }

    /// What the setup offers.
    public var catalogue: ModelList { configuration.resources.manifest.catalogue }

    /// What the first install downloads for the setup's choice: the locked wheels and the
    /// chosen models.
    public var downloadBytes: Int64 {
        configuration.resources.manifest.wheelsBytes + setup.downloads(in: catalogue).reduce(0) { $0 + $1.downloadBytes }
    }

    /// Opening the app shows the setup by itself when nothing is installed yet (no plists,
    /// and no other account's), unless this version of the app saw the setup closed unused
    /// or voiced uninstalled: then the menu offers it. An install the command line or an
    /// older app made is left to the menu, as before.
    public var opensAtLaunch: Bool {
        phase == .welcome && !isInstalled
            && configuration.defaults.string(forKey: Self.setupClosedKey) != configuration.resources.manifest.version
    }

    /// The user default that keeps the setup from opening at launch: the app version that
    /// saw it closed, so a new version opens it again.
    static let setupClosedKey = "setupClosedBy"

    private func rememberSetupClosed() {
        configuration.defaults.set(configuration.resources.manifest.version, forKey: Self.setupClosedKey)
    }

    /// A first install failed at `voiced install`, perhaps on the model chosen: the failure
    /// offers the setup again beside Try Again.
    public var offersSetup: Bool {
        guard task == .install, case .failed(let failure) = phase else { return false }
        return failure.stage == .install
    }

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
        guard phase == .welcome, !setup.isEmpty else { return }
        configuration.defaults.removeObject(forKey: Self.setupClosedKey)
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
                // A CLI install may have pruned the environment meanwhile: build it again.
                if let environment, FileManager.default.fileExists(atPath: environment.path) {
                    await installAndContinue(in: environment)
                } else {
                    await runTask()
                }
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

    /// "Change Choices" on a first install that failed at `voiced install`: back to the
    /// setup; Install then runs again in the environment already built.
    public func changeChoices() {
        guard offersSetup else { return }
        phase = .welcome
        refreshWelcome()
    }

    /// The window was closed: a run that ended starts from the welcome screen next time, so
    /// "Install voiced…" after an uninstall installs, and so does a first install that failed
    /// at `voiced install`. The setup itself closed unused does not open by itself again.
    public func windowClosed() {
        if phase == .welcome { rememberSetupClosed() }
        if offersSetup { return changeChoices() }
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
    /// Lifecycle the check that asks for it only when needed. The first install
    /// writes the setup's choice to the config; the Lifecycle keeps the config as it is.
    private func installAndContinue(in environment: URL) async {
        phase = .installing(InstallProgress())
        let installed = await stages.runInstall(in: environment, request, progress: { self.phase = .installing($0) })
        guard value(installed) != nil else { return }
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
        if task == .install {
            guard value(await stages.waitForServices(model)) != nil else { return }
            phase = .homeAssistant(.asking)
            return
        }
        guard let environment = environment ?? record?.environment.map(URL.init(fileURLWithPath:)) else { return await runTask() }
        // An update waits for this app's pins too, not only for the environment's path.
        let waited = await stages.waitForServices(
            model, running: environment, pins: task == .update ? configuration.resources.manifest : nil,
            restartFirst: task == .repair)
        guard value(waited) != nil else { return }
        if task == .update { await stages.cleanUpAfterUpdate() }
        if !Task.isCancelled { phase = .finished(version: record?.version) }
    }

    // MARK: - Uninstall

    /// ADR 0018's Uninstall: the privileged script's `uninstall`, then `voiced uninstall
    /// --yes`. Safe to run again after a failure: plists already removed need no prompt, and
    /// when no `voiced` is left to run (an interrupted removal, a Python gone), the app removes
    /// the state directory by the same rules and says the models, logs and config stayed.
    private func runUninstall(_ choices: UninstallChoices) async {
        let step = "Uninstalling voiced"
        if stages.daemonsInstalled {
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
        }
        phase = .uninstalling(step: "Removing voiced's files")
        if let voiced = stages.runnableVoiced() {
            guard value(await stages.removeFiles(with: voiced, choices)) != nil else { return }
            phase = .uninstalled(choices)
        } else {
            guard value(await stages.removeStateDirectory()) != nil else { return }
            phase = .uninstalled(UninstallChoices())
        }
        // Uninstalled on purpose: the next launch does not offer to install again.
        rememberSetupClosed()
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

    /// What `voiced install` is asked for: the setup's choice on a first install; otherwise
    /// nothing to write, and the models that run now (the recommended ones while the services
    /// do not say).
    private var request: InstallRequest {
        guard task != .install else {
            return InstallRequest(settings: setup.settings(in: catalogue), models: setup.downloads(in: catalogue))
        }
        let running = model.settings.map(SetupChoice.init(running:)) ?? .recommended(in: catalogue)
        return InstallRequest(settings: [], models: running.downloads(in: catalogue))
    }

    /// The Wyoming ports of the services that are on; one that is off opens none (ADR 0033).
    private func homeAssistantInfo() -> HomeAssistantInfo {
        let settings = model.settings
        let ports = settings?.ports
        // Before the settings are read, a first install knows what it chose.
        func on(_ kind: ServiceKind) -> Bool { settings?.isEnabled(kind) ?? (task != .install || setup[kind].isOn) }
        return HomeAssistantInfo(
            identity: configuration.hostIdentity(), sttPort: on(.stt) ? ports?.sttWyoming ?? 10300 : nil,
            ttsPort: on(.tts) ? ports?.ttsWyoming ?? 10200 : nil, apiKey: nil)
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
