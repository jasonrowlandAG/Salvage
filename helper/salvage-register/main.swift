// salvage-register — spike CLI for helper/DESIGN.md investigation.
//
// Calls SMAppService.daemon(plistName:).register() from inside the running
// app process. SMAppService resolves the plist relative to Bundle.main, so
// this only behaves correctly when it is launched from
// Salvage.app/Contents/MacOS/salvage-register (Bundle.main must resolve to
// the .app, not to a bare executable). Prints status to stdout so the spike
// harness can capture it without a GUI.
//
// Usage:
//   salvage-register status   — print current SMAppService.Status
//   salvage-register register — call register(), print result/error
//   salvage-register unregister — call unregister(), print result/error

import Foundation
import ServiceManagement

let plistName = "com.salvage.helper.plist"

func statusString(_ s: SMAppService.Status) -> String {
    switch s {
    case .notRegistered: return "notRegistered"
    case .enabled: return "enabled"
    case .requiresApproval: return "requiresApproval"
    case .notFound: return "notFound"
    @unknown default: return "unknown(\(s.rawValue))"
    }
}

print("Bundle.main.bundlePath = \(Bundle.main.bundlePath)")
print("Bundle.main.bundleIdentifier = \(Bundle.main.bundleIdentifier ?? "nil")")

let service = SMAppService.daemon(plistName: plistName)
let args = CommandLine.arguments

let command = args.count > 1 ? args[1] : "status"

switch command {
case "status":
    print("status = \(statusString(service.status))")

case "register":
    print("status before register = \(statusString(service.status))")
    do {
        try service.register()
        print("register() succeeded")
    } catch {
        print("register() FAILED: \(error)")
        let nsError = error as NSError
        print("  domain=\(nsError.domain) code=\(nsError.code) userInfo=\(nsError.userInfo)")
    }
    print("status after register = \(statusString(service.status))")

case "unregister":
    do {
        try service.unregister()
        print("unregister() succeeded")
    } catch {
        print("unregister() FAILED: \(error)")
    }

default:
    print("unknown command: \(command)")
    exit(1)
}
