// SalvageHelper — spike daemon.
//
// Listens on a Unix domain socket and, on request, opens a raw block device
// and reads back N bytes from an offset. The ONLY question this spike answers:
// does a root daemon registered via SMAppService (signed with the same
// self-signed "Salvage Dev" identity as Salvage.app, no Team ID) inherit the
// app's Full Disk Access when it opens /dev/rdiskN? See helper/DESIGN.md.
//
// Wire protocol (line-delimited JSON request, binary response):
//   request:  {"device":"/dev/rdisk3s1","offset":0,"length":1048576}\n
//   response: 4-byte little-endian Int32 status (0 = ok, else -errno)
//             followed by, if status == 0, an 8-byte little-endian UInt64
//             byte count and then that many raw bytes.
//
// This is NOT the final design (see DESIGN.md) — no XPC, no auth, no
// PhotoRec invocation. It exists only to prove or disprove the TCC question.

import Darwin
import Foundation

let socketPath = "/var/run/com.salvage.helper.sock"
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

func handleClient(_ clientFd: Int32) {
    defer { close(clientFd) }

    var lineData = Data()
    var byte: UInt8 = 0
    while true {
        let n = read(clientFd, &byte, 1)
        if n <= 0 { break }
        if byte == 0x0A { break } // \n
        lineData.append(byte)
    }

    guard !lineData.isEmpty,
          let json = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any],
          let device = json["device"] as? String,
          let offsetNum = json["offset"] as? NSNumber,
          let lengthNum = json["length"] as? NSNumber
    else {
        log("bad request: \(String(data: lineData, encoding: .utf8) ?? "<binary>")")
        var status: Int32 = -EINVAL
        withUnsafeBytes(of: &status) { _ = write(clientFd, $0.baseAddress, 4) }
        return
    }

    let offset = offsetNum.uint64Value
    let length = lengthNum.intValue

    let (status, data) = readRawDevice(path: device, offset: offset, length: length)

    var statusLE = status.littleEndian
    withUnsafeBytes(of: &statusLE) { _ = write(clientFd, $0.baseAddress, 4) }
    if status == 0 {
        var countLE = UInt64(data.count).littleEndian
        withUnsafeBytes(of: &countLE) { _ = write(clientFd, $0.baseAddress, 8) }
        data.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let n = write(clientFd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                if n <= 0 { break }
                offset += n
            }
        }
    }
}

func runServer() {
    log("SalvageHelper starting, pid=\(getpid()) uid=\(getuid()) euid=\(geteuid())")

    unlink(socketPath)

    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else {
        log("socket() failed errno=\(errno)")
        exit(1)
    }

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(socketPath.utf8CString)
    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
        ptr.withMemoryRebound(to: CChar.self, capacity: pathBytes.count) { dest in
            for (i, c) in pathBytes.enumerated() { dest[i] = c }
        }
    }

    let bindResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    guard bindResult == 0 else {
        log("bind() failed errno=\(errno)")
        exit(1)
    }

    // root:admin 660 per the spike spec.
    chmod(socketPath, 0o660)
    chown(socketPath, 0, 80) // 80 = admin group

    guard listen(fd, 5) == 0 else {
        log("listen() failed errno=\(errno)")
        exit(1)
    }

    log("listening on \(socketPath)")

    while true {
        let clientFd = accept(fd, nil, nil)
        if clientFd < 0 { continue }
        handleClient(clientFd)
    }
}

runServer()
