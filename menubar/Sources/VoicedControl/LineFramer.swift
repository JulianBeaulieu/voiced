import Foundation

/// Splits a byte stream into newline-terminated lines. The control protocol sends one JSON
/// object per line; a read can end in the middle of a line or carry several at once.
public struct LineFramer: Sendable {
    /// A line longer than this means the peer is not speaking the protocol.
    public static let defaultMaximumLineLength = 4 * 1024 * 1024

    public enum FramingError: Error, Equatable {
        case lineTooLong(Int)
    }

    private var buffer = Data()
    private let maximumLineLength: Int

    public init(maximumLineLength: Int = LineFramer.defaultMaximumLineLength) {
        self.maximumLineLength = maximumLineLength
    }

    /// Bytes received but not yet terminated by a newline.
    public var pendingByteCount: Int { buffer.count }

    /// Appends bytes and returns every complete line, without its terminator. A trailing
    /// carriage return is dropped and blank lines are skipped.
    public mutating func append(_ data: Data) throws -> [Data] {
        buffer.append(data)
        var lines: [Data] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            var line = buffer[buffer.startIndex..<newline]
            buffer = Data(buffer[buffer.index(after: newline)...])
            if line.last == 0x0D { line = line.dropLast() }
            if !line.isEmpty { lines.append(Data(line)) }
        }
        if buffer.count > maximumLineLength {
            let length = buffer.count
            buffer.removeAll()
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
