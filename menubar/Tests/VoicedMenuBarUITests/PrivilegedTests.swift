import Foundation
import TestSupport
import XCTest

@testable import VoicedMenuBarUI

/// The one administrator prompt left (the removal of an earlier release's LaunchDaemons), how
/// the app runs it, and the real process runner. Nothing here prompts or touches /Library:
/// the AppleScript is only compiled.
@MainActor
final class PrivilegedTests: XCTestCase {
    private var scratch: URL!

    override func setUp() async throws {
        scratch = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    private func collect(_ stream: AsyncThrowingStream<ProcessOutput, Error>) async throws -> [ProcessOutput] {
        var outputs: [ProcessOutput] = []
        for try await output in stream { outputs.append(output) }
        return outputs
    }

    private func stdout(_ outputs: [ProcessOutput]) -> [String] {
        outputs.compactMap { if case .stdout(let line) = $0 { line } else { nil } }
    }

    // MARK: - How the app runs it

    func testThePromptGetsNothingOfTheAppsEnvironment() {
        let command = LegacyDaemons.command
        XCTAssertEqual(command.executable, "/usr/bin/osascript")
        XCTAssertFalse(command.inheritsEnvironment)
        XCTAssertEqual(command.environment, ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        // The commands and the prompt are arguments, never part of the AppleScript source.
        XCTAssertEqual(command.arguments, ["-e", LegacyDaemons.appleScript, LegacyDaemons.removal, LegacyDaemons.prompt])
        XCTAssertTrue(LegacyDaemons.appleScript.contains("set {command_text, prompt_text} to argv"))
        XCTAssertTrue(
            LegacyDaemons.appleScript.contains(
                "do shell script command_text with prompt prompt_text with administrator privileges"))
    }

    func testTheAppleScriptCompiles() async throws {
        // osacompile only compiles; nothing runs and nothing prompts.
        let outputs = try await collect(SystemInstallRunner().run(InstallCommand(
            "/usr/bin/osacompile", ["-e", LegacyDaemons.appleScript, "-o", scratch.appendingPathComponent("p.scpt").path])))
        XCTAssertEqual(outputs.last, .exit(0), "\(outputs)")
    }

    /// The attack ADR 0018's review found: `SHELLOPTS=xtrace` and a `PS4` with a command
    /// substitution in the app's environment run that command in the first shell that starts.
    func testAPoisonedEnvironmentDoesNotReachACommandThatInheritsNothing() async throws {
        let marker = scratch.appendingPathComponent("ran")
        setenv("SHELLOPTS", "xtrace", 1)
        setenv("PS4", "$(/usr/bin/touch '\(marker.path)')", 1)
        defer {
            unsetenv("SHELLOPTS")
            unsetenv("PS4")
        }
        let runner = SystemInstallRunner()
        let exact = try await collect(runner.run(InstallCommand(
            "/usr/bin/env", [], environment: LegacyDaemons.environment, inheritsEnvironment: false)))
        XCTAssertEqual(stdout(exact), ["PATH=/usr/bin:/bin:/usr/sbin:/sbin"])
        _ = try await collect(runner.run(InstallCommand(
            "/bin/sh", ["-c", "/usr/bin/true"], environment: LegacyDaemons.environment, inheritsEnvironment: false)))
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
        // The same command inheriting the app's environment does run it: the test can fail.
        _ = try await collect(runner.run(InstallCommand("/bin/sh", ["-c", "/usr/bin/env > /dev/null"])))
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
    }

    // MARK: - Reading the result

    func testOsascriptOutcomes() {
        XCTAssertEqual(LegacyDaemons.outcome(status: 0, stdout: [], stderr: []), .finished(status: 0, output: ""))
        XCTAssertEqual(
            LegacyDaemons.outcome(status: 1, stdout: [], stderr: ["0:342: execution error: User canceled. (-128)"]),
            .cancelled)
        XCTAssertEqual(
            LegacyDaemons.outcome(status: 1, stdout: [], stderr: ["0:342: execution error: rm: denied (1)"]),
            .finished(status: 1, output: "rm: denied"))
        XCTAssertEqual(
            LegacyDaemons.outcome(status: 1, stdout: [], stderr: ["something else"]),
            .finished(status: 1, output: "something else"))
    }

    func testResults() {
        typealias R = LegacyDaemons.Result
        XCTAssertEqual(LegacyDaemons.result(of: .finished(status: 0, output: "")), R.done)
        XCTAssertEqual(LegacyDaemons.result(of: .cancelled), R.cancelled)
        XCTAssertEqual(
            LegacyDaemons.result(of: .finished(status: 1, output: "rm: denied")),
            R.failed(cause: "The old background services could not be removed (status 1).", output: ["rm: denied"]))
        guard case .failed(let cause, []) = LegacyDaemons.result(of: .damaged("a sealed resource is missing")) else {
            return XCTFail("a damaged bundle is a failure")
        }
        XCTAssertTrue(cause.hasPrefix("This copy of voiced is damaged"))
    }

    // MARK: - The bundle check

    func testTheBundleCheckPassesASignedAppAndCatchesAModifiedOne() throws {
        let original = URL(fileURLWithPath: "/System/Applications/Calculator.app")
        XCTAssertNil(BundleCheck.problem(at: original))
        let copy = scratch.appendingPathComponent("Calculator.app")
        try FileManager.default.copyItem(at: original, to: copy)
        try Data("tampered".utf8).write(to: copy.appendingPathComponent("Contents/Resources/voiced-test"))
        XCTAssertNotNil(BundleCheck.problem(at: copy))
        // `swift run` is not a bundle and is not checked.
        XCTAssertNil(BundleCheck.problem(at: scratch))
    }

    // MARK: - SystemInstallRunner

    func testTheRunnerReportsBothStreamsThenTheExitStatus() async throws {
        let outputs = try await collect(SystemInstallRunner().run(InstallCommand(
            "/bin/sh", ["-c", "echo one; echo \"$VOICED_X\" >&2; echo two; printf last; exit 3"],
            environment: ["VOICED_X": "set"])))
        XCTAssertEqual(stdout(outputs), ["one", "two", "last"])
        XCTAssertTrue(outputs.contains(.stderr("set")))
        XCTAssertEqual(outputs.last, .exit(3))
    }

    func testCommandsStartInTheRootDirectory() async throws {
        // Never in the app's working directory: the privileged run's root shell starts there.
        let outputs = try await collect(SystemInstallRunner().run(InstallCommand("/bin/pwd", [])))
        XCTAssertEqual(stdout(outputs), ["/"])
    }

    func testTheRunnerThrowsWhenItCannotStart() async {
        do {
            _ = try await collect(SystemInstallRunner().run(InstallCommand("/nonexistent/voiced", [])))
            XCTFail("started")
        } catch {}
    }

    private func pid(in file: URL) async throws -> pid_t {
        let written = await eventually { ((try? String(contentsOf: file, encoding: .utf8)) ?? "").hasSuffix("\n") }
        XCTAssertTrue(written)
        return try XCTUnwrap(pid_t(String(contentsOf: file, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)))
    }

    func testCancellingTerminatesTheWholeProcessGroup() async throws {
        let runner = SystemInstallRunner()
        let marker = scratch.appendingPathComponent("grandchild")
        // The child starts a grandchild that outlives it unless the group is signalled.
        let task = Task {
            try await self.collect(runner.run(InstallCommand(
                "/bin/sh", ["-c", "/bin/sleep 30 & echo $! > '\(marker.path)'; wait"])))
        }
        let grandchild = try await pid(in: marker)
        task.cancel()
        let gone = await eventually { kill(grandchild, 0) != 0 }
        XCTAssertTrue(gone)
    }

    func testCancellingKillsWhatIgnoresSIGTERM() async throws {
        // A grace far above the 150 ms check below, which a loaded runner may oversleep.
        let runner = SystemInstallRunner(terminationGrace: 2)
        let marker = scratch.appendingPathComponent("pid")
        let task = Task {
            try await self.collect(runner.run(InstallCommand(
                "/bin/sh", ["-c", "trap '' TERM; echo $$ > '\(marker.path)'; while :; do /bin/sleep 0.1; done"])))
        }
        let child = try await pid(in: marker)
        // Without the SIGKILL the child would outlive the test; make sure it does not.
        defer { if kill(child, 0) == 0 { kill(child, SIGKILL) } }
        task.cancel()
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(kill(child, 0), 0, "SIGTERM is ignored, so it still runs within the grace")
        let gone = await eventually { kill(child, 0) != 0 }
        XCTAssertTrue(gone)
        let ended = await finished(task, within: .seconds(3))
        XCTAssertNotNil(ended, "the stream ends once the group is killed")
    }

    func testTerminateAllKillsWhatIgnoresSIGTERM() async throws {
        let runner = SystemInstallRunner(terminationGrace: 0.3)
        let marker = scratch.appendingPathComponent("pid")
        let stream = runner.run(InstallCommand(
            "/bin/sh", ["-c", "trap '' TERM; echo $$ > '\(marker.path)'; while :; do /bin/sleep 0.1; done"]))
        let task = Task { try await self.collect(stream) }
        let child = try await pid(in: marker)
        defer { if kill(child, 0) == 0 { kill(child, SIGKILL) } }
        let began = ContinuousClock.now
        runner.terminateAll()
        XCTAssertGreaterThanOrEqual(ContinuousClock.now - began, .milliseconds(250))
        let outputs = await finished(task, within: .seconds(3))
        XCTAssertEqual(outputs?.last, .exit(128 + SIGKILL))
    }

    func testOutputLines() {
        var lines = OutputLines()
        XCTAssertEqual(lines.append(Data("a\r\nb".utf8)), ["a"])
        XCTAssertEqual(lines.append(Data("c\n\nd".utf8)), ["bc"])
        XCTAssertEqual(lines.finish(), "d")
        XCTAssertNil(lines.finish())
        // A line too long is passed on in pieces, and the lines before it are kept.
        let long = Data(repeating: 0x61, count: OutputLines.maximum + 1)
        let pieces = lines.append(Data("x\n".utf8) + long)
        XCTAssertEqual(pieces.first, "x")
        XCTAssertEqual(pieces.count, 2)
    }
}
