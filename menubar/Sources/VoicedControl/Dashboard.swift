import Foundation

/// How far the app got in talking to voiced.
public enum ConnectionState: Sendable, Equatable {
    case connecting
    case connected(version: String?)
    /// No usable server. `installed` says whether a voiced watchdog LaunchDaemon exists.
    case unreachable(installed: Bool, reason: UnreachableReason)
    /// voiced answered with a protocol this app does not speak.
    case incompatible(message: String)
}

public enum UnreachableReason: Sendable, Equatable {
    case cannotConnect(ConnectFailure)
    /// Connected, but no `status` arrived for longer than the contract allows.
    case stoppedAnswering
    /// Connected, but the server sent something that is not the protocol.
    case protocolError(String)
    /// Connected, but a request right after connecting failed.
    case setupFailed(String)
    /// A connection that was up ended.
    case disconnected
}

/// The three states of the menu bar icon.
public enum OverallHealth: Sendable, Equatable {
    case running
    case attention
    case off

    public var symbolName: String {
        switch self {
        case .running: "waveform"
        case .attention: "waveform.badge.exclamationmark"
        case .off: "waveform.slash"
        }
    }
}

/// The menu bar icon (ADR 0018, "In the menu bar"): idle while voiced runs, busy while it
/// starts, loads a model, connects, installs or downloads a model (ADR 0022), and an error
/// badge for everything else that needs a look (degraded, stopped, a missing model, not
/// running, not installed, an incompatible version). While idle, a newer app waiting to be
/// installed (ADR 0034) adds a neutral badge: news, not an error.
public enum MenuBarIconState: Sendable, Hashable {
    case idle, update, busy, error
}

/// The icon's state and what VoiceOver says for it.
public struct MenuBarIconStatus: Sendable, Equatable {
    public var state: MenuBarIconState
    public var accessibilityLabel: String

    public init(dashboard: Dashboard, installing: Bool, downloading: Bool = false, updateAvailable: Bool = false) {
        if installing || downloading || dashboard.isBusy {
            state = .busy
        } else if dashboard.health != .running {
            state = .error
        } else {
            state = updateAvailable ? .update : .idle
        }
        let activity = installing ? "Installing" : downloading ? "Downloading a model" : dashboard.headline
        accessibilityLabel = "voiced: \(activity)" + (updateAvailable ? ", update available" : "")
    }
}

/// How a service row presents its state.
public enum ServiceTone: Sendable, Equatable {
    case good, busy, bad, inactive
}

public struct ServiceRowState: Sendable, Equatable, Identifiable {
    public var kind: ServiceKind
    public var stateLabel: String
    public var tone: ServiceTone
    /// Engine and model, for example "kokoro · Kokoro-82M-bf16".
    public var engineLine: String?
    /// Uptime and restart count, for example "up 3 h 12 min · 2 restarts".
    public var detailLine: String
    /// Why the service is degraded, when it is.
    public var problem: String?
    /// The model to download, while the service reports `missing_model`.
    public var missingModel: String?

    public var id: ServiceKind { kind }
}

public struct Dashboard: Sendable, Equatable {
    public var health: OverallHealth
    public var headline: String
    /// Service rows; present whenever the app is connected, so the controls stay usable.
    public var rows: [ServiceRowState]
    /// Show the install steps instead of service rows.
    public var showsInstallHelp: Bool
    /// An explanation shown under the headline when there is something to explain.
    public var message: String?
    /// A command that fixes the situation, shown copyable under the message.
    public var command: String?
    /// A note above the rows while connected, such as a missing install.
    public var notice: String?
    /// A command for the notice, shown copyable.
    public var noticeCommand: String?
    /// On its way to running: connecting, starting, or loading a model.
    public var isBusy: Bool

    public init(
        health: OverallHealth, headline: String, rows: [ServiceRowState] = [],
        showsInstallHelp: Bool = false, message: String? = nil, command: String? = nil,
        notice: String? = nil, isBusy: Bool = false
    ) {
        self.health = health
        self.headline = headline
        self.rows = rows
        self.showsInstallHelp = showsInstallHelp
        self.message = message
        self.command = command
        self.notice = notice
        self.isBusy = isBusy
    }

    public var isControllable: Bool { !rows.isEmpty }
}

