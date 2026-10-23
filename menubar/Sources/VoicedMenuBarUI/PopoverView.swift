import AppKit
import ServiceManagement
import SwiftUI
import VoicedControl

/// The menu bar icon in its three states (ADR 0018), with a spoken label. Each is a template
/// image, so the menu bar draws it for light and dark and for a selected item.
public struct MenuBarIcon: View {
    let dashboard: Dashboard
    let installing: Bool
    let downloading: Bool

    public init(dashboard: Dashboard, installing: Bool = false, downloading: Bool = false) {
        self.dashboard = dashboard
        self.installing = installing
        self.downloading = downloading
    }

    public var body: some View {
        let status = MenuBarIconStatus(dashboard: dashboard, installing: installing, downloading: downloading)
        Image(nsImage: MenuBarIconImage.image(for: status.state))
            .accessibilityLabel(status.accessibilityLabel)
    }
}

/// The icon's images. Idle and error are SF Symbols; busy is drawn here, since SF Symbols has
/// no waveform with a progress badge: the waveform with a filled circle cut out of its corner
/// and three dots knocked out of the circle, the way the symbols draw their own badges.
@MainActor
public enum MenuBarIconImage {
    static let pointSize: CGFloat = 15

    public static func image(for state: MenuBarIconState) -> NSImage {
        switch state {
        case .idle: idle
        case .busy: busy
        case .error: error
        }
    }

    static let idle = symbol("waveform")
    static let error = symbol("waveform.badge.exclamationmark")
    static let busy: NSImage = {
        let wave = symbol("waveform")
        // The badge sits on the bottom right and sticks out past the waveform, as the error
        // symbol's does.
        let diameter = (wave.size.height * 0.56).rounded()
        let size = NSSize(width: (wave.size.width + diameter * 0.3).rounded(), height: wave.size.height)
        let image = NSImage(size: size, flipped: false) { rect in
            wave.draw(in: NSRect(origin: .zero, size: wave.size))
            guard let context = NSGraphicsContext.current else { return true }
            let badge = NSRect(x: rect.maxX - diameter, y: rect.minY, width: diameter, height: diameter)
            context.compositingOperation = .destinationOut
            NSBezierPath(ovalIn: badge.insetBy(dx: -1.5, dy: -1.5)).fill()
            context.compositingOperation = .sourceOver
            NSColor.black.setFill()
            NSBezierPath(ovalIn: badge).fill()
            context.compositingOperation = .destinationOut
            let dot = max(1.5, diameter / 6)
            for index in -1...1 {
                let x = badge.midX + CGFloat(index) * dot * 1.6 - dot / 2
                NSBezierPath(ovalIn: NSRect(x: x, y: badge.midY - dot / 2, width: dot, height: dot)).fill()
            }
            context.compositingOperation = .sourceOver
            return true
        }
        image.isTemplate = true
        return image
    }()

    private static func symbol(_ name: String) -> NSImage {
        let configuration = NSImage.SymbolConfiguration(pointSize: pointSize, weight: .regular)
        let image = NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(configuration) ?? NSImage(size: NSSize(width: pointSize, height: pointSize))
        image.isTemplate = true
        return image
    }
}

/// The window behind the menu bar icon: status and one-click actions. Settings live in the
/// standard Settings window, opened from here.
public struct PopoverView: View {
    let model: AppModel
    /// The bundle carries what the installer needs; "not installed" then offers it.
    let canInstall: Bool
    /// What this launch found; nil where nothing is checked, as in some snapshots.
    let launch: LaunchChecks?
    /// "Update voiced" on the version card of an install the app manages; nil hides it.
    let update: (() -> Void)?
    /// "Repair voiced…" while an install the app manages does not answer; nil hides it.
    let repair: (() -> Void)?
    /// The model download's progress row and the Download of a missing model.
    let models: ModelManager
    /// The app's own updates (ADR 0034); nil in a build without a feed, which hides them.
    let updates: AppUpdates?
    @Environment(\.openSettings) private var openSettings
    @Environment(\.openWindow) private var openWindow

    public init(
        model: AppModel, models: ModelManager, canInstall: Bool = false, launch: LaunchChecks? = nil,
        update: (() -> Void)? = nil, repair: (() -> Void)? = nil, updates: AppUpdates? = nil
    ) {
        self.model = model
        self.canInstall = canInstall
        self.launch = launch
        self.update = update
        self.repair = repair
        self.models = models
        self.updates = updates
    }

    /// Update and Repair wait for a model download or delete (ADR 0022).
    private var downloading: Bool { models.isBusy }

