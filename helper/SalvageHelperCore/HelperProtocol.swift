// The XPC contract between Salvage.app and the root helper.
//
// This replaces the spike's line-delimited-JSON Unix socket (see DESIGN.md).
// The socket authenticated nobody: its root:admin 0660 mode let any process
// running as an admin user ask a root daemon to read arbitrary raw bytes off
// any device. NSXPCConnection gives the daemon the peer's audited identity, so
// it can require the caller to be Salvage.app itself.

import Foundation

/// Constants shared by the daemon and any future in-app client.
public enum HelperConstants {
    /// Must match the `MachServices` key in com.salvage.helper.plist.
    public static let machServiceName = "com.salvage.helper"

    /// Bundle identifier the connecting peer must be signed as.
    public static let clientBundleIdentifier = "com.salvage.app"

    /// Upper bound on a single read request. The spike took `length` straight
    /// from the wire into `Data(count:)`, so a hostile caller could make a root
    /// daemon attempt an unbounded allocation.
    public static let maxReadLength = 64 * 1024 * 1024
}

/// The only operation the helper exposes.
///
/// `status` is 0 on success, otherwise a negative errno (`-EACCES` when the
/// request is refused by policy, `-EINVAL` when it is malformed).
@objc public protocol SalvageHelperProtocol {
    func readRawDevice(
        device: String,
        offset: UInt64,
        length: Int,
        withReply reply: @escaping (Int32, Data?) -> Void
    )
}
