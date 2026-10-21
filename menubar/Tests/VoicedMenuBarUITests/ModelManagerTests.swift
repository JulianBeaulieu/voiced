import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl
@testable import VoicedMenuBarUI

/// The model rows against the fake server, with `voiced models` answered by the fake command
/// from the same cache file the server reads.
@MainActor
final class ModelManagerTests: XCTestCase {
    private let phonon31 = "gradium/phonon-31m-en"
    private let kokoro = "mlx-community/Kokoro-82M-bf16"

    private struct Fixture {
        let server: FakeControlServer
        let app: AppModel
        let runner: FakeInstallRunner
        let models: ModelManager
        let cache: FakeModels

        @MainActor func stop() {
            app.stop()
            server.stop()
        }
    }

    private func fixture(_ scenario: FakeScenario = .running) async throws -> Fixture {
        let socket = temporarySocketPath()
        let cache = FakeModels(socketPath: socket)
        let server = try startServer(scenario) { $0.modelsFile = cache.file }
        let daemons = try launchDaemonsDirectory()
        let app = AppModel(
            socketPath: server.socketPath, installProbe: InstallProbe(launchDaemonsDirectory: daemons),
            backoff: Backoff(initial: .milliseconds(20), maximum: .milliseconds(80)), opener: { _ in true })
        app.start()
        let ready = await eventually { app.status != nil && app.settings != nil && app.engines != nil }
        XCTAssertTrue(ready)
        let runner = FakeInstallRunner()
        runner.respond = { .models($0.arguments, in: cache) }
        // Never run: the fake runner answers in its place.
        let models = ModelManager(app: app, runner: runner, executable: "/usr/bin/true")
        await models.refresh()
        return Fixture(server: server, app: app, runner: runner, models: models, cache: cache)
    }

    func testTheListComesFromTheCommand() async throws {
        let f = try await fixture()
        defer { f.stop() }
        XCTAssertEqual(f.runner.commands.last?.arguments, ["models", "list", "--json"])
        XCTAssertEqual(f.models.list, FakeModelState.sample.list)
        XCTAssertNil(f.models.listError)
    }

    func testAMissingCommandOrAnOldVoicedSaysSo() async throws {
        let app = AppModel(socketPath: temporarySocketPath())
        let runner = FakeInstallRunner()
        let missing = ModelManager(app: app, runner: runner, executable: "/nonexistent/voiced")
        await missing.refresh()
        XCTAssertEqual(missing.listError, "voiced is not installed at /nonexistent/voiced, so its models cannot be listed.")
        XCTAssertTrue(runner.commands.isEmpty)

        runner.respond = { _ in .init([.stderr("Error: No such command 'models'."), .exit(2)]) }
        let old = ModelManager(app: app, runner: runner, executable: "/usr/bin/true")
        await old.refresh()
        XCTAssertEqual(
            old.listError,
            "Cannot list the models (Error: No such command 'models'.). An older voiced has no `voiced models`; update voiced.")
    }

    func testDownloadShowsProgressThenTheRowIsDownloaded() async throws {
        let f = try await fixture()
        defer { f.stop() }
        f.runner.respond = { command in
            command.arguments.contains("download") ? .models(command.arguments, in: f.cache, delay: .milliseconds(20))
                : .models(command.arguments, in: f.cache)
        }
        let download = Task { await f.models.download(phonon31) }
        let moving = await eventually { (f.models.download?.progress?.bytes ?? 0) > 0 }
        XCTAssertTrue(moving)
        XCTAssertEqual(f.models.download?.model, phonon31)
        XCTAssertTrue(f.models.isDownloading)
        // One at a time.
        let second = await f.models.download("gradium/phonon-40m-en")
        XCTAssertFalse(second)
        let done = await download.value
        XCTAssertTrue(done)
        XCTAssertNil(f.models.download)
        XCTAssertEqual(
            f.runner.commands(named: "true").filter { $0.arguments.contains("download") }.map(\.arguments),
            [["models", "download", phonon31, "--json-progress"]])
        XCTAssertEqual(f.models.list?.model(phonon31)?.model.downloaded, true)
        XCTAssertEqual(f.models.list?.engines.first { $0.name == "phonon" }?.shared.first?.downloaded, true, "with its voices")
    }

