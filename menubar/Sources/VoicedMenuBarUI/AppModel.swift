import AppKit
import Foundation
import Observation
import UniformTypeIdentifiers
import VoicedControl

/// Owns the connection to voiced and everything the window shows. It keeps one client
/// connected, reconnecting with a backoff, applies `status` and `settings` events as they
/// arrive, and turns user actions into requests.
@MainActor
@Observable
public final class AppModel {
    public private(set) var connection: ConnectionState = .connecting {
        didSet { if connection != oldValue { updateDashboard() } }
    }
    public private(set) var status: StatusPayload? {
        didSet { if status != oldValue { updateDashboard() } }
    }
    public private(set) var settings: SettingsPayload?
    public private(set) var engines: EnginesPayload?
    /// What `install.json` records, from the watchdog's hello; nil while not connected.
    public private(set) var installRecord: InstallRecord?
    /// Services whose settings changed and that have not restarted since.
    public private(set) var restartRequired: Set<ServiceKind> = []
    public private(set) var restarting: Set<ServiceKind> = []
    /// Warnings from the last `settings.set`, such as `lan_without_api_key`.
    public private(set) var warnings: [String] = []
    public private(set) var isSaving = false
    public var errorMessage: String?

    /// What the windows and the icon show. Kept up to date when the connection or the status
    /// changes, rather than recomputed by every view that reads it.
    public private(set) var dashboard = Dashboard(health: .attention, headline: "Connecting")

    private func updateDashboard() {
        let next = DashboardReducer.reduce(connection: connection, status: status, socketPath: socketPath)
        if next != dashboard { dashboard = next }
    }

    public var serverVersion: String? {
        if case .connected(let version) = connection { return version }
        return nil
    }

    /// The contract asks the UI to show this whenever it applies.
    public var showsLANWarning: Bool {
        guard let settings else { return false }
        return settings.isLANWithoutAPIKey || warnings.contains("lan_without_api_key")
    }

    /// Warnings other than the LAN one, described for people.
    public var otherWarnings: [String] {
        warnings.filter { $0 != "lan_without_api_key" }.map(SettingsWarning.describe)
    }

    public let socketPath: String
    private let installProbe: InstallProbe
    private var backoff: Backoff
    private let livenessTimeout: Duration
    private let restartTimeout: Duration
    private let opener: @MainActor (OpenAction) -> Bool
    private var client: ControlClient?
    private var loop: Task<Void, Never>?
    private var lastHeard = ContinuousClock.now

    /// `livenessTimeout`: the contract pushes `status` at least every 10 s, so a connection
    /// silent for longer than this is treated as dead.
    public init(
        socketPath: String = SocketLocation.resolve(),
        installProbe: InstallProbe = InstallProbe(),
        backoff: Backoff = Backoff(),
        livenessTimeout: Duration = .seconds(25),
        restartTimeout: Duration = ControlClient.restartTimeout,
        opener: @escaping @MainActor (OpenAction) -> Bool = AppModel.openWithWorkspace
    ) {
        self.socketPath = socketPath
        self.installProbe = installProbe
        self.backoff = backoff
        self.livenessTimeout = livenessTimeout
        self.restartTimeout = restartTimeout
        self.opener = opener
        updateDashboard()
    }

    // MARK: - Connection

    public func start() {
        guard loop == nil else { return }
        loop = Task { await run() }
    }

    public func stop() {
        loop?.cancel()
        loop = nil
        client?.close()
        client = nil
    }

    private func run() async {
        while !Task.isCancelled {
            let client = ControlClient(socketPath: socketPath)
            // Set before connecting so `stop()` can interrupt the attempt.
            self.client = client
            let reason = await session(with: client)
            // A cancelled loop belongs to an earlier `start()`; it must not touch the state.
            guard !Task.isCancelled else { return }
            if self.client === client { self.client = nil }
            status = nil
            installRecord = nil
            restarting = []
            lastHeard = .now
            if let reason {
                connection = .unreachable(installed: installProbe.isInstalled(), reason: reason)
            }
            try? await Task.sleep(for: backoff.next())
        }
    }

