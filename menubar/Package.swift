// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VoicedMenuBar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "voiced-menubar", targets: ["VoicedMenuBar"]),
        .executable(name: "voiced-fake-control", targets: ["FakeControlServer"]),
    ],
    targets: [
        // The control protocol: framing, client, payload models, view state. No UI.
        .target(name: "VoicedControl"),
        // SwiftUI views and the observable model that drives them.
        .target(name: "VoicedMenuBarUI", dependencies: ["VoicedControl"]),
        // The app entry point; kept tiny so everything else is testable.
        .executableTarget(name: "VoicedMenuBar", dependencies: ["VoicedMenuBarUI"]),
        // A scripted control server for tests and for running the app without voiced.
        .target(name: "FakeControlServerKit", dependencies: ["VoicedControl"]),
        .executableTarget(name: "FakeControlServer", dependencies: ["FakeControlServerKit"]),
        // Renders the popover in each state to PNG files, for documentation.
        .executableTarget(
            name: "MenuBarSnapshots",
            dependencies: ["VoicedMenuBarUI", "FakeControlServerKit"]
        ),
        .testTarget(
            name: "VoicedControlTests",
            dependencies: ["VoicedControl", "VoicedMenuBarUI", "FakeControlServerKit"]
        ),
    ]
)
