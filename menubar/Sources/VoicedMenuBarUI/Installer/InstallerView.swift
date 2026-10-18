import AppKit
import SwiftUI
import VoicedControl

/// The installer window: one screen per phase, each saying what it does in one sentence.
public struct InstallerView: View {
    /// The installer's `Window` scene.
    public static let windowID = "installer"

    let installer: Installer
    let loginItem: LoginItem
    /// Closes the window, from Done and from the refusal.
    let close: () -> Void

    public init(installer: Installer, loginItem: LoginItem, close: @escaping () -> Void) {
        self.installer = installer
        self.loginItem = loginItem
        self.close = close
    }

    public var body: some View {
        Group {
            switch installer.phase {
            case .welcome: WelcomeScreen(installer: installer)
            case .environment(let step): EnvironmentScreen(step: step)
            case .installing(let progress): InstallingScreen(progress: progress)
            case .approval(let firewallOn): ApprovalScreen(installer: installer, firewallOn: firewallOn, waiting: false)
            case .approving: ApprovalScreen(installer: installer, firewallOn: false, waiting: true)
            case .starting: StartingScreen(warning: installer.warning)
            case .homeAssistant(let stage): HomeAssistantScreen(installer: installer, stage: stage)
            case .done: DoneScreen(warning: installer.warning, loginItem: loginItem, close: close)
            case .failed(let failure): FailureScreen(installer: installer, failure: failure)
            case .refused(let message): RefusedScreen(message: message, close: close)
            }
        }
        .padding(24)
        .frame(width: 520, alignment: .topLeading)
        .frame(minHeight: 400, alignment: .topLeading)
    }
}

/// A title, its one sentence, the screen's content, then its buttons at the bottom right.
private struct Screen<Content: View, Buttons: View>: View {
    let symbol: String
    var tint: Color = .accentColor
    let title: String
    let sentence: String
    @ViewBuilder let content: Content
    @ViewBuilder let buttons: Buttons

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 30))
                    .foregroundStyle(tint)
                    .frame(width: 40)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.title2.weight(.semibold))
                    Text(sentence)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            content
                .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
            HStack(spacing: 8) {
                Spacer()
                buttons
            }
        }
    }
}

private struct Working: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).foregroundStyle(.secondary)
        }
    }
}

enum ByteText {
    static func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func line(_ download: InstallProgress.Download) -> String {
        var parts = ["\(size(download.bytes)) of \(size(download.total))"]
        if download.isFinished { return parts[0] }
        if let speed = download.bytesPerSecond, speed > 0 { parts.append("\(size(Int64(speed)))/s") }
        if let seconds = download.secondsRemaining {
            let left = Duration.seconds(max(1, seconds.rounded()))
                .formatted(.units(allowed: [.hours, .minutes, .seconds], width: .wide, maximumUnitCount: 1))
            parts.append("\(left) left")
        }
        return parts.joined(separator: " · ")
    }
}

/// The installer in its own window; Done and Close dismiss it.
public struct InstallerWindow: View {
    let installer: Installer
    let loginItem: LoginItem
    @Environment(\.dismissWindow) private var dismissWindow

    public init(installer: Installer, loginItem: LoginItem) {
        self.installer = installer
        self.loginItem = loginItem
    }

    public var body: some View {
        InstallerView(installer: installer, loginItem: loginItem) {
            dismissWindow(id: InstallerView.windowID)
        }
    }
}

// MARK: - Screens

private struct WelcomeScreen: View {
    let installer: Installer

    var body: some View {
        let welcome = installer.welcome
        Screen(
            symbol: "waveform", title: "Install voiced",
            sentence: "voiced turns speech into text and text into speech on this Mac, for Home Assistant and other devices."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                Place(symbol: "folder", text: "Python and the voiced environment go in ~/.voiced.")
                Place(symbol: "waveform.circle", text: "The speech models go in the Hugging Face cache.")
                Place(
                    symbol: "gearshape.2",
                    text: "Three background services run as you, and start at boot before anyone logs in.")
                Place(symbol: "trash", text: "Moving this app to the Trash leaves the services running.")
                Divider().padding(.vertical, 2)
                Text("Downloads about \(ByteText.size(welcome.downloadBytes)) and needs \(ByteText.size(welcome.requiredBytes)) free while it installs.")
                    .fixedSize(horizontal: false, vertical: true)
                if let available = welcome.availableBytes {
                    Text(
                        welcome.hasRoom
                            ? "\(ByteText.size(available)) is free on this disk."
                            : "Only \(ByteText.size(available)) is free on this disk. Make room, then reopen this window."
                    )
                    .foregroundStyle(welcome.hasRoom ? Color.secondary : Color.red)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }
        } buttons: {
            Button(welcome.resuming ? "Continue" : "Install", action: installer.install)
                .keyboardShortcut(.defaultAction)
                .disabled(!welcome.hasRoom)
        }
    }
}

private struct Place: View {
    let symbol: String
    let text: String

    var body: some View {
        Label {
            Text(text).fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: symbol).foregroundStyle(.secondary).frame(width: 20)
        }
    }
}

private struct EnvironmentScreen: View {
    let step: String

    var body: some View {
        Screen(
            symbol: "shippingbox", title: "Setting up Python",
            sentence: "voiced installs its own Python and packages into ~/.voiced. This takes about half a minute."
        ) {
            Working(text: step)
        } buttons: {
            EmptyView()
        }
    }
}

private struct InstallingScreen: View {
    let progress: InstallProgress