    /// One connection from connect to close. Returns why it ended, or nil when the protocol
    /// was refused (the state is already set).
    private func session(with client: ControlClient) async -> UnreachableReason? {
        do {
            let hello = try await client.connect()
            guard !Task.isCancelled else { return .disconnected }
            installRecord = hello.install
            connection = .connected(version: hello.version)
            lastHeard = .now
        } catch ControlError.unsupportedProtocol(let server, let supported) {
            connection = .incompatible(
                message: ControlError.unsupportedProtocol(server: server, supported: supported)
                    .localizedDescription)
            return nil
        } catch ControlError.cannotConnect(let failure) {
            return .cannotConnect(failure)
        } catch ControlError.protocolViolation(let detail) {
            return .protocolError("voiced sent an unexpected message: \(detail).")
        } catch {
            return .cannotConnect(.other(error.localizedDescription))
        }

        var stalled = false
        var setupError: String?
        let liveness = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: livenessTimeout / 5)
                // A restart in flight can hold the server's attention for longer than the
                // liveness window; its answer, or its timeout, decides instead.
                if restarting.isEmpty, ContinuousClock.now - lastHeard > livenessTimeout {
                    stalled = true
                    client.close()
                    return
                }
            }
        }
        let setup = Task {
            if let failure = await refresh(using: client) {
                setupError = failure
                client.close()
            }
        }
        for await event in client.events { handle(event) }
        liveness.cancel()
        setup.cancel()
        if stalled { return .stoppedAnswering }
        if let setupError { return .setupFailed(setupError) }
        return .disconnected
    }

    /// Fetches status, settings and the engine list after connecting. Returns a description
    /// of the failure, if any; a half-filled model is worse than a reconnect.
    private func refresh(using client: ControlClient) async -> String? {
        do {
            async let status = client.status()
            async let settings = client.settings()
            async let engines = client.engines()
            let (newStatus, newSettings, newEngines) = try await (status, settings, engines)
            guard self.client === client else { return nil }
            handleStatus(newStatus)
            self.settings = newSettings
            self.engines = newEngines
            return nil
        } catch ControlError.connectionClosed, ControlError.notConnected {
            // The connection went away under the requests; that is a disconnect, not a
            // misbehaving server.
            return nil
        } catch {
            return "voiced did not answer the first requests: \(Self.describe(error))"
        }
    }

    func handle(_ event: ControlEvent) {
        lastHeard = .now
        switch event {
        case .status(let payload):
            handleStatus(payload)
        case .settings(let payload):
            settings = payload
        case .other(name: "settings", _):
            // The contract does not fix this event's payload; fetch when it is not the
            // `settings.get` shape.
            guard let client else { return }
            Task {
                if let fresh = try? await client.settings(), self.client === client {
                    settings = fresh
                }
            }
        case .other:
            break
        }
    }

    private func handleStatus(_ payload: StatusPayload) {
        // A service that restarted, whoever restarted it, has picked up the changes.
        for kind in restartRequired {
            guard let old = status?[kind], let new = payload[kind] else { continue }
            let newProcess = new.pid != nil && new.pid != old.pid
            let uptimeDropped = (new.uptime ?? 0) < (old.uptime ?? 0)
            if newProcess || uptimeDropped { restartRequired.remove(kind) }
        }
        status = payload
        // Only a connection that delivers status has proved itself; a watchdog that says
        // hello and dies keeps backing off.
        backoff.reset()
    }

    // MARK: - Actions

    public func restart(_ target: RestartTarget) async {
        guard let client else { return }
        let kinds: [ServiceKind] = if case .service(let kind) = target { [kind] } else { ServiceKind.allCases }
        restarting.formUnion(kinds)
        defer { restarting.subtract(kinds) }
        do {
            let refused = try await client.restart(target, timeout: restartTimeout)
            restartRequired.subtract(kinds.filter { refused[$0] == nil })
            errorMessage = refused.isEmpty ? nil : Self.describe(refused: refused)
        } catch ControlError.timedOut {
            // The request went out; a hung service can take longer to kill than the timeout.
            // Status events show how it turns out.
            restartRequired.subtract(kinds)
        } catch ControlError.remote(let remote) {
            // Every service refused: the same wording as a partial refusal.
            errorMessage = "Not restarted: \(remote.displayMessage)"
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

    /// One short line in `voice` from the running text to speech service, as a WAV file. Each
    /// preview has a connection of its own: cancelling the task closes it, and the server then
    /// drops the request to the service, which stops synthesizing. The app's connection is
    /// never closed by a stopped preview.
    public func speak(_ text: String, voice: String) async throws -> Data {
        guard self.client != nil else { throw ControlError.notConnected }
        let client = ControlClient(socketPath: socketPath)
        defer { client.close() }
        try await client.connect()
        return try await client.speak(text, voice: voice)
    }

    public func open(_ target: OpenTarget) async {
        guard let client else { return }
        do {
            let location = try await client.openTarget(target)
            guard let action = OpenPolicy.action(for: target, location: location) else {
                errorMessage = Self.isMissingPath(target, location)
                    ? "Not opening \(location): it does not exist."
                    : "Not opening \(location): it is not the kind of thing asked for."
                return
            }
            errorMessage = opener(action) ? nil : "Cannot open \(location)."
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// A folder or file target whose path is not there, such as the logs folder before
    /// anything was logged.
    static func isMissingPath(_ target: OpenTarget, _ location: String) -> Bool {
        guard target != .statusPage else { return false }
        let path = location.hasPrefix("file://") ? URL(string: location)?.path ?? location : location
        return path.hasPrefix("/") && !FileManager.default.fileExists(atPath: path)
    }

    /// "Not restarted: ..." with the server's reason, without its error code prefix.
    static func describe(refused: [ServiceKind: String]) -> String {
        let reasons = ServiceKind.allCases.compactMap { kind in
            refused[kind].map { text in
                let parts = text.split(separator: ":", maxSplits: 1)
                let isCode = parts.count == 2 && parts[0].allSatisfy { $0.isLowercase || $0 == "_" }
                return isCode ? parts[1].trimmingCharacters(in: .whitespaces) : text
            }
        }
        return "Not restarted: " + reasons.joined(separator: "; ")
    }

    static func describe(_ error: Error) -> String {
        if case ControlError.remote(let remote) = error {
            return remote.code == "invalid_setting" ? "Not saved. \(remote.displayMessage)" : remote.displayMessage
        }
        return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    /// Opens a checked target: web pages in the browser, folders in Finder, the config file
    /// in the default plain-text editor.
    public static func openWithWorkspace(_ action: OpenAction) -> Bool {
        let workspace = NSWorkspace.shared
        switch action {
        case .webPage(let url), .folder(let url):
            return workspace.open(url)
        case .textFile(let url):
            guard let editor = workspace.urlForApplication(toOpen: .plainText) else {
                workspace.activateFileViewerSelecting([url])
                return true
            }
            workspace.open([url], withApplicationAt: editor, configuration: NSWorkspace.OpenConfiguration())
            return true
        }
    }
}
