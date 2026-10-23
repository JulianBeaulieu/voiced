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
            case .migration: MigrationScreen(installer: installer, waiting: false)
            case .migrating: MigrationScreen(installer: installer, waiting: true)
            case .starting: StartingScreen()
            case .homeAssistant(let stage): HomeAssistantScreen(installer: installer, stage: stage)
            case .done: DoneScreen(loginItem: loginItem, close: close)
            case .finished(let version):
                FinishedScreen(task: installer.task, version: version, close: close)
            case .confirmUninstall: ConfirmUninstallScreen(installer: installer, close: close)
            case .uninstalling(let step): UninstallingScreen(step: step)
            case .uninstalled(let choices): UninstalledScreen(choices: choices, close: close)
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

struct Working: View {
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
        Formatting.bytes(bytes)
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
        .onDisappear { installer.windowClosed() }
    }
}

// MARK: - Screens

/// The welcome screen is the setup: what to install, what goes where, and the one Install.
private struct WelcomeScreen: View {
    let installer: Installer

    var body: some View {
        let welcome = installer.welcome
        Screen(
            symbol: "waveform", title: "Install voiced",
            sentence: "voiced turns speech into text and text into speech on this Mac, for Home Assistant and other devices."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                ServiceSetup(installer: installer, kind: .stt)
                ServiceSetup(installer: installer, kind: .tts)
                Text("Settings changes the models later and has those that need a Hugging Face token.")
                    .footnote()
                Divider().padding(.vertical, 2)
                Place(symbol: "folder", text: "Python and the voiced environment go in ~/.voiced.")
                Place(symbol: "waveform.circle", text: "The speech models go in the Hugging Face cache.")
                Place(
                    symbol: "gearshape.2",
                    text: "Three background services run as you, while you are logged in, and start when you log in.")
                Place(
                    symbol: "trash",
                    text: "Moving this app to the Trash leaves the services running; use Uninstall in Settings > About first.")
                Divider().padding(.vertical, 2)
                Text("Downloads about \(ByteText.size(installer.downloadBytes)) and needs \(ByteText.size(welcome.requiredBytes)) free while it installs.")
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
                Text("No administrator password is needed: the services run as you.")
                    .foregroundStyle(.secondary)
            }
        } buttons: {
            if installer.setup.isEmpty {
                Text("Turn on at least one service.").foregroundStyle(.secondary)
            }
            Button(welcome.resuming ? "Continue" : "Install", action: installer.install)
                .keyboardShortcut(.defaultAction)
                .disabled(!welcome.hasRoom || installer.setup.isEmpty)
        }
    }
}

/// One service on the setup screen: its switch, then its model, or what off means.
private struct ServiceSetup: View {
    @Bindable var installer: Installer
    let kind: ServiceKind

    var body: some View {
        let engines = SetupChoice.options(in: installer.catalogue, for: kind)
        let choice = installer.setup[kind]
        VStack(alignment: .leading, spacing: 6) {
            Toggle(kind.title, isOn: $installer.setup[kind].isOn)
                .toggleStyle(.switch)
                .font(.body.weight(.medium))
            if !choice.isOn {
                Text("Off: nothing is downloaded for it, and its service loads no model and opens no port. Turn it on later in Settings > \(kind == .stt ? "Speech to Text" : "Text to Speech").")
                    .footnote()
            } else if !engines.isEmpty {
                Picker("Model", selection: $installer.setup[kind].model) {
                    ForEach(engines) { engine in
                        Section(engine.label) {
                            ForEach(engine.models) { model in
                                Text(Self.item(model)).tag(Optional(model.id))
                            }
                        }
                    }
                }
                .pickerStyle(.menu)
                .fixedSize()
                if let model = engines.flatMap(\.models).first(where: { $0.id == choice.model }) {
                    ModelSummary(title: model.name, model: model, detail: "\(Formatting.bytes(model.downloadBytes)) download")
                }
            }
        }
        .padding(.leading, 2)
    }

