import Darwin
import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

final class InstallRecordTests: XCTestCase {
    private var state: URL!

    override func setUpWithError() throws {
        state = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: state, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: state)
    }

    private var manifest: URL { state.appendingPathComponent("install.json") }

    // MARK: - hello

    func testHelloCarriesWhatInstallJSONRecords() async throws {
        let server = try startServer {
            $0.install = .object([
                "source": "app", "environment": "/Users/someone/.voiced/envs/20261018T120000",
                "version": "0.1.0", "lock_sha256": "ABC", "python": "3.12.15",
            ])
        }
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        defer { client.close() }
        let hello = try await client.connect()
        XCTAssertEqual(
            hello.install,
            InstallRecord(
                source: .app, environment: "/Users/someone/.voiced/envs/20261018T120000", version: "0.1.0",
                lockSHA256: "ABC", python: "3.12.15"))
    }

    func testHelloWithoutAnInstallHasNone() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        defer { client.close() }
        let hello = try await client.connect()
        XCTAssertNil(hello.install)
    }

    /// Protocol 1 ignores what it cannot use: a malformed `install` does not refuse the hello.
    func testAMalformedInstallIsIgnored() async throws {
        for install in [#""app""#, #"{"environment": "/x"}"#, #"{"source": 3}"#] {
            let server = try startServer { $0.firstLine = #"{"event":"hello","data":{"protocol":1,"version":"0.1.0","install":\#(install)}}"# }
            defer { server.stop() }
            let client = ControlClient(socketPath: server.socketPath)
            let hello = try await client.connect()
            XCTAssertEqual(hello, Hello(protocol: 1, version: "0.1.0"), install)
            client.close()
        }
    }

    // MARK: - install.json

    func testReadsTheFile() throws {
        try Data(#"{"source": "homebrew", "environment": "/opt/homebrew/opt/voiced/libexec", "version": "0.2.0", "lock_sha256": "abc", "python": "3.12.15", "later": 1}"#.utf8)
            .write(to: manifest)
        XCTAssertEqual(
            InstallRecord.read(stateDirectory: state),
            InstallRecord(
                source: .homebrew, environment: "/opt/homebrew/opt/voiced/libexec", version: "0.2.0", lockSHA256: "abc",
                python: "3.12.15"))
    }

    /// What the app writes when it claims the folder, before any environment is linked.
    func testAClaimHasNoEnvironment() throws {
        try Data(#"{"source": "app", "environment": null, "version": "", "lock_sha256": ""}"#.utf8).write(to: manifest)
        XCTAssertEqual(InstallRecord.read(stateDirectory: state), InstallRecord(source: .app))
    }

    func testNoFileNoRecord() {
        XCTAssertNil(InstallRecord.read(stateDirectory: state))
    }

    func testWhatIsNotAManifestIsNoRecord() throws {
        for text in ["not json", "[]", #"{"environment": "/x"}"#, "", #"{"source": "pipx"}"#] {
            try Data(text.utf8).write(to: manifest)
            XCTAssertNil(InstallRecord.read(stateDirectory: state), text)
        }
    }

    func testAnOversizedFileIsNoRecord() throws {
        let padding = String(repeating: " ", count: 64 * 1024)
        try Data(#"{"source": "app"\#(padding)}"#.utf8).write(to: manifest)
        XCTAssertNil(InstallRecord.read(stateDirectory: state))
    }

    func testADirectoryIsNoRecord() throws {
        try FileManager.default.createDirectory(at: manifest, withIntermediateDirectories: true)
        XCTAssertNil(InstallRecord.read(stateDirectory: state))
    }

    /// A FIFO with no writer would block a plain open; it reads as nothing instead.
    func testAFIFODoesNotBlock() throws {
        XCTAssertEqual(mkfifo(manifest.path, 0o600), 0)
        XCTAssertNil(InstallRecord.read(stateDirectory: state))
    }

    // MARK: - The menu bar icon

    func testIconStates() {
        func state(_ connection: ConnectionState, _ status: StatusPayload? = nil, installing: Bool = false) -> MenuBarIconState {
            let dashboard = DashboardReducer.reduce(connection: connection, status: status, socketPath: "/s")
            return MenuBarIconStatus(dashboard: dashboard, installing: installing).state
        }
        let connected = ConnectionState.connected(version: "0.1.0")
        XCTAssertEqual(state(connected, FakeScenario.running.initialState.status), .idle)
        XCTAssertEqual(state(connected, FakeScenario.starting.initialState.status), .busy)
        XCTAssertEqual(state(.connecting), .busy)
        XCTAssertEqual(state(connected, nil), .busy, "connected, status not yet in")
        XCTAssertEqual(state(connected, FakeScenario.degraded.initialState.status), .error)
        XCTAssertEqual(state(connected, FakeScenario.stopped.initialState.status), .error)
        XCTAssertEqual(state(.unreachable(installed: false, reason: .cannotConnect(.noSocket))), .error)
        XCTAssertEqual(state(.unreachable(installed: true, reason: .cannotConnect(.noSocket))), .error)
        XCTAssertEqual(state(.incompatible(message: "")), .error)
        XCTAssertEqual(state(.unreachable(installed: false, reason: .cannotConnect(.noSocket)), installing: true), .busy)
        XCTAssertEqual(state(connected, FakeScenario.running.initialState.status, installing: true), .busy)
    }

    func testIconLabels() {
        let running = DashboardReducer.reduce(
            connection: .connected(version: "0.1.0"), status: FakeScenario.running.initialState.status, socketPath: "/s")
        XCTAssertEqual(MenuBarIconStatus(dashboard: running, installing: false).accessibilityLabel, "voiced: Running")
        XCTAssertEqual(MenuBarIconStatus(dashboard: running, installing: true).accessibilityLabel, "voiced: Installing")
    }
}
