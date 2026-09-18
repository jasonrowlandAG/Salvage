// Device allowlist.
//
// The spike did a bare `open(path, O_RDONLY)` on whatever string arrived on
// the wire, as root. This narrows that to DESIGN.md's own stated scope:
// whole disks and partitions that `diskutil` currently reports as unmounted.
//
// "Unmounted" is checked over the whole dependency subtree, not just the named
// node: a whole disk whose partition is mounted, or an APFS physical store
// whose container has a mounted volume, is in use even though the named device
// has no mount point of its own. That is deliberately stricter than
// "MountPoint is empty" — it is what stops a caller reading the live startup
// disk's container by naming the parent device instead of the volume.

import Foundation

public enum DeviceAccessError: Error, Equatable {
    /// Not a /dev/disk… or /dev/rdisk… device path at all.
    case malformedPath
    /// Syntactically a device path, but diskutil does not know it.
    case unknownDevice
    /// The device, or something layered on top of it, is mounted.
    case deviceInUse
    /// diskutil could not be run or its output could not be parsed.
    case inventoryUnavailable
}

/// A snapshot of `diskutil list -plist`: which devices exist, which are
/// mounted, and what is layered on top of what.
public struct DiskInventory {
    private struct Node {
        var mounted = false
        var children: [String] = []
    }

    private var nodes: [String: Node] = [:]

    /// Parses the plist emitted by `diskutil list -plist`.
    public init(diskutilPlist data: Data) throws {
        guard
            let root = try PropertyListSerialization.propertyList(
                from: data, options: [], format: nil) as? [String: Any]
        else {
            throw DeviceAccessError.inventoryUnavailable
        }

        for identifier in root["AllDisks"] as? [String] ?? [] {
            nodes[identifier] = Node()
        }

        for entry in root["AllDisksAndPartitions"] as? [[String: Any]] ?? [] {
            guard let identifier = entry["DeviceIdentifier"] as? String else { continue }
            setMounted(identifier, Self.isMounted(entry))

            for child in (entry["Partitions"] as? [[String: Any]] ?? [])
                + (entry["APFSVolumes"] as? [[String: Any]] ?? [])
            {
                guard let childIdentifier = child["DeviceIdentifier"] as? String else { continue }
                addChild(childIdentifier, to: identifier)
                setMounted(childIdentifier, Self.isMounted(child))
            }

            // An APFS container is layered on top of its physical stores, so a
            // mounted volume in the container makes those stores in-use too.
            for store in entry["APFSPhysicalStores"] as? [[String: Any]] ?? [] {
                guard let storeIdentifier = store["DeviceIdentifier"] as? String else { continue }
                addChild(identifier, to: storeIdentifier)
            }
        }
    }

    private static func isMounted(_ entry: [String: Any]) -> Bool {
        if let mountPoint = entry["MountPoint"] as? String, !mountPoint.isEmpty { return true }
        if let snapshots = entry["MountedSnapshots"] as? [Any], !snapshots.isEmpty { return true }
        return false
    }

    private mutating func setMounted(_ identifier: String, _ mounted: Bool) {
        var node = nodes[identifier] ?? Node()
        node.mounted = node.mounted || mounted
        nodes[identifier] = node
    }

    private mutating func addChild(_ child: String, to parent: String) {
        var node = nodes[parent] ?? Node()
        if !node.children.contains(child) { node.children.append(child) }
        nodes[parent] = node
        if nodes[child] == nil { nodes[child] = Node() }
    }

    public func contains(_ identifier: String) -> Bool {
        nodes[identifier] != nil
    }

    /// True if `identifier` or anything layered on top of it is mounted.
    public func isInUse(_ identifier: String) -> Bool {
        var seen: Set<String> = []
        var pending = [identifier]
        while let current = pending.popLast() {
            guard seen.insert(current).inserted, let node = nodes[current] else { continue }
            if node.mounted { return true }
            pending.append(contentsOf: node.children)
        }
        return false
    }
}

/// Maps a device path to its diskutil identifier: `/dev/rdisk3s1` -> `disk3s1`.
/// Returns nil for anything that is not exactly a whole-disk or partition
/// device node — which is every path an attacker would actually want.
public func deviceIdentifier(forPath path: String) -> String? {
    let prefix = "/dev/"
    guard path.hasPrefix(prefix) else { return nil }
    var rest = Substring(path.dropFirst(prefix.count))
    if rest.hasPrefix("r") { rest = rest.dropFirst() }
    guard rest.hasPrefix("disk") else { return nil }

    // Remainder must be "<digits>" followed by zero or more "s<digits>".
    var tail = rest.dropFirst("disk".count)
    guard let digits = takeDigits(&tail), !digits.isEmpty else { return nil }
    while tail.hasPrefix("s") {
        tail = tail.dropFirst()
        guard let part = takeDigits(&tail), !part.isEmpty else { return nil }
    }
    guard tail.isEmpty else { return nil }
    return String(rest)
}

private func takeDigits(_ text: inout Substring) -> String? {
    var digits = ""
    while let first = text.first, first.isASCII, first.isNumber {
        digits.append(first)
        text = text.dropFirst()
    }
    return digits.isEmpty ? nil : digits
}

/// The full policy check for one request path. Returns the path to open,
/// rebuilt from the validated identifier rather than reusing the caller's
/// string, so only a canonical device node is ever passed to `open()`.
public func authoriseDevicePath(_ path: String, inventory: DiskInventory) -> Result<String, DeviceAccessError> {
    guard let identifier = deviceIdentifier(forPath: path) else { return .failure(.malformedPath) }
    guard inventory.contains(identifier) else { return .failure(.unknownDevice) }
    guard !inventory.isInUse(identifier) else { return .failure(.deviceInUse) }
    let rawPrefix = path.hasPrefix("/dev/r") ? "r" : ""
    return .success("/dev/\(rawPrefix)\(identifier)")
}

/// Rejects a read length the spike would have passed straight to `Data(count:)`.
public func isReadLengthAllowed(_ length: Int) -> Bool {
    length > 0 && length <= HelperConstants.maxReadLength
}
