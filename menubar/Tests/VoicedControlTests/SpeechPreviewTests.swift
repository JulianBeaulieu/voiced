import FakeControlServerKit
import Foundation
import XCTest

@testable import VoicedControl

final class SpeechSpeedTests: XCTestCase {
    func testStepsByATenthWithinTheRange() {
        XCTAssertEqual(SpeechSpeed.stepped(1, up: true), 1.1)
        XCTAssertEqual(SpeechSpeed.stepped(1, up: false), 0.9)
        XCTAssertEqual(SpeechSpeed.stepped(1.2, up: true), 1.3, "1.2 is on the grid despite binary")
        XCTAssertEqual(SpeechSpeed.stepped(1.9, up: true), 2)
        XCTAssertEqual(SpeechSpeed.stepped(2, up: true), 2)
        XCTAssertEqual(SpeechSpeed.stepped(0.5, up: false), 0.5)
    }

    func testAValueSetByHandMovesToTheGridOrTheNearestEnd() {
        XCTAssertEqual(SpeechSpeed.stepped(1.25, up: true), 1.3)
        XCTAssertEqual(SpeechSpeed.stepped(1.25, up: false), 1.2)
        XCTAssertEqual(SpeechSpeed.stepped(3, up: false), 2)
        XCTAssertEqual(SpeechSpeed.stepped(0.3, up: true), 0.5)
        XCTAssertFalse(SpeechSpeed.canStep(3, up: true))
        XCTAssertTrue(SpeechSpeed.canStep(3, up: false))
        XCTAssertFalse(SpeechSpeed.canStep(0.5, up: false))
    }
}

final class PreviewLineTests: XCTestCase {
    func testTheVoiceSaysItsNameInItsLanguage() {
        let kokoro = FakeState.sample.engines.ttsEngine(named: "kokoro")
        XCTAssertEqual(PreviewLine.text(for: "af_heart", of: kokoro), "Hi, I'm Heart.")
        XCTAssertEqual(PreviewLine.text(name: "Léa", language: "fr-FR"), "Bonjour, je m'appelle Léa.")
        XCTAssertEqual(PreviewLine.text(name: "Hans", language: "de"), "Hallo, ich bin Hans.")
        XCTAssertEqual(PreviewLine.text(name: "Xiaoxiao", language: "zh_CN"), "你好，我是Xiaoxiao。")
    }

    func testEveryShippedLanguageHasALine() {
        // The languages of the voices in `engines.list` today (Kokoro, Marvis, OmniVoice, Phonon,
        // Pocket and the others), plus Kokoro's beyond English.
        for language in ["en", "de", "fr", "es", "zh", "apc", "ja", "hi", "it", "pt"] {
            XCTAssertNotNil(PreviewLine.lines[language], language)
        }
    }

    func testALanguageWithoutALineSpeaksEnglish() {
        XCTAssertEqual(PreviewLine.text(name: "Olga", language: "ru-RU"), "Hi, I'm Olga.")
        XCTAssertEqual(PreviewLine.text(name: "Olga", language: nil), "Hi, I'm Olga.")
    }

    func testAVoiceWithoutANameOfItsOwn() {
        let engines = FakeState.sample.engines
        XCTAssertEqual(PreviewLine.text(for: "default", of: engines.ttsEngine(named: "soprano")), "Hi, this is my voice.")
        XCTAssertEqual(PreviewLine.text(for: "fake", of: engines.ttsEngine(named: "fake")), "Hi, this is my voice.")
        XCTAssertEqual(PreviewLine.text(for: "The Snow Queen", of: engines.ttsEngine(named: "phonon")), "Hi, I'm The Snow Queen.")
        XCTAssertEqual(PreviewLine.text(name: nil, language: "es"), "Hola, esta es mi voz.")
    }
}