    func testAFailedDownloadPutsItsCodeOnTheRow() async throws {
        let f = try await fixture()
        defer { f.stop() }
        // The sample's gated model fails without a token.
        let done = await f.models.download("gradium/phonon-40m-en")
        XCTAssertFalse(done)
        let gated = f.models.list?.model("gradium/phonon-40m-en")?.model
        XCTAssertEqual(f.models.messages["gradium/phonon-40m-en"], .needsToken(gated))

        f.runner.respond = { _ in
            .progress([
                RecordedProgress.hello,
                #"{"event": "error", "code": "busy", "message": "The state directory is locked.", "repo": "x"}"#,
            ], status: 1)
        }
        _ = await f.models.download(phonon31)
        XCTAssertEqual(
            f.models.messages[phonon31]?.text, "voiced is busy installing, updating or changing models; try again when it ends.")
    }

    /// The fake command refuses the model of an engine that cannot run here, as the real one does.
    func testTheFakeRefusesADownloadForAnEngineThatCannotRunHere() async throws {
        let f = try await fixture()
        defer { f.stop() }
        let moonshine = "moonshine-ai/moonshine-streaming-small"
        var lines: [String] = []
        let status = f.cache.run(["download", moonshine, "--json-progress"], output: { lines.append($0) }, pause: {})
        XCTAssertEqual(status, 1)
        let error = try XCTUnwrap(lines.last.flatMap { try JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] })
        XCTAssertEqual(error["event"] as? String, "error")
        XCTAssertEqual(error["code"] as? String, "failed")
        XCTAssertEqual(
            error["message"] as? String, "the moonshine engine needs macOS 15 or later, so \(moonshine) cannot run here")
        XCTAssertEqual(f.cache.read()?.list.model(moonshine)?.model.downloaded, false)
    }

    func testCancelEndsTheDownloadAndLeavesTheModelAsItWas() async throws {
        let f = try await fixture()
        defer { f.stop() }
        f.runner.respond = { command in
            guard command.arguments.contains("download") else { return .models(command.arguments, in: f.cache) }
            return .init(
                [.stdout(RecordedProgress.hello), .stdout(RecordedProgress.download(self.phonon31, 1_000, 65_049_306))],
                hangs: true)
        }
        let download = Task { await f.models.download(phonon31) }
        let started = await eventually { f.models.download?.progress != nil }
        XCTAssertTrue(started)
        f.models.cancelDownload()
        let done = await finished(Task { () throws -> Bool in await download.value })
        XCTAssertEqual(done, false)
        let terminated = await eventually { f.runner.terminated.contains { $0.arguments.contains("download") } }
        XCTAssertTrue(terminated, "the runner terminates the process group")
        XCTAssertNil(f.models.download)
        XCTAssertNil(f.models.messages[phonon31], "a cancel is not a failure")
        XCTAssertEqual(f.models.list?.model(phonon31)?.model.downloaded, false)
    }

    func testUseDownloadsSwitchesEngineAndModelTogetherAndRestarts() async throws {
        let f = try await fixture()
        defer { f.stop() }
        let row = try XCTUnwrap(
            ModelRow.rows(try XCTUnwrap(f.models.list), kind: .tts, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: true)
                .first { $0.model?.id == phonon31 })
        // The server refuses a model that is not downloaded.
        let refused = await f.app.apply(.ttsEngine("phonon", model: phonon31))
        XCTAssertFalse(refused)
        XCTAssertEqual(f.app.errorMessage, "tts.model: \(phonon31) is not downloaded; run voiced models download \(phonon31)")

        await f.models.use(row, for: .tts)
        XCTAssertEqual(f.models.list?.model(phonon31)?.model.downloaded, true)
        XCTAssertEqual(
            f.server.requests.last { $0.op == "settings.set" }?.args,
            ["tts.engine": "phonon", "tts.model": .string(phonon31), "tts.voice": "default"])
        XCTAssertEqual(f.server.requests.last?.op, "service.restart")
        XCTAssertEqual(f.server.requests.last?.args, ["service": "tts"])
        XCTAssertEqual(f.app.settings?.tts.model, phonon31)
        XCTAssertEqual(f.models.switchedVoiceEngine, "Phonon", "the Home Assistant hint")
        let ready = await eventually { f.app.status?[.tts]?.state == .running && f.app.status?[.tts]?.model == self.phonon31 }
        XCTAssertTrue(ready)
        f.models.dismissVoiceHint()
        XCTAssertNil(f.models.switchedVoiceEngine)
    }

    func testUseOfAPluginEngineSendsTheEngineAndShowsNoVoiceHint() async throws {
        let f = try await fixture()
        defer { f.stop() }
        let row = try XCTUnwrap(
            ModelRow.rows(try XCTUnwrap(f.models.list), kind: .stt, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: true)
                .first { $0.model == nil })
        await f.models.use(row, for: .stt)
        // The engine's own model, from `engines.list`, as an engine switch has always sent.
        XCTAssertEqual(f.server.requests.last { $0.op == "settings.set" }?.args, ["stt.engine": "fake", "stt.model": "fake"])
        XCTAssertEqual(f.server.requests.last?.args, ["service": "stt"])
        XCTAssertNil(f.models.switchedVoiceEngine, "the hint is for text to speech")
    }

    func testAFailedDownloadStopsUse() async throws {
        let f = try await fixture()
        defer { f.stop() }
        let row = try XCTUnwrap(
            ModelRow.rows(try XCTUnwrap(f.models.list), kind: .tts, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: true)
                .first { $0.model?.id == "gradium/phonon-40m-en" })
        await f.models.use(row, for: .tts)
        XCTAssertFalse(f.server.requests.contains { $0.op == "settings.set" })
        XCTAssertNotNil(f.models.messages["gradium/phonon-40m-en"])
    }

    func testDeleteAndDeleteOldVersion() async throws {
        let f = try await fixture()
        defer { f.stop() }
        _ = await f.models.download(phonon31)
        await f.models.delete(phonon31)
        XCTAssertEqual(
            f.runner.commands.first { $0.arguments.contains("delete") }?.arguments,
            ["models", "delete", phonon31, "--yes", "--json"])
        XCTAssertEqual(f.models.list?.model(phonon31)?.model.downloaded, false)
        XCTAssertEqual(
            f.models.list?.engines.first { $0.name == "phonon" }?.shared.first?.downloaded, false,
            "its voices go with Phonon's last model")

        await f.models.delete(kokoro, previous: true)
        XCTAssertEqual(
            f.runner.commands.last { $0.arguments.contains("delete") }?.arguments,
            ["models", "delete", kokoro, "--previous", "--yes", "--json"])
        XCTAssertEqual(f.models.list?.model(kokoro)?.model.previous, [])
        XCTAssertEqual(f.models.list?.model(kokoro)?.model.downloaded, true, "the current version stays")
        XCTAssertTrue(f.models.messages.isEmpty)
    }

    func testARefusedDeleteSaysWhy() async throws {
        let f = try await fixture()
        defer { f.stop() }
        // The fake, like the real command, refuses the configured model.
        await f.models.delete(kokoro)
        XCTAssertEqual(f.models.messages[kokoro]?.text, "In use. Choose another model first.")
        XCTAssertNil(f.models.deleting)
        XCTAssertEqual(f.models.list?.model(kokoro)?.model.downloaded, true)
        // After Use moves away from it, it can go.
        let row = try XCTUnwrap(
            ModelRow.rows(try XCTUnwrap(f.models.list), kind: .tts, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: true)
                .first { $0.model?.id == phonon31 })
        await f.models.use(row, for: .tts)
        await f.models.delete(kokoro)
        XCTAssertNil(f.models.messages[kokoro])
        XCTAssertEqual(f.models.list?.model(kokoro)?.model.downloaded, false)
    }

    func testAnotherVoicedCommandHoldingTheLockIsBusy() async throws {
        let f = try await fixture()
        defer { f.stop() }
        let marker = f.cache.busyMarker.path
        FileManager.default.createFile(atPath: marker, contents: nil)
        defer { try? FileManager.default.removeItem(atPath: marker) }
        let busy = "voiced is busy installing, updating or changing models; try again when it ends."
        _ = await f.models.download(phonon31)
        XCTAssertEqual(f.models.messages[phonon31]?.text, busy)
        await f.models.delete("mlx-community/Soprano-80M-bf16", previous: true)
        XCTAssertEqual(f.models.messages["mlx-community/Soprano-80M-bf16"]?.text, busy)
    }

    func testNoDownloadStartsWhileADeleteRuns() async throws {
        let f = try await fixture()
        defer { f.stop() }
        f.runner.respond = { command in
            command.arguments.contains("delete") ? .init([.stdout("{}")], hangs: true) : .models(command.arguments, in: f.cache)
        }
        let delete = Task { await f.models.delete(kokoro, previous: true) }
        let deleting = await eventually { f.models.deleting != nil }
        XCTAssertTrue(deleting)
        XCTAssertTrue(f.models.isBusy, "Update and Repair wait for a delete too")
        let started = await f.models.download(phonon31)
        XCTAssertFalse(started)
        delete.cancel()
    }

    func testACancelThatLandsAfterDoneStillCounts() async throws {
        let f = try await fixture()
        defer { f.stop() }
        f.runner.respond = { command in
            guard command.arguments.contains("download") else { return .models(command.arguments, in: f.cache) }
            return .init(
                [RecordedProgress.hello, RecordedProgress.download(self.phonon31, 10, 10), RecordedProgress.done]
                    .map(ProcessOutput.stdout), hangs: true)
        }
        let download = Task { await f.models.download(phonon31) }
        let reported = await eventually { f.models.download?.isDone == true }
        XCTAssertTrue(reported)
        f.models.cancelDownload()
        let done = await download.value
        XCTAssertTrue(done, "the model is on disk: Use goes on")
    }

    func testACancelThatLandsAfterAnErrorKeepsTheError() async throws {
        let f = try await fixture()
        defer { f.stop() }
        f.runner.respond = { command in
            guard command.arguments.contains("download") else { return .models(command.arguments, in: f.cache) }
            return .init(
                [RecordedProgress.hello, #"{"event": "error", "code": "network", "message": "offline"}"#]
                    .map(ProcessOutput.stdout), hangs: true)
        }
        let download = Task { await f.models.download(phonon31) }
        let ended = await eventually { f.models.download?.hasEnded == true }
        XCTAssertTrue(ended)
        f.models.cancelDownload()
        let done = await download.value
        XCTAssertFalse(done)
        XCTAssertEqual(
            f.models.messages[phonon31]?.text, "Hugging Face could not be reached. Check the connection and try again.")
    }

    func testAListThisAppCannotReadIsNotCalledAnOldVoiced() async throws {
        let app = AppModel(socketPath: temporarySocketPath())
        let runner = FakeInstallRunner()
        runner.respond = { _ in .init([.stdout(#"{"engines": "not a list"}"#), .exit(0)]) }
        let models = ModelManager(app: app, runner: runner, executable: "/usr/bin/true")
        await models.refresh()
        XCTAssertTrue(models.listError?.hasPrefix("voiced models list printed a list this app cannot read") == true)
    }

    func testAMissingModelDownloadsThenTheServiceRestartsIntoIt() async throws {
        let f = try await fixture(.missingModel)
        defer { f.stop() }
        let row = try XCTUnwrap(f.app.dashboard.rows.first { $0.kind == .tts })
        XCTAssertEqual(row.missingModel, kokoro)
        XCTAssertEqual(f.app.dashboard.headline, "Model not downloaded")

        // A restart without the model comes back missing it.
        await f.app.restart(.service(.tts))
        let stillMissing = await eventually { f.app.status?[.tts]?.state == .missingModel }
        XCTAssertTrue(stillMissing)

        // The menu window's Download.
        await f.models.download(kokoro)
        XCTAssertEqual(f.server.requests.last?.op, "service.restart")
        let running = await eventually { f.app.status?[.tts]?.state == .running && f.app.status?[.tts]?.warm == true }
        XCTAssertTrue(running)
        XCTAssertEqual(f.app.dashboard.headline, "Running")
    }

    /// The pane's Download on the row in use restarts the service too: the watchdog leaves
    /// `missing_model` alone, so without it the service would stay at 503.
    func testDownloadFromTheRowInUseRestartsAMissingModelService() async throws {
        let f = try await fixture(.missingModel)
        defer { f.stop() }
        let row = try XCTUnwrap(
            ModelRow.rows(try XCTUnwrap(f.models.list), kind: .tts, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: true)
                .first { $0.model?.id == kokoro })
        XCTAssertEqual(row.state, .inUseMissing)
        XCTAssertEqual(row.downloadTitle, "Download")
        let restartsBefore = f.server.requests.filter { $0.op == "service.restart" }.count
        await f.models.download(try XCTUnwrap(row.model?.id))
        XCTAssertEqual(f.server.requests.filter { $0.op == "service.restart" }.count, restartsBefore + 1)
        XCTAssertEqual(f.server.requests.last?.args, ["service": "tts"])
        let running = await eventually { f.app.status?[.tts]?.isReady == true }
        XCTAssertTrue(running)
        let ready = ModelRow.isReady(f.app.status?[.tts], restarting: f.app.restarting.contains(.tts))
        let after = ModelRow.rows(
            try XCTUnwrap(f.models.list), kind: .tts, settings: try XCTUnwrap(f.app.settings), downloading: nil, ready: ready)
        XCTAssertEqual(after.first { $0.model?.id == self.kokoro }?.state, .inUse)
    }

    func testADownloadOfAModelNoServiceIsMissingRestartsNothing() async throws {
        let f = try await fixture()
        defer { f.stop() }
        _ = await f.models.download(phonon31)
        XCTAssertFalse(f.server.requests.contains { $0.op == "service.restart" })
    }
}
