import AppKit
import SwiftUI
import VoicedControl

/// The model rows of the Speech to Text or Text to Speech pane (ADR 0022, section 5): one
/// section per engine, a row per model with its licence, size, state and actions.
struct ModelSections: View {
    let models: ModelManager
    let app: AppModel
    let kind: ServiceKind
    let settings: SettingsPayload

    var body: some View {
        if let list = models.list {
            let (engine, model) =
                kind == .stt ? (settings.stt.engine, settings.stt.model) : (settings.tts.engine, settings.tts.model)
            let rows = ModelRow.rows(
                list, kind: kind, engine: engine, model: model, downloading: models.download?.model)
            // Next to the voice picker: the voice is what changes for Home Assistant.
            if kind == .tts, let switched = models.switchedVoiceEngine {
                Section {
                    Notice(
                        symbol: "house", tint: .accentColor,
                        text: "In Home Assistant, pick a \(switched) voice under Settings > Voice assistants > "
                            + "your assistant > Text-to-speech > Voice. If the new voices are missing, reload "
                            + "voiced under Devices & services.",
                        actionTitle: "Dismiss"
                    ) { models.dismissVoiceHint() }
                }
            }
            ForEach(list.engines(of: kind)) { engine in
                Section {
                    ForEach(rows.filter { $0.engine.id == engine.id }) { row in
                        ModelRowView(row: row, kind: kind, models: models, app: app)
                    }
                } header: {
                    Text(engine.label)
                } footer: {
                    if !engine.installed {
                        Text("This engine is not in the installed voiced; update voiced to use it.").footnote()
                    }
                }
            }
        } else if let error = models.listError {
            Section("Models") {
                Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.secondary)
                Button("Try Again") { Task { await models.refresh() } }
            }
        } else {
            Section("Models") { Working(text: "Reading the models") }
        }
    }
}

struct ModelRowView: View {
    let row: ModelRow
    let kind: ServiceKind
    let models: ModelManager
    let app: AppModel
    @State private var confirming: Deletion?

    private enum Deletion: Identifiable {
        case model, previous
        var id: Self { self }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(row.title).font(.body.weight(.medium))
                        if let model = row.model, !model.license.isEmpty {
                            if let card = model.cardURL {
                                Link(model.license, destination: card).font(.caption)
                            } else {
                                Text(model.license).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    Text(([row.sizeLine, stateLine].compactMap { $0 }).joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(row.state == .inUseMissing ? Color.red : Color.secondary)
                }
                Spacer(minLength: 8)
                actions
            }
            if row.state == .downloading {
                DownloadBar(models: models)
            }
            if let message = models.messages[row.model?.id ?? ""] {
                ModelMessageView(message: message)
            } else if row.state == .gated {
                ModelMessageView(message: .needsToken(row.model))
            }
            if let title = row.deleteOldTitle {
                Button(title) { confirming = .previous }
                    .buttonStyle(.link)
                    .font(.caption)
                    .disabled(busy)
            }
        }
        .padding(.vertical, 2)
        .confirmationDialog(
            dialog.title, isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
            titleVisibility: .visible, presenting: confirming
        ) { deletion in
            Button("Delete", role: .destructive) {
                guard let id = row.model?.id else { return }
                Task { await models.delete(id, previous: deletion == .previous) }
            }
        } message: { _ in
            Text(dialog.message)
        }
    }

    /// A download, a delete or a restart of this service is under way.
    private var busy: Bool {
        models.isDownloading || models.deleting != nil || app.restarting.contains(kind)
    }

    /// The state, with the service's own while the model in use loads.
    private var stateLine: String {
        guard row.state == .inUse, let status = app.status?[kind] else { return row.stateLabel }
        let ready = status.state == .running && status.warm
        return ready && !app.restarting.contains(kind) ? row.stateLabel : "In use, loading"
    }

    private var dialog: (title: String, message: String) {
        if confirming == .previous, let model = row.model {
            return (
                "Delete the old version of \(model.name)?",
                "This frees \(Formatting.bytes(model.previousBytes)). The current version stays."
            )
        }
        return row.deleteConfirmation() ?? ("", "")
    }

    @ViewBuilder
    private var actions: some View {
        HStack(spacing: 6) {
            if row.state == .downloading {
                Button("Cancel") { models.cancelDownload() }
            }
            if let title = row.downloadTitle, let id = row.model?.id {
                Button(title) { Task { await models.download(id) } }.disabled(busy)
            }
            if row.canUse {
                Button("Use") { Task { await models.use(row, for: kind) } }
                    .disabled(busy)
                    .help(row.model?.downloaded == false ? "Downloads the model, then switches to it." : "")
            }
            if row.model?.downloaded == true {
                Button("Delete", role: .destructive) { confirming = .model }
                    .disabled(!row.canDelete || busy)
                    .help(row.canDelete ? "" : "In use. Choose another model first.")
            }
        }
        .controlSize(.small)
    }
}

/// A failure, or what a gated model needs, with the model card and a command to copy.
struct ModelMessageView: View {
    let message: ModelMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(message.text)
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if let link = message.link {
                Link("Open the model's page", destination: link).font(.caption)
            }
            if let command = message.command {
                CommandBlock(command: command)
            }
        }
    }
}

/// The byte bar of the download in flight, with its speed and time left.
struct DownloadBar: View {
    let models: ModelManager

    var body: some View {
        let progress = models.download?.progress
        VStack(alignment: .leading, spacing: 2) {
            if let progress {
                ProgressView(value: progress.fraction)
                let name = progress.model == models.download?.model ? "" : "\(progress.model): "
                Text(name + ByteText.line(progress))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            } else {
                ProgressView().progressViewStyle(.linear)
                Text("Starting").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// Below the panes' rows, outside the scrolling list: what voiced's models take, where, and
/// that the folder is shared.
struct ModelFooter: View {
    let list: ModelList

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text("voiced's models use \(Formatting.bytes(list.totalBytes)) in \(Self.abbreviated(list.cache)).")
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("This folder is shared with other apps; voiced deletes only its own models.").footnote()
            }
            Spacer(minLength: 8)
            Button("Show in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: list.cache)])
            }
            .controlSize(.small)
            .disabled(!FileManager.default.fileExists(atPath: list.cache))
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 10)
    }

    /// The home directory as `~`.
    nonisolated static func abbreviated(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }
}

/// The menu bar window's row while a model downloads: what, how far, and Cancel. The
/// download is the app's, so it goes on while the Settings window is closed.
struct DownloadProgressRow: View {
    let models: ModelManager

    var body: some View {
        if let download = models.download {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Downloading \(download.model.split(separator: "/").last.map(String.init) ?? download.model)")
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 8)
                    Button("Cancel") { models.cancelDownload() }.controlSize(.small)
                }
                DownloadBar(models: models)
            }
        }
    }
}