    /// A picker item: the name and the size, so models compare without choosing each.
    static func item(_ model: CatalogueModel) -> String {
        let name = "\(model.name), \(Formatting.bytes(model.downloadBytes))"
        return model.recommended ? "\(name) (Recommended)" : name
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

/// The one administrator prompt left (ADR 0035): the services still run as the LaunchDaemons
/// of an earlier release, which only root can remove.
private struct MigrationScreen: View {
    let installer: Installer
    let waiting: Bool

    var body: some View {
        Screen(
            symbol: "lock.shield", title: "Remove the old background services",
            sentence: "voiced's services now run as you while you are logged in, with no administrator. macOS asks once more, to remove the old background services (the last time a password is needed)."
        ) {
            VStack(alignment: .leading, spacing: 10) {
                Text("Until then the services keep running the old way.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if waiting { Working(text: "Waiting for approval") }
            }
        } buttons: {
            Button("Remove Old Services", action: installer.removeOldServices)
                .keyboardShortcut(.defaultAction)
                .disabled(waiting)
        }
    }
}

private struct StartingScreen: View {
    var body: some View {
        Screen(
            symbol: "bolt.circle", title: "Starting voiced",
            sentence: "The services are loading their models, which can take a minute the first time."
        ) {
            Working(text: "Waiting for voiced")
        } buttons: {
            EmptyView()
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
                if let port = info.sttPort { row("Speech to text", "\(port)") }
                if let port = info.ttsPort { row("Text to speech", "\(port)") }
            }
            Text("The API key protects the HTTP API only. Wyoming has no authentication, so anyone on your network can use \(Self.ports(info)).")
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

    /// "port 10300", or "ports 10300 and 10200": the Wyoming ports open on the network.
    static func ports(_ info: HomeAssistantInfo) -> String {
        let open = [info.sttPort, info.ttsPort].compactMap { $0 }.map { String($0) }
        return (open.count == 1 ? "port " : "ports ") + open.joined(separator: " and ")
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            CommandBlock(command: value)
        }
    }
}

private struct DoneScreen: View {
    @Bindable var loginItem: LoginItem
    let close: () -> Void

    var body: some View {
        Screen(
            symbol: "checkmark.circle", tint: .green, title: "voiced is running",
            sentence: "voiced runs while you are logged in. It appears in Login Items as a background item; leave it on so the services start when you log in."
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
            if let fallback = failure.fallback {
                Text(fallback)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
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
            if installer.offersRollback, let target = installer.rollbackTarget() {
                Button(target.version.map { "Roll Back to \($0)" } ?? "Roll Back", action: installer.rollBack)
            }
            if installer.offersSetup {
                Button("Change Choices", action: installer.changeChoices)
            }
            Button("Try Again", action: installer.retry)
                .keyboardShortcut(.defaultAction)
        }
    }
}

/// An update, rollback or repair that ended with the services answering.
private struct FinishedScreen: View {
    let task: InstallerTask
    let version: String?
    let close: () -> Void

    var body: some View {
        let running = version.map { "voiced \($0) is running." } ?? "voiced is running."
        Screen(symbol: "checkmark.circle", tint: .green, title: title, sentence: running) {
            VStack(alignment: .leading, spacing: 12) {
                if task == .update {
                    Text("The previous version is kept; Settings > About can roll back to it.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } buttons: {
            Button("Done", action: close)
                .keyboardShortcut(.defaultAction)
        }
    }

    private var title: String {
        switch task {
        case .update: "voiced is up to date"
        case .rollBack: "voiced is rolled back"
        case .repair: "voiced is repaired"
        case .install, .uninstall: "voiced is running"
        }
    }
}

private struct ConfirmUninstallScreen: View {
    let installer: Installer
    let close: () -> Void
    @State private var choices = UninstallChoices()

    var body: some View {
        Screen(
            symbol: "trash", tint: .red, title: "Uninstall voiced",
            sentence: "This stops and removes voiced's background services and deletes voiced's own files in ~/.voiced."
        ) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Also delete:").font(.callout.weight(.medium))
                Toggle(
                    "The downloaded speech models",
                    isOn: $choices.models)
                Text("voiced's models in the Hugging Face cache (~/.cache/huggingface/hub, or the configured hf_home). Another app that uses the same models downloads them again.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 20)
                Toggle("The logs in ~/Library/Logs/voiced", isOn: $choices.logs)
                Toggle("The config file, with your settings and API key", isOn: $choices.config)
                Text("What you leave unticked stays, so a later install reuses it.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } buttons: {
            Button("Cancel", action: close)
                .keyboardShortcut(.cancelAction)
            Button("Uninstall") { installer.uninstall(choices) }
        }
    }
}

private struct UninstallingScreen: View {
    let step: String

    var body: some View {
        Screen(symbol: "trash", title: "Uninstalling voiced", sentence: "voiced removes its services, then its files.") {
            Working(text: step)
        } buttons: {
            EmptyView()
        }
    }
}

private struct UninstalledScreen: View {
    let choices: UninstallChoices
    let close: () -> Void

    var body: some View {
        Screen(
            symbol: "checkmark.circle", tint: .green, title: "voiced is uninstalled",
            sentence: "Its background services and its files in ~/.voiced are gone."
        ) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Still on this Mac:").font(.callout.weight(.medium))
                Place(symbol: "app", text: "This app. Move it to the Trash to finish, or install again from it.")
                if !choices.models { Place(symbol: "waveform.circle", text: "The speech models, in the Hugging Face cache.") }
                if !choices.logs { Place(symbol: "doc.text", text: "The logs, in ~/Library/Logs/voiced.") }
                if !choices.config { Place(symbol: "doc.badge.gearshape", text: "The config file, in ~/.config/voiced.") }
                Place(
                    symbol: "shield",
                    text: "If you let voiced's Python through the firewall, its entry, which now names a removed file. Remove it in System Settings > Network > Firewall > Options.")
            }
        } buttons: {
            Button("Done", action: close)
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
