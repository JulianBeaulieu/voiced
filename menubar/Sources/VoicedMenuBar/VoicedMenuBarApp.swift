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
            PopoverView(model: model, loginItem: loginItem)
        } label: {
            MenuBarIcon(dashboard: model.dashboard)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // The bundle sets LSUIElement; this covers `swift run`, which has no Info.plist.
        NSApplication.shared.setActivationPolicy(.accessory)
    }
}
