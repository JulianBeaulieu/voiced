import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

final class EnvironmentTests: XCTestCase {
    func testBackoffDoublesToItsCapAndResets() {
        var backoff = Backoff(initial: .milliseconds(500), maximum: .seconds(3))
        XCTAssertEqual(
            (0..<5).map { _ in backoff.next() },
            [.milliseconds(500), .seconds(1), .seconds(2), .seconds(3), .seconds(3)])
        backoff.reset()
        XCTAssertEqual(backoff.next(), .milliseconds(500))
    }

    func testSocketLocationOrder() throws {
        let home = URL(fileURLWithPath: "/Users/someone")
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "voiced-menubar-tests-\(UUID().uuidString)"))
        XCTAssertEqual(
            SocketLocation.resolve(environment: [:], defaults: defaults, home: home),
            "/Users/someone/.voiced/control.sock")
        defaults.set("~/elsewhere.sock", forKey: SocketLocation.defaultsKey)
        XCTAssertEqual(
            SocketLocation.resolve(environment: [:], defaults: defaults, home: home),
            "/Users/someone/elsewhere.sock")
        XCTAssertEqual(
            SocketLocation.resolve(
                environment: ["VOICED_CONTROL_SOCKET": "/tmp/x.sock"], defaults: defaults, home: home),
            "/tmp/x.sock")
    }

    func testInstallProbeFindsThisUsersWatchdogWhateverTheLabel() throws {
        let empty = try launchDaemonsDirectory()
        XCTAssertFalse(InstallProbe(launchDaemonsDirectory: empty, userName: "alex").isInstalled())

        let standard = try launchDaemonsDirectory(watchdogUser: "alex")
        XCTAssertTrue(InstallProbe(launchDaemonsDirectory: standard, userName: "alex").isInstalled())
        XCTAssertFalse(
            InstallProbe(launchDaemonsDirectory: standard, userName: "sam").isInstalled(),
            "another user's voiced is not this user's install")

        let custom = try launchDaemonsDirectory(watchdogUser: "alex", label: "computer.speech")
        XCTAssertTrue(InstallProbe(launchDaemonsDirectory: custom, userName: "alex").isInstalled())
    }

    func testInstallProbeIgnoresOtherWatchdogs() throws {
        let directory = try launchDaemonsDirectory()
        try writePlist(
            ["ProgramArguments": ["/usr/local/bin/other", "--watch"], "UserName": "alex"],
            to: directory.appendingPathComponent("com.other.watchdog.plist"))
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("broken.watchdog.plist").path, contents: Data("x".utf8))
        XCTAssertFalse(InstallProbe(launchDaemonsDirectory: directory, userName: "alex").isInstalled())
    }

    func testWarningsAreDescribed() {
        XCTAssertEqual(SettingsWarning.describe("lan_without_api_key"), SettingsWarning.lanWithoutKey)
        XCTAssertTrue(SettingsWarning.describe("env_override:tts.voice").contains("overrides tts.voice"))
        XCTAssertEqual(SettingsWarning.describe("something_new"), "voiced warned: something_new.")
    }
}

final class OpenPolicyTests: XCTestCase {
    private func action(_ target: OpenTarget, _ location: String, local: Set<String> = []) -> OpenAction? {
        OpenPolicy.action(for: target, location: location, localHosts: local)
    }

    func testStatusPageMustBeServedByThisMac() {
        XCTAssertEqual(
            action(.statusPage, "http://127.0.0.1:8880/"), .webPage(URL(string: "http://127.0.0.1:8880/")!))
        XCTAssertNotNil(action(.statusPage, "http://localhost:8880/"))
        XCTAssertNotNil(action(.statusPage, "http://[::1]:8880/"))
        XCTAssertNotNil(action(.statusPage, "http://10.0.0.5:8880/", local: ["10.0.0.5"]))
        XCTAssertNil(action(.statusPage, "http://10.0.0.6:8880/", local: ["10.0.0.5"]))
        XCTAssertNil(action(.statusPage, "https://example.com/"))
        XCTAssertNil(action(.statusPage, "file:///Applications/Calculator.app"))
        XCTAssertNil(action(.statusPage, "/Users"))
    }

    func testInterfaceAddressesIncludeLoopback() {
        XCTAssertTrue(OpenPolicy.interfaceAddresses().contains("127.0.0.1"))
    }

    func testLogsMustBeAPlainFolder() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let bundle = folder.appendingPathComponent("Thing.app")
        try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("stt.log")
        FileManager.default.createFile(atPath: file.path, contents: Data())

        XCTAssertEqual(action(.logsDirectory, folder.path), .folder(folder.resolvingSymlinksInPath()))
        XCTAssertNil(action(.logsDirectory, bundle.path))
        XCTAssertNil(action(.logsDirectory, file.path))
        XCTAssertNil(action(.logsDirectory, folder.appendingPathComponent("missing").path))
        XCTAssertNil(action(.logsDirectory, "Library/Logs"))
    }

    func testConfigMustBeARegularFile() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let config = folder.appendingPathComponent("config.toml")
        FileManager.default.createFile(atPath: config.path, contents: Data())
        let script = folder.appendingPathComponent("run.command")
        FileManager.default.createFile(atPath: script.path, contents: Data("echo hi".utf8))
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("x.app"), withIntermediateDirectories: true)

        XCTAssertEqual(action(.configFile, config.path), .textFile(config.resolvingSymlinksInPath()))
        XCTAssertEqual(
            action(.configFile, config.absoluteString), .textFile(config.resolvingSymlinksInPath()))
        XCTAssertNil(action(.configFile, folder.path))
        XCTAssertNil(action(.configFile, folder.appendingPathComponent("x.app").path))
        // A script is a regular file too; it is opened in the text editor, never run.
        XCTAssertEqual(action(.configFile, script.path), .textFile(script.resolvingSymlinksInPath()))
        XCTAssertNil(action(.configFile, folder.appendingPathComponent("missing.toml").path))
    }
}
