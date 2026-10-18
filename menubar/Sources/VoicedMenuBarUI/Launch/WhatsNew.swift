import Foundation
import Observation
import SwiftUI
import VoicedControl

/// "What's new" (ADR 0018, "In the menu bar"): the release notes bundled with the app
/// (`Contents/Resources/WhatsNew.md`, written by bundle.sh from the CHANGELOG), shown once
/// after the services move to the version this app carries. The first version the app sees
/// running is only remembered: a first install has nothing to compare with, so it shows no
/// notes; an update, by the app or by Homebrew, shows them once.
@MainActor
@Observable
public final class WhatsNew {
    public static let windowID = "whats-new"
    /// The last voiced version the app saw the services run with its own pins.
    public static let seenKey = "whatsNewSeenVersion"

    public let notes: String?
    /// Set when the notes should open; the window clears it once shown.
    public private(set) var isPending = false

    private let version: String?
    private let defaults: UserDefaults

    public init(notes: String?, version: String?, defaults: UserDefaults = .standard) {
        self.notes = notes
        self.version = version
        self.defaults = defaults
    }

    /// The notes in the bundle, nil when it has none.
    public static func notes(in paths: VoicedPaths = VoicedPaths()) -> String? {
        guard let text = try? String(contentsOf: paths.whatsNew, encoding: .utf8), !text.isEmpty else { return nil }
        return text
    }

    /// Called whenever how the installed voiced compares with the app's pins is known.
    public func observe(_ match: InstallMatch?) {
        guard match == .matches, let version else { return }
        let seen = defaults.string(forKey: Self.seenKey)
        guard seen != version else { return }
        defaults.set(version, forKey: Self.seenKey)
        if seen != nil, notes != nil { isPending = true }
    }

    public func shown() {
        isPending = false
    }
}

/// The release notes in a window of their own. The Markdown is read block by block: SwiftUI's
/// `Text` renders inline Markdown only, so headings and list items are laid out here.
public struct WhatsNewView: View {
    let notes: String

    public init(notes: String) {
        self.notes = notes
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(MarkdownBlock.parse(notes).enumerated()), id: \.offset) { _, block in
                    view(for: block)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(width: 520, height: 460)
    }

    @ViewBuilder
    private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(Self.inline(text))
                .font(level <= 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                .padding(.top, level <= 1 ? 0 : 6)
        case .item(let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•").accessibilityHidden(true)
                Text(Self.inline(text)).fixedSize(horizontal: false, vertical: true)
            }
        case .paragraph(let text):
            Text(Self.inline(text)).fixedSize(horizontal: false, vertical: true)
        }
    }

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

/// The parts of Markdown the CHANGELOG uses: headings, list items (continued on indented
/// lines) and paragraphs.
enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case item(String)
    case paragraph(String)

    static func parse(_ markdown: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var current: MarkdownBlock?
        func flush() {
            if let block = current { blocks.append(block) }
            current = nil
        }
        for raw in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty {
                flush()
            } else if line.hasPrefix("#") {
                flush()
                let level = line.prefix { $0 == "#" }.count
                blocks.append(.heading(level: level, text: line.dropFirst(level).trimmingCharacters(in: .whitespaces)))
            } else if line.hasPrefix("- ") || line.hasPrefix("* ") {
                flush()
                current = .item(String(line.dropFirst(2)))
            } else {
                switch current {
                case .item(let text): current = .item(text + " " + line)
                case .paragraph(let text): current = .paragraph(text + " " + line)
                default: current = .paragraph(line)
                }
            }
        }
        flush()
        return blocks
    }
}
