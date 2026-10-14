import SwiftUI
import VoicedControl

/// The Settings window. Every control saves on change through `settings.set`; what needs a
/// restart is offered at the top, as in the status window.
public struct SettingsView: View {
    let model: AppModel
    let loginItem: LoginItem
    @State private var newKey = ""
    @State private var confirmingRemoval = false

    let maxHeight: CGFloat

    /// `maxHeight` caps the window: past it the form scrolls, so it fits a 13-inch screen
    /// whatever notices are showing.
    public init(model: AppModel, loginItem: LoginItem, maxHeight: CGFloat = 640) {
        self.model = model
        self.loginItem = loginItem
        self.maxHeight = maxHeight
    }

    public var body: some View {
        // Notices and form at their natural height inside a scroll view: the window is as
        // tall as its content up to `maxHeight`, and scrolls past it.
        ScrollView {
            VStack(spacing: 0) {
                NoticeStrip(model: model)
                    .padding(.top, 12)
                Form {
                    if let settings = model.settings {
                        network(settings)
                        access(settings)
                        engines(settings)
                        statusPage(settings)
                    } else {
                        Section {
                            Label("Not connected to voiced", systemImage: "waveform.slash")
                        } footer: {
                            Text("The voiced settings appear while voiced is running.").footnote()
                        }
                    }
                    menuBarApp
                }
                .formStyle(.grouped)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .fixedSize(horizontal: false, vertical: true)
                .disabled(model.isSaving)
            }
        }
        .frame(width: 460)
        .frame(maxHeight: maxHeight)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear { loginItem.refresh() }
    }

    private func save(_ edit: SettingsEdit) {
        Task { await model.apply(edit) }
    }

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

    @ViewBuilder
    private func engines(_ settings: SettingsPayload) -> some View {
        let catalogue = model.engines
        Section("Speech") {
            Picker("Speech to text", selection: binding(settings.stt.engine) { .sttEngine($0) }) {
                ForEach(options(catalogue?.stt.map(\.name), current: settings.stt.engine), id: \.self) { name in
                    Text(catalogue?.sttEngine(named: name)?.displayName ?? name).tag(name)
                }
            }
            Picker("Text to speech", selection: binding(settings.tts.engine) { .ttsEngine($0) }) {
                ForEach(options(catalogue?.tts.map(\.name), current: settings.tts.engine), id: \.self) { name in
                    Text(catalogue?.ttsEngine(named: name)?.displayName ?? name).tag(name)
                }
            }
            let engine = catalogue?.ttsEngine(named: settings.tts.engine)
            Picker("Voice", selection: binding(settings.tts.voice) { .ttsVoice($0) }) {
                ForEach(options(engine?.voices, current: settings.tts.voice), id: \.self) { voice in
                    Text(engine?.voiceName(voice) ?? voice).tag(voice)
                }
            }
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
            Text("The page asks for the API key when one is set, and refuses on the local network without one.")
                .footnote()
        }
    }

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
        }
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