    var body: some View {
        Screen(
            symbol: "arrow.down.circle", title: "Installing voiced",
            sentence: "voiced downloads its speech models and checks that they load."
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if let current = progress.current {
                    Working(text: current.title)
                } else {
                    Working(text: "Starting")
                }
                ForEach(progress.downloads) { download in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(download.model)
                            .font(.callout.weight(.medium))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        ProgressView(value: download.fraction)
                        Text(ByteText.line(download))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        } buttons: {
            EmptyView()
        }
    }
}

private struct ApprovalScreen: View {
    let installer: Installer
    let firewallOn: Bool
    let waiting: Bool

    var body: some View {
        Screen(
            symbol: "lock.shield", title: "Install background services",
            sentence: "To start voiced at boot, macOS needs an administrator to install three background services. They run as you, not as root."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                if firewallOn {
                    Text("Your firewall is on; voiced will allow its services to accept connections.")
                        .fixedSize(horizontal: false, vertical: true)
                }
                if waiting { Working(text: "Waiting for approval") }
            }
        } buttons: {
            Button("Install Background Services", action: installer.approve)
                .keyboardShortcut(.defaultAction)
                .disabled(waiting)
        }
    }
}

private struct StartingScreen: View {
    let warning: String?

    var body: some View {
        Screen(
            symbol: "bolt.circle", title: "Starting voiced",
            sentence: "The services are loading their models, which can take a minute the first time."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                Working(text: "Waiting for voiced")
                WarningNotice(warning: warning)
            }
        } buttons: {
            EmptyView()
        }
    }
}

private struct WarningNotice: View {
    let warning: String?

    var body: some View {
        if let warning {
            Notice(symbol: "exclamationmark.triangle.fill", tint: .orange, text: warning)
        }
    }
}

private struct HomeAssistantScreen: View {
    let installer: Installer
    let stage: HomeAssistantStage

    var body: some View {
        switch stage {
        case .asking, .applying:
            Screen(
                symbol: "house", title: "Connect Home Assistant",
                sentence: "Use voiced with Home Assistant on another device?"
            ) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Yes lets devices on your network reach voiced and announces it so Home Assistant can discover it. No keeps voiced on this Mac only; you can change this later in Settings.")
                        .fixedSize(horizontal: false, vertical: true)
                    if stage == .applying { Working(text: "Applying") }
                    if let message = installer.errorMessage {
                        Notice(symbol: "xmark.circle.fill", tint: .red, text: message)
                    }
                    WarningNotice(warning: installer.warning)
                }
            } buttons: {
                Button("No, This Mac Only") { installer.connectHomeAssistant(false) }
                    .disabled(stage == .applying)
                Button("Yes") { installer.connectHomeAssistant(true) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(stage == .applying)
            }
        case .connected(let info):
            Screen(
                symbol: "house", title: "Connect Home Assistant",
                sentence: "Home Assistant finds voiced under Settings > Devices & services > Discovered."
            ) {
                ConnectionDetails(installer: installer, info: info)
            } buttons: {
                Button("Continue", action: installer.continueToDone)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }
}

private struct ConnectionDetails: View {
    let installer: Installer
    let info: HomeAssistantInfo

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 6) {
                row("This Mac", info.identity.host)
                if let address = info.identity.address { row("Address", address) }
                row("Speech to text", "\(info.sttPort)")
                row("Text to speech", "\(info.ttsPort)")
            }
            Text("The API key protects the HTTP API only. Wyoming has no authentication, so anyone on your network can use ports \(String(info.sttPort)) and \(String(info.ttsPort)).")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let key = info.apiKey {
                Text("Your API key, shown once:").font(.callout)
                CommandBlock(command: key)
            } else {
                Button("Generate API Key", action: installer.generateAPIKey)
            }
            if let message = installer.errorMessage {
                Notice(symbol: "xmark.circle.fill", tint: .red, text: message)
            }
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            CommandBlock(command: value)
        }
    }
}

private struct DoneScreen: View {
    let warning: String?
    @Bindable var loginItem: LoginItem
    let close: () -> Void

    var body: some View {
        Screen(
            symbol: "checkmark.circle", tint: .green, title: "voiced is running",
            sentence: "voiced appears in Login Items as a background item; leave it on so the services start at boot."
        ) {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(
                    "Open voiced at login",
                    isOn: Binding(get: { loginItem.isEnabled }, set: { loginItem.setEnabled($0) })
                )
                .disabled(!loginItem.isAvailable)
                Text("Only the menu bar icon needs this; the services run without the app.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                WarningNotice(warning: warning)
            }
        } buttons: {
            Button("Done", action: close)
                .keyboardShortcut(.defaultAction)
        }
    }
}

private struct FailureScreen: View {
    let installer: Installer
    let failure: InstallFailure

    var body: some View {
        Screen(symbol: "xmark.octagon", tint: .red, title: failure.step, sentence: failure.cause) {
            if !failure.output.isEmpty {
                Text(failure.output.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
        } buttons: {
            Button("Try Again", action: installer.retry)
                .keyboardShortcut(.defaultAction)
        }
    }
}

private struct RefusedScreen: View {
    let message: String
    let close: () -> Void

    var body: some View {
        Screen(
            symbol: "person.2", tint: .secondary, title: "Installed for another account", sentence: message
        ) {
            EmptyView()
        } buttons: {
            Button("Close", action: close)
                .keyboardShortcut(.defaultAction)
        }
    }
}
