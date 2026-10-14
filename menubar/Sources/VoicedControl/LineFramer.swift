import Foundation

/// Splits a byte stream into newline-terminated lines. The control protocol sends one JSON
/// object per line; a read can end in the middle of a line or carry several at once.
public struct LineFramer: Sendable {
    /// A line longer than this means the peer is not speaking the protocol. The server caps
    /// its own lines at 64 KiB.
    public static let defaultMaximumLineLength = 1024 * 1024

    public enum FramingError: Error, Equatable {
        case lineTooLong(Int)
    }

    private var buffer = Data()
    /// Bytes of `buffer` already searched for a newline, so a long line is scanned once.
    private var scanned = 0
    private let maximumLineLength: Int

    public init(maximumLineLength: Int = LineFramer.defaultMaximumLineLength) {
        self.maximumLineLength = maximumLineLength
    }

    /// Appends bytes and returns every complete line, without its terminator. A trailing
    /// carriage return is dropped and blank lines are skipped.
    public mutating func append(_ data: Data) throws -> [Data] {
        buffer.append(data)
        var lines: [Data] = []
        var start = buffer.startIndex
        var searchFrom = buffer.startIndex + scanned
        while let newline = buffer[searchFrom...].firstIndex(of: 0x0A) {
            var line = buffer[start..<newline]
            if line.last == 0x0D { line = line.dropLast() }
            if !line.isEmpty { lines.append(Data(line)) }
            start = buffer.index(after: newline)
            searchFrom = start
        }
        if start != buffer.startIndex { buffer = Data(buffer[start...]) }
        scanned = buffer.count
        if buffer.count > maximumLineLength {
            let length = buffer.count
            buffer.removeAll()
            scanned = 0
            throw FramingError.lineTooLong(length)
        }
        return lines
    }

    /// Encodes one message as a line.
    public static func frame(_ message: JSONValue) throws -> Data {
        var data = try message.serialized()
        data.append(0x0A)
        return data
    }
}
