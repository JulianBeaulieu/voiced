import AppKit
import SwiftUI
import VoicedControl

/// The menu bar icon: one of three SF Symbols, with a spoken label.
public struct MenuBarIcon: View {
    let dashboard: Dashboard

    public init(dashboard: Dashboard) {
        self.dashboard = dashboard
    }

    public var body: some View {
        Image(systemName: dashboard.health.symbolName)
            .accessibilityLabel("voiced: \(dashboard.headline)")
    }
}

/// The window behind the menu bar icon: status and one-click actions. Settings live in the
/// standard Settings window, opened from here.
public struct PopoverView: View {
    let model: AppModel
    @Environment(\.openSettings) private var openSettings

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some View {
        let dashboard = model.dashboard
        VStack(alignment: .leading, spacing: 0) {
            HeaderView(dashboard: dashboard)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
            Divider()
            if dashboard.showsInstallHelp {
                InstallHelpView()
                    .padding(14)
            } else if dashboard.isControllable {
                ServiceList(model: model, dashboard: dashboard)
            } else {
                MessageView(dashboard: dashboard, isConnecting: model.connection == .connecting)
                    .padding(14)
            }
            NoticeStrip(model: model)
            Divider()
            VStack(spacing: 0) {
                if dashboard.isControllable {
                    ActionRow(title: "Open Status Page", symbol: "safari") {
                        Task { await model.open(.statusPage) }
                    }
                    ActionRow(title: "Open Logs Folder", symbol: "doc.text.magnifyingglass") {
                        Task { await model.open(.logsDirectory) }
                    }
                    ActionRow(title: "Open Config File", symbol: "doc.badge.gearshape") {
                        Task { await model.open(.configFile) }
                    }
                    ActionRow(title: "Restart Both Services", symbol: "arrow.clockwise") {
                        Task { await model.restart(.all) }
                    }
                    .disabled(!model.restarting.isEmpty)
                }
                ActionRow(title: "Settings…", symbol: "gearshape", shortcut: "⌘,") {
                    NSApplication.shared.activate()
                    openSettings()
                }
                .keyboardShortcut(",")
                .disabled(model.settings == nil)
            }
            .padding(6)
            Divider()
            FooterView(model: model)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        .frame(width: 340)
    }
}

struct HeaderView: View {
    let dashboard: Dashboard

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: dashboard.health.symbolName)
                .font(.title2)
                .foregroundStyle(dashboard.health.tint)
                .frame(width: 28)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("voiced").font(.headline)
                Text(dashboard.headline)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
        }
        .accessibilityElement(children: .combine)
    }
}

extension OverallHealth {
    var tint: Color {
        switch self {
        case .running: .green
        case .attention: .orange
        case .off: .secondary
        }
    }
}

extension ServiceTone {
    var color: Color {
        switch self {
        case .good: .green
        case .busy: .orange
        case .bad: .red
        case .inactive: .secondary
        }
    }
}

// MARK: - Status

struct ServiceList: View {
    let model: AppModel
    let dashboard: Dashboard

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let notice = dashboard.notice {
                Notice(symbol: "info.circle.fill", tint: .secondary, text: notice)
            }
            ForEach(dashboard.rows) { row in
                ServiceRowView(
                    row: row, isRestarting: model.restarting.contains(row.kind),
                    restart: { Task { await model.restart(.service(row.kind)) } },
                    openLogs: { Task { await model.open(.logsDirectory) } })
            }
        }
        .padding(14)
    }
}

