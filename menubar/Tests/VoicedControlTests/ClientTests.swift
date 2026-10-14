import Darwin
import FakeControlServerKit
import Foundation
import TestSupport
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

    func testHelloWithoutVersionIsAccepted() async throws {
        let server = try startServer { $0.firstLine = #"{"event":"hello","data":{"protocol":1}}"# }
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        let hello = try await client.connect()
        XCTAssertNil(hello.version)
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
        XCTAssertEqual(engines.ttsEngine(named: "kokoro")?.voiceName("bm_george"), "George (British English, male)")
        XCTAssertEqual(engines.ttsEngine(named: "soprano")?.voiceName("default"), "Default")
        XCTAssertEqual(engines.ttsEngine(named: "fake")?.voiceName("fake"), "fake")

        let target = try await client.openTarget(.statusPage)
        XCTAssertEqual(target, "http://127.0.0.1:8880/")
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
            _ = try await client.setSettings(["volume": 11])
            XCTFail("expected invalid_setting")
        } catch ControlError.remote(let error) {
            XCTAssertEqual(error.field, "volume")
        }
        do {
            _ = try await client.request("no.such.op")
            XCTFail("expected unknown_op")
        } catch ControlError.remote(let error) {
            XCTAssertEqual(error.code, "unknown_op")
        }
    }

    func testSettingsSetReportsRestartsAndWarnings() async throws {
        let server = try startServer { $0.environmentOverrides = [.ttsVoice] }
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

        let voice = try await client.setSettings(["tts.voice": "bf_emma"])
        XCTAssertEqual(voice.warnings, ["env_override:tts.voice"])
        XCTAssertEqual(voice.restartRequired, [.tts])
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
        await assertConnect(server.socketPath, throws: .protocolViolation("the first message was not hello"))
    }

    func testMalformedHelloIsNamed() async throws {
        let server = try startServer { $0.firstLine = #"{"event":"hello","data":{"protocol":"one"}}"# }
        defer { server.stop() }
        await assertConnect(server.socketPath, throws: .protocolViolation("malformed hello"))
    }

    // MARK: - Why a connection fails

    private func assertConnect(
        _ path: String, throws expected: ControlError, file: StaticString = #filePath, line: UInt = #line
    ) async {
        let client = ControlClient(socketPath: path)
        do {
            try await client.connect(timeout: .milliseconds(500))
            XCTFail("expected \(expected)", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? ControlError, expected, file: file, line: line)
        }
    }

    func testMissingSocket() async {
        await assertConnect(temporarySocketPath(), throws: .cannotConnect(.noSocket))
    }

    func testStaleSocketFile() async throws {
        let path = temporarySocketPath()
        let server = FakeControlServer(socketPath: path)
        try server.start()
        server.stop()
        // stop() removes the file; leave one behind the way a killed watchdog does.
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: path.utf8) }
        _ = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        close(fd)
        defer { unlink(path) }
        await assertConnect(path, throws: .cannotConnect(.notListening))
    }

    func testRegularFileAtThePath() async throws {
        let path = temporarySocketPath()
        FileManager.default.createFile(atPath: path, contents: Data())
        defer { unlink(path) }
        await assertConnect(path, throws: .cannotConnect(.notASocket))
    }

    func testSocketWithoutPermission() async throws {
        let server = try startServer()
        defer { server.stop() }
        chmod(server.socketPath, 0)
        await assertConnect(server.socketPath, throws: .cannotConnect(.permissionDenied))
    }

    func testPathTooLongFailsInsteadOfTrapping() async {
        let path = "/tmp/" + String(repeating: "x", count: 120) + ".sock"
        await assertConnect(path, throws: .cannotConnect(.pathTooLong))
    }

    func testListenerThatNeverSaysHello() async throws {
        let server = try startServer { $0.firstLine = "" }
        defer { server.stop() }
        await assertConnect(server.socketPath, throws: .cannotConnect(.noHello))
    }

    func testServerClosingBeforeHello() async throws {
        // What the peer check does to another user; the other-user path itself cannot be
        // exercised without a second account.
        let server = try startServer { $0.closeOnAccept = true }
        defer { server.stop() }
        await assertConnect(server.socketPath, throws: .cannotConnect(.closedBeforeHello))
    }

    // MARK: - Timeouts, closing, cancellation

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
        let sent = await eventually { server.requests.count == 1 }
        XCTAssertTrue(sent)
        server.dropConnections()
        do {
            _ = try await pending
            XCTFail("expected connectionClosed")
        } catch ControlError.connectionClosed {}

        // The stream finishes: iterating it returns instead of hanging.
        var iterator = client.events.makeAsyncIterator()
        let next = await iterator.next()
        XCTAssertNil(next)
        do {
            _ = try await client.status()
            XCTFail("expected notConnected")
        } catch ControlError.notConnected {}
    }

    func testCancellingARequestClosesTheConnection() async throws {
        let server = try startServer { $0.unansweredOps = ["status"] }
        defer { server.stop() }
        let client = try await connectedClient(server)
        let task = Task { try await client.request("status") }
        let sent = await eventually { server.requests.count == 1 }
        XCTAssertTrue(sent)
        task.cancel()
        do {
            _ = try await task.value
            XCTFail("expected the request to end")
        } catch ControlError.connectionClosed {}
    }

    func testCancellingConnectEndsIt() async throws {
        let server = try startServer { $0.firstLine = "" }
        defer { server.stop() }
        let client = ControlClient(socketPath: server.socketPath)
        let task = Task { try await client.connect(timeout: .seconds(30)) }
        let accepted = await eventually { server.acceptedConnections == 1 }
        XCTAssertTrue(accepted)
        task.cancel()
        do {
            _ = try await task.value
            XCTFail("expected connect to end")
        } catch ControlError.connectionClosed {}
    }

    func testSameUserPassesThePeerCheck() async throws {
        let server = try startServer()
        defer { server.stop() }
        _ = try await connectedClient(server)
        XCTAssertEqual(server.acceptedConnections, 1)
    }

    func testFakeServerWillNotReplaceALiveSocketOrAFile() throws {
        let live = try startServer()
        defer { live.stop() }
        XCTAssertThrowsError(try FakeControlServer(socketPath: live.socketPath).start())

        let path = temporarySocketPath()
        FileManager.default.createFile(atPath: path, contents: Data("keep".utf8))
        defer { unlink(path) }
        XCTAssertThrowsError(try FakeControlServer(socketPath: path).start())
        XCTAssertEqual(FileManager.default.contents(atPath: path), Data("keep".utf8))
    }
}
