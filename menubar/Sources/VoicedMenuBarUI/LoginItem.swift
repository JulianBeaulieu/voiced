import Foundation
import Observation
import ServiceManagement

/// Launch at login through `SMAppService`. Only works when running from the app bundle;
/// `swift run` starts a bare executable, which has nothing to register.
@MainActor
@Observable
public final class LoginItem {
    public private(set) var isEnabled = false
    public private(set) var needsApproval = false
    public var errorMessage: String?

    public let isAvailable: Bool

    public init(bundleURL: URL = Bundle.main.bundleURL) {
        isAvailable = bundleURL.pathExtension == "app"
        refresh()
    }

    public func refresh() {
        guard isAvailable else { return }
        let status = SMAppService.mainApp.status
        isEnabled = status == .enabled
        needsApproval = status == .requiresApproval
    }

    public func setEnabled(_ enabled: Bool) {
        guard isAvailable else { return }
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
        refresh()
    }

    public func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
