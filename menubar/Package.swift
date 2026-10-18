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
        // The privileged script's bytes are compiled into the executable (ADR 0018), never a
        // file in the bundle. Installer/voiced-privileged.sh is a symlink to the one in the
        // voiced package (src/voiced/launchd), which `voiced install` prints for sudo.
        .target(
            name: "VoicedMenuBarUI", dependencies: ["VoicedControl"],
            resources: [.embedInCode("Installer/voiced-privileged.sh")]
        ),
        // The app entry point; kept tiny so everything else is testable.
        .executableTarget(name: "VoicedMenuBar", dependencies: ["VoicedMenuBarUI"]),
        // A scripted control server for tests and for running the app without voiced.
        .target(name: "FakeControlServerKit", dependencies: ["VoicedControl"]),
        .executableTarget(name: "FakeControlServer", dependencies: ["FakeControlServerKit"]),
        // Renders every window in each state to PNG files; UI pull requests attach these.
        .executableTarget(
            name: "MenuBarSnapshots",
            dependencies: ["VoicedMenuBarUI", "FakeControlServerKit", "TestSupport"]
        ),
        // Helpers shared by both test targets and the snapshots, the fake install runner among them.
        .target(
            name: "TestSupport",
            dependencies: ["VoicedControl", "VoicedMenuBarUI", "FakeControlServerKit"],
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
