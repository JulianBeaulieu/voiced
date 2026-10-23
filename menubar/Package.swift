// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "VoicedMenuBar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "voiced-menubar", targets: ["VoicedMenuBar"]),
        .executable(name: "voiced-fake-control", targets: ["FakeControlServer"]),
    ],
    dependencies: [
        // The app's own updates (ADR 0034). Pinned exactly: Package.resolved records the
        // commit, and Sparkle's binary framework is checked against its checksum.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.0")
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
        // The app entry point; kept tiny so everything else is testable. Only it links Sparkle:
        // the rest of the app sees the updater through `AppUpdater`.
        .executableTarget(
            name: "VoicedMenuBar",
            dependencies: ["VoicedMenuBarUI", .product(name: "Sparkle", package: "Sparkle")]
        ),
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
