/// Reconnect delays: doubling from `initial` up to `maximum`, reset once a connection has
/// proved itself.
public struct Backoff: Sendable, Equatable {
    public let initial: Duration
    public let maximum: Duration
    private var current: Duration?

    public init(initial: Duration = .milliseconds(500), maximum: Duration = .seconds(15)) {
        self.initial = initial
        self.maximum = maximum
    }

    public mutating func next() -> Duration {
        let delay = current.map { min($0 * 2, maximum) } ?? initial
        current = delay
        return delay
    }

    public mutating func reset() { current = nil }
}
