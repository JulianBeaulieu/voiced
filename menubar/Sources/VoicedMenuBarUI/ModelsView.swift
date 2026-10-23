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
        // A failed refresh keeps the last list; it says so above it.
        if let error = models.listError {
            Section("Models") {
                Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.secondary)
                Button("Try Again") { Task { await models.refresh() } }
            }
        }
        if let list = models.list {
            let ready = ModelRow.isReady(app.status?[kind], restarting: app.restarting.contains(kind))
            let rows = ModelRow.rows(
                list, kind: kind, settings: settings, downloading: models.download?.model, ready: ready)
            // Next to the voice picker: the voice is what changes for Home Assistant.
            if kind == .tts, let switched = models.switchedVoiceEngine {
                Section {
                    Notice(
                        symbol: "house", tint: .accentColor,
                        text: ModelMessage.homeAssistantHint(engine: switched),
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
                    } else if let note = engine.unavailableNote {
                        Text(note).footnote()
                    }
                }
            }
        } else if models.listError == nil {
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
                ModelSummary(
                    title: row.title, model: row.model, detail: ([row.sizeLine, row.stateLabel].compactMap { $0 }).joined(separator: " · "),
                    detailTint: row.state == .inUseMissing ? .red : .secondary)
                Spacer(minLength: 8)
                actions
            }
            if row.state == .downloading {
                DownloadBar(models: models)
            }
            if let note = row.note {
                Text(note).font(.caption).foregroundStyle(.secondary)
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

    private var dialog: (title: String, message: String) {
        (confirming == .previous ? row.deleteOldConfirmation() : row.deleteConfirmation()) ?? ("", "")
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
            if row.showsDelete {
                Button("Delete", role: .destructive) { confirming = .model }
                    .disabled(!row.canDelete || busy)
            }
        }
        .controlSize(.small)
    }
}

/// A model's name, licence, Recommended badge, ratings and what it is for, then `detail`: the
/// left of a row in the Models pane, and the chosen model on the setup screen.
struct ModelSummary: View {
    let title: String
    /// Nil for a plugin engine the catalogue does not know.
    let model: CatalogueModel?
    let detail: String
    var detailTint: Color = .secondary

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(title).font(.body.weight(.medium))
                if let model, !model.license.isEmpty {
                    if let card = model.cardURL {
                        Link(model.license, destination: card).font(.caption)
                    } else {
                        Text(model.license).font(.caption).foregroundStyle(.secondary)
                    }
                }
                if model?.recommended == true {
                    RecommendedBadge()
                }
            }
            if let ratings = model?.ratings, !ratings.isEmpty {
                RatingBars(ratings: ratings)
            }
            if let summary = model?.summary {
                Text(summary).font(.caption).foregroundStyle(.secondary)
            }
            Text(detail)
                .font(.caption)
                .foregroundStyle(detailTint)
        }
    }
}

/// The default models' badge, beside the name.
struct RecommendedBadge: View {
    var body: some View {
        Text("Recommended")
            .font(.caption2.weight(.medium))
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(Capsule().fill(Color.accentColor.opacity(0.15)))
    }
}

/// Quality and Speed as five-step bars, linking to the benchmarks they come from.
struct RatingBars: View {
    let ratings: [ModelRow.Rating]

    var body: some View {
        Link(destination: ModelRow.benchmarks) {
            HStack(spacing: 12) {
                ForEach(ratings, id: \.label) { rating in
                    HStack(spacing: 4) {
                        Text(rating.label).font(.caption).foregroundStyle(.secondary)
                        HStack(spacing: 2) {
                            ForEach(1...5, id: \.self) { step in
                                RoundedRectangle(cornerRadius: 1.5)
                                    .fill(step <= rating.value ? Color.accentColor : Color.secondary.opacity(0.25))
                                    .frame(width: 10, height: 5)
                            }
                        }
                    }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(rating.label) \(rating.value) of 5")
                }
            }
        }
        .buttonStyle(.plain)
        .help(ModelRow.ratingsHelp)
        .accessibilityHint("Opens the benchmarks on GitHub")
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
                Text(list.usageLine)
                    .font(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(ModelList.sharedFolderNote).footnote()
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
}

/// The menu bar window's row while a model downloads: what, how far, and Cancel. The
/// download is the app's, so it goes on while the Settings window is closed.
struct DownloadProgressRow: View {
    let models: ModelManager

    var body: some View {
        if let download = models.download {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Downloading \(models.list?.model(download.model)?.model.name ?? download.model)")
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
