import AppKit
import SwiftUI
import VoicedMenuBarUI

@main
struct VoicedMenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model: AppModel
    @State private var models: ModelManager
    @State private var loginItem = LoginItem()
    @State private var launch: LaunchChecks
    @State private var whatsNew: WhatsNew

    init() {
        let model = AppModel()
        model.start()
        _model = State(initialValue: model)
        let models = ModelManager(app: model, runner: Install.runner)
        _models = State(initialValue: models)
        Install.models = models
        let resources = InstallResources.inBundle()
        let installSource = DevelopmentOverride.installSource()
        Install.installer = resources.map {
            var configuration = InstallerConfiguration(resources: $0)
            configuration.installSource = installSource
            return Installer(configuration: configuration, runner: Install.runner, model: model)
        }
        let launch = LaunchChecks(model: model, manifest: resources?.manifest, installSource: installSource)
        Task { await launch.refresh() }
        _launch = State(initialValue: launch)
        _whatsNew = State(initialValue: WhatsNew(notes: WhatsNew.notes(), version: resources?.manifest.version))
    }

    var body: some Scene {
        MenuBarExtra {
            PopoverView(
                model: model, models: models, canInstall: Install.installer != nil, launch: launch,
                update: Install.installer.map { installer in { installer.update() } },
                repair: Install.installer.map { installer in { installer.repair() } })
        } label: {
            MenuBarIcon(
                dashboard: model.dashboard, installing: Install.installer?.isWorking ?? false,
                downloading: models.isDownloading)
                .background(SettingsOpener { delegate.openSettings = $0 })
                .background(WhatsNewOpener(launch: launch, whatsNew: whatsNew))
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(
                model: model, models: models, loginItem: loginItem, launch: launch, installer: Install.installer)
        }
        .windowResizability(.contentSize)

        Window("What's New in voiced", id: WhatsNew.windowID) {
            if let notes = whatsNew.notes {
                WhatsNewView(notes: notes)
            }
        }
        .windowResizability(.contentSize)

        Window("Install voiced", id: InstallerView.windowID) {
            if let installer = Install.installer {
                InstallerWindow(installer: installer, loginItem: loginItem)
            }
        }
        .windowResizability(.contentSize)
    }
}

/// The installer, when the bundle carries what it needs (ADR 0018), the model rows (ADR 0022),
/// which the app delegate asks before quitting, and the runner whose children are terminated
/// when the app quits.
@MainActor
enum Install {
    static let runner = SystemInstallRunner()
    static var installer: Installer?
    static var models: ModelManager?
}

/// Opens "What's new" once the services run this app's pins after running others. It lives
/// in the menu bar label, the one view that exists for the app's whole life.
private struct WhatsNewOpener: View {
    let launch: LaunchChecks
    let whatsNew: WhatsNew
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Color.clear
            .onChange(of: launch.installMatch, initial: true) { whatsNew.observe(launch.installMatch) }
            .onChange(of: whatsNew.isPending, initial: true) {
                guard whatsNew.isPending else { return }
                whatsNew.shown()
                NSApplication.shared.activate()
                openWindow(id: WhatsNew.windowID)
            }
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

    /// Quitting while a model downloads asks first; the next download resumes the files.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        MainActor.assumeIsolated {
            guard Install.models?.isDownloading == true else { return .terminateNow }
            let alert = NSAlert()
            alert.messageText = "Quit while a model downloads?"
            alert.informativeText = "Quitting stops the download. Downloading it again later resumes where it stopped."
            alert.addButton(withTitle: "Keep Downloading")
            alert.addButton(withTitle: "Quit")
            NSApplication.shared.activate()
            return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
        }
    }

    /// Quitting mid-install stops the installer's children (SIGTERM to their process groups,
    /// then SIGKILL after 5 s); the next launch continues from what is on disk.
    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            Install.installer?.cancel()
            Install.runner.terminateAll()
        }
    }

    /// Launching the app again while it runs (from Finder, Spotlight or `open`) opens the
    /// Settings window: on a crowded menu bar macOS may hide the icon, and this is the way
    /// back in.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if let openSettings { SettingsWindow.show(openSettings) }
        return false
    }
}
