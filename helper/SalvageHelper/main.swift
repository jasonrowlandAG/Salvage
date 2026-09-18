// SalvageHelper — root daemon registered via SMAppService.
//
// Reads raw bytes off a block device on behalf of Salvage.app, which cannot do
// it itself (see helper/DESIGN.md for what that privilege does and does not
// buy). The raw-read mechanic is unchanged from the spike; what changed is who
// is allowed to ask and what they are allowed to ask for:
//
//   * transport is NSXPCConnection over a launchd MachService, not a
//     root:admin 0660 Unix socket that any admin-group process could open;
//   * every connection's peer is checked against a code requirement derived
//     from the helper's own signature, so only Salvage.app gets served;
//   * `device` must name a whole disk or partition that diskutil currently
//     reports as unmounted, top to bottom (see DeviceAllowlist.swift);
//   * `length` is bounded.
//
// Anything that fails those checks gets a negative errno and a log line; the
// device is never opened.

import Darwin
import Foundation
import SalvageHelperCore

let logPath = "/var/log/salvage-helper.log"

func log(_ message: String) {
    let timestamp = ISO8601DateFormatter().string(from: Date())
    let line = "[\(timestamp)] [uid=\(getuid())] \(message)\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
    if let handle = FileHandle(forWritingAtPath: logPath) {
        handle.seekToEndOfFile()
        handle.write(line.data(using: .utf8)!)
        handle.closeFile()
    } else {
        FileManager.default.createFile(atPath: logPath, contents: line.data(using: .utf8))
    }
}

func readRawDevice(path: String, offset: UInt64, length: Int) -> (Int32, Data) {
    let fd = open(path, O_RDONLY)
    if fd < 0 {
        let e = errno
        log("open(\(path)) failed errno=\(e) (\(String(cString: strerror(e))))")
        return (-e, Data())
    }
    defer { close(fd) }

    if lseek(fd, off_t(offset), SEEK_SET) < 0 {
        let e = errno
        log("lseek(\(path), \(offset)) failed errno=\(e) (\(String(cString: strerror(e))))")
        return (-e, Data())
    }

    var buffer = Data(count: length)
    let bytesRead: Int = buffer.withUnsafeMutableBytes { raw in
        guard let base = raw.baseAddress else { return -1 }
        return read(fd, base, length)
    }
    if bytesRead < 0 {
        let e = errno
        log("read(\(path), len=\(length)) failed errno=\(e) (\(String(cString: strerror(e))))")
        return (-e, Data())
    }
    log("read(\(path), offset=\(offset), len=\(length)) OK, got \(bytesRead) bytes")
    return (0, buffer.prefix(bytesRead))
}

/// Current device inventory, re-read per request: a device that was unmounted
/// when the connection opened may have been mounted since.
func currentDiskInventory() -> DiskInventory? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/diskutil")
    process.arguments = ["list", "-plist"]
    let output = Pipe()
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        log("diskutil could not be run: \(error)")
        return nil
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        log("diskutil list -plist exited \(process.terminationStatus)")
        return nil
    }
    do {
        return try DiskInventory(diskutilPlist: data)
    } catch {
        log("diskutil output could not be parsed: \(error)")
        return nil
    }
}

final class HelperService: NSObject, SalvageHelperProtocol {
    func readRawDevice(
        device: String,
        offset: UInt64,
        length: Int,
        withReply reply: @escaping (Int32, Data?) -> Void
    ) {
        guard isReadLengthAllowed(length) else {
            log("refused: length \(length) outside 1...\(HelperConstants.maxReadLength)")
            reply(-EINVAL, nil)
            return
        }
        guard let inventory = currentDiskInventory() else {
            reply(-EAGAIN, nil)
            return
        }
        switch authoriseDevicePath(device, inventory: inventory) {
        case .failure(let error):
            log("refused \(device): \(error)")
            reply(error == .malformedPath ? -EINVAL : -EACCES, nil)
        case .success(let path):
            let (status, data) = SalvageHelper.readRawDevice(
                path: path, offset: offset, length: length)
            reply(status, status == 0 ? data : nil)
        }
    }
}

final class ListenerDelegate: NSObject, NSXPCListenerDelegate {
    private let requirement: String?

    init(requirement: String?) {
        self.requirement = requirement
    }

    func listener(
        _ listener: NSXPCListener,
        shouldAcceptNewConnection connection: NSXPCConnection
    ) -> Bool {
        guard let requirement else {
            log("rejecting connection from pid \(connection.processIdentifier): "
                + "helper is not signed with a pinnable identity")
            return false
        }
        let pid = connection.processIdentifier
        guard isPeerAuthorised(processIdentifier: pid, requirement: requirement) else {
            log("rejecting connection from pid \(pid): does not satisfy \(requirement)")
            return false
        }

        connection.exportedInterface = NSXPCInterface(with: SalvageHelperProtocol.self)
        connection.exportedObject = HelperService()
        connection.resume()
        log("accepted connection from pid \(pid)")
        return true
    }
}

func runServer() {
    log("SalvageHelper starting, pid=\(getpid()) uid=\(getuid()) euid=\(geteuid())")

    let identity = OwnSigningIdentity.current()
    let requirement = identity.flatMap {
        peerRequirementString(
            expectedIdentifier: HelperConstants.clientBundleIdentifier,
            teamIdentifier: $0.teamIdentifier,
            leafCertificateSHA1Hex: $0.leafCertificateSHA1Hex
        )
    }
    if let requirement {
        log("peer requirement: \(requirement)")
    } else {
        // Fail closed. An ad-hoc signature pins nothing, so there is no way to
        // tell Salvage.app apart from any other process claiming to be it.
        log("WARNING: no peer requirement could be derived from this helper's "
            + "own signature (ad-hoc signed?). All connections will be refused.")
    }

    let listener = NSXPCListener(machServiceName: HelperConstants.machServiceName)
    let delegate = ListenerDelegate(requirement: requirement)
    listener.delegate = delegate
    listener.resume()
    log("listening on Mach service \(HelperConstants.machServiceName)")
    dispatchMain()
}

runServer()
