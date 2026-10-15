import Foundation
import TestSupport
import XCTest

@testable import VoicedControl

final class FramingTests: XCTestCase {
    func testSplitsLinesAcrossReads() throws {
        var framer = LineFramer()
        XCTAssertEqual(try framer.append(Data(#"{"a":"#.utf8)), [])
        let lines = try framer.append(Data("1}\n{\"b\":2}\n{\"c\"".utf8))
        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, [#"{"a":1}"#, #"{"b":2}"#])
        XCTAssertEqual(try framer.append(Data(":3}\n".utf8)).count, 1)
    }

    func testALongLineArrivingInPiecesIsScannedOnce() throws {
        var framer = LineFramer()
        let chunk = Data(repeating: 0x61, count: 64 * 1024)
        let start = ContinuousClock.now
        for _ in 0..<15 { XCTAssertEqual(try framer.append(chunk), []) }
        let lines = try framer.append(Data("\n".utf8))
        XCTAssertEqual(lines.first?.count, 15 * 64 * 1024)
        XCTAssertLessThan(ContinuousClock.now - start, .seconds(1))
    }

    func testDefaultLimitIsOneMebibyte() {
        var framer = LineFramer()
        XCTAssertThrowsError(try framer.append(Data(repeating: 0x61, count: 1024 * 1024 + 1)))
    }

    func testDropsCarriageReturnsAndBlankLines() throws {
        var framer = LineFramer()
        let lines = try framer.append(Data("{}\r\n\n\r\n[]\n".utf8))
        XCTAssertEqual(lines.map { String(decoding: $0, as: UTF8.self) }, ["{}", "[]"])
    }

    func testRefusesOverlongLines() {
        var framer = LineFramer(maximumLineLength: 8)
        XCTAssertThrowsError(try framer.append(Data("0123456789".utf8))) { error in
            XCTAssertEqual(error as? LineFramer.FramingError, .lineTooLong(10))
        }
        XCTAssertEqual(try framer.append(Data("{}\n".utf8)).count, 1, "usable after the error")
    }

    func testFrameIsOneLineOfCompactJSON() throws {
        let data = try LineFramer.frame(["id": 3, "op": "status", "args": [:]])
        XCTAssertEqual(String(decoding: data, as: UTF8.self), #"{"args":{},"id":3,"op":"status"}"# + "\n")
    }

    func testJSONValueKeepsBooleansAndNumbersApart() throws {
        let value = try JSONValue(data: Data(#"{"t":true,"one":1,"half":0.5,"n":null,"s":"x","a":[false]}"#.utf8))
        XCTAssertEqual(value["t"], .bool(true))
        XCTAssertEqual(value["one"], .number(1))
        XCTAssertEqual(value["one"]?.intValue, 1)
        XCTAssertEqual(value["half"], .number(0.5))
        XCTAssertNil(value["half"]?.intValue)
        XCTAssertEqual(value["n"], .null)
        XCTAssertEqual(value["a"], .array([.bool(false)]))
        XCTAssertEqual(try JSONValue(data: value.serialized()), value)
    }

    func testClassifiesResponsesAndEvents() {
        XCTAssertEqual(
            IncomingMessage(["id": 4, "ok": true, "result": ["accepted": true]]),
            .response(id: 4, result: .success(["accepted": true])))
        XCTAssertEqual(
            IncomingMessage([
                "id": 5, "ok": false,
                "error": ["code": "invalid_setting", "message": "tts.voice: no", "field": "tts.voice"],
            ]),
            .response(
                id: 5,
                result: .failure(RemoteError(code: "invalid_setting", message: "tts.voice: no", field: "tts.voice"))))
        XCTAssertEqual(
            IncomingMessage(["event": "something_new", "data": ["x": 1]]),
            .event(name: "something_new", data: ["x": 1]))
        XCTAssertNil(IncomingMessage(["id": "not-an-int", "ok": true]))
        XCTAssertNil(IncomingMessage([1, 2]))
    }

    func testRemoteErrorsLeadWithTheField() {
        XCTAssertEqual(
            RemoteError(code: "invalid_setting", message: "must be greater than 0", field: "tts.speed").displayMessage,
            "tts.speed: must be greater than 0")
        XCTAssertEqual(
            RemoteError(code: "invalid_setting", message: "tts.speed: too fast", field: "tts.speed").displayMessage,
            "tts.speed: too fast")
        XCTAssertEqual(RemoteError(code: "internal", message: "").displayMessage, "internal")
    }

    func testUnknownEventsAndUndecodableKnownEventsAreKeptAsOther() {
        XCTAssertEqual(ControlEvent(name: "later", data: [:]), .other(name: "later", data: [:]))
        XCTAssertEqual(
            ControlEvent(name: "status", data: ["nonsense": true]),
            .other(name: "status", data: ["nonsense": true]))
    }
}
