import FakeControlServerKit
import Foundation
import XCTest

@testable import VoicedControl

final class ClientTests: XCTestCase {
    func testHelloCarriesTheServerVersion() async throws {
        let server = try startServer { $0.version = "1.2.3" }
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        let hello = try await client.connect()
        XCTAssertEqual(hello, Hello(protocol: 1, version: "1.2.3"))
        client.close()
    }

    func testTypedOperationsDecodeTheFakeServersPayloads() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }

        let status = try await client.status()
        XCTAssertTrue(status.installed)
        XCTAssertEqual(status[.tts]?.engine, "kokoro")
        XCTAssertEqual(status.watchdog?.state, .running)

        let settings = try await client.settings()
        XCTAssertEqual(settings.bind, .loopback)
        XCTAssertTrue(settings.apiKeySet)
        XCTAssertEqual(settings.ports?.ttsHTTP, 8880)

        let engines = try await client.engines()
        XCTAssertEqual(engines.tts.map(\.name), ["kokoro", "soprano", "fake"])

        let target = try await client.openTarget(.statusPage)
        XCTAssertEqual(target, "http://127.0.0.1:8880/")

        let lines = try await client.tailLogs(service: "tts", lines: 2)
        XCTAssertEqual(lines.count, 2)
    }

    func testRequestsCarryDistinctIdsAndConcurrentAnswersReachTheirCallers() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }

        let ops = (0..<30).map { ["status", "settings.get", "engines.list"][$0 % 3] }
        let answers = try await withThrowingTaskGroup(of: (String, JSONValue).self) { group in
            for op in ops {
                group.addTask { (op, try await client.request(op)) }
            }
            return try await group.reduce(into: [(String, JSONValue)]()) { $0.append($1) }
        }
        XCTAssertEqual(answers.count, 30)
        for (op, answer) in answers {
            switch op {
            case "status": XCTAssertNotNil(answer["services"])
            case "settings.get": XCTAssertNotNil(answer["api_key_set"])
            default: XCTAssertNotNil(answer["tts"])
            }
        }
        let ids = server.requests.map(\.id)
        XCTAssertEqual(ids.count, 30)
        XCTAssertEqual(Set(ids).count, 30, "every request has its own id")
    }

    func testRequestIdsIncrease() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }
        _ = try await client.request("status")
        _ = try await client.request("status")
        _ = try await client.request("settings.get")
        XCTAssertEqual(server.requests.map(\.id), [1, 2, 3])
        XCTAssertEqual(server.requests.map(\.op), ["status", "status", "settings.get"])
    }

    func testServerErrorsBecomeRemoteErrors() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }

        do {
            _ = try await client.setSettings(["tts.voice": "nobody"])
            XCTFail("expected invalid_setting")
        } catch ControlError.remote(let error) {
            XCTAssertEqual(error.code, "invalid_setting")
            XCTAssertEqual(error.field, "tts.voice")
        }
        do {
            _ = try await client.request("no.such.op")
            XCTFail("expected unknown_op")
        } catch ControlError.remote(let error) {
            XCTAssertEqual(error.code, "unknown_op")
        }
    }

    func testSettingsSetReportsRestartsAndTheLANWarning() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }

        let cleared = try await client.setSettings(["api_key": ""])
        XCTAssertFalse(cleared.settings.apiKeySet)
        let result = try await client.setSettings(["bind": "lan"])
        XCTAssertEqual(result.settings.bind, .lan)
        XCTAssertEqual(Set(result.restartRequired), [.stt, .tts])
        XCTAssertEqual(result.warnings, ["lan_without_api_key"])
        XCTAssertTrue(result.settings.isLANWithoutAPIKey)
    }

    func testStatusAndSettingsEventsArriveOnTheStream() async throws {
        let server = try startServer()
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }

        try await client.restart(.service(.tts))
        let starting = await nextEvent(client) {
            if case .status(let status) = $0 { return status[.tts]?.state == .starting }
            return false
        }
        XCTAssertNotNil(starting)
        let running = await nextEvent(client) {
            if case .status(let status) = $0 { return status[.tts]?.state == .running }
            return false
        }
        XCTAssertNotNil(running)

        _ = try await client.setSettings(["zeroconf": false])
        let settings = await nextEvent(client) {
            if case .settings = $0 { return true }
            return false
        }
        guard case .settings(let payload)? = settings else { return XCTFail("no settings event") }
        XCTAssertFalse(payload.zeroconf)
    }

    func testRefusesANewerProtocol() async throws {
        let server = try startServer(.newerProtocol)
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        do {
            try await client.connect()
            XCTFail("expected unsupportedProtocol")
        } catch let error as ControlError {
            XCTAssertEqual(error, .unsupportedProtocol(server: 2, supported: 1))
            XCTAssertTrue(error.localizedDescription.contains("Update the menu bar app"))
        }
    }

    func testRefusesAServerThatDoesNotSayHelloFirst() async throws {
        let server = try startServer { $0.firstLine = #"{"event":"status","data":{}}"# }
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        do {
            try await client.connect()
            XCTFail("expected protocolViolation")
        } catch ControlError.protocolViolation {}
    }

    func testMissingSocketIsCannotConnect() async {
        let client = ControlClient(socketPath: temporarySocketPath())
        do {
            try await client.connect(timeout: .seconds(2))
            XCTFail("expected cannotConnect")
        } catch ControlError.cannotConnect {
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testUnansweredRequestTimesOut() async throws {
        let server = try startServer { $0.unansweredOps = ["logs.tail"] }
        defer { server.stop() }
        let client = try await connectedClient(server)
        defer { client.close() }
        do {
            _ = try await client.request("logs.tail", timeout: .milliseconds(200))
            XCTFail("expected timeout")
        } catch ControlError.timedOut(let operation) {
            XCTAssertEqual(operation, "logs.tail")
        }
        // The connection is still usable afterwards.
        _ = try await client.status()
    }

    func testServerClosingFinishesEventsAndFailsPendingRequests() async throws {
        let server = try startServer { $0.unansweredOps = ["status"] }
        defer { server.stop() }
        let client = try await connectedClient(server)

        async let pending: JSONValue = client.request("status")
        try await Task.sleep(for: .milliseconds(100))
        server.dropConnections()
        do {
            _ = try await pending
            XCTFail("expected connectionClosed")
        } catch ControlError.connectionClosed {}

        var count = 0
        for await _ in client.events { count += 1 }  // returns because the stream finished
        XCTAssertGreaterThanOrEqual(count, 0)
        do {
            _ = try await client.status()
            XCTFail("expected notConnected")
        } catch ControlError.notConnected {}
    }

    func testSameUserPassesThePeerCheck() async throws {
        let server = try startServer()
        defer { server.stop() }
        _ = try await connectedClient(server)
        XCTAssertEqual(server.acceptedConnections, 1)
    }
}
