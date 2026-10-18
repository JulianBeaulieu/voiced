import Foundation

/// What the installer is doing: the first install, or one of ADR 0018's Lifecycle actions on
/// an install the app manages. Each runs the same stages in its own order.
public enum InstallerTask: Sendable, Equatable {
    case install, update, rollBack, repair, uninstall
}

/// The screens of the installer, in the order of ADR 0018's Flow, then those of the Lifecycle.
public enum InstallerPhase: Sendable, Equatable {
    case welcome
    /// Building the environment with the bundled uv: the current step's name, no bar.
    case environment(step: String)
    /// `voiced install --json-progress`: its steps and downloads.
    case installing(InstallProgress)
    /// "Install background services", before the administrator prompt.
    case approval(firewallOn: Bool)
    /// The administrator prompt and the privileged script.
    case approving(firewallOn: Bool)
    /// `voiced-privileged check`: whether an update or repair needs the administrator.
    case checking
    /// Waiting for the watchdog's hello.
    case starting
    case homeAssistant(HomeAssistantStage)
    case done
    /// An update, rollback or repair is done: the services run this voiced.
    case finished(version: String?)
    /// What Uninstall removes, with the boxes for what it may also remove.
    case confirmUninstall
    case uninstalling(step: String)
    /// What was kept, as chosen.
    case uninstalled(UninstallChoices)
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
public struct InstallFailure: Error, Sendable, Equatable {
    public enum Stage: Sendable, Equatable {
        case environment, install, approval, starting, uninstall
    }

    public var stage: Stage
    public var step: String
    public var cause: String
    public var output: [String]
    /// What the user can do by hand, such as fetching the models.
    public var fallback: String?

    public init(stage: Stage, step: String, cause: String, output: [String] = [], fallback: String? = nil) {
        self.stage = stage
        self.step = step
        self.cause = cause
        self.output = output
        self.fallback = fallback
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

/// What Uninstall also removes besides the services and `~/.voiced`; all off by default, as
/// in ADR 0006.
public struct UninstallChoices: Sendable, Equatable {
    public var models = false
    public var logs = false
    public var config = false

    public init(models: Bool = false, logs: Bool = false, config: Bool = false) {
        self.models = models
        self.logs = logs
        self.config = config
    }
}

/// An environment the install kept, which "Roll back" returns to.
public struct RollbackTarget: Sendable, Equatable {
    public var environment: URL
    public var version: String?
}
