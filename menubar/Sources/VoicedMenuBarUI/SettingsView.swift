import AppKit
import SwiftUI
import VoicedControl

/// The panes of the Settings window, in sidebar order.
public enum SettingsPane: String, CaseIterable, Identifiable, Sendable {
    case general, network, speechToText, textToSpeech, about

    public var id: String { rawValue }

    /// The user default holding the selected pane, so the window reopens where it was and
    /// other windows can choose the pane before opening it.
    public static let storageKey = "settingsPane"

    public var title: String {
        switch self {
        case .general: "General"
        case .network: "Network"
        case .speechToText: "Speech to Text"
        case .textToSpeech: "Text to Speech"
        case .about: "About"
        }
    }

    public var symbol: String {
        switch self {
        case .general: "gearshape"
        case .network: "network"
        case .speechToText: "waveform"
        case .textToSpeech: "speaker.wave.2"
        case .about: "info.circle"
        }
    }

    /// Makes this the pane the Settings window shows, open or not.
    @MainActor
    public func select() {
        UserDefaults.standard.set(rawValue, forKey: Self.storageKey)
    }
}

/// The Settings window: a sidebar of panes. Every control saves on change through
/// `settings.set`; what needs a restart is offered above the pane, as in the status window.
public struct SettingsView: View {
    let model: AppModel
    let loginItem: LoginItem
    @AppStorage(SettingsPane.storageKey) private var storedPane: SettingsPane = .general
    /// Set for snapshots: shows this pane and leaves the remembered one alone.
    private let fixedPane: SettingsPane?
    @State private var newKey = ""
    @State private var confirmingRemoval = false

    public init(model: AppModel, loginItem: LoginItem, pane: SettingsPane? = nil) {
        self.model = model
        self.loginItem = loginItem
        self.fixedPane = pane
    }

    private var pane: SettingsPane { fixedPane ?? storedPane }

