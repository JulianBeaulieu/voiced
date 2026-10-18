import TestSupport
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
        XCTAssertEqual(InstallEvent(line: RecordedProgress.hello), .hello(protocol: 1, version: "0.1.0"))
        XCTAssertEqual(
            InstallEvent(line: RecordedProgress.step("ports", "start", "Checking the ports")),
            .step(id: "ports", state: .start, shown: "Checking the ports"))
        XCTAssertEqual(
            InstallEvent(line: RecordedProgress.download("m", 5, 10)), .download(model: "m", bytes: 5, total: 10))
        XCTAssertEqual(InstallEvent(line: #"{"event": "log", "line": "hi"}"#), .log("hi"))
        XCTAssertEqual(InstallEvent(line: RecordedProgress.done), .done(stagingDir: "/Users/someone/.config/voiced/launchd"))
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
            .hello(protocol: 1, version: "1"))
    }

    func testEventsMissingWhatTheyNeedAreIgnored() {
        XCTAssertNil(InstallEvent(line: #"{"event": "step", "state": "start"}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "step", "step": "x", "state": "halfway"}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "download", "model": "m", "bytes": "1", "total": 2}"#))
        XCTAssertNil(InstallEvent(line: #"{"event": "hello"}"#))
        XCTAssertEqual(InstallEvent(line: #"{"event": "error"}"#), .error(step: nil, message: "voiced install failed."))
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
        XCTAssertEqual(run.finish(status: 0), .done(stagingDir: "/Users/someone/.config/voiced/launchd"))
        XCTAssertEqual(run.version, "0.1.0")
        XCTAssertEqual(
            run.progress.steps.map(\.id),
            ["config", "claim", "check", "ports", "models", "link", "manifest", "prune", "stage"])
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
        guard case .failed(nil, let cause) = run.finish(status: 0) else { return XCTFail() }
        XCTAssertTrue(cause.contains("did not start with a progress hello"))
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
        XCTAssertEqual(run.finish(status: 0), .done(stagingDir: "/Users/someone/.config/voiced/launchd"))
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
}