    public var body: some View {
        let dashboard = model.dashboard
        VStack(alignment: .leading, spacing: 0) {
            HeaderView(dashboard: dashboard)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
            Divider()
            if let updates {
                AppUpdateCard(updates: updates)
            }
            if let launch {
                LaunchCards(launch: launch, update: downloading ? nil : update.map(inInstaller))
            }
            if dashboard.showsInstallHelp {
                InstallHelpView(openInstaller: installAction)
                    .padding(14)
            } else if dashboard.isControllable {
                ServiceList(model: model, dashboard: dashboard, models: models)
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    MessageView(dashboard: dashboard, isConnecting: model.connection == .connecting)
                    if let repairAction {
                        Button("Repair voiced…", action: repairAction)
                    }
                }
                .padding(14)
            }
            // Outside the service rows: a download goes on while the services or the
            // watchdog are down, and Cancel must stay in reach.
            if models.isDownloading {
                DownloadProgressRow(models: models)
                    .padding(.horizontal, 14)
                    .padding(.bottom, 12)
            }
            NoticeStrip(model: model) {
                // The LAN warning's action: the key is set on the Network pane.
                SettingsPane.network.select()
                showSettings()
            }
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
                if let updates {
                    ActionRow(title: "Check for Updates…", symbol: "arrow.down.circle", action: updates.check)
                        .disabled(updates.isBusy)
                }
                ActionRow(title: "Settings…", symbol: "gearshape", shortcut: "⌘,", action: showSettings)
                    .keyboardShortcut(",")
            }
            .padding(6)
            Divider()
            FooterView(model: model)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
        }
        .frame(width: 340)
        // While the window is open: the Login Items card clears soon after the switch is on.
        .task { await launch?.watch() }
    }

    private func showSettings() {
        SettingsWindow.show(openSettings)
    }

    /// What "Install voiced…" does, or nil to show the guide instead: also nil while the app
    /// runs translocated.
    var installAction: (() -> Void)? {
        // A translocated app asks to be moved first (its card says so).
        guard canInstall, launch?.allowsInstall ?? true else { return nil }
        return openInstaller
    }

    /// Repair, offered while an install the app made is in place but does not answer
    /// (ADR 0018, Repair).
    var repairAction: (() -> Void)? {
        guard let repair, !downloading, case .unreachable(installed: true, _) = model.connection,
            launch?.installRecord?.source == .app
        else { return nil }
        return inInstaller(repair)
    }

    /// Starts `action` and shows the installer window, where it runs.
    private func inInstaller(_ action: @escaping () -> Void) -> () -> Void {
        {
            action()
            openInstaller()
        }
    }

    private func openInstaller() {
        NSApplication.shared.activate()
        openWindow(id: InstallerView.windowID)
    }
}

/// Opening the Settings window from a menu bar app: the app is not active, so the window
/// would open behind whatever is in front. Activate first, then bring the window forward once
/// SwiftUI has created it.
@MainActor
public enum SettingsWindow {
    public static func show(_ openSettings: OpenSettingsAction) {
        show { openSettings() }
    }

    public static func show(_ open: () -> Void) {
        NSApplication.shared.activate()
        open()
        DispatchQueue.main.async {
            let window = NSApplication.shared.windows.first {
                $0.identifier?.rawValue.contains("Settings") == true
            }
            window?.makeKeyAndOrderFront(nil)
            window?.orderFrontRegardless()
        }
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
    let models: ModelManager

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let notice = dashboard.notice {
                VStack(alignment: .leading, spacing: 6) {
                    Notice(symbol: "info.circle.fill", tint: .secondary, text: notice)
                    if let command = dashboard.noticeCommand {
                        CommandBlock(command: command)
                    }
                }
            }
            ForEach(dashboard.rows) { row in
                ServiceRowView(
                    row: row, isRestarting: model.restarting.contains(row.kind),
                    restart: { Task { await model.restart(.service(row.kind)) } },
                    openLogs: { Task { await model.open(.logsDirectory) } },
                    download: download(row),
                    message: row.missingModel.flatMap { models.messages[$0] })
            }
        }
        .padding(14)
    }

    /// Download for a service whose model is missing, then the restart that loads it.
    private func download(_ row: ServiceRowState) -> (() -> Void)? {
        guard let id = row.missingModel, !models.isDownloading, models.deleting == nil else { return nil }
        return { Task { await models.download(id) } }
    }
}

