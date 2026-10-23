import AppKit
import SwiftUI
import VoicedControl

/// The panes of the Settings window, in sidebar order.
public enum SettingsPane: String, CaseIterable, Identifiable, Sendable {
    case general, network, speechToText, textToSpeech, about, debug

    public var id: String { rawValue }

    /// The panes with model rows.
    public var hasModels: Bool { self == .speechToText || self == .textToSpeech }

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
        case .debug: "Debug"
        }
    }

    public var symbol: String {
        switch self {
        case .general: "gearshape"
        case .network: "network"
        case .speechToText: "waveform"
        case .textToSpeech: "speaker.wave.2"
        case .about: "info.circle"
        case .debug: "ladybug"
        }
    }

    /// The user default that shows the Debug pane, off by default (ADR 0018).
    public static let showDebugKey = "showDebugPane"

    /// The panes in the sidebar: Debug only while its switch in General is on.
    public static func visible(showingDebug: Bool) -> [SettingsPane] {
        allCases.filter { $0 != .debug || showingDebug }
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
    let launch: LaunchChecks?
    let installer: Installer?
    /// The model rows of the Speech to Text and Text to Speech panes.
    let models: ModelManager
    /// The play button beside the Voice picker.
    let preview: VoicePreview
    /// The app's own updates (ADR 0034); nil in a build without a feed.
    let updates: AppUpdates?
    @AppStorage(SettingsPane.storageKey) private var storedPane: SettingsPane = .general
    @AppStorage(SettingsPane.showDebugKey) private var showDebug = false
    /// Set for snapshots: shows this pane and leaves the remembered one alone.
    private let fixedPane: SettingsPane?
    @State private var newKey = ""
    @State private var confirmingRemoval = false
    @Environment(\.openWindow) private var openWindow

    public init(
        model: AppModel, models: ModelManager, loginItem: LoginItem, launch: LaunchChecks? = nil,
        installer: Installer? = nil, updates: AppUpdates? = nil, pane: SettingsPane? = nil, preview: VoicePreview
    ) {
        self.model = model
        self.preview = preview
        self.updates = updates
        self.loginItem = loginItem
        self.launch = launch
        self.installer = installer
        self.models = models
        self.fixedPane = pane
    }

    private var pane: SettingsPane {
        if let fixedPane { return fixedPane }
        // A remembered Debug pane whose switch was turned off falls back to General.
        return storedPane == .debug && !showDebug ? .general : storedPane
    }

    public var body: some View {
        // A fixed sidebar next to the pane rather than a split view: nothing to collapse or
        // resize, and it draws the same in the window and in snapshots.
        HStack(spacing: 0) {
            List(SettingsPane.visible(showingDebug: showDebug || fixedPane == .debug), selection: selection) { pane in
                Label(pane.title, systemImage: pane.symbol).tag(pane)
            }
            .listStyle(.sidebar)
            .frame(width: 190)
            Divider()
            VStack(spacing: 0) {
                // Inset to line up with the form's sections, on the same background.
                NoticeStrip(model: model)
                    .padding(.horizontal, 6)
                    .padding(.top, 16)
                Form {
                    content
                }
                .formStyle(.grouped)
                .scrollContentBackground(.hidden)
                .disabled(model.isSaving)
                // Pinned below the scrolling rows (ADR 0022).
                if let list = models.list, pane.hasModels, model.settings != nil {
                    Divider()
                    ModelFooter(list: list)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .frame(width: 700, height: 500)
        .navigationTitle(pane.title)
        .onAppear { loginItem.refresh() }
        .task(id: pane) { if pane.hasModels { await models.refresh() } }
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
        case .debug:
            debug
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
            if let updates {
                Toggle(
                    "Check for updates automatically",
                    isOn: Binding(get: { updates.automaticallyChecks }, set: { updates.automaticallyChecks = $0 }))
                    .help("Checks when the app opens and once a day. Installing an update always waits for you.")
            } else {
                LabeledContent("Check for updates automatically") {
                    Text("Available in the app bundle").foregroundStyle(.secondary)
                }
            }
            Toggle("Show Debug pane", isOn: $showDebug)
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
                // Turning it on without a key would only make a form that refuses every
                // change; turning it off stays possible.
                .disabled(!settings.apiKeySet && !settings.browserSettings)
            if !settings.apiKeySet {
                Button("Set API Key…") { storedPane = .network }
            }
        } header: {
            Text("Status page")
        } footer: {
            Text(
                settings.apiKeySet
                    ? "The page asks for the API key before it changes anything."
                    : "The page asks for the API key before it changes anything, so this needs a key first."
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
        let languages = SpeechLanguages(
            settings.stt, engine: model.engines?.sttEngine(named: settings.stt.engine), name: Self.languageName)
        Section {
            // `line` carries the wording; a language the model does not serve also gets the symbol.
            LabeledContent("Language") {
                if languages.isServed {
                    Text(languages.line)
                } else {
                    Label(languages.line, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
            }
            if !languages.served.isEmpty {
                LabeledContent("Model languages", value: languages.served.joined(separator: ", "))
            }
        } header: {
            Text("Speech to text")
        } footer: {
            Text("Use switches to a model, downloading it first if needed, and restarts the service.").footnote()
        }
        serviceSection(.stt, settings)
        ModelSections(models: models, app: model, kind: .stt, settings: settings)
    }

    /// Off, and when to unload the model (ADR 0033). Either change restarts the service, which
    /// reads them when it starts.
    @ViewBuilder
    private func serviceSection(_ kind: ServiceKind, _ settings: SettingsPayload) -> some View {
        let enabled = settings.isEnabled(kind)
        let idleUnload = settings.idleUnloadSeconds(kind)
        Section {
            Toggle(
                kind == .stt ? "Run speech to text" : "Run text to speech",
                isOn: Binding(
                    get: { enabled },
                    set: { on in if on != enabled { saveAndRestart(.enabled(kind, on), kind) } }))
            Picker(
                "Unload the model",
                selection: Binding(
                    get: { idleUnload },
                    set: { seconds in
                        if seconds != idleUnload { saveAndRestart(.idleUnload(kind, seconds: seconds), kind) }
                    })
            ) {
                ForEach(IdleUnload.choices(including: idleUnload), id: \.self) { seconds in
                    Text(IdleUnload.label(seconds)).tag(seconds)
                }
            }
            .disabled(!enabled)
        } header: {
            Text("Service")
        } footer: {
            Text(
                enabled
                    ? "An unloaded model frees its memory; the next request waits while it loads again, about 0.25 to 0.4 seconds. Changing either setting restarts the service."
                    : "Off: the service loads no model and answers nothing, so Home Assistant and other apps cannot use it. Turning it on restarts it."
            )
            .footnote()
        }
    }

    private func saveAndRestart(_ edit: SettingsEdit, _ kind: ServiceKind) {
        Task {
            if await model.apply(edit) { await model.restart(.service(kind)) }
        }
    }

    @ViewBuilder
    private func textToSpeech(_ settings: SettingsPayload) -> some View {
        let engine = model.engines?.ttsEngine(named: settings.tts.engine)
        let voice = settings.tts.voice
        let unavailable = VoicePreview.unavailableReason(model.status?[.tts])
        Section {
            HStack {
                Picker("Voice", selection: binding(voice) { .ttsVoice($0) }) {
                    ForEach(options(engine?.voices, current: voice), id: \.self) { voice in
                        Text(engine?.voiceName(voice) ?? voice).tag(voice)
                    }
                }
                previewButton(voice, line: PreviewLine.text(for: voice, of: engine), unavailable: unavailable)
            }
            previewNote(voice, unavailable: unavailable)
            // A value set by hand outside the range is shown as it is; a step brings it in.
            LabeledContent("Speed") {
                HStack(spacing: 8) {
                    Text(Self.speedText(settings.tts.speed)).monospacedDigit()
                    Stepper("Speed", value: binding(settings.tts.speed) { .ttsSpeed($0) }, in: Self.speeds, step: 0.1)
                        .labelsHidden()
                }
            }
        } header: {
            Text("Text to speech")
        } footer: {
            Text("The voice and the speed are used by requests that do not name their own; the voices are the engine in use's, and the play button speaks a line in one at that speed. A new speed applies at once. Use switches to a model, downloading it first if needed, and restarts the service.")
                .footnote()
        }
        .onChange(of: voice) { preview.stop() }
        .onDisappear { preview.stop() }
        serviceSection(.tts, settings)
        ModelSections(models: models, app: model, kind: .tts, settings: settings)
    }

    /// Play, or stop while the line is fetched (a spinner) or plays.
    private func previewButton(_ voice: String, line: String, unavailable: String?) -> some View {
        let active = preview.isActive(voice)
        return Button {
            preview.toggle(voice, line: line)
        } label: {
            ZStack {
                if case .loading = preview.state, active {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: active ? "stop.fill" : "play.fill")
                }
            }
            .frame(width: 16, height: 16)
        }
        .buttonStyle(.borderless)
        .disabled(unavailable != nil && !active)
        .help(active ? "Stop" : unavailable ?? "Hear \"\(line)\"")
        .accessibilityLabel(active ? "Stop the voice preview" : "Hear this voice")
    }

    /// What went wrong, or why the button is off, in one line under the picker.
    @ViewBuilder
    private func previewNote(_ voice: String, unavailable: String?) -> some View {
        if case .failed(let failed, let message) = preview.state, failed == voice {
            Text(message).font(.callout).foregroundStyle(.red)
        } else if let unavailable {
            Text(unavailable).footnote()
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
            LabeledContent("Menu bar app", value: VoicedPaths.appVersion)
            LabeledContent("voiced", value: voicedVersion)
            LabeledContent("Control protocol", value: "\(ControlClient.supportedProtocol)")
            LabeledContent("Control socket") {
                Text(model.socketPath)
                    .textSelection(.enabled)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
        }
        installSection
        Section {
            if let url = URL(string: "https://github.com/JulianBeaulieu/voiced") {
                LabeledContent("Project") {
                    Link("github.com/JulianBeaulieu/voiced", destination: url)
                }
            }
            LabeledContent("License", value: "MIT")
            if FileManager.default.fileExists(atPath: paths.notice.path) {
                LabeledContent("Third-party software") {
                    Button("Show Notices") { NSWorkspace.shared.open(paths.notice) }
                }
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

    /// Who made the install, and what can be done with it: the Lifecycle actions for the
    /// app's own (ADR 0018), the commands for one Homebrew or the command line manages.
    @ViewBuilder
    private var installSection: some View {
        if let record = launch?.installRecord ?? model.installRecord {
            let managed = installer?.managesInstall == true
            Section {
                LabeledContent("Installed by", value: Self.sourceName(record.source))
                if let environment = record.environment {
                    LabeledContent("Environment") {
                        Text(environment)
                            .textSelection(.enabled)
                            .lineLimit(2)
                            .truncationMode(.middle)
                    }
                }
                if let installer, managed {
                    // Update and Repair wait for a model download or delete (ADR 0022), and for an
                    // update of the app, whose relaunch would stop them (ADR 0034).
                    let working = installer.isWorking || models.isBusy || updates?.isBusy == true
                    if case .differs = launch?.installMatch {
                        Button("Update voiced…") { inInstaller(installer.update) }.disabled(working)
                    }
                    if let target = installer.rollbackTarget() {
                        Button(target.version.map { "Roll Back to voiced \($0)…" } ?? "Roll Back…") {
                            inInstaller(installer.rollBack)
                        }
                        .disabled(working)
                    }
                    Button("Repair voiced…") { inInstaller(installer.repair) }
                        .disabled(working)
                        .help(installer.repairNote ?? "Installs voiced again from this app, keeping the config and models.")
                    Button("Uninstall voiced…") { inInstaller(installer.confirmUninstall) }.disabled(working)
                } else {
                    ForEach(ExternalInstall.commands(for: record.source), id: \.purpose) { entry in
                        LabeledContent(entry.purpose) {
                            VStack(alignment: .leading, spacing: 4) {
                                CommandBlock(command: entry.command)
                                if let note = entry.note { Text(note).footnote() }
                            }
                        }
                    }
                }
            } header: {
                Text("Install")
            } footer: {
                let note = managed ? installer?.repairNote.map { $0 + " " } ?? "" : ""
                Text(
                    managed
                        ? note + "Moving this app to the Trash leaves the services running; uninstall here first."
                        : "This install is not the app's, so the app only shows it; these commands change it."
                )
                .footnote()
            }
        }
    }

    nonisolated static func sourceName(_ source: InstallSource) -> String {
        switch source {
        case .app: "This app"
        case .homebrew: "Homebrew"
        case .cli: "The command line"
        }
    }

    /// Starts a Lifecycle action and shows the installer window, where it runs.
    private func inInstaller(_ action: () -> Void) {
        action()
        NSApplication.shared.activate()
        openWindow(id: InstallerView.windowID)
    }

    // MARK: - Debug

    private var diagnostics: Diagnostics {
        Diagnostics(
            appVersion: VoicedPaths.appVersion, paths: paths, translocatedFrom: launch?.translocatedFrom,
            manifest: launch?.manifest, installSource: launch?.installSource,
            record: launch?.installRecord ?? model.installRecord, backgroundItems: launch?.backgroundItems ?? [:],
            installer: installer.map { String(describing: $0.phase) }, connection: model.dashboard.headline,
            serverVersion: model.serverVersion, socketPath: model.socketPath)
    }

    private var paths: VoicedPaths { launch?.paths ?? VoicedPaths() }

    @ViewBuilder
    private var debug: some View {
        let diagnostics = diagnostics
        ForEach(diagnostics.sections) { section in
            Section(section.title) {
                ForEach(section.entries) { entry in
                    LabeledContent(entry.label) {
                        HStack(spacing: 6) {
                            Text(entry.value)
                                .textSelection(.enabled)
                                .lineLimit(2)
                                .truncationMode(.middle)
                            if let path = entry.path, FileManager.default.fileExists(atPath: path) {
                                Button {
                                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                                } label: {
                                    Image(systemName: "magnifyingglass")
                                }
                                .buttonStyle(.borderless)
                                .help("Show in Finder")
                                .accessibilityLabel("Show \(entry.label.lowercased()) in Finder")
                            }
                        }
                    }
                }
            }
        }
        Section {
            Button("Refresh") { Task { await launch?.refresh() } }
            Button("Copy Diagnostics") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(diagnostics.report, forType: .string)
            }
        } footer: {
            Text("Copies the above as text for a bug report. It holds no settings and no API key.").footnote()
        }
    }

    // Pure formatting, so callable off the main actor: a View's members are main-actor
    // isolated under Swift 6.1 (the CI toolchain), unlike the inference in 6.2.

    /// "English" for "en", in the user's language; the code itself when unknown.
    nonisolated static func languageName(_ code: String) -> String {
        Locale.current.localizedString(forIdentifier: code) ?? code
    }

    /// The default speed's stepper: 0.5× to 2× by 0.1 (the server takes up to 4×).
    nonisolated static let speeds: ClosedRange<Double> = 0.5...2.0

    /// "1×", "1.25×".
    nonisolated static func speedText(_ speed: Double) -> String {
        speed.formatted(.number.precision(.fractionLength(0...2))) + "×"
    }

    private var voicedVersion: String {
        guard case .connected = model.connection else { return "Not connected" }
        return model.serverVersion ?? "Unknown version"
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