/// Turns protocol payloads into what the window and the icon show. Pure, so it is tested
/// without a UI.
public enum DashboardReducer {
    public static func reduce(
        connection: ConnectionState, status: StatusPayload?, socketPath: String
    ) -> Dashboard {
        switch connection {
        case .connecting:
            return Dashboard(health: .attention, headline: "Connecting", isBusy: true)
        case .unreachable(let installed, let reason):
            return unreachable(installed: installed, reason: reason, socketPath: socketPath)
        case .incompatible(let message):
            return Dashboard(health: .off, headline: "Incompatible version", message: message)
        case .connected:
            guard let status else { return Dashboard(health: .attention, headline: "Connecting", isBusy: true) }
            var dashboard = reduce(status)
            if !status.installed {
                // The watchdog answers (run by hand, say) but the LaunchDaemons are missing:
                // keep the controls, say what is missing.
                dashboard.notice = "voiced is running but will not start automatically when "
                    + "this Mac restarts. To set that up:"
                dashboard.noticeCommand = "voiced install --source ~/voiced"
            }
            return dashboard
        }
    }

    static func unreachable(installed: Bool, reason: UnreachableReason, socketPath: String) -> Dashboard {
        let notRunning = "voiced is installed, but its watchdog is not running. The watchdog "
            + "supervises both services and answers this app. Start it with:"
        switch reason {
        case .cannotConnect(.noSocket) where !installed, .cannotConnect(.notListening) where !installed:
            return Dashboard(health: .off, headline: "Not installed", showsInstallHelp: true)
        case .cannotConnect(.noSocket):
            return Dashboard(
                health: .off, headline: "Not running", message: notRunning,
                command: "voiced start watchdog")
        case .cannotConnect(.notListening):
            return Dashboard(
                health: .off, headline: "Not running",
                message: "The control socket is left over from a watchdog that stopped. "
                    + "Start the watchdog with:",
                command: "voiced start watchdog")
        case .cannotConnect(.permissionDenied):
            return Dashboard(
                health: .off, headline: "No access",
                message: "The control socket at \(socketPath) belongs to another user. voiced "
                    + "only answers the user it runs as.")
        case .cannotConnect(.closedBeforeHello):
            return Dashboard(
                health: .off, headline: "Connection refused",
                message: "The watchdog closed the connection before saying hello. It only "
                    + "accepts the user it runs as; check that voiced was installed by this user.")
        case .cannotConnect(.notASocket):
            return Dashboard(
                health: .off, headline: "Wrong socket path",
                message: "\(socketPath) is not a socket. Remove it, or point the app at the "
                    + "right socket with VOICED_CONTROL_SOCKET.")
        case .cannotConnect(.pathTooLong):
            return Dashboard(
                health: .off, headline: "Wrong socket path",
                message: "\(socketPath) is longer than macOS allows for a socket "
                    + "(\(SocketLocation.maximumPathLength) bytes).")
        case .cannotConnect(.noHello):
            return Dashboard(
                health: .off, headline: "No answer",
                message: "Something is listening on \(socketPath) but did not say hello. "
                    + "It may be stuck, or it may not be voiced.")
        case .cannotConnect(.other(let detail)):
            return Dashboard(
                health: .off, headline: "Cannot connect", message: "\(detail) (\(socketPath)).")
        case .stoppedAnswering:
            return Dashboard(
                health: .off, headline: "Not answering",
                message: "The watchdog stopped sending status. Reconnecting.")
        case .protocolError(let detail), .setupFailed(let detail):
            return Dashboard(health: .off, headline: "Cannot talk to voiced", message: detail)
        case .disconnected:
            return Dashboard(
                health: .off, headline: "Disconnected", message: "The connection to voiced closed. Reconnecting.")
        }
    }

    static func reduce(_ status: StatusPayload) -> Dashboard {
        let services = ServiceKind.allCases.map { ($0, status[$0] ?? ServiceStatus(state: .unknown)) }
        let rows = services.map { row(kind: $0.0, status: $0.1) }

        // A service switched off is as it should be; the others decide (ADR 0033).
        let active = services.filter { $0.1.state != .off }
        if active.isEmpty {
            return Dashboard(health: .off, headline: "Both services off", rows: rows)
        }
        let ready = active.filter { $0.1.state == .running && $0.1.warm }
        let stopped = services.filter { $0.1.state == .stopped }
        if ready.count == active.count {
            return Dashboard(health: .running, headline: "Running", rows: rows)
        }
        if stopped.count == services.count {
            return Dashboard(health: .off, headline: "Stopped", rows: rows)
        }
        let headline: String
        var isBusy = false
        if services.contains(where: { $0.1.state == .degraded }) {
            headline = "Degraded"
        } else if services.contains(where: { $0.1.state == .missingModel }) {
            headline = "Model not downloaded"
        } else if services.contains(where: { $0.1.state == .starting || ($0.1.state == .running && !$0.1.warm) }) {
            headline = "Starting"
            isBusy = true
        } else if let off = stopped.first {
            headline = "\(off.0.shortTitle) stopped"
        } else {
            headline = "Unknown state"
        }
        return Dashboard(health: .attention, headline: headline, rows: rows, isBusy: isBusy)
    }

