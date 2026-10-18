import AppKit
import TestSupport
import XCTest

@testable import VoicedMenuBarUI

/// The privileged script as compiled into the app, run the way the app runs it but in the
/// script's test mode (a scratch destination, no launchctl, no firewall), and the real
/// process runner. Nothing here prompts or touches /Library.
@MainActor
final class PrivilegedTests: XCTestCase {
    private var scratch: URL!

    override func setUp() async throws {
        scratch = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: scratch.appendingPathComponent("dest"), withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    /// `env -i ... bash --noprofile --norc -c "$SCRIPT" voiced-privileged MODE USER`, as the
    /// AppleScript runs it, plus the test-mode destination.
    private func runScript(_ mode: String, _ user: String) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [
            "-i", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "VOICED_TEST_DEST=\(scratch.appendingPathComponent("dest").path)",
            "/bin/bash", "--noprofile", "--norc", "-c", PrivilegedScript.text, "voiced-privileged", mode, user,
        ]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    private var installed: [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: scratch.appendingPathComponent("dest").path)) ?? []).sorted()
    }

    func testTheCompiledScriptRendersTheThreePlists() throws {
        let install = try runScript("install", NSUserName())
        XCTAssertEqual(install.status, 0, install.output)
        XCTAssertEqual(installed, ["stt", "tts", "watchdog"].map { "io.github.julianbeaulieu.voiced.\($0).plist" })
        let data = try Data(contentsOf: scratch.appendingPathComponent("dest/io.github.julianbeaulieu.voiced.watchdog.plist"))
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        XCTAssertEqual(plist["ProgramArguments"] as? [String], ["\(home)/.voiced/bin/voiced", "watchdog"])
        XCTAssertEqual(plist["UserName"] as? String, NSUserName())
        XCTAssertEqual(plist["AssociatedBundleIdentifiers"] as? [String], ["io.github.julianbeaulieu.voiced.menubar"])
        XCTAssertNil(plist["StandardOutPath"])
        XCTAssertEqual(try runScript("check", NSUserName()).status, 0)
    }

    func testTheCompiledScriptRefusesRootAndWritesNothing() throws {
        let refused = try runScript("install", "root")
        XCTAssertEqual(refused.status, 1)
        XCTAssertTrue(refused.output.contains("refused: root is not a regular account"), refused.output)
        XCTAssertEqual(installed, [])
    }

    func testTheAppleScriptCompiles() throws {
        var error: NSDictionary?
        let script = try XCTUnwrap(NSAppleScript(source: PrivilegedScript.appleScript))
        XCTAssertTrue(script.compileAndReturnError(&error), "\(String(describing: error))")
    }

    func testTheScriptTextIsTheWholeScript() {
        XCTAssertTrue(PrivilegedScript.text.hasPrefix("#!/bin/bash\n"))
        XCTAssertTrue(PrivilegedScript.text.hasSuffix("esac"))
    }

    func testFailedLabelsNameTheirService() {
        XCTAssertEqual(Installer.failedService("failed io.github.julianbeaulieu.voiced.stt"), "speech to text")
        XCTAssertEqual(Installer.failedService("failed io.github.julianbeaulieu.voiced.watchdog"), "the watchdog")
        XCTAssertNil(Installer.failedService("installed io.github.julianbeaulieu.voiced.stt"))
        XCTAssertEqual(Installer.lines("a\rb\nc"), ["a", "b", "c"])
    }

    // MARK: - SystemInstallRunner

    private func collect(_ stream: AsyncThrowingStream<ProcessOutput, Error>) async throws -> [ProcessOutput] {
        var outputs: [ProcessOutput] = []
        for try await output in stream { outputs.append(output) }
        return outputs
    }

    func testTheRunnerReportsBothStreamsThenTheExitStatus() async throws {
        let runner = SystemInstallRunner()
        let outputs = try await collect(runner.run(InstallCommand(
            "/bin/sh", ["-c", "echo one; echo \"$VOICED_X\" >&2; echo two; exit 3"], environment: ["VOICED_X": "set"])))
        XCTAssertEqual(outputs.filter { if case .stdout = $0 { true } else { false } }, [.stdout("one"), .stdout("two")])
        XCTAssertTrue(outputs.contains(.stderr("set")))
        XCTAssertEqual(outputs.last, .exit(3))
    }

    func testTheRunnerThrowsWhenItCannotStart() async {
        let runner = SystemInstallRunner()
        do {
            _ = try await collect(runner.run(InstallCommand("/nonexistent/voiced", [])))
            XCTFail("started")
        } catch {}
    }

    func testCancellingTerminatesTheProcess() async throws {
        let runner = SystemInstallRunner()
        let marker = scratch.appendingPathComponent("pid")
        let task = Task {
            try await self.collect(runner.run(InstallCommand("/bin/sh", ["-c", "echo $$ > '\(marker.path)'; exec sleep 30"])))
        }
        let started = await eventually { FileManager.default.fileExists(atPath: marker.path) }
        XCTAssertTrue(started)
        let pid = try XCTUnwrap(Int32(String(decoding: Data(contentsOf: marker), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)))
        task.cancel()
        let gone = await eventually { kill(pid, 0) != 0 }
        XCTAssertTrue(gone)
    }

    func testTerminateAllKillsWhatIgnoresSIGTERM() async throws {
        let runner = SystemInstallRunner(terminationGrace: 0.3)
        let marker = scratch.appendingPathComponent("pid")
        let stream = runner.run(InstallCommand("/bin/sh", ["-c", "trap '' TERM; echo $$ > '\(marker.path)'; while :; do sleep 0.1; done"]))
        let task = Task { try await self.collect(stream) }
        let started = await eventually { FileManager.default.fileExists(atPath: marker.path) }
        XCTAssertTrue(started)
        let began = ContinuousClock.now
        runner.terminateAll()
        XCTAssertGreaterThanOrEqual(ContinuousClock.now - began, .milliseconds(250))
        let outputs = try await task.value
        XCTAssertEqual(outputs.last, .exit(SIGKILL))
    }
}
