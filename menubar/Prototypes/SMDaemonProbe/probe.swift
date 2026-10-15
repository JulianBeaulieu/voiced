// Throwaway: registers, reports and unregisters an SMAppService LaunchDaemon from inside a
// bundle, to measure what ADR 0018 needs. Not part of the app. See README.md.
import Foundation
import ServiceManagement

let plistName = "io.github.julianbeaulieu.voiced.smprobe.plist"
let service = SMAppService.daemon(plistName: plistName)

func describe(_ status: SMAppService.Status) -> String {
    switch status {
    case .notRegistered: return "notRegistered"
    case .enabled: return "enabled"
    case .requiresApproval: return "requiresApproval"
    case .notFound: return "notFound"
    @unknown default: return "unknown(\(status.rawValue))"
    }
}

let command = CommandLine.arguments.dropFirst().first ?? "status"
print("bundle: \(Bundle.main.bundlePath)")
print("before: \(describe(service.status))")
do {
    switch command {
    case "register": try service.register()
    case "unregister": try service.unregister()
    case "settings": SMAppService.openSystemSettingsLoginItems()
    default: break
    }
    print("\(command): ok")
} catch let error as NSError {
    print("\(command): error domain=\(error.domain) code=\(error.code) \(error.localizedDescription)")
}
print("after: \(describe(service.status))")