struct ServiceRowView: View {
    let row: ServiceRowState
    let isRestarting: Bool
    let restart: () -> Void
    let openLogs: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "circle.fill")
                .font(.system(size: 9))
                .foregroundStyle(row.tone.color)
                .padding(.top, 5)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                // Read as one element by VoiceOver; the buttons stay separate elements.
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(row.kind.title).font(.body.weight(.medium))
                        Text(row.stateLabel)
                            .font(.callout)
                            .foregroundStyle(row.tone == .good ? Color.secondary : row.tone.color)
                    }
                    if let engine = row.engineLine {
                        Text(engine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    Text(row.detailLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                    if let problem = row.problem {
                        Text(problem)
                            .font(.caption)
                            .foregroundStyle(.primary)
                            .lineLimit(3)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
                .accessibilityElement(children: .combine)
                if row.problem != nil {
                    Button("Open Logs", action: openLogs)
                        .buttonStyle(.link)
                        .font(.caption)
                }
            }
            Spacer(minLength: 8)
            Group {
                if isRestarting {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("Restarting \(row.kind.title.lowercased())")
                } else {
                    Button(action: restart) {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                    .help("Restart \(row.kind.title.lowercased())")
                    .accessibilityLabel("Restart \(row.kind.title.lowercased())")
                }
            }
            .frame(width: 22, height: 22)
        }
    }
}

/// Things the user should act on, shown in the status window and the settings window alike.
struct NoticeStrip: View {
    let model: AppModel

    var body: some View {
        let restart = model.restartRequired
        let others = model.otherWarnings
        if model.showsLANWarning || !restart.isEmpty || !others.isEmpty || model.errorMessage != nil {
            VStack(spacing: 8) {
                if model.showsLANWarning {
                    Notice(
                        symbol: "exclamationmark.triangle.fill", tint: .orange,
                        text: SettingsWarning.lanWithoutKey)
                }
                ForEach(others, id: \.self) { warning in
                    Notice(symbol: "exclamationmark.triangle.fill", tint: .orange, text: warning)
                }
                if !restart.isEmpty {
                    Notice(
                        symbol: "arrow.triangle.2.circlepath", tint: .accentColor,
                        text: restartText(restart), actionTitle: "Restart"
                    ) { Task { await model.restartPending() } }
                }
                if let message = model.errorMessage {
                    Notice(
                        symbol: "xmark.circle.fill", tint: .red, text: message,
                        actionTitle: "Dismiss"
                    ) { model.errorMessage = nil }
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 12)
        }
    }

    private func restartText(_ kinds: Set<ServiceKind>) -> String {
        let names = ServiceKind.allCases.filter(kinds.contains).map { $0.title.lowercased() }
        return "Restart \(names.joined(separator: " and ")) to apply your changes."
    }
}

/// A full-width, menu-like button row with a hover highlight.
struct ActionRow: View {
    let title: String
    let symbol: String
    var shortcut: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: symbol)
                    .frame(width: 20)
                    .accessibilityHidden(true)
                Text(title)
                Spacer()
                if let shortcut {
                    Text(shortcut)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(MenuRowButtonStyle())
    }
}

struct MenuRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        MenuRow(configuration: configuration)
    }

    private struct MenuRow: View {
        let configuration: Configuration
        @State private var isHovered = false
        @Environment(\.isEnabled) private var isEnabled

        var body: some View {
            configuration.label
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .foregroundStyle(isEnabled ? .primary : .secondary)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(isHovered && isEnabled ? Color.primary.opacity(0.1) : .clear)
                )
                .opacity(configuration.isPressed ? 0.7 : 1)
                .onHover { isHovered = $0 }
        }
    }
}

struct Notice: View {
    let symbol: String
    let tint: Color
    let text: String
    var actionTitle: String?
    var action: () -> Void = {}

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .accessibilityHidden(true)
            Text(text)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle {
                Button(actionTitle, action: action)
                    .controlSize(.small)
            }
        }
        .padding(10)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct MessageView: View {
    let dashboard: Dashboard
    let isConnecting: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if isConnecting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Connecting to voiced")
                        .foregroundStyle(.secondary)
                }
            }
            if let message = dashboard.message {
                Text(message)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            if let command = dashboard.command {
                CommandBlock(command: command)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct InstallHelpView: View {
    static let guide = URL(string: "https://github.com/JulianBeaulieu/voiced#quickstart")

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("voiced is not installed on this Mac.")
                .font(.body.weight(.medium))
            Text("The installation guide lists what to install first and walks through every step.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let guide = Self.guide {
                Link(destination: guide) {
                    Label("Open Installation Guide", systemImage: "book")
                }
                .buttonStyle(.borderedProminent)
            }
            Text("With the prerequisites in place, from your checkout:")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 4)
            CommandBlock(command: "voiced install --source ~/voiced")
            Text("Then finish with administrator rights:")
                .foregroundStyle(.secondary)
            CommandBlock(command: "sudo bash ~/.config/voiced/launchd/install-daemons.sh")
        }
    }
}

/// A command in monospace with a copy button.
struct CommandBlock: View {
    let command: String
    @State private var copied = false

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Text(command)
                .font(.system(.callout, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(command, forType: .string)
                copied = true
            } label: {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
            }
            .buttonStyle(.borderless)
            .help("Copy")
            .accessibilityLabel("Copy command")
        }
        .padding(8)
        .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
    }
}

struct FooterView: View {
    let model: AppModel

    var body: some View {
        HStack {
            if let version = model.serverVersion {
                Text("voiced \(version)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.borderless)
                .keyboardShortcut("q")
                .help("Quit the menu bar app. The voiced services keep running.")
        }
    }
}
