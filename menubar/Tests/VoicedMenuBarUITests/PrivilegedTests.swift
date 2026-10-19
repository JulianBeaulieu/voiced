import Foundation
import TestSupport
import XCTest

@testable import VoicedMenuBarUI

/// The privileged script as compiled into the app, how the app runs it, and the real process
/// runner. Nothing here prompts or touches /Library: the script runs in its test mode (a
/// scratch destination, no launchctl, no firewall) and the AppleScript is only compiled.
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

    private func collect(_ stream: AsyncThrowingStream<ProcessOutput, Error>) async throws -> [ProcessOutput] {
        var outputs: [ProcessOutput] = []
        for try await output in stream { outputs.append(output) }
        return outputs
    }

    private func stdout(_ outputs: [ProcessOutput]) -> [String] {
        outputs.compactMap { if case .stdout(let line) = $0 { line } else { nil } }
    }

    // MARK: - The script

    /// The script as the root shell runs it: `env -i ... bash --noprofile --norc -c`, plus
    /// the test-mode destination.
    private func runScript(_ mode: String, _ user: String) async throws -> (status: Int32, output: [String]) {
        let command = InstallCommand(
            "/usr/bin/env",
            [
                "-i", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "VOICED_TEST_DEST=\(scratch.appendingPathComponent("dest").path)",
                "/bin/bash", "--noprofile", "--norc", "-c", PrivilegedScript.text, "voiced-privileged", mode, user,
            ])
        let outputs = try await collect(SystemInstallRunner().run(command))
        guard case .exit(let status) = outputs.last else { throw CancellationError() }
        let lines = outputs.compactMap { output -> String? in
            switch output {
            case .stdout(let line), .stderr(let line): line
            case .exit: nil
            }
        }
        return (status, lines)
    }

    private var installed: [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: scratch.appendingPathComponent("dest").path)) ?? []).sorted()
    }

    /// One root script for the app and the CLI: the bytes compiled in are the file the voiced
    /// package ships, which `voiced install` prints for sudo.
    func testTheCompiledScriptIsThePackagedOne() throws {
        let packaged = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("src/voiced/launchd/voiced-privileged.sh")
        let compiled = Data(PackageResources.voiced_privileged_sh)
        XCTAssert(compiled == (try Data(contentsOf: packaged)), "the compiled-in script differs from \(packaged.path)")
    }

    func testTheCompiledScriptRendersTheThreePlists() async throws {
        let install = try await runScript("install", NSUserName())
        XCTAssertEqual(install.status, 0, install.output.joined(separator: "\n"))
        XCTAssertEqual(installed, ["stt", "tts", "watchdog"].map { "io.github.julianbeaulieu.voiced.\($0).plist" })
        let data = try Data(contentsOf: scratch.appendingPathComponent("dest/io.github.julianbeaulieu.voiced.watchdog.plist"))
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        XCTAssertEqual(plist["ProgramArguments"] as? [String], ["\(home)/.voiced/bin/voiced", "watchdog"])
        XCTAssertEqual(plist["UserName"] as? String, NSUserName())
        XCTAssertEqual(plist["AssociatedBundleIdentifiers"] as? [String], ["io.github.julianbeaulieu.voiced.menubar"])
        XCTAssertNil(plist["StandardOutPath"])
        let check = try await runScript("check", NSUserName())
        XCTAssertEqual(check.status, 0)
    }

    /// What `check` prints for a changed or missing plist reads as a difference, so an update
    /// or repair asks for the administrator.
    func testTheScriptsCheckOutputReadsAsADifference() async throws {
        _ = try await runScript("install", NSUserName())
        var check = try await runScript("check", NSUserName())
        XCTAssertEqual(PrivilegedScript.check(status: check.status, lines: check.output), .upToDate)
        let tts = scratch.appendingPathComponent("dest/io.github.julianbeaulieu.voiced.tts.plist")
        try FileManager.default.removeItem(at: tts)
        check = try await runScript("check", NSUserName())
        XCTAssertEqual(
            PrivilegedScript.check(status: check.status, lines: check.output),
            .differs("differs io.github.julianbeaulieu.voiced.tts"))
        check = try await runScript("check", "root")
        XCTAssertEqual(
            PrivilegedScript.check(status: check.status, lines: check.output),
            .failed("Refused: root is not a regular account (uid 0)."))
    }

    func testCheckResults() {
        XCTAssertEqual(PrivilegedScript.check(status: 0, lines: ["up to date"]), .upToDate)
        XCTAssertEqual(
            PrivilegedScript.check(status: 1, lines: ["not loaded io.github.julianbeaulieu.voiced.stt"]),
            .differs("not loaded io.github.julianbeaulieu.voiced.stt"))
        XCTAssertEqual(PrivilegedScript.check(status: 1, lines: []), .failed("The check exited with status 1."))
        XCTAssertEqual(PrivilegedScript.check(status: 2, lines: ["differs x"]), .failed("The check exited with status 2."))
    }

    func testTheCheckRunsAsTheUserWithNothingOfTheAppsEnvironment() {
        let command = PrivilegedScript.checkCommand(user: "someone")
        XCTAssertEqual(command.executable, "/bin/bash")
        XCTAssertFalse(command.inheritsEnvironment)
        XCTAssertEqual(command.environment, ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        XCTAssertEqual(
            command.arguments, ["--noprofile", "--norc", "-c", PrivilegedScript.text, "voiced-privileged", "check", "someone"])
    }

    func testTheCompiledScriptRefusesRootAndWritesNothing() async throws {
        let refused = try await runScript("install", "root")
        XCTAssertEqual(refused.status, 1)
        XCTAssertTrue(refused.output.contains("refused: root is not a regular account (uid 0)"), "\(refused.output)")
        XCTAssertEqual(installed, [])
    }

    // MARK: - How the app runs it

    func testThePrivilegedCommandGetsNothingOfTheAppsEnvironment() {
        let command = PrivilegedScript.command(.install, user: "someone")
        XCTAssertEqual(command.executable, "/usr/bin/osascript")
        XCTAssertFalse(command.inheritsEnvironment)
        XCTAssertEqual(command.environment, ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        // The script and its inputs are arguments, never part of the AppleScript source.
        XCTAssertEqual(
            command.arguments,
            ["-e", PrivilegedScript.appleScript, PrivilegedScript.text, "install", "someone", PrivilegedScript.prompt(for: .install)])
    }

    func testTheAppleScriptQuotesEveryInputAndClearsTheEnvironment() {
        let source = PrivilegedScript.appleScript
        XCTAssertTrue(source.contains("set {script_text, mode_name, user_name, prompt_text} to argv"))
        XCTAssertTrue(
            source.contains(
                #"do shell script "/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash --noprofile --norc -c " & quoted form of script_text & " voiced-privileged " & quoted form of mode_name & " " & quoted form of user_name with prompt prompt_text with administrator privileges"#
            ))
    }

    func testTheAppleScriptCompiles() async throws {
        // osacompile only compiles; nothing runs and nothing prompts.
        let outputs = try await collect(SystemInstallRunner().run(InstallCommand(
            "/usr/bin/osacompile", ["-e", PrivilegedScript.appleScript, "-o", scratch.appendingPathComponent("p.scpt").path])))
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
            "/usr/bin/env", [], environment: PrivilegedScript.environment, inheritsEnvironment: false)))
        XCTAssertEqual(stdout(exact), ["PATH=/usr/bin:/bin:/usr/sbin:/sbin"])
        _ = try await collect(runner.run(InstallCommand(
            "/bin/sh", ["-c", "/usr/bin/true"], environment: PrivilegedScript.environment, inheritsEnvironment: false)))
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
        // The same command inheriting the app's environment does run it: the test can fail.
        _ = try await collect(runner.run(InstallCommand("/bin/sh", ["-c", "/usr/bin/env > /dev/null"])))
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
    }

    // MARK: - Reading the result

    func testOsascriptOutcomes() {
        XCTAssertEqual(
            PrivilegedScript.outcome(status: 0, stdout: ["installed a", "installed b"], stderr: []),
            .finished(status: 0, output: "installed a\ninstalled b"))
        XCTAssertEqual(
            PrivilegedScript.outcome(status: 1, stdout: [], stderr: ["0:342: execution error: User canceled. (-128)"]),
            .cancelled)
        XCTAssertEqual(
            PrivilegedScript.outcome(
                status: 1, stdout: [],
                stderr: ["0:342: execution error: failed io.github.julianbeaulieu.voiced.tts", "bootstrap failed: io.github.julianbeaulieu.voiced.tts (3)"]),
            .finished(status: 3, output: "failed io.github.julianbeaulieu.voiced.tts\nbootstrap failed: io.github.julianbeaulieu.voiced.tts"))
        XCTAssertEqual(
            PrivilegedScript.outcome(status: 1, stdout: [], stderr: ["something else"]),
            .finished(status: 1, output: "something else"))
    }

    func testResults() {
        typealias R = PrivilegedScript.Result
        XCTAssertEqual(PrivilegedScript.result(of: .finished(status: 0, output: "")), R.done(warning: nil))
        XCTAssertEqual(PrivilegedScript.result(of: .cancelled), R.cancelled)
        guard case .done(let warning4?) = PrivilegedScript.result(of: .finished(status: 4, output: "")),
            case .done(let warning5?) = PrivilegedScript.result(of: .finished(status: 5, output: ""))
        else { return XCTFail("4 and 5 are warnings") }
        XCTAssertTrue(warning4.contains("could not allow"))
        XCTAssertTrue(warning5.contains("outside your home folder"))
        XCTAssertEqual(
            PrivilegedScript.result(of: .finished(status: 3, output: "failed io.github.julianbeaulieu.voiced.stt\rfailed io.github.julianbeaulieu.voiced.watchdog")),
            R.failed(
                cause: "macOS did not start speech to text and the watchdog.",
                output: ["failed io.github.julianbeaulieu.voiced.stt", "failed io.github.julianbeaulieu.voiced.watchdog"]))
        XCTAssertEqual(
            PrivilegedScript.result(of: .finished(status: 1, output: "refused: voiced is already installed for alex on this Mac")),
            R.anotherAccount("voiced is already installed for alex on this Mac."))
        XCTAssertEqual(
            PrivilegedScript.result(of: .finished(status: 1, output: "refused: bad user name")),
            R.failed(cause: "Refused: bad user name.", output: ["refused: bad user name"]))
        guard case .failed(let cause, []) = PrivilegedScript.result(of: .damaged("a sealed resource is missing")) else {
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
        let runner = SystemInstallRunner(terminationGrace: 0.3)
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
        let gone = await eventually(timeout: .seconds(3)) { kill(child, 0) != 0 }
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