    static func row(kind: ServiceKind, status: ServiceStatus) -> ServiceRowState {
        let label: String
        let tone: ServiceTone
        // `/health` says whether a warm service holds its model now (ADR 0033).
        let health = status.health?["status"]?.stringValue
        switch status.state {
        case .running where health == "loading": (label, tone) = ("Loading model", .busy)
        case .running where health == "idle": (label, tone) = ("Idle, model unloaded", .good)
        case .running where status.warm: (label, tone) = ("Running", .good)
        case .running: (label, tone) = ("Loading model", .busy)
        case .off: (label, tone) = ("Off", .inactive)
        case .starting: (label, tone) = ("Starting", .busy)
        case .degraded: (label, tone) = ("Degraded", .bad)
        case .stopped: (label, tone) = ("Stopped", .inactive)
        case .unknown: (label, tone) = ("Unknown", .inactive)
        case .missingModel: (label, tone) = ("Model not downloaded", .bad)
        }

        var details: [String] = []
        if let uptime = status.uptime, status.state != .stopped {
            details.append("up \(Formatting.duration(uptime))")
        }
        if status.restarts > 0 {
            details.append(status.restarts == 1 ? "1 restart" : "\(status.restarts) restarts")
        }
        if status.state == .off {
            details = ["Switched off in Settings"]
        }
        if details.isEmpty { details.append(status.state == .stopped ? "Not running" : "No details") }

        var problem: String?
        if status.state == .degraded {
            // `/health` carries the last error unless an API key hides it.
            let lastError = status.health?["last_error"]?.stringValue
            problem = lastError.map { "Last error: \($0)" } ?? "Not passing health checks."
        }
        // The status's `model` names it: `public_health` hides `last_error` behind an API key.
        let missingModel = status.state == .missingModel ? status.model : nil
        if status.state == .missingModel {
            problem = "The service is up, but its model is not downloaded."
        }

        return ServiceRowState(
            kind: kind, stateLabel: label, tone: tone,
            engineLine: Formatting.engineLine(engine: status.engine, model: status.model),
            detailLine: details.joined(separator: " · "), problem: problem, missingModel: missingModel)
    }
}

public enum Formatting {
    /// A compact duration: "42 s", "5 min", "3 h 12 min", "2 d 4 h".
    public static func duration(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded(.down)))
        let days = total / 86_400
        let hours = (total % 86_400) / 3_600
        let minutes = (total % 3_600) / 60
        if days > 0 { return hours > 0 ? "\(days) d \(hours) h" : "\(days) d" }
        if hours > 0 { return minutes > 0 ? "\(hours) h \(minutes) min" : "\(hours) h" }
        if minutes > 0 { return "\(minutes) min" }
        return "\(total) s"
    }

    /// "kokoro · Kokoro-82M-bf16": the engine name and the last part of the model id.
    public static func engineLine(engine: String?, model: String?) -> String? {
        let shortModel = model.flatMap { $0.split(separator: "/").last.map(String.init) }
        switch (engine, shortModel) {
        case let (engine?, model?) where engine.caseInsensitiveCompare(model) != .orderedSame:
            return "\(engine) · \(model)"
        case let (engine?, _): return engine
        case let (nil, model?): return model
        case (nil, nil): return nil
        }
    }
}

/// How the window describes the warnings `settings.set` can return.
public enum SettingsWarning {
    public static func describe(_ code: String) -> String {
        if code == "lan_without_api_key" { return lanWithoutKey }
        if code.hasPrefix("env_override:") {
            let key = code.dropFirst("env_override:".count)
            return "Saved, but a VOICED_ environment variable overrides \(key), so the change "
                + "has no effect until that variable is removed."
        }
        return "voiced warned: \(code)."
    }

    /// Wyoming, which Home Assistant uses, has no authentication, so a key protects only the
    /// HTTP API.
    public static let lanWithoutKey = "Open to your local network. Home Assistant connects "
        + "without a key; set an API key to protect the HTTP API."
}