    public var body: some View {
        // A fixed sidebar next to the pane rather than a split view: nothing to collapse or
        // resize, and it draws the same in the window and in snapshots.
        HStack(spacing: 0) {
            List(SettingsPane.allCases, selection: selection) { pane in
                Label(pane.title, systemImage: pane.symbol).tag(pane)
            }
            .listStyle(.sidebar)
            .frame(width: 190)
            Divider()
            VStack(spacing: 0) {
                NoticeStrip(model: model)
                    .padding(.top, 12)
                Form {
                    content
                }
                .formStyle(.grouped)
                .disabled(model.isSaving)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .frame(width: 700, height: 500)
        .navigationTitle(pane.title)
        .onAppear { loginItem.refresh() }
    }

    private var selection: Binding<SettingsPane?> {
        Binding(get: { pane }, set: { if let new = $0 { storedPane = new } })
    }

    @ViewBuilder
    private var content: some View {
        switch pane {
        case .general:
            menuBarApp
            if let settings = model.settings { statusPage(settings) }
        case .network:
            if let settings = model.settings {
                network(settings)
                access(settings)
            } else {
                notConnected
            }
        case .speechToText:
            if let settings = model.settings { speechToText(settings) } else { notConnected }
        case .textToSpeech:
            if let settings = model.settings { textToSpeech(settings) } else { notConnected }
        case .about:
            about
        }
    }

    private var notConnected: some View {
        Section {
            Label("Not connected to voiced", systemImage: "waveform.slash")
        } footer: {
            Text("These settings appear while voiced is running.").footnote()
        }
    }

    private func save(_ edit: SettingsEdit) {
        Task { await model.apply(edit) }
    }

    // MARK: - General

    @ViewBuilder
    private var menuBarApp: some View {
        Section {
            if loginItem.isAvailable {
                Toggle(
                    "Open at login",
                    isOn: Binding(get: { loginItem.isEnabled }, set: { loginItem.setEnabled($0) }))
                if loginItem.needsApproval {
                    Button("Allow in System Settings") { loginItem.openLoginItemsSettings() }
                }
                if let error = loginItem.errorMessage {
                    Text(error).font(.callout).foregroundStyle(.red)
                }
            } else {
                LabeledContent("Open at login") {
                    Text("Available in the app bundle").foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Menu bar app")
        } footer: {
            Text("The services keep running when the menu bar app is not open.").footnote()
        }
    }

    @ViewBuilder
    private func statusPage(_ settings: SettingsPayload) -> some View {
        Section {
            Toggle(
                "Allow changing settings from the status page",
                isOn: binding(settings.browserSettings) { .browserSettings($0) })
        } header: {
            Text("Status page")
        } footer: {
            Text(
                settings.apiKeySet
                    ? "The page asks for the API key before it changes anything."
                    : "The page asks for the API key before it changes anything, so it refuses every change until a key is set under Network."
            )
            .footnote()
        }
    }

    // MARK: - Network

    @ViewBuilder
    private func network(_ settings: SettingsPayload) -> some View {
        Section {
            Picker("Listen on", selection: binding(settings.bind) { .bind($0) }) {
                Text("This Mac only").tag(BindMode.loopback)
                Text("Local network").tag(BindMode.lan)
            }
            Toggle("Home Assistant discovery", isOn: binding(settings.zeroconf) { .zeroconf($0) })
                .disabled(settings.bind == .loopback)
        } header: {
            Text("Network")
        } footer: {
            Text(
                settings.bind == .loopback
                    ? "Discovery needs Local network: Home Assistant on another machine cannot reach this Mac otherwise."
                    : "Discovery announces the Wyoming services over Bonjour so Home Assistant finds them."
            )
            .footnote()
        }
    }

    @ViewBuilder
    private func access(_ settings: SettingsPayload) -> some View {
        Section {
            LabeledContent("API key") {
                HStack(spacing: 8) {
                    Text(settings.apiKeySet ? "Set" : "Not set")
                        .foregroundStyle(settings.apiKeySet ? Color.secondary : Color.orange)
                    if settings.apiKeySet {
                        Button("Remove", role: .destructive) {
                            if settings.bind == .lan { confirmingRemoval = true } else { save(.clearAPIKey) }
                        }
                        .controlSize(.small)
                    }
                }
            }
            .confirmationDialog(
                "Remove the API key?", isPresented: $confirmingRemoval, titleVisibility: .visible
            ) {
                Button("Remove Key", role: .destructive) { save(.clearAPIKey) }
            } message: {
                Text("Anyone on your network will be able to use the HTTP API.")
            }
            HStack {
                SecureField(settings.apiKeySet ? "New key" : "Key", text: $newKey, prompt: Text("Type or paste"))
                    .onSubmit(saveKey)
                Button(settings.apiKeySet ? "Replace" : "Set", action: saveKey)
                    .controlSize(.small)
                    .disabled(newKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        } header: {
            Text("HTTP API")
        } footer: {
            Text("Apps using the HTTP API must send this key. It is stored in the config file and never shown here.")
                .footnote()
        }
    }

    private func saveKey() {
        let key = newKey
        Task {
            if await model.apply(.apiKey(key)) { newKey = "" }
        }
    }

    // MARK: - Speech

    @ViewBuilder
    private func speechToText(_ settings: SettingsPayload) -> some View {
        let catalogue = model.engines
        Section {
            Picker("Engine", selection: binding(settings.stt.engine) { .sttEngine($0) }) {
                ForEach(options(catalogue?.stt.map(\.name), current: settings.stt.engine), id: \.self) { name in
                    Text(catalogue?.sttEngine(named: name)?.displayName ?? name).tag(name)
                }
            }
            LabeledContent("Model", value: settings.stt.model)
            LabeledContent("Language", value: settings.stt.language)
        } header: {
            Text("Speech to text")
        } footer: {
            Text("Switching the engine selects its own model. The service loads it after a restart.").footnote()
        }
    }

    @ViewBuilder
    private func textToSpeech(_ settings: SettingsPayload) -> some View {
        let catalogue = model.engines
        let engine = catalogue?.ttsEngine(named: settings.tts.engine)
        Section {
            Picker("Engine", selection: binding(settings.tts.engine) { .ttsEngine($0) }) {
                ForEach(options(catalogue?.tts.map(\.name), current: settings.tts.engine), id: \.self) { name in
                    Text(catalogue?.ttsEngine(named: name)?.displayName ?? name).tag(name)
                }
            }
            Picker("Voice", selection: binding(settings.tts.voice) { .ttsVoice($0) }) {
                ForEach(options(engine?.voices, current: settings.tts.voice), id: \.self) { voice in
                    Text(engine?.voiceName(voice) ?? voice).tag(voice)
                }
            }
            LabeledContent("Model", value: settings.tts.model)
            LabeledContent("Speed", value: settings.tts.speed.formatted(.number.precision(.fractionLength(0...2))))
        } header: {
            Text("Text to speech")
        } footer: {
            Text("The voice is the default for requests that do not name one. Switching the engine selects its own model and voice.")
                .footnote()
        }
    }

    // MARK: - About

    @ViewBuilder
    private var about: some View {
        Section {
            HStack(spacing: 12) {
                Image(nsImage: NSApplication.shared.applicationIconImage)
                    .resizable()
                    .frame(width: 48, height: 48)
                VStack(alignment: .leading, spacing: 2) {
                    Text("voiced").font(.headline)
                    Text("Local speech to text and text to speech for Apple Silicon.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            LabeledContent("Menu bar app", value: Self.appVersion)
            LabeledContent("voiced", value: voicedVersion)
            LabeledContent("Control protocol", value: "\(ControlClient.supportedProtocol)")
            LabeledContent("Control socket") {
                Text(model.socketPath)
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
        }
        if model.settings != nil {
            Section {
                Button("Open Config File") { Task { await model.open(.configFile) } }
                Button("Open Logs Folder") { Task { await model.open(.logsDirectory) } }
                Button("Open Status Page") { Task { await model.open(.statusPage) } }
            }
        }
    }

    private var voicedVersion: String {
        guard case .connected = model.connection else { return "Not connected" }
        return model.serverVersion ?? "Unknown version"
    }

    private static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development build"
    }

    /// The catalogue's options, always including the current value so the picker never
    /// shows an empty selection.
    private func options(_ catalogue: [String]?, current: String) -> [String] {
        let list = catalogue ?? []
        return list.contains(current) ? list : [current] + list
    }

    private func binding<Value: Equatable>(
        _ value: Value, edit: @escaping (Value) -> SettingsEdit
    ) -> Binding<Value> {
        Binding(get: { value }, set: { if $0 != value { save(edit($0)) } })
    }
}

extension Text {
    /// Section footers: small, secondary, leading aligned.
    func footnote() -> some View {
        font(.caption)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
    }
}
