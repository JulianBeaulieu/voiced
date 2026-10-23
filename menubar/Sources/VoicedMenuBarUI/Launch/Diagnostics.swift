import Foundation
import ServiceManagement
import VoicedControl

/// What the Debug pane shows and "Copy Diagnostics" copies: the app, what it pins, the
/// install it found, the services' Login Items statuses, the installer, and where everything
/// lives. No settings values and no API key; the config file is named, not read.
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
        appVersion: String, paths: VoicedPaths, translocatedFrom: URL?, manifest: InstallManifest?,
        installSource: URL?, record: InstallRecord?, backgroundItems: [String: SMAppService.Status],
        installer: String?, connection: String, serverVersion: String?, socketPath: String
    ) {
        func entry(_ label: String, _ value: String?, path: URL? = nil) -> Entry {
            Entry(label: label, value: value ?? "None", path: path?.path)
        }
        var app = [entry("Version", appVersion), entry("Location", paths.bundle.path, path: paths.bundle)]
        if let translocatedFrom { app.append(entry("Translocated from", translocatedFrom.path)) }
        if let installSource { app.append(entry("Install source override", installSource.path, path: installSource)) }
        if let installer { app.append(entry("Installer", installer)) }
        var pins = [entry("Pins", "None: this build has no installer resources")]
        if let manifest {
            pins = [
                entry("voiced", manifest.version),
                entry("Wheel", manifest.wheel.name),
                entry("Wheel sha256", manifest.wheel.sha256),
                entry("Lock sha256", manifest.lockSHA256),
                entry("Python", manifest.python),
            ] + SetupChoice.recommended(in: manifest.catalogue).downloads(in: manifest.catalogue).map {
                entry($0.repo, $0.revision.map { "revision \($0)" } ?? "not pinned")
            }
        }
        let install = record.map { record in
            [
                entry("Source", record.source.rawValue),
                entry("Environment", record.environment, path: record.environment.map { URL(fileURLWithPath: $0) }),
                entry("voiced", record.version),
                entry("Lock sha256", record.lockSHA256),
                entry("Python", record.python),
            ]
        } ?? [entry("install.json", "Not found")]
        let loginItems = BackgroundItems.labels.map { label in
            entry(label, backgroundItems[label].map { "Login Items: \($0.name)" } ?? "Not installed")
        }
        sections = [
            Section(title: "Menu bar app", entries: app),
            Section(title: "Bundled pins", entries: pins),
            Section(title: "Installed environment", entries: install),
            Section(
                title: "Services",
                entries: [entry("State", connection), entry("Watchdog reports voiced", serverVersion)] + loginItems),
            Section(
                title: "Paths",
                entries: [
                    entry("State directory", paths.stateDirectory.path, path: paths.stateDirectory),
                    entry("Control socket", socketPath),
                    entry("Config file", paths.configFile.path, path: paths.configFile.deletingLastPathComponent()),
                    entry("Logs", paths.logs.path, path: paths.logs),
                    entry("uv cache", paths.uvCache.path),
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
