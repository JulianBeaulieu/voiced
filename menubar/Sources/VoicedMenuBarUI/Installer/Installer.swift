import Darwin
import Foundation
import Observation
import Security
import VoicedControl

/// The installer (ADR 0018): sequences the stages of the Flow, from the welcome screen to
/// Done, and turns their results into screens. The stages do the work through the runner;
/// the services are reached through the app's own `AppModel`, as everything else is.
@MainActor
@Observable
public final class Installer {
    /// What a whole install needs free while it runs (ADR 0016's measurement): more than the
    /// sum of the downloads, because the uv cache, `.incomplete` files and the Python download
    /// take room meanwhile.
    public static let requiredBytes: Int64 = 4_400_000_000

    public private(set) var phase: InstallerPhase
    public private(set) var welcome: WelcomeInfo
    /// A warning from the privileged step (the firewall), shown until the end.
    public private(set) var warning: String?
    /// A Home Assistant change the watchdog refused.
    public private(set) var errorMessage: String?

    private let configuration: InstallerConfiguration
    private let stages: InstallStages
    private let model: AppModel
    private var work: Task<Void, Never>?
    /// The environment this run builds or resumes, once it is complete.
    private var environment: URL?

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

    private var isInstalled: Bool { configuration.installProbe.isInstalled() }

    /// Building, downloading, installing the services or waiting for them: the menu bar icon
    /// shows it as busy.
    public var isWorking: Bool {
        switch phase {
        case .environment, .installing, .approving, .starting, .homeAssistant(.applying): true
        default: false
        }
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
        let environments = (try? fileManager.contentsOfDirectory(atPath: configuration.environments.path)) ?? []
        welcome.availableBytes = configuration.availableCapacity(configuration.home)
        welcome.resuming = isInstalled || !environments.isEmpty || fileManager.fileExists(atPath: configuration.manifest.path)
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

    /// "Try Again" on a failure: the failed stage again.
    public func retry() {
        guard case .failed(let failure) = phase else { return }
        begin { [self] in
            switch failure.stage {
            case .environment: await firstInstall()
            case .install:
                if let environment { await installAndApprove(in: environment) } else { await firstInstall() }
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
        await installAndApprove(in: environment)
    }

    private func installAndApprove(in environment: URL) async {
        phase = .installing(InstallProgress())
        guard value(await stages.runInstall(in: environment, progress: { self.phase = .installing($0) })) != nil
        else { return }
        await showApproval()
    }

    private func showApproval() async {
        phase = .approval(firewallOn: await stages.isFirewallOn())
    }

    private func installDaemons(firewallOn: Bool) async {
        phase = .approving(firewallOn: firewallOn)
        switch await stages.installDaemons() {
        case .installed(let warning):
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
        guard value(await stages.waitForServices(model)) != nil else { return }
        phase = .homeAssistant(.asking)
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
