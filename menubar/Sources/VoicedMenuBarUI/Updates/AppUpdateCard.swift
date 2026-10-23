import SwiftUI

/// The app's own update in the menu bar window (ADR 0034): a found update with Install and
/// Relaunch, its download and install, and the answer to a check the user asked for. Nothing
/// while there is nothing to say.
struct AppUpdateCard: View {
    let updates: AppUpdates

    var body: some View {
        if updates.state != .idle {
            content
                .padding(.horizontal, 14)
                .padding(.top, 12)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch updates.state {
        case .idle:
            EmptyView()
        case .checking:
            Progress(text: "Checking for updates", fraction: nil, cancel: nil)
        case .installing(let update) where updates.canRetryRelaunch:
            Notice(
                symbol: "arrow.clockwise.circle.fill", tint: .accentColor,
                text: "voiced \(update.version) is ready; the app relaunches to finish installing it.",
                actionTitle: "Relaunch Now", action: updates.relaunchNow)
        case .available(let update):
            Available(update: update, updates: updates)
        case .downloading(let update, let fraction):
            Progress(text: "Downloading voiced \(update.version)", fraction: fraction, cancel: updates.cancelDownload)
        case .installing(let update):
            Progress(text: "Installing voiced \(update.version); it opens again in a moment.", fraction: nil, cancel: nil)
        case .upToDate:
            Notice(
                symbol: "checkmark.circle.fill", tint: .green,
                text: "voiced \(updates.appVersion) is the newest version.", actionTitle: "OK",
                action: updates.dismiss)
        case .failed(let message):
            Notice(
                symbol: "exclamationmark.triangle.fill", tint: .orange, text: "Could not update the app. \(message)",
                actionTitle: "Dismiss", action: updates.dismiss)
        }
    }

    private struct Available: View {
        let update: AvailableUpdate
        let updates: AppUpdates

        var body: some View {
            let blocker = updates.installBlocker()
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "arrow.down.circle.fill")
                        .foregroundStyle(Color.accentColor)
                        .accessibilityHidden(true)
                    Text("voiced \(update.version) is available.")
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack(spacing: 8) {
                    Button("Install and Relaunch", action: updates.install)
                        .buttonStyle(.borderedProminent)
                        .disabled(blocker != nil)
                    Button("Later", action: updates.later)
                    Spacer(minLength: 0)
                    if let page = update.releasePage {
                        Link("Release Notes", destination: page).font(.callout)
                    }
                }
                .controlSize(.small)
                if let blocker {
                    Text(blocker)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(10)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }

    private struct Progress: View {
        let text: String
        let fraction: Double?
        let cancel: (() -> Void)?

        var body: some View {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    if fraction == nil { ProgressView().controlSize(.small) }
                    Text(text)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if let cancel {
                        Button("Cancel", action: cancel).controlSize(.small)
                    }
                }
                if let fraction {
                    ProgressView(value: fraction)
                        .accessibilityLabel(text)
                }
            }
            .padding(10)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }
}
