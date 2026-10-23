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

    func testInstallProbeFindsTheWatchdogAgentWhateverTheLabel() throws {
        let none = try launchDaemonsDirectory()
        let empty = try launchAgentsDirectory()
        XCTAssertFalse(InstallProbe(launchAgentsDirectory: empty, launchDaemonsDirectory: none).isInstalled())
        let standard = try launchAgentsDirectory(watchdog: true)
        XCTAssertTrue(InstallProbe(launchAgentsDirectory: standard, launchDaemonsDirectory: none).isInstalled())
        let custom = try launchAgentsDirectory(watchdog: true, label: "computer.speech")
        XCTAssertTrue(InstallProbe(launchAgentsDirectory: custom, launchDaemonsDirectory: none).isInstalled())
    }

    /// An install from before ADR 0035: this user's watchdog LaunchDaemon counts as installed,
    /// and as one to migrate; another account's is neither.
    func testInstallProbeFindsThisUsersOldWatchdogDaemon() throws {
        let agents = try launchAgentsDirectory()
        let daemons = try launchDaemonsDirectory(watchdogUser: "alex")
        let alex = InstallProbe(launchAgentsDirectory: agents, launchDaemonsDirectory: daemons, userName: "alex")
        XCTAssertTrue(alex.isInstalled())
        XCTAssertTrue(alex.hasLegacyDaemons())
        let sam = InstallProbe(launchAgentsDirectory: agents, launchDaemonsDirectory: daemons, userName: "sam")
        XCTAssertFalse(sam.isInstalled(), "another user's voiced is not this user's install")
        XCTAssertFalse(sam.hasLegacyDaemons())
        XCTAssertEqual(sam.legacyDaemonUsers(), ["alex"])
    }

    /// The three default labels, as `voiced.launchd.control.LEGACY_LABELS`: any of them left
    /// is an old install, not only the watchdog; another prefix never had daemons.
    func testInstallProbeChecksEveryDefaultLabel() throws {
        let agents = try launchAgentsDirectory()
        let daemons = try launchDaemonsDirectory()
        try writePlist(["UserName": "alex"], to: daemons.appendingPathComponent("io.github.julianbeaulieu.voiced.tts.plist"))
        try writePlist(["UserName": "sam"], to: daemons.appendingPathComponent("org.example.voiced.stt.plist"))
        let alex = InstallProbe(launchAgentsDirectory: agents, launchDaemonsDirectory: daemons, userName: "alex")
        XCTAssertTrue(alex.hasLegacyDaemons())
        XCTAssertEqual(alex.legacyDaemonUsers(), ["alex"])
    }

    func testInstallProbeIgnoresOtherWatchdogs() throws {
        let directory = try launchAgentsDirectory()
        try writePlist(
            ["ProgramArguments": ["/usr/local/bin/other", "--watch"]],
            to: directory.appendingPathComponent("com.other.watchdog.plist"))
        // Another vendor's watchdog, ending in "watchdog" too.
        try writePlist(
            ["ProgramArguments": ["/opt/vendor/bin/agent", "watchdog"]],
            to: directory.appendingPathComponent("com.vendor.watchdog.plist"))
        try writePlist(
            ["ProgramArguments": ["/opt/voiced", "--verbose", "watchdog"]],
            to: directory.appendingPathComponent("com.extra.watchdog.plist"))
        FileManager.default.createFile(
            atPath: directory.appendingPathComponent("broken.watchdog.plist").path, contents: Data("x".utf8))
        XCTAssertFalse(
            InstallProbe(launchAgentsDirectory: directory, launchDaemonsDirectory: try launchDaemonsDirectory()).isInstalled())
    }

    func testErrorTextIsForPeople() {
        let text = ControlError.cannotConnect(.closedBeforeHello).localizedDescription
        XCTAssertFalse(text.contains("closedBeforeHello"))
        XCTAssertTrue(text.contains("closed the connection before saying hello"))
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
        XCTAssertNotNil(action(.statusPage, "http://127.1.2.3:8880/"))
        XCTAssertNil(action(.statusPage, "http://127.evil.example/"))
        XCTAssertNil(action(.statusPage, "http://1270.0.0.1/"))
        XCTAssertNil(action(.statusPage, "http://[::2]/"))
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
