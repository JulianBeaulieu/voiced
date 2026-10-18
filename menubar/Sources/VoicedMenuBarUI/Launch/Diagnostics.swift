import Foundation
import VoicedControl

/// What the Debug pane shows and "Copy Diagnostics" copies: the app, what it pins, the
/// install it found, and where everything lives. No settings values and no API key; the
/// config file is named, not read.
public struct Diagnostics: Sendable, Equatable {
    public struct Entry: Sendable, Equatable, Identifiable {
        public var label: String
        public var value: String
        /// A file or folder "Show in Finder" can reveal.
        public var path: String?
        public var id: String { label }
    }

    public struct Section: Sendable, Equatable, Identifiable {
        public var title: String
        public var entries: [Entry]
        public var id: String { title }
    }

    public var sections: [Section]

    public init(
        appVersion: String, bundlePath: String, translocatedFrom: String?, manifest: InstallManifest?,
        installSource: String?, record: InstallRecord?, connection: String, serverVersion: String?,
        socketPath: String, home: URL
    ) {
        func entry(_ label: String, _ value: String?, path: String? = nil) -> Entry {
            Entry(label: label, value: value ?? "None", path: path)
        }
        let state = home.appendingPathComponent(".voiced").path
        var app = [
            entry("Version", appVersion),
            entry("Location", bundlePath, path: bundlePath),
        ]
        if let translocatedFrom { app.append(entry("Translocated from", translocatedFrom)) }
        if let installSource { app.append(entry("Install source override", installSource, path: installSource)) }
        var pins = [entry("Pins", manifest == nil ? "None: this build has no installer resources" : nil)]
        if let manifest {
            pins = [
                entry("voiced wheel", manifest.wheel.name),
                entry("Wheel sha256", manifest.wheel.sha256),
                entry("Lock sha256", manifest.lockSHA256),
                entry("Python", manifest.python),
            ] + manifest.models.map { entry($0.repo, "revision \($0.revision)") }
        }
        let install = record.map { record in
            [
                entry("Source", record.source),
                entry("Environment", record.environment, path: record.environment),
                entry("voiced", record.version),
                entry("Lock sha256", record.lockSHA256),
                entry("Python", record.python),
            ]
        } ?? [entry("install.json", "Not found")]
        sections = [
            Section(title: "Menu bar app", entries: app),
            Section(title: "Bundled pins", entries: pins),
            Section(title: "Installed environment", entries: install),
            Section(
                title: "Services",
                entries: [entry("State", connection), entry("Watchdog reports voiced", serverVersion)]),
            Section(
                title: "Paths",
                entries: [
                    entry("State directory", state, path: state),
                    entry("Control socket", socketPath),
                    entry("Config file", home.appendingPathComponent(".config/voiced/config.toml").path,
                          path: home.appendingPathComponent(".config/voiced").path),
                    entry("Logs", home.appendingPathComponent("Library/Logs/voiced").path,
                          path: home.appendingPathComponent("Library/Logs/voiced").path),
                    entry("uv cache", home.appendingPathComponent("Library/Application Support/voiced/uv-cache").path),
                ]),
        ]
    }

    /// Plain text for a bug report.
    public var report: String {
        sections.map { section in
            ([section.title] + section.entries.map { "  \($0.label): \($0.value)" }).joined(separator: "\n")
        }.joined(separator: "\n\n") + "\n"
    }
}
