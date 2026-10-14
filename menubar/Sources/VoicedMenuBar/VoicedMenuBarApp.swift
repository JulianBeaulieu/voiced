import AppKit
import SwiftUI
import VoicedMenuBarUI

@main
struct VoicedMenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model: AppModel
    @State private var loginItem = LoginItem()

    init() {
        let model = AppModel()
        model.start()
        _model = State(initialValue: model)
    }

    var body: some Scene {
        MenuBarExtra {
            PopoverView(model: model)
        } label: {
            MenuBarIcon(dashboard: model.dashboard)
                .background(SettingsOpener { delegate.openSettings = $0 })
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(model: model, loginItem: loginItem)
        }
        .windowResizability(.contentSize)
    }
}

/// Hands SwiftUI's `openSettings` action to the app delegate, which has no environment.
private struct SettingsOpener: View {
    let register: (@escaping () -> Void) -> Void
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Color.clear.onAppear {
            let action = openSettings
            register { action() }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var openSettings: (() -> Void)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The bundle sets LSUIElement; this covers `swift run`, which has no Info.plist.
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    /// Launching the app again while it runs (from Finder, Spotlight or `open`) opens the
    /// Settings window: on a crowded menu bar macOS may hide the icon, and this is the way
    /// back in.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        NSApplication.shared.activate()
        openSettings?()
        return false
    }
}
