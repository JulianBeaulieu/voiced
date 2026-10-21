import FakeControlServerKit
import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

final class ModelCatalogueTests: XCTestCase {
    /// docs/operations.md's example, with a field this app does not know.
    static let documented = #"""
        {
          "cache": "/Users/you/.cache/huggingface/hub",
          "total_bytes": 2864000000,
          "future": 1,
          "engines": [
            {
              "name": "phonon", "label": "Phonon", "kind": "tts", "installed": true,
              "available": true, "unavailable_reason": "",
              "languages": ["en-US", "en-GB"], "voices": ["default", "Zoey"],
              "models": [
                {
                  "id": "gradium/phonon-31m-en", "repo": "gradium/phonon-31m-en", "shared": false,
                  "license": "Apache-2.0", "url": "https://huggingface.co/gradium/phonon-31m-en",
                  "gated": false, "revision": "8c64b55e", "download_bytes": 65049306,
                  "on_disk_bytes": 65049306, "downloaded": true,
                  "previous": [{"revision": "1a2b", "bytes": 64000000}],
                  "refs": ["main"], "in_use": true,
                  "quality": 5, "speed": 5,
                  "note": "Runs on the CPU, leaving the GPU to speech to text; 38 English voices",
                  "recommended": false
                }
              ],
              "shared": [{"id": "gradium/phonon-voices", "shared": true, "license": "Apache-2.0",
                          "download_bytes": 9510439, "on_disk_bytes": 9510439, "downloaded": true}]
            },
            {"name": "plugin", "label": "Plugin", "kind": "stt", "installed": true, "models": [], "shared": []}
          ]
        }
        """#

    func testTheDocumentedListDecodes() throws {
        let list = try decodeJSON(ModelList.self, Self.documented)
        XCTAssertEqual(list.cache, "/Users/you/.cache/huggingface/hub")
        XCTAssertEqual(list.totalBytes, 2_864_000_000)
        let phonon = try XCTUnwrap(list.engines.first)
        XCTAssertEqual(phonon.voices, ["default", "Zoey"])
        XCTAssertTrue(phonon.available)
        XCTAssertNil(phonon.unavailableNote)
        let model = try XCTUnwrap(phonon.models.first)
        XCTAssertEqual(model.name, "phonon-31m-en")
        XCTAssertEqual(model.cardURL?.absoluteString, "https://huggingface.co/gradium/phonon-31m-en")
        XCTAssertEqual(model.previous, [.init(revision: "1a2b", bytes: 64_000_000)])
        XCTAssertEqual(model.refs, ["main"])
        XCTAssertEqual(phonon.shared.map(\.id), ["gradium/phonon-voices"])
        XCTAssertEqual(list.engines(of: .stt).map(\.name), ["plugin"])
        XCTAssertEqual(list.model("gradium/phonon-31m-en")?.engine.name, "phonon")
        XCTAssertNil(list.model("gradium/phonon-voices"), "shared models are not rows")
        XCTAssertEqual(model.quality, 5)
        XCTAssertEqual(model.speed, 5)
        XCTAssertEqual(model.note, "Runs on the CPU, leaving the GPU to speech to text; 38 English voices")
        XCTAssertFalse(model.recommended)
    }

    /// A voiced from before the ratings lists its models without them, and a field of another
    /// type does not lose the list.
    func testModelsWithoutRatingsStillDecode() throws {
        let older = try decodeJSON(
            CatalogueModel.self, #"{"id": "org/m", "license": "MIT", "download_bytes": 1, "downloaded": false}"#)
        XCTAssertNil(older.quality)
        XCTAssertNil(older.speed)
        XCTAssertEqual(older.note, "")
        XCTAssertFalse(older.recommended)
        let odd = try decodeJSON(
            CatalogueModel.self,
            #"{"id": "org/m", "quality": "high", "speed": null, "note": 3, "recommended": "yes"}"#)
        XCTAssertNil(odd.quality)
        XCTAssertNil(odd.speed)
        XCTAssertEqual(odd.note, "")
        XCTAssertFalse(odd.recommended)
    }

    // MARK: - Rows

    private func rows(
        _ state: FakeModelState = .sample, kind: ServiceKind, engine: String, model: String, downloading: String? = nil,
        ready: Bool = true
    ) -> [String: ModelRow] {
        var settings = FakeState.sample.settings
        if kind == .stt {
            (settings.stt.engine, settings.stt.model) = (engine, model)
        } else {
            (settings.tts.engine, settings.tts.model) = (engine, model)
        }
        let rows = ModelRow.rows(state.list, kind: kind, settings: settings, downloading: downloading, ready: ready)
        return Dictionary(uniqueKeysWithValues: rows.map { ($0.model?.id ?? $0.engine.name, $0) })
    }

    private let kokoro = "mlx-community/Kokoro-82M-bf16"
    private let nemotron = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    private let moonshineSmall = "moonshine-ai/moonshine-streaming-small"

    func testEveryRowStateFromTheSample() {
        let tts = rows(kind: .tts, engine: "kokoro", model: kokoro)
        XCTAssertEqual(tts[kokoro]?.state, .inUse)
        XCTAssertEqual(tts["mlx-community/Soprano-80M-bf16"]?.state, .oldVersion)
        XCTAssertEqual(tts["gradium/phonon-31m-en"]?.state, .notDownloaded)
        XCTAssertEqual(tts["gradium/phonon-40m-en"]?.state, .gated)
        XCTAssertEqual(tts["fake"]?.state, .selfManaged, "a plugin engine: Use only")
        XCTAssertEqual(tts["fake"]?.canUse, true)
        XCTAssertNil(tts["fake"]?.downloadTitle)
        XCTAssertEqual(tts["fake"]?.showsDelete, false)
        XCTAssertNil(tts["gradium/phonon-voices"])

        let stt = rows(kind: .stt, engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3")
        XCTAssertEqual(stt["mlx-community/parakeet-tdt-0.6b-v3"]?.state, .inUse)
        XCTAssertEqual(stt["fake"]?.stateLabel, "Manages its own model")

        let downloading = rows(kind: .tts, engine: "kokoro", model: kokoro, downloading: "gradium/phonon-31m-en")
        XCTAssertEqual(downloading["gradium/phonon-31m-en"]?.state, .downloading)

        let missing = rows(FakeScenario.missingModel.initialModels, kind: .tts, engine: "kokoro", model: kokoro)
        XCTAssertEqual(missing[kokoro]?.state, .inUseMissing)

        // The model in use while its service loads it, and a plugin engine in use.
        let loading = rows(kind: .tts, engine: "kokoro", model: kokoro, ready: false)[kokoro]
        XCTAssertEqual(loading?.state, .inUseLoading)
        XCTAssertEqual(loading?.stateLabel, "In use, loading")
        XCTAssertEqual(loading?.canDelete, false)
        XCTAssertEqual(rows(kind: .tts, engine: "fake", model: "fake")["fake"]?.state, .inUse)
        XCTAssertEqual(rows(kind: .tts, engine: "fake", model: "fake", ready: false)["fake"]?.state, .inUseLoading)
    }

    func testRatedModelsShowBarsTheNoteAndTheDefaultsTheBadge() {
        let tts = rows(kind: .tts, engine: "kokoro", model: kokoro)
        XCTAssertEqual(tts[kokoro]?.ratings, [.init(label: "Quality", value: 5), .init(label: "Speed", value: 5)])
        XCTAssertEqual(tts[kokoro]?.isRecommended, true)
        let soprano = tts["mlx-community/Soprano-80M-bf16"]
        XCTAssertEqual(soprano?.ratings.map(\.value), [4, 5])
        XCTAssertEqual(soprano?.isRecommended, false)
        XCTAssertEqual(soprano?.summary, "One expressive English voice; each reading of a text differs a little")

        // Not measured: no bars, the note only.
        let p40 = tts["gradium/phonon-40m-en"]
        XCTAssertEqual(p40?.ratings, [])
        XCTAssertNotNil(p40?.summary)
        // A plugin engine has no model to rate or describe.
        XCTAssertEqual(tts["fake"]?.ratings, [])
        XCTAssertNil(tts["fake"]?.summary)
        XCTAssertEqual(tts["fake"]?.isRecommended, false)

        let stt = rows(kind: .stt, engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3")
        XCTAssertEqual(stt["mlx-community/parakeet-tdt-0.6b-v3"]?.isRecommended, true)

        // Only one of the two measured, or a value off the scale: only the bars that hold.
        var state = FakeModelState.sample
        state.modify(kokoro) { ($0.quality, $0.speed, $0.note) = (nil, 9, "") }
        let partial = rows(state, kind: .tts, engine: "kokoro", model: kokoro)[kokoro]
        XCTAssertEqual(partial?.ratings, [])
        state.modify(kokoro) { $0.speed = 3 }
        XCTAssertEqual(rows(state, kind: .tts, engine: "kokoro", model: kokoro)[kokoro]?.ratings, [.init(label: "Speed", value: 3)])
        XCTAssertNil(rows(state, kind: .tts, engine: "kokoro", model: kokoro)[kokoro]?.summary)

        XCTAssertEqual(ModelRow.ratingsHelp, "Measured on an Apple M1 Max; see docs/benchmarks.md")
        XCTAssertEqual(
            ModelRow.benchmarks.absoluteString, "https://github.com/JulianBeaulieu/voiced/blob/main/docs/benchmarks.md")
    }

    func testSelectionForKind() {
        let settings = FakeState.sample.settings
        XCTAssertEqual(settings.selection(for: .stt).engine, "parakeet")
        XCTAssertEqual(settings.selection(for: .stt).model, "mlx-community/parakeet-tdt-0.6b-v3")
        XCTAssertEqual(settings.selection(for: .tts).engine, "kokoro")
        XCTAssertEqual(settings.selection(for: .tts).model, "mlx-community/Kokoro-82M-bf16")
    }

    func testAServiceIsReadyOnceWarmAndNotRestarting() {
        let warm = ServiceStatus(state: .running, warm: true)
        XCTAssertTrue(ModelRow.isReady(warm, restarting: false))
        XCTAssertFalse(ModelRow.isReady(warm, restarting: true))
        XCTAssertFalse(ModelRow.isReady(ServiceStatus(state: .running, warm: false), restarting: false))
        XCTAssertFalse(ModelRow.isReady(ServiceStatus(state: .starting), restarting: false))
        XCTAssertTrue(ModelRow.isReady(nil, restarting: false), "no status yet: nothing to wait for")
    }

    func testEachStateOffersItsActions() {
        let tts = rows(kind: .tts, engine: "kokoro", model: kokoro)
        let inUse = tts[kokoro]
        XCTAssertNil(inUse?.downloadTitle)
        XCTAssertEqual(inUse?.canUse, false)
        XCTAssertEqual(inUse?.showsDelete, true)
        XCTAssertEqual(inUse?.canDelete, false, "Delete is disabled on the model in use")
        XCTAssertEqual(inUse?.note, "In use. Choose another model first.")
        XCTAssertEqual(inUse?.sizeLine, "389.4 MB on disk")
        XCTAssertEqual(inUse?.deleteOldTitle, "Delete Old Version (341 MB)", "offered on the row in use too")

        let old = tts["mlx-community/Soprano-80M-bf16"]
        XCTAssertEqual(old?.downloadTitle, "Update (220.6 MB)")
        XCTAssertEqual(old?.canUse, true)
        XCTAssertEqual(old?.sizeLine, "220.6 MB download")
        XCTAssertNotNil(old?.deleteOldTitle)

        let new = tts["gradium/phonon-31m-en"]
        XCTAssertEqual(new?.downloadTitle, "Download")
        XCTAssertEqual(new?.canUse, true)
        XCTAssertEqual(new?.canDelete, false)
        XCTAssertNil(new?.note)
        XCTAssertNil(new?.deleteOldTitle)

        XCTAssertEqual(tts["gradium/phonon-40m-en"]?.downloadTitle, "Download")

        var downloaded = FakeModelState.sample
        downloaded.setDownloaded("gradium/phonon-31m-en", true)
        let row = rows(downloaded, kind: .tts, engine: "kokoro", model: kokoro)["gradium/phonon-31m-en"]
        XCTAssertEqual(row?.state, .downloaded)
        XCTAssertNil(row?.downloadTitle)
        XCTAssertEqual(row?.canUse, true)
        XCTAssertEqual(row?.canDelete, true)

        let missing = rows(FakeScenario.missingModel.initialModels, kind: .tts, engine: "kokoro", model: kokoro)[kokoro]
        XCTAssertEqual(missing?.downloadTitle, "Download")
        XCTAssertEqual(missing?.canUse, false)

        let downloading = rows(kind: .tts, engine: "kokoro", model: kokoro, downloading: "gradium/phonon-31m-en")
        XCTAssertNil(downloading["gradium/phonon-31m-en"]?.downloadTitle)
        XCTAssertEqual(downloading["gradium/phonon-31m-en"]?.canUse, false)
    }

    func testAnEngineThatIsNotInstalledCannotBeUsed() throws {
        var state = FakeModelState.sample
        let index = try XCTUnwrap(state.list.engines.firstIndex { $0.name == "soprano" })
        state.list.engines[index].installed = false
        XCTAssertEqual(rows(state, kind: .tts, engine: "kokoro", model: kokoro)["mlx-community/Soprano-80M-bf16"]?.canUse, false)
    }

    /// Moonshine on macOS 14: its rows show why under the engine and offer neither Download
    /// nor Use; a download already on disk can still be deleted.
    func testAnEngineThatCannotRunHereOffersNoDownloadOrUse() throws {
        let engine = try XCTUnwrap(FakeModelState.sample.list.engines.first { $0.name == "moonshine" })
        XCTAssertFalse(engine.available)
        XCTAssertEqual(engine.unavailableNote, "Moonshine needs macOS 15 or later, so it cannot run on this Mac.")
        let row = try XCTUnwrap(rows(kind: .stt, engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3")[moonshineSmall])
        XCTAssertEqual(row.state, .notDownloaded)
        XCTAssertNil(row.downloadTitle)
        XCTAssertFalse(row.canUse)

        var state = FakeModelState.sample
        state.setDownloaded(moonshineSmall, true)
        let downloaded = rows(state, kind: .stt, engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3")[moonshineSmall]
        XCTAssertEqual(downloaded?.canUse, false)
        XCTAssertEqual(downloaded?.canDelete, true)

        let nemotronRow = rows(kind: .stt, engine: "parakeet", model: "mlx-community/parakeet-tdt-0.6b-v3")[nemotron]
        XCTAssertEqual(nemotronRow?.downloadTitle, "Download", "an engine that can run here is unchanged")
        XCTAssertEqual(nemotronRow?.canUse, true)
    }

    /// A voiced from before `available` lists every engine as available; a reason of another
    /// type does not lose the engine, and an empty one still says it cannot run.
    func testAvailabilityIsOptional() throws {
        let older = try decodeJSON(ModelEngine.self, #"{"name": "e", "kind": "stt", "models": []}"#)
        XCTAssertTrue(older.available)
        XCTAssertEqual(older.unavailableReason, "")
        let odd = try decodeJSON(
            ModelEngine.self, #"{"name": "e", "label": "E", "kind": "stt", "available": false, "unavailable_reason": 3}"#)
        XCTAssertFalse(odd.available)
        XCTAssertEqual(odd.unavailableNote, "E cannot run on this Mac.")
    }

    func testTheDeleteConfirmationNamesWhatGoes() throws {
        let tts = rows(kind: .tts, engine: "parakeet", model: "x")
        let kokoroDelete = try XCTUnwrap(tts[kokoro]?.deleteConfirmation())
        XCTAssertEqual(kokoroDelete.title, "Delete Kokoro-82M-bf16?")
        XCTAssertEqual(
            kokoroDelete.message,
            "This frees 389.4 MB. Downloading it again takes 355.3 MB. This model is also cached as main, "
                + "perhaps by another app; that app downloads it again if it needs it.")

        // Phonon's last downloaded model takes the voices with it; with the other one
        // downloaded too, it does not.
        var state = FakeModelState.sample
        state.setDownloaded("gradium/phonon-31m-en", true)
        state.setDownloaded("gradium/phonon-voices", true)
        let last = try XCTUnwrap(rows(state, kind: .tts, engine: "kokoro", model: kokoro)["gradium/phonon-31m-en"]?.deleteConfirmation())
        XCTAssertEqual(
            last.message,
            "This frees 74.6 MB. Downloading it again takes 65 MB. Phonon's shared files (gradium/phonon-voices) "
                + "are deleted with it.")
        state.setDownloaded("gradium/phonon-40m-en", true)
        let notLast = try XCTUnwrap(rows(state, kind: .tts, engine: "kokoro", model: kokoro)["gradium/phonon-31m-en"]?.deleteConfirmation())
        XCTAssertEqual(notLast.message, "This frees 65 MB. Downloading it again takes 65 MB.")

        let old = try XCTUnwrap(tts[kokoro]?.deleteOldConfirmation())
        XCTAssertEqual(old.title, "Delete the old version of Kokoro-82M-bf16?")
        XCTAssertEqual(old.message, "This frees 341 MB. The current version stays.")
        XCTAssertNil(tts["gradium/phonon-31m-en"]?.deleteOldConfirmation())
    }

    func testTheDeleteOutputDecodes() throws {
        XCTAssertEqual(
            try decodeJSON(
                ModelDeleteResult.self,
                #"{"model": "m", "deleted": [{"repo": "m", "revision": "r", "bytes": 5}], "freed": 5, "notes": ["n"]}"#),
            .deleted(freed: 5, notes: ["n"]))
        XCTAssertEqual(
            try decodeJSON(ModelDeleteResult.self, #"{"error": {"code": "in_use", "message": "used"}}"#),
            .failed(code: "in_use", message: "used"))
        XCTAssertThrowsError(try decodeJSON(ModelDeleteResult.self, #"{"something": "else"}"#))
    }

    func testTheFooterAndTheHomeAssistantHint() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let list = ModelList(cache: "\(home)/.cache/huggingface/hub", totalBytes: 3_460_000_000, engines: [])
        XCTAssertEqual(list.usageLine, "voiced's models use 3.46 GB in ~/.cache/huggingface/hub.")
        XCTAssertEqual(
            ModelList(cache: "/Volumes/Models/hub", totalBytes: 0, engines: []).usageLine,
            "voiced's models use Zero KB in /Volumes/Models/hub.")
        XCTAssertEqual(
            ModelMessage.homeAssistantHint(engine: "Phonon"),
            "In Home Assistant, pick a Phonon voice under Settings > Voice assistants > your assistant > "
                + "Text-to-speech > Voice. If the new voices are missing, reload voiced under Devices & services.")
    }

    // MARK: - Messages

    func testErrorCodesAreDecodedIntoWhatToDo() {
        let model = CatalogueModel(id: "gradium/phonon-40m-en", license: "CC-BY-NC-4.0", gated: true, downloadBytes: 1, downloaded: false)
        let token = ModelMessage.failure(code: "token_missing", message: "401", model: model)
        XCTAssertEqual(token, .needsToken(model))
        XCTAssertEqual(token.command, "hf auth login")
        XCTAssertEqual(token.link?.absoluteString, "https://huggingface.co/gradium/phonon-40m-en")
        XCTAssertEqual(ModelMessage.failure(code: "gated_terms", message: "403", model: model), .needsToken(model))
        XCTAssertTrue(ModelMessage.failure(code: "token_invalid", message: "", model: model).text.contains("rejected the token"))
        XCTAssertTrue(ModelMessage.failure(code: "network", message: "", model: nil).text.contains("could not be reached"))
        XCTAssertEqual(
            ModelMessage.failure(code: "disk", message: "Needs 2.1 GB free.", model: nil).text,
            "Not enough free disk space. Needs 2.1 GB free.")
        XCTAssertEqual(
            ModelMessage.failure(code: "busy", message: "", model: nil).text,
            "voiced is busy installing, updating or changing models; try again when it ends.")
        XCTAssertEqual(ModelMessage.failure(code: "in_use", message: "", model: nil).text, "In use. Choose another model first.")
        XCTAssertEqual(ModelMessage.failure(code: "failed", message: "No such repo.", model: nil).text, "No such repo.")
        XCTAssertEqual(ModelMessage.failure(code: "from_the_future", message: "Something new.", model: nil).text, "Something new.")
    }

    // MARK: - missing_model

    func testAMissingModelIsItsOwnStateWithTheModelToDownload() throws {
        let status = try decodeJSON(
            StatusPayload.self,
            #"{"installed": true, "services": {"stt": {"state": "running", "warm": true}, "tts": {"state": "missing_model", "pid": 7, "engine": "kokoro", "model": "mlx-community/Kokoro-82M-bf16", "health": {"status": "missing_model"}}}}"#)
        XCTAssertEqual(status[.tts]?.state, .missingModel)
        let dashboard = DashboardReducer.reduce(connection: .connected(version: nil), status: status, socketPath: "/s")
        XCTAssertEqual(dashboard.headline, "Model not downloaded")
        XCTAssertEqual(dashboard.health, .attention)
        XCTAssertFalse(dashboard.isBusy)
        let row = try XCTUnwrap(dashboard.rows.first { $0.kind == .tts })
        XCTAssertEqual(row.stateLabel, "Model not downloaded")
        XCTAssertEqual(row.tone, .bad)
        XCTAssertEqual(row.missingModel, "mlx-community/Kokoro-82M-bf16")
        XCTAssertNil(dashboard.rows.first { $0.kind == .stt }?.missingModel)
        XCTAssertEqual(MenuBarIconStatus(dashboard: dashboard, installing: false).state, .error)
    }

    func testTheIconIsBusyWhileAModelDownloads() {
        let running = Dashboard(health: .running, headline: "Running")
        XCTAssertEqual(MenuBarIconStatus(dashboard: running, installing: false).state, .idle)
        let downloading = MenuBarIconStatus(dashboard: running, installing: false, downloading: true)
        XCTAssertEqual(downloading.state, .busy)
        XCTAssertEqual(downloading.accessibilityLabel, "voiced: Downloading a model")
    }

    // MARK: - Use

    func testUseSendsTheEngineAndTheModelTogether() {
        let current = FakeState.sample.settings
        let engines = FakeState.sample.engines
        XCTAssertEqual(
            SettingsEdit.ttsEngine("phonon", model: "gradium/phonon-40m-en").arguments(current: current, engines: engines),
            ["tts.engine": "phonon", "tts.model": "gradium/phonon-40m-en", "tts.voice": "default"])
        XCTAssertEqual(
            SettingsEdit.sttEngine("parakeet", model: "other").arguments(current: current, engines: engines),
            ["stt.engine": "parakeet", "stt.model": "other"], "the same engine with another model sends both")
        XCTAssertEqual(
            SettingsEdit.ttsEngine("kokoro", model: "mlx-community/Kokoro-82M-bf16").arguments(current: current, engines: engines),
            [:])
    }
}
