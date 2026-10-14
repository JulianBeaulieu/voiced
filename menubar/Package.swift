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
        // Renders both windows in each state to PNG files; UI pull requests attach these.
        .executableTarget(
            name: "MenuBarSnapshots",
            dependencies: ["VoicedMenuBarUI", "FakeControlServerKit"]
        ),
        // Helpers shared by both test targets.
        .target(
            name: "TestSupport",
            dependencies: ["VoicedControl", "FakeControlServerKit"],
            path: "Tests/TestSupport"
        ),
        .testTarget(
            name: "VoicedControlTests",
            dependencies: ["VoicedControl", "FakeControlServerKit", "TestSupport"]
        ),
        .testTarget(
            name: "VoicedMenuBarUITests",
            dependencies: ["VoicedControl", "VoicedMenuBarUI", "FakeControlServerKit", "TestSupport"]
        ),
    ]
)
