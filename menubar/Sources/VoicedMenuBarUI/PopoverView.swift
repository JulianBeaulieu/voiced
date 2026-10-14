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

/// Everything in the menu bar window.
public struct PopoverView: View {
    @Bindable var model: AppModel
    let loginItem: LoginItem
    let settingsHeight: CGFloat

    /// `settingsHeight` is the height of the scrolling settings form.
    public init(model: AppModel, loginItem: LoginItem, settingsHeight: CGFloat = 520) {
        self.model = model
        self.loginItem = loginItem
        self.settingsHeight = settingsHeight
    }

    private var showsSettings: Bool {
        model.tab == .settings && model.dashboard.isControllable && model.settings != nil
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HeaderView(model: model)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
            Divider()
            if showsSettings {
                SettingsPane(model: model, loginItem: loginItem, height: settingsHeight)
            } else {
                StatusPane(model: model)
            }
            if let message = model.errorMessage {
                Divider()
                ErrorBanner(message: message) { model.errorMessage = nil }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
            }
            Divider()
            FooterView(model: model)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        .frame(width: 340)
    }
}

struct HeaderView: View {
    @Bindable var model: AppModel

    var body: some View {
        let dashboard = model.dashboard
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
            if dashboard.isControllable && model.settings != nil {
                Picker("View", selection: $model.tab) {
                    Text("Status").tag(AppModel.Tab.status)
                    Text("Settings").tag(AppModel.Tab.settings)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
        }
        .accessibilityElement(children: .contain)
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

struct StatusPane: View {
    @Bindable var model: AppModel

    var body: some View {
        let dashboard = model.dashboard
        VStack(alignment: .leading, spacing: 0) {
            if dashboard.showsInstallHelp {
                InstallHelpView()
                    .padding(14)
            } else if dashboard.isControllable {
                VStack(spacing: 10) {
                    ForEach(dashboard.rows) { row in
                        ServiceRowView(
                            row: row, isRestarting: model.restarting.contains(row.kind)
                        ) {
                            Task { await model.restart(.service(row.kind)) }
                        }
                    }
                }
                .padding(14)
                if model.showsLANWarning {
                    Notice(
                        symbol: "exclamationmark.triangle.fill", tint: .orange,
                        text: "voiced listens on your local network without an API key. "
                            + "Anyone on the network can use it.",
                        actionTitle: "Set Key"
                    ) { model.tab = .settings }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 10)
                }
                if !model.restartRequired.isEmpty {
                    Notice(
                        symbol: "arrow.triangle.2.circlepath", tint: .accentColor,
                        text: restartText(model.restartRequired),
                        actionTitle: "Restart"
                    ) { Task { await model.restartPending() } }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 10)
                }
                Divider()
                VStack(spacing: 0) {
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
                .padding(.vertical, 6)
                .padding(.horizontal, 6)
            } else {
                MessageView(dashboard: dashboard, isConnecting: model.connection == .connecting)
                    .padding(14)
            }
        }
    }

    private func restartText(_ kinds: Set<ServiceKind>) -> String {
        let names = ServiceKind.allCases.filter(kinds.contains).map { $0.title.lowercased() }
        return "Restart \(names.joined(separator: " and ")) to apply your changes."
    }
}

struct ServiceRowView: View {
    let row: ServiceRowState
    let isRestarting: Bool
    let restart: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "circle.fill")
                .font(.system(size: 9))
                .foregroundStyle(row.tone.color)
                .padding(.top, 5)
                .accessibilityHidden(true)
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
            }
            Spacer(minLength: 8)
            Group {
                if isRestarting {
                    ProgressView().controlSize(.small)
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
        .accessibilityElement(children: .combine)
    }
}

/// A full-width, menu-like button row with a hover highlight.
struct ActionRow: View {
    let title: String
    let symbol: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: symbol)
                    .frame(width: 20)
                    .accessibilityHidden(true)
                Text(title)
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
    let actionTitle: String
    let action: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .accessibilityHidden(true)
            Text(text)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            Button(actionTitle, action: action)
                .controlSize(.small)
        }
        .padding(10)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            Text(message)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 4)
            Button("Dismiss", action: dismiss)
                .buttonStyle(.borderless)
                .controlSize(.small)
        }
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
            }
            if let command = dashboard.command {
                CommandBlock(command: command)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct InstallHelpView: View {
    static let readme = URL(string: "https://github.com/JulianBeaulieu/voiced#quickstart")!

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("voiced is not installed on this Mac.")
                .font(.body.weight(.medium))
            Text("After installing the prerequisites from the README, run:")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            CommandBlock(command: "voiced install --source ~/voiced")
            Text("Then run the root step it prints:")
                .foregroundStyle(.secondary)
            CommandBlock(command: "sudo bash ~/.config/voiced/launchd/install-daemons.sh")
            Link(destination: Self.readme) {
                Label("Installation guide", systemImage: "book")
            }
            .padding(.top, 2)
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
