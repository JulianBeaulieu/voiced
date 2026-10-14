import Foundation

/// How far the app got in talking to voiced.
public enum ConnectionState: Sendable, Equatable {
    case connecting
    case connected(version: String)
    /// The socket is not there or refuses connections.
    case unreachable(installed: Bool)
    /// voiced answered with a protocol this app does not speak.
    case incompatible(message: String)
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
    /// Warmth, uptime and restart count, for example "Warm · up 3 h 12 min".
    public var detailLine: String

    public var id: ServiceKind { kind }
}

public struct Dashboard: Sendable, Equatable {
    public var health: OverallHealth
    public var headline: String
    public var rows: [ServiceRowState]
    /// Show the install commands instead of service rows.
    public var showsInstallHelp: Bool
    /// An explanation shown under the headline when there is something to explain.
    public var message: String?
    /// A command that fixes the situation, shown copyable under the message.
    public var command: String?

    public init(
        health: OverallHealth, headline: String, rows: [ServiceRowState],
        showsInstallHelp: Bool = false, message: String? = nil, command: String? = nil
    ) {
        self.health = health
        self.headline = headline
        self.rows = rows
        self.showsInstallHelp = showsInstallHelp
        self.message = message
        self.command = command
    }

    public var isControllable: Bool { !rows.isEmpty }
}

/// Turns protocol payloads into what the popover and the icon show. Pure, so it is tested
/// without a UI.
public enum DashboardReducer {
    public static func reduce(connection: ConnectionState, status: StatusPayload?) -> Dashboard {
        switch connection {
        case .connecting:
            return Dashboard(
                health: .attention, headline: "Connecting", rows: [], showsInstallHelp: false,
                message: nil)
        case .unreachable(installed: false):
            return notInstalled
        case .unreachable(installed: true):
            return Dashboard(
                health: .off, headline: "Not running", rows: [],
                message: "voiced is installed, but its watchdog is not running. The watchdog "
                    + "supervises both services and answers this app. Start it with:",
                command: "voiced start watchdog")
        case .incompatible(let message):
            return Dashboard(
                health: .off, headline: "Incompatible version", rows: [],
                showsInstallHelp: false, message: message)
        case .connected:
            guard let status else {
                return Dashboard(
                    health: .attention, headline: "Connecting", rows: [],
                    showsInstallHelp: false, message: nil)
            }
            guard status.installed else { return notInstalled }
            return reduce(status)
        }
    }

    static let notInstalled = Dashboard(
        health: .off, headline: "Not installed", rows: [], showsInstallHelp: true, message: nil)

    static func reduce(_ status: StatusPayload) -> Dashboard {
        let services = ServiceKind.allCases.map { ($0, status[$0] ?? ServiceStatus(state: .unknown)) }
        let rows = services.map { row(kind: $0.0, status: $0.1) }

        let ready = services.filter { $0.1.state == .running && $0.1.warm }
        let stopped = services.filter { $0.1.state == .stopped }
        let health: OverallHealth
        let headline: String
        if ready.count == services.count {
            health = .running
            headline = "Running"
        } else if stopped.count == services.count {
            health = .off
            headline = "Stopped"
        } else {
            health = .attention
            if services.contains(where: { $0.1.state == .degraded }) {
                headline = "Degraded"
            } else if services.contains(where: { $0.1.state == .starting || ($0.1.state == .running && !$0.1.warm) }) {
                headline = "Starting"
            } else if let off = stopped.first {
                headline = "\(off.0.shortTitle) stopped"
            } else {
                headline = "Unknown state"
            }
        }
        return Dashboard(
            health: health, headline: headline, rows: rows, showsInstallHelp: false, message: nil)
    }

    static func row(kind: ServiceKind, status: ServiceStatus) -> ServiceRowState {
        let label: String
        let tone: ServiceTone
        switch status.state {
        case .running where status.warm:
            (label, tone) = ("Running", .good)
        case .running:
            (label, tone) = ("Loading model", .busy)
        case .starting:
            (label, tone) = ("Starting", .busy)
        case .degraded:
            (label, tone) = ("Degraded", .bad)
        case .stopped:
            (label, tone) = ("Stopped", .inactive)
        case .unknown:
            (label, tone) = ("Unknown", .inactive)
        }

        var details: [String] = []
        if status.state == .running || status.state == .degraded {
            details.append(status.warm ? "Warm" : "Cold")
        }
        if let uptime = status.uptime, status.state != .stopped {
            details.append("up \(Formatting.duration(uptime))")
        }
        if status.restarts > 0 {
            details.append(status.restarts == 1 ? "1 restart" : "\(status.restarts) restarts")
        }
        if details.isEmpty { details.append(status.state == .stopped ? "Not running" : "No details") }

        return ServiceRowState(
            kind: kind, stateLabel: label, tone: tone,
            engineLine: Formatting.engineLine(engine: status.engine, model: status.model),
            detailLine: details.joined(separator: " · "))
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

    /// The last path component of a model id, for pickers.
    public static func shortModel(_ model: String) -> String {
        model.split(separator: "/").last.map(String.init) ?? model
    }
}
