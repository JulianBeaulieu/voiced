import AppKit
import Foundation
import Observation
import VoicedControl

/// Owns the connection to voiced and everything the popover shows. It keeps one client
/// connected, reconnecting with a backoff, applies `status` and `settings` events as they
/// arrive, and turns user actions into requests.
@MainActor
@Observable
public final class AppModel {
    public enum Tab: Hashable, Sendable {
        case status, settings
    }

    public private(set) var connection: ConnectionState = .connecting
    public private(set) var status: StatusPayload?
    public private(set) var settings: SettingsPayload?
    public private(set) var engines: EnginesPayload?
    /// Services whose settings changed and that have not been restarted since.
    public private(set) var restartRequired: Set<ServiceKind> = []
    public private(set) var restarting: Set<ServiceKind> = []
    /// Warnings from the last `settings.set`, such as `lan_without_api_key`.
    public private(set) var warnings: [String] = []
    public private(set) var isSaving = false
    public var errorMessage: String?
    public var tab: Tab = .status

    public var dashboard: Dashboard { DashboardReducer.reduce(connection: connection, status: status) }

    public var serverVersion: String? {
        if case .connected(let version) = connection { return version }
        return nil
    }

    /// The contract asks the UI to show this whenever it applies.
    public var showsLANWarning: Bool {
        guard let settings else { return false }
        return settings.isLANWithoutAPIKey || warnings.contains("lan_without_api_key")
    }

    public let socketPath: String
    private let installProbe: InstallProbe
    private var backoff: Backoff
    private let opener: @MainActor (String) -> Bool
    private var client: ControlClient?
    private var loop: Task<Void, Never>?

    public init(
        socketPath: String = SocketLocation.resolve(),
        installProbe: InstallProbe = InstallProbe(),
        backoff: Backoff = Backoff(),
        opener: @escaping @MainActor (String) -> Bool = AppModel.openWithWorkspace
    ) {
        self.socketPath = socketPath
        self.installProbe = installProbe
        self.backoff = backoff
        self.opener = opener
    }

    // MARK: - Connection

    public func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in await self?.run() }
    }

    public func stop() {
        loop?.cancel()
        loop = nil
        client?.close()
    }

    private func run() async {
        while !Task.isCancelled {
            let client = ControlClient(socketPath: socketPath)
            do {
                let hello = try await client.connect()
                self.client = client
                backoff.reset()
                connection = .connected(version: hello.version)
                let refresh = Task { await self.refresh(using: client) }
                for await event in client.events { apply(event) }
                refresh.cancel()
                connection = .unreachable(installed: installProbe.isInstalled())
            } catch let error as ControlError {
                if case .unsupportedProtocol = error {
                    connection = .incompatible(message: error.localizedDescription)
                } else {
                    connection = .unreachable(installed: installProbe.isInstalled())
                }
            } catch {
                connection = .unreachable(installed: installProbe.isInstalled())
            }
            if self.client === client { self.client = nil }
            status = nil
            restarting = []
            if Task.isCancelled { break }
            try? await Task.sleep(for: backoff.next())
        }
    }

    /// Fetches status, settings and the engine list after connecting.
    public func refresh() async {
        guard let client else { return }
        await refresh(using: client)
    }

    private func refresh(using client: ControlClient) async {
        async let status = try? client.status()
        async let settings = try? client.settings()
        async let engines = try? client.engines()
        let (newStatus, newSettings, newEngines) = await (status, settings, engines)
        guard self.client === client else { return }
        if let newStatus { self.status = newStatus }
        if let newSettings { self.settings = newSettings }
        if let newEngines { self.engines = newEngines }
    }

    func apply(_ event: ControlEvent) {
        switch event {
        case .status(let payload): status = payload
        case .settings(let payload): settings = payload
        case .other(name: "settings", _):
            // The contract does not fix this event's payload; fetch when it is not the
            // `settings.get` shape.
            guard let client else { return }
            Task {
                if let fresh = try? await client.settings(), self.client === client {
                    settings = fresh
                }
            }
        case .other: break
        }
    }

    // MARK: - Actions

    public func restart(_ target: RestartTarget) async {
        guard let client else { return }
        let kinds: [ServiceKind] = if case .service(let kind) = target { [kind] } else { ServiceKind.allCases }
        restarting.formUnion(kinds)
        defer { restarting.subtract(kinds) }
        do {
            try await client.restart(target)
            restartRequired.subtract(kinds)
            errorMessage = nil
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// Restarts whatever the last settings changes need.
    public func restartPending() async {
        let pending = restartRequired
        if pending.count == ServiceKind.allCases.count {
            await restart(.all)
        } else if let kind = pending.first {
            await restart(.service(kind))
        }
    }

    /// Sends one settings change. Returns true when the server accepted it.
    @discardableResult
    public func apply(_ edit: SettingsEdit) async -> Bool {
        guard let client, let settings else { return false }
        let args = edit.arguments(current: settings, engines: engines)
        guard !args.isEmpty else { return true }
        isSaving = true
        defer { isSaving = false }
        do {
            let result = try await client.setSettings(args)
            self.settings = result.settings
            restartRequired.formUnion(result.restartRequired)
            warnings = result.warnings
            errorMessage = nil
            return true
        } catch {
            errorMessage = Self.describe(error)
            return false
        }
    }

    public func open(_ target: OpenTarget) async {
        guard let client else { return }
        do {
            let location = try await client.openTarget(target)
            if !opener(location) { errorMessage = "Cannot open \(location)." }
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    static func describe(_ error: Error) -> String {
        if case ControlError.remote(let remote) = error, remote.code == "invalid_setting" {
            return "Not saved. \(remote.message)"
        }
        return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    /// Opens a URL from `open`: web pages in the browser, folders in Finder, files in their
    /// default app (or TextEdit when nothing claims them, as for `.toml`).
    public static func openWithWorkspace(_ target: String) -> Bool {
        let workspace = NSWorkspace.shared
        if let url = URL(string: target), let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https"
        {
            return workspace.open(url)
        }
        let path = target.hasPrefix("file://") ? URL(string: target)?.path ?? target : target
        guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path) else { return false }
        let url = URL(fileURLWithPath: path)
        if workspace.urlForApplication(toOpen: url) == nil,
            let textEdit = workspace.urlForApplication(withBundleIdentifier: "com.apple.TextEdit")
        {
            workspace.open([url], withApplicationAt: textEdit, configuration: NSWorkspace.OpenConfiguration())
            return true
        }
        return workspace.open(url)
    }
}
