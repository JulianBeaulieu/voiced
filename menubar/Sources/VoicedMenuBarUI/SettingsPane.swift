import SwiftUI
import VoicedControl

/// Common settings. Every control saves on change through `settings.set`; what needs a
/// restart is collected and offered on the status tab.
struct SettingsPane: View {
    @Bindable var model: AppModel
    let loginItem: LoginItem
    let height: CGFloat
    @State private var newKey = ""

    var body: some View {
        if let settings = model.settings {
            Form {
                network(settings)
                apiKey(settings)
                engines(settings)
                general(settings)
            }
            .formStyle(.grouped)
            .disabled(model.isSaving)
            .frame(height: height)
        }
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
            if model.showsLANWarning {
                Label {
                    Text("Anyone on your network can use voiced. Set an API key below.")
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                .font(.callout)
            }
            Toggle("Home Assistant discovery", isOn: binding(settings.zeroconf) { .zeroconf($0) })
        } header: {
            Text("Network")
        } footer: {
            Text("Discovery announces the Wyoming services over Bonjour while voiced listens on the local network.")
                .footnote()
        }
    }

    @ViewBuilder
    private func apiKey(_ settings: SettingsPayload) -> some View {
        Section {
            LabeledContent("API key") {
                Text(settings.apiKeySet ? "Set" : "Not set")
                    .foregroundStyle(settings.apiKeySet ? Color.secondary : Color.orange)
            }
            SecureField("New key", text: $newKey, prompt: Text("Type or paste"))
                .onSubmit(saveKey)
            HStack {
                if settings.apiKeySet {
                    Button("Remove Key", role: .destructive) { save(.clearAPIKey) }
                }
                Spacer()
                Button(settings.apiKeySet ? "Replace Key" : "Set Key", action: saveKey)
                    .disabled(newKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        } header: {
            Text("Access")
        } footer: {
            Text("Clients send the key as a bearer token. It is stored in the config file and never shown here.")
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
        Section("Speech to text") {
            Picker("Engine", selection: binding(settings.stt.engine) { .sttEngine($0) }) {
                ForEach(options(catalogue?.stt.map(\.name), current: settings.stt.engine), id: \.self) {
                    Text($0).tag($0)
                }
            }
        }
        Section("Text to speech") {
            Picker("Engine", selection: binding(settings.tts.engine) { .ttsEngine($0) }) {
                ForEach(options(catalogue?.tts.map(\.name), current: settings.tts.engine), id: \.self) {
                    Text($0).tag($0)
                }
            }
            let voices = catalogue?.ttsEngine(named: settings.tts.engine)?.voices
            Picker("Voice", selection: binding(settings.tts.voice) { .ttsVoice($0) }) {
                ForEach(options(voices, current: settings.tts.voice), id: \.self) {
                    Text(VoiceName.display($0)).tag($0)
                }
            }
        }
    }

    @ViewBuilder
    private func general(_ settings: SettingsPayload) -> some View {
        Section {
            Toggle("Settings in browser", isOn: binding(settings.browserSettings) { .browserSettings($0) })
            if loginItem.isAvailable {
                Toggle(
                    "Open menu bar app at login",
                    isOn: Binding(get: { loginItem.isEnabled }, set: { loginItem.setEnabled($0) }))
                if loginItem.needsApproval {
                    Button("Allow in System Settings") { loginItem.openLoginItemsSettings() }
                }
            }
        } footer: {
            Text("The status page can change settings when this is on. It asks for the API key when one is set, and refuses on the local network without one.")
                .footnote()
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

/// Readable names for voice ids that follow the Kokoro convention (`af_heart`: American
/// English, female, Heart). Other ids are shown as they are.
public enum VoiceName {
    static let languages: [Character: String] = [
        "a": "American English", "b": "British English", "e": "Spanish", "f": "French",
        "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Mandarin",
    ]

    public static func display(_ id: String) -> String {
        let parts = id.split(separator: "_", maxSplits: 1)
        guard parts.count == 2, parts[0].count == 2, !parts[1].isEmpty,
            let language = languages[parts[0].first!],
            let gender = ["f": "female", "m": "male"][String(parts[0].last!)]
        else { return id }
        return "\(parts[1].capitalized) (\(language), \(gender))"
    }
}
