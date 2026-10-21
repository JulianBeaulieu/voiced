import FakeControlServerKit
import Foundation
import VoicedControl

// voiced-fake-control: serves the control protocol with scripted state so the menu bar app
// can be run and looked at without voiced. Nothing here touches the network.

let usage = """
    usage: voiced-fake-control [--socket PATH] [--scenario NAME] [--status-interval SECONDS]
           voiced-fake-control models list|download|delete ...

    Serves the voiced control protocol on a Unix socket with scripted state.
    Default socket: $TMPDIR/voiced-fake.sock. Then run the app against it:

        VOICED_CONTROL_SOCKET=<socket> swift run voiced-menubar

    The fake's models live in <socket>.models.json. Run as `voiced-fake-control models ...`,
    it is a fake `voiced models` that changes them; a debug build of the app runs it in place
    of voiced with VOICED_EXECUTABLE=.build/debug/voiced-fake-control.

    Scenarios:
    \(FakeScenario.allCases.map { "  \($0.rawValue.padding(toLength: 16, withPad: " ", startingAt: 0))\($0.summary)" }.joined(separator: "\n"))
    """

setvbuf(stdout, nil, _IOLBF, 0)

var socketPath = (NSTemporaryDirectory() as NSString).appendingPathComponent("voiced-fake.sock")

// The fake `voiced models`: the app runs it with its own environment, so the socket path,
// and with it the models file, is the app's VOICED_CONTROL_SOCKET.
if CommandLine.arguments.dropFirst().first == "models" {
    let models = FakeModels(socketPath: ProcessInfo.processInfo.environment["VOICED_CONTROL_SOCKET"] ?? socketPath)
    // Cancel is SIGTERM: like the real command, an error event and exit 143 at once.
    signal(SIGTERM, SIG_IGN)
    let cancel = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    // Off the main actor: the main thread is busy writing the download.
    cancel.setEventHandler { @Sendable in
        FileHandle.standardOutput.write(Data(#"{"event": "error", "code": "failed", "message": "Cancelled."}"#.utf8 + [0x0A]))
        _exit(143)
    }
    cancel.resume()
    let status = models.run(
        Array(CommandLine.arguments.dropFirst(2)), output: { print($0) },
        pause: { Thread.sleep(forTimeInterval: 0.5) })
    exit(status)
}

/// Bad arguments: usage on stderr, exit status 2.
func fail() -> Never {
    FileHandle.standardError.write(Data((usage + "\n").utf8))
    exit(2)
}

var scenario = FakeScenario.running
var options = FakeControlServer.Options()

var arguments = CommandLine.arguments.dropFirst().makeIterator()
while let argument = arguments.next() {
    switch argument {
    case "--socket":
        guard let value = arguments.next() else { fail() }
        socketPath = value
    case "--scenario":
        guard let value = arguments.next(), let parsed = FakeScenario(rawValue: value) else { fail() }
        scenario = parsed
    case "--status-interval":
        guard let value = arguments.next().flatMap(Double.init), value > 0 else { fail() }
        options.statusInterval = value
    case "-h", "--help":
        print(usage)
        exit(0)
    default:
        fail()
    }
}

options.modelsFile = FakeModels(socketPath: socketPath).file
let server = FakeControlServer(socketPath: socketPath, scenario: scenario, options: options)
do {
    try server.start()
} catch {
    FileHandle.standardError.write(Data("cannot listen on \(socketPath): \(error)\n".utf8))
    exit(1)
}
print("serving scenario \(scenario.rawValue) on \(socketPath)")
print("run the app with: VOICED_CONTROL_SOCKET=\(socketPath) VOICED_EXECUTABLE=\(CommandLine.arguments[0]) swift run voiced-menubar")

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
