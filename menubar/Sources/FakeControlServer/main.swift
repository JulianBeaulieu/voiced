import FakeControlServerKit
import Foundation
import VoicedControl

// voiced-fake-control: serves the control protocol with scripted state so the menu bar app
// can be run and looked at without voiced. Nothing here touches the network.

let usage = """
    usage: voiced-fake-control [--socket PATH] [--scenario NAME] [--status-interval SECONDS]

    Serves the voiced control protocol on a Unix socket with scripted state.
    Default socket: $TMPDIR/voiced-fake.sock. Then run the app against it:

        VOICED_CONTROL_SOCKET=<socket> swift run voiced-menubar

    Scenarios:
    \(FakeScenario.allCases.map { "  \($0.rawValue.padding(toLength: 16, withPad: " ", startingAt: 0))\($0.summary)" }.joined(separator: "\n"))
    """

setvbuf(stdout, nil, _IOLBF, 0)

var socketPath = (NSTemporaryDirectory() as NSString).appendingPathComponent("voiced-fake.sock")
var scenario = FakeScenario.running
var options = FakeControlServer.Options()

var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--socket":
        guard let value = arguments.next() else { fatalError(usage) }
        socketPath = value
    case "--scenario":
        guard let value = arguments.next(), let parsed = FakeScenario(rawValue: value) else {
            print(usage)
            exit(2)
        }
        scenario = parsed
    case "--status-interval":
        guard let value = arguments.next().flatMap(Double.init), value > 0 else {
            print(usage)
            exit(2)
        }
        options.statusInterval = value
    case "-h", "--help":
        print(usage)
        exit(0)
    default:
        print(usage)
        exit(2)
    }
}

let server = FakeControlServer(socketPath: socketPath, scenario: scenario, options: options)
do {
    try server.start()
} catch {
    FileHandle.standardError.write(Data("cannot listen on \(socketPath): \(error)\n".utf8))
    exit(1)
}
print("serving scenario \(scenario.rawValue) on \(socketPath)")
print("run the app with: VOICED_CONTROL_SOCKET=\(socketPath) swift run voiced-menubar")

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let stopSources = [SIGINT, SIGTERM].map { number in
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler {
        server.stop()
        exit(0)
    }
    source.resume()
    return source
}
withExtendedLifetime(stopSources) { dispatchMain() }
