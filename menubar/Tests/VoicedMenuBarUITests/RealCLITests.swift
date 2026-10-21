import Foundation
import TestSupport
import XCTest

@testable import VoicedMenuBarUI

/// Against a real `voiced` through the real runner, skipped unless `VOICED_REAL_CLI` names
/// one: `VOICED_REAL_CLI=~/.voiced/bin/voiced swift test --filter RealCLITests`. It downloads
/// Phonon 31m (65 MB) into the Hugging Face cache, cancelling the first try.
@MainActor
final class RealCLITests: XCTestCase {
    func testListDownloadAndCancelWithTheRealCommand() async throws {
        guard let path = ProcessInfo.processInfo.environment["VOICED_REAL_CLI"] else {
            throw XCTSkip("set VOICED_REAL_CLI to a voiced with `voiced models`")
        }
        let id = "gradium/phonon-31m-en"
        let models = ModelManager(
            app: AppModel(socketPath: temporarySocketPath()), runner: SystemInstallRunner(),
            executable: (path as NSString).expandingTildeInPath)
        await models.refresh()
        XCTAssertNil(models.listError)
        XCTAssertNotNil(models.list?.model(id))

        let cancelled = Task { await models.download(id) }
        _ = await eventually(timeout: .seconds(60)) { models.download?.progress != nil }
        models.cancelDownload()
        let first = await cancelled.value
        XCTAssertNil(models.messages[id], "a cancel is not a failure")
        if !first { XCTAssertEqual(models.list?.model(id)?.model.downloaded, false) }

        let done = await models.download(id)
        XCTAssertTrue(done, "\(models.messages[id]?.text ?? "")")
        XCTAssertEqual(models.list?.model(id)?.model.downloaded, true)
    }
}