struct ServiceRowView: View {
    let row: ServiceRowState
    let isRestarting: Bool
    let restart: () -> Void
    let openLogs: () -> Void
    /// Offered while the service's model is not downloaded.
    var download: (() -> Void)?
    /// Why the last download of that model failed.
    var message: ModelMessage?

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
                if let message {
                    ModelMessageView(message: message)
                }
                if let download {
                    Button("Download", action: download)
                        .buttonStyle(.link)
                        .font(.caption)
                } else if row.problem != nil, row.missingModel == nil {
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
                            .frame(width: 22, height: 22)
                            .contentShape(Rectangle())
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
    /// Offered on the LAN warning in the menu bar window (it opens the Network pane); the
    /// Settings window has none.
    var openSettings: (() -> Void)?

    var body: some View {
        let restart = model.restartRequired
        let others = model.otherWarnings
        if model.showsLANWarning || !restart.isEmpty || !others.isEmpty || model.errorMessage != nil {
            VStack(spacing: 8) {
                if model.showsLANWarning {
                    Notice(
                        symbol: "exclamationmark.triangle.fill", tint: .orange,
                        text: SettingsWarning.lanWithoutKey,
                        actionTitle: openSettings == nil ? nil : "Set Key"
                    ) { openSettings?() }
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
    /// Opens the installer; without it the guide and the terminal commands are shown.
    var openInstaller: (() -> Void)?

    var body: some View {
        if let openInstaller {
            VStack(alignment: .leading, spacing: 10) {
                Text("voiced is not installed on this Mac.")
                    .font(.body.weight(.medium))
                Text("The installer sets up voiced, downloads its speech models and starts its background services.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button(action: openInstaller) {
                    Label("Install voiced…", systemImage: "arrow.down.circle")
                }
                .buttonStyle(.borderedProminent)
            }
        } else {
            guideAndCommands
        }
    }

    private var guideAndCommands: some View {
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
            Text("With the prerequisites in place, run:")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 4)
            CommandBlock(command: "voiced install --source ~/voiced")
            Text("It ends with the one command to run with administrator rights.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
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
            // The app's version, as a menu's last, disabled line; the services' is in About.
            Text("voiced \(VoicedPaths.appVersion)")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.borderless)
                .keyboardShortcut("q")
                .help("Quit the menu bar app. The voiced services keep running.")
        }
    }
}

/// What this launch found that needs the user (ADR 0018, "On every launch"): an app run from
/// where it was downloaded, the services switched off in Login Items, or services that run
/// another voiced than this app carries.
struct LaunchCards: View {
    let launch: LaunchChecks
    var update: (() -> Void)?

    var body: some View {
        let mismatch = launch.installMatch.flatMap(Self.notice)
        if launch.translocatedFrom != nil || launch.backgroundItemsOff || mismatch != nil {
            VStack(spacing: 8) {
                if let original = launch.translocatedFrom {
                    Notice(
                        symbol: "folder.badge.questionmark", tint: .orange,
                        text: "Move voiced to your Applications folder, then open it from there. "
                            + "macOS runs it from a temporary, read-only copy until then.",
                        actionTitle: "Show in Finder"
                    ) { NSWorkspace.shared.activateFileViewerSelecting([original]) }
                }
                if launch.backgroundItemsOff {
                    Notice(
                        symbol: "exclamationmark.triangle.fill", tint: .orange,
                        text: "voiced can't start at boot while it is off in Login Items.",
                        actionTitle: "Open Login Items"
                    ) { SMAppService.openSystemSettingsLoginItems() }
                }
                if let mismatch {
                    VStack(alignment: .leading, spacing: 6) {
                        let offersUpdate = mismatch.managed && update != nil
                        Notice(
                            symbol: "arrow.triangle.2.circlepath", tint: .accentColor, text: mismatch.text,
                            actionTitle: offersUpdate ? "Update voiced" : nil
                        ) { update?() }
                        if let command = mismatch.command { CommandBlock(command: command) }
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
        }
    }

    /// What the card says for a mismatch, a command that resolves it when another tool
    /// manages the install, and whether the app manages it (and so offers to update it).
    nonisolated static func notice(_ match: InstallMatch) -> (text: String, command: String?, managed: Bool)? {
        guard case .differs(let installed, let bundled, let source, let sameVersion) = match else { return nil }
        let running = sameVersion
            ? "The services run voiced \(installed) with other dependencies than this app carries."
            : "The services run voiced \(installed); this app carries \(bundled)."
        switch source {
        case .app:
            return (running, nil, true)
        case .homebrew:
            return ("\(running) Homebrew manages this install; update it with:", "brew upgrade voiced", false)
        case .cli:
            return (
                "\(running) This install was made from the command line; update it from your checkout with:",
                ExternalInstall.commands(for: .cli).first?.command, false
            )
        }
    }
}
