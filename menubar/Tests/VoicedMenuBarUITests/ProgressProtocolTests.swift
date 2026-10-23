import FakeControlServerKit
import Foundation
import TestSupport
import VoicedControl
import XCTest

@testable import VoicedMenuBarUI

final class ProgressProtocolTests: XCTestCase {
    private let start = ContinuousClock.now

    private func session(_ lines: [String], every step: Duration = .milliseconds(250)) -> InstallSession {
        var session = InstallSession()
        for (index, line) in lines.enumerated() {
            _ = session.receive(line: line, at: start + step * index)
        }
        return session
    }

    // MARK: - Events

    func testParsesEveryEvent() {
        XCTAssertEqual(InstallEvent(line: RecordedProgress.hello), .hello(protocol: 1))
        XCTAssertEqual(
            InstallEvent(line: RecordedProgress.step("ports", "start", "Checking the ports")),
            .step(id: "ports", state: .start, shown: "Checking the ports"))
        XCTAssertEqual(
            InstallEvent(line: RecordedProgress.download("m", 5, 10)), .download(model: "m", bytes: 5, total: 10))
        XCTAssertEqual(InstallEvent(line: #"{"event": "log", "line": "hi"}"#), .log("hi"))
        XCTAssertEqual(InstallEvent(line: RecordedProgress.done), .done)
        XCTAssertEqual(
            InstallEvent(line: RecordedProgress.error("ports", "Port 10300 is in use by python (pid 12).")),
            .error(step: "ports", message: "Port 10300 is in use by python (pid 12)."))
    }

    func testUnknownEventsAndFieldsAreIgnored() {
        XCTAssertNil(InstallEvent(line: #"{"event": "future", "x": 1}"#))
        XCTAssertNil(InstallEvent(line: #"{"no_event": true}"#))
        XCTAssertEqual(
            InstallEvent(line: #"{"event": "download", "model": "m", "bytes": 1, "total": 2, "eta": 3}"#),
            .download(model: "m", bytes: 1, total: 2))
        XCTAssertEqual(
            InstallEvent(line: #"{"event": "hello", "protocol": 1, "version": "1", "extra": [1]}"#),
            .hello(protocol: 1))
    }

    func testEventsMissingWhatTheyNeedAreIgnored() {
        XCTAssertNil(InstallEvent(line: #"{"event": "step", "state": "start"}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "step", "step": "x", "state": "halfway"}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "download", "model": "m", "bytes": "1", "total": 2}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "hello"}"#))
        XCTAssertEqual(InstallEvent(line: #"{"event": "error"}"#), .error(step: nil, message: ""))
    }

    func testDryRunPlannedStepsAreIgnored() {
        XCTAssertNil(InstallEvent(line: RecordedProgress.step("config", "planned")))
    }

    func testANonJSONLineIsLogText() {
        XCTAssertEqual(InstallEvent(line: "Warning: something"), .log("Warning: something"))
    }

    func testRefusalNamesTheSideToUpdate() {
        XCTAssertNil(InstallEvent.refusal(protocol: 1))
        XCTAssertTrue(InstallEvent.refusal(protocol: 2)!.hasSuffix("Update the app."))
        XCTAssertTrue(InstallEvent.refusal(protocol: 0)!.hasSuffix("Update voiced."))
    }

    // MARK: - Session

    func testASuccessfulRunIsDone() {
        let run = session(RecordedProgress.success)
        XCTAssertEqual(run.finish(status: 0), .done)
        XCTAssertEqual(
            run.progress.steps.map(\.id),
            ["config", "claim", "check", "ports", "models", "link", "manifest", "prune", "agents"])
        XCTAssertTrue(run.progress.steps.allSatisfy(\.isDone))
        XCTAssertEqual(run.progress.downloads.map(\.model), [RecordedProgress.parakeet, RecordedProgress.kokoro])
        XCTAssertTrue(run.progress.downloads.allSatisfy(\.isFinished))
        XCTAssertEqual(run.tail, ["loaded parakeet"])
    }

    func testKnownStepsAreTitledByTheAppOthersByWhatTheyShow() {
        let run = session([
            RecordedProgress.hello, RecordedProgress.step("models", "start"),
            RecordedProgress.step("build", "start", "uv venv /tmp/env"),
        ])
        XCTAssertEqual(run.progress.steps.map(\.title), ["Downloading the models", "uv venv /tmp/env"])
    }

    func testTheCurrentStepIsTheLastUnfinishedOne() {
        let run = session(RecordedProgress.downloading)
        XCTAssertEqual(run.progress.current?.id, "models")
        XCTAssertEqual(run.progress.downloads.first?.fraction, 0.5)
    }

    func testDownloadSpeedAndTimeLeft() throws {
        // 627 MB every 250 ms of the parakeet lines: about 2.5 GB/s, one second left at 50 %.
        let run = session(RecordedProgress.downloading)
        let parakeet = try XCTUnwrap(run.progress.downloads.first)
        let speed = try XCTUnwrap(parakeet.bytesPerSecond)
        XCTAssertEqual(speed, 1_254_250_000 / 1.0, accuracy: 1)
        XCTAssertEqual(try XCTUnwrap(parakeet.secondsRemaining), 1, accuracy: 0.01)
    }

    func testBytesAreCappedAtTheTotal() {
        let run = session([RecordedProgress.hello, RecordedProgress.download("m", 15, 10)])
        XCTAssertEqual(run.progress.downloads.first?.bytes, 10)
    }

    func testAWrongProtocolIsRefusedAndStopsTheRun() {
        var run = InstallSession()
        XCTAssertFalse(run.receive(line: #"{"event": "hello", "protocol": 2}"#, at: start))
        XCTAssertFalse(run.receive(line: RecordedProgress.done, at: start))
        XCTAssertEqual(run.finish(status: 0), .failed(step: nil, cause: InstallEvent.refusal(protocol: 2)!))
    }

    func testAnythingBeforeHelloIsRefused() {
        var run = InstallSession()
        XCTAssertFalse(run.receive(line: RecordedProgress.step("config", "start", "x"), at: start))
        guard case .failed(nil, let cause, nil) = run.finish(status: 0) else { return XCTFail() }
        XCTAssertTrue(cause.contains("did not start with a progress hello"))
    }

    func testAStrayLineBeforeHelloIsKeptNotRefused() {
        let run = session(["Warning: a library printed this", RecordedProgress.hello, RecordedProgress.done])
        XCTAssertEqual(run.finish(status: 0), .done)
        XCTAssertEqual(run.tail, ["Warning: a library printed this"])
        // Without a hello after it, it is still an older voiced.
        let unanswered = session(["Warning: a library printed this"])
        XCTAssertEqual(
            unanswered.finish(status: 0), .failed(step: nil, cause: "This voiced does not report install progress. Update voiced."))
    }

    func testStepsAndDownloadsAreBounded() {
        let lines = [RecordedProgress.hello]
            + (0..<100).map { RecordedProgress.step("s\($0)", "start", "x") }
            + (0..<100).map { RecordedProgress.download("m\($0)", 1, 2) }
        let run = session(lines)
        XCTAssertEqual(run.progress.steps.count, InstallSession.maximumEntries)
        XCTAssertEqual(run.progress.downloads.count, InstallSession.maximumEntries)
    }

    func testSpeedSamplesAreBoundedByCountAsWellAsTime() {
        // Ten thousand events inside the time window.
        let lines = [RecordedProgress.hello] + (1...10_000).map { RecordedProgress.download("m", Int64($0), 20_000) }
        let run = session(lines, every: .microseconds(1))
        XCTAssertEqual(run.samples["m"]?.count, InstallSession.maximumSamples)
        XCTAssertNotNil(run.progress.downloads.first?.bytesPerSecond)
    }

    func testNoOutputAtAllMeansAnOlderVoiced() {
        var run = InstallSession()
        run.receive(stderr: "error: unrecognized arguments: --json-progress")
        XCTAssertEqual(
            run.finish(status: 2), .failed(step: nil, cause: "This voiced does not report install progress. Update voiced."))
        XCTAssertEqual(run.tail, ["error: unrecognized arguments: --json-progress"])
    }

    func testAnErrorEventNamesItsStep() {
        let run = session([RecordedProgress.hello, RecordedProgress.error("ports", "Port 10300 is in use.")])
        XCTAssertEqual(run.finish(status: 1), .failed(step: "ports", cause: "Port 10300 is in use."))
        // The exit status must agree; an error is an error whatever it says.
        XCTAssertEqual(run.finish(status: 0), .failed(step: "ports", cause: "Port 10300 is in use."))
    }

    func testTheFirstEndingEventCounts() {
        let run = session([RecordedProgress.hello, RecordedProgress.done, RecordedProgress.error("x", "late")])
        XCTAssertEqual(run.finish(status: 0), .done)
    }

    func testDoneWithAFailingExitIsAFailure() {
        let run = session([RecordedProgress.hello, RecordedProgress.done])
        XCTAssertEqual(
            run.finish(status: 1), .failed(step: nil, cause: "voiced install reported done but exited with status 1."))
    }

    func testExitWithoutAnEndingEvent() {
        let run = session([RecordedProgress.hello, RecordedProgress.step("models", "start", "Downloading")])
        XCTAssertEqual(run.finish(status: 0), .failed(step: "models", cause: "voiced install ended without reporting done."))
        XCTAssertEqual(run.finish(status: 9), .failed(step: "models", cause: "voiced install exited with status 9."))
    }

    func testTheTailKeepsTheLastLinesOfLogAndStderr() {
        var run = session([RecordedProgress.hello] + (1...6).map { #"{"event": "log", "line": "log \#($0)"}"# })
        for n in 1...4 { run.receive(stderr: "err \(n)") }
        XCTAssertEqual(run.tail, ["log 3", "log 4", "log 5", "log 6", "err 1", "err 2", "err 3", "err 4"])
    }

    func testStallsOnlyWhileADownloadIsUnfinished() {
        var run = InstallSession()
        _ = run.receive(line: RecordedProgress.hello, at: start)
        XCTAssertFalse(run.isStalled(at: start + .seconds(100), after: .seconds(60)))
        _ = run.receive(line: RecordedProgress.download("m", 5, 10), at: start)
        XCTAssertFalse(run.isStalled(at: start + .seconds(59), after: .seconds(60)))
        // The same bytes again are no progress.
        _ = run.receive(line: RecordedProgress.download("m", 5, 10), at: start + .seconds(30))
        XCTAssertTrue(run.isStalled(at: start + .seconds(61), after: .seconds(60)))
        _ = run.receive(line: RecordedProgress.download("m", 10, 10), at: start + .seconds(62))
        XCTAssertFalse(run.isStalled(at: start + .seconds(500), after: .seconds(60)))
        // A new download starts its own clock, however long the last one is past.
        _ = run.receive(line: RecordedProgress.download("n", 0, 10), at: start + .seconds(500))
        XCTAssertFalse(run.isStalled(at: start + .seconds(501), after: .seconds(60)))
    }

    // MARK: - voiced models download (ADR 0022)

    func testAModelDownloadErrorCarriesItsCode() {
        let line = #"{"event": "error", "code": "gated_terms", "message": "Accept the terms.", "repo": "m", "revision": "r", "path": "p"}"#
        XCTAssertEqual(InstallEvent(line: line), .error(step: nil, message: "Accept the terms.", code: "gated_terms"))
        var run = InstallSession(command: "voiced models download", reports: "download progress")
        _ = run.receive(line: RecordedProgress.hello, at: start)
        _ = run.receive(line: line, at: start)
        XCTAssertEqual(run.finish(status: 1), .failed(step: nil, cause: "Accept the terms.", code: "gated_terms"))
    }

    func testAModelDownloadIsWordedAsItsCommand() {
        var silent = InstallSession(command: "voiced models download", reports: "download progress")
        XCTAssertEqual(
            silent.finish(status: 2),
            .failed(step: nil, cause: "This voiced does not report download progress. Update voiced."))
        _ = silent.receive(line: RecordedProgress.hello, at: start)
        XCTAssertEqual(silent.finish(status: 143), .failed(step: nil, cause: "voiced models download exited with status 143."))
        _ = silent.receive(line: #"{"event": "error", "code": "failed"}"#, at: start)
        XCTAssertEqual(
            silent.finish(status: 1), .failed(step: nil, cause: "voiced models download failed.", code: "failed"),
            "an empty message is worded as the command")
        var install = InstallSession()
        _ = install.receive(line: RecordedProgress.hello, at: start)
        _ = install.receive(line: #"{"event": "error"}"#, at: start)
        XCTAssertEqual(install.finish(status: 1), .failed(step: nil, cause: "voiced install failed."))
    }

    /// The fake command speaks the protocol as the real one does: the model, then its
    /// engine's shared files, then done; a failure is one error with its code.
    func testTheFakeCommandSpeaksTheProtocol() throws {
        let models = FakeModels(file: FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID()).json"))
        defer { try? FileManager.default.removeItem(at: models.file) }
        models.write(.sample)
        var lines: [String] = []
        XCTAssertEqual(models.run(["download", "gradium/phonon-31m-en", "--json-progress"], output: { lines.append($0) }, pause: {}), 0)
        var run = InstallSession(command: "voiced models download", reports: "download progress")
        for line in lines { XCTAssertTrue(run.receive(line: line, at: start)) }
        XCTAssertEqual(run.finish(status: 0), .done)
        XCTAssertEqual(run.progress.downloads.map(\.model), ["gradium/phonon-31m-en", "gradium/phonon-voices"])
        XCTAssertTrue(run.progress.downloads.allSatisfy(\.isFinished))
        XCTAssertEqual(models.read()?.isMissing("gradium/phonon-31m-en"), false)
        XCTAssertNil(models.read()?.isMissing("someone/elses-model"), "a model outside the catalogue is not checked")

        lines = []
        XCTAssertEqual(models.run(["download", "gradium/phonon-40m-en", "--json-progress"], output: { lines.append($0) }, pause: {}), 1)
        var failed = InstallSession(command: "voiced models download", reports: "download progress")
        for line in lines { _ = failed.receive(line: line, at: start) }
        guard case .failed(_, _, "token_missing") = failed.finish(status: 1) else { return XCTFail("\(lines)") }

        lines = []
        XCTAssertEqual(models.run(["delete", "gradium/phonon-31m-en", "--yes", "--json"], output: { lines.append($0) }, pause: {}), 0)
        XCTAssertEqual(
            try JSONDecoder().decode(ModelDeleteResult.self, from: Data(lines.joined().utf8)),
            .deleted(freed: 65_049_306 + 9_510_439, notes: ["Phonon's shared files (gradium/phonon-voices) are deleted with it."]))
        XCTAssertEqual(models.read()?.isMissing("gradium/phonon-31m-en"), true)
    }
}
