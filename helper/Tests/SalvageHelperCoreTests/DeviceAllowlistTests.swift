// Swift Testing rather than XCTest: DESIGN.md built this whole component
// with Command Line Tools only, which ships Testing.framework but no XCTest.
import Foundation
import Testing

@testable import SalvageHelperCore

/// A cut-down `diskutil list -plist` shaped like a real Mac's:
///   disk0            physical disk, no mount point of its own
///     disk0s1        physical store for container disk1
///     disk0s2        physical store for container disk3 (the live system)
///   disk1            container, one unmounted volume
///   disk3            container, volumes mounted at / and /System/Volumes/Data
///   disk4            external disk, one unmounted partition
///   disk5            external disk, one partition mounted at /Volumes/Stick
private let samplePlist = """
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>AllDisks</key>
  <array>
    <string>disk0</string><string>disk0s1</string><string>disk0s2</string>
    <string>disk1</string><string>disk1s1</string>
    <string>disk3</string><string>disk3s1</string><string>disk3s3</string><string>disk3s3s1</string>
    <string>disk4</string><string>disk4s1</string>
    <string>disk5</string><string>disk5s1</string>
  </array>
  <key>AllDisksAndPartitions</key>
  <array>
    <dict>
      <key>DeviceIdentifier</key><string>disk0</string>
      <key>Partitions</key>
      <array>
        <dict><key>DeviceIdentifier</key><string>disk0s1</string></dict>
        <dict><key>DeviceIdentifier</key><string>disk0s2</string></dict>
      </array>
    </dict>
    <dict>
      <key>DeviceIdentifier</key><string>disk1</string>
      <key>APFSPhysicalStores</key>
      <array><dict><key>DeviceIdentifier</key><string>disk0s1</string></dict></array>
      <key>APFSVolumes</key>
      <array><dict><key>DeviceIdentifier</key><string>disk1s1</string></dict></array>
    </dict>
    <dict>
      <key>DeviceIdentifier</key><string>disk3</string>
      <key>APFSPhysicalStores</key>
      <array><dict><key>DeviceIdentifier</key><string>disk0s2</string></dict></array>
      <key>APFSVolumes</key>
      <array>
        <dict>
          <key>DeviceIdentifier</key><string>disk3s1</string>
          <key>MountPoint</key><string>/System/Volumes/Data</string>
        </dict>
        <dict>
          <key>DeviceIdentifier</key><string>disk3s3</string>
          <key>MountedSnapshots</key>
          <array><dict><key>SnapshotMountPoint</key><string>/</string></dict></array>
        </dict>
        <dict>
          <key>DeviceIdentifier</key><string>disk3s3s1</string>
          <key>MountPoint</key><string>/</string>
        </dict>
      </array>
    </dict>
    <dict>
      <key>DeviceIdentifier</key><string>disk4</string>
      <key>Partitions</key>
      <array><dict><key>DeviceIdentifier</key><string>disk4s1</string></dict></array>
    </dict>
    <dict>
      <key>DeviceIdentifier</key><string>disk5</string>
      <key>Partitions</key>
      <array>
        <dict>
          <key>DeviceIdentifier</key><string>disk5s1</string>
          <key>MountPoint</key><string>/Volumes/Stick</string>
        </dict>
      </array>
    </dict>
  </array>
</dict>
</plist>
"""

@Suite("Device path parsing")
struct DevicePathParsingTests {
    @Test("whole-disk and partition nodes are recognised")
    func acceptsWholeDiskAndPartitionNodes() {
        #expect(deviceIdentifier(forPath: "/dev/disk0") == "disk0")
        #expect(deviceIdentifier(forPath: "/dev/rdisk0") == "disk0")
        #expect(deviceIdentifier(forPath: "/dev/rdisk3s1") == "disk3s1")
        #expect(deviceIdentifier(forPath: "/dev/rdisk3s3s1") == "disk3s3s1")
    }

    @Test("anything that is not a device node is rejected", arguments: [
        "/etc/passwd",
        "/Users/someone/Library/Mail/backup.mbox",
        "disk3",
        "/dev/disk",
        "/dev/rdisk",
        "/dev/rdisk3s",
        "/dev/rdisk3s1x",
        "/dev/rdisk3s1 ",
        "/dev/rdisk3s1;/etc/passwd",
        "/dev/../etc/passwd",
        "/dev/rdisk3s1/../../etc/passwd",
        "/dev/disk0\u{0}/etc/passwd",
        "/private/var/db/dslocal/nodes/Default/users/root.plist",
        "",
    ])
    func rejectsAnythingThatIsNotADeviceNode(path: String) {
        #expect(deviceIdentifier(forPath: path) == nil)
    }
}

@Suite("Disk inventory policy")
struct DiskInventoryTests {
    private let inventory: DiskInventory

    init() throws {
        inventory = try DiskInventory(diskutilPlist: Data(samplePlist.utf8))
    }

    @Test("an unmounted external disk and its partition are allowed")
    func unmountedExternalDiskIsAllowed() {
        #expect(authoriseDevicePath("/dev/rdisk4", inventory: inventory) == .success("/dev/rdisk4"))
        #expect(authoriseDevicePath("/dev/rdisk4s1", inventory: inventory) == .success("/dev/rdisk4s1"))
    }

    @Test("the buffered node stays buffered, the raw node stays raw")
    func rawAndBufferedNodesAreBothPreserved() {
        #expect(authoriseDevicePath("/dev/disk4", inventory: inventory) == .success("/dev/disk4"))
    }

    @Test("a mounted volume is refused")
    func mountedVolumeIsRefused() {
        #expect(authoriseDevicePath("/dev/rdisk3s1", inventory: inventory) == .failure(.deviceInUse))
        #expect(authoriseDevicePath("/dev/rdisk5s1", inventory: inventory) == .failure(.deviceInUse))
    }

    @Test("a volume whose only mount is a snapshot is refused")
    func mountedSnapshotCountsAsInUse() {
        #expect(authoriseDevicePath("/dev/rdisk3s3", inventory: inventory) == .failure(.deviceInUse))
    }

    @Test("a whole disk is refused while one of its partitions is mounted")
    func wholeDiskIsRefusedWhenAPartitionIsMounted() {
        #expect(authoriseDevicePath("/dev/rdisk5", inventory: inventory) == .failure(.deviceInUse))
    }

    // The case DESIGN.md's spike log read successfully: the live system's
    // container and its physical store report no mount point of their own.
    @Test("a container and its physical store are refused while a volume is mounted")
    func containerAndPhysicalStoreAreRefused() {
        #expect(authoriseDevicePath("/dev/rdisk3", inventory: inventory) == .failure(.deviceInUse))
        #expect(authoriseDevicePath("/dev/rdisk0s2", inventory: inventory) == .failure(.deviceInUse))
        #expect(authoriseDevicePath("/dev/rdisk0", inventory: inventory) == .failure(.deviceInUse))
    }

    @Test("an unmounted container and its store stay allowed")
    func unmountedContainerStaysAllowed() {
        #expect(authoriseDevicePath("/dev/rdisk1", inventory: inventory) == .success("/dev/rdisk1"))
        #expect(authoriseDevicePath("/dev/rdisk1s1", inventory: inventory) == .success("/dev/rdisk1s1"))
        #expect(authoriseDevicePath("/dev/rdisk0s1", inventory: inventory) == .success("/dev/rdisk0s1"))
    }

    @Test("a device diskutil does not know is refused")
    func unknownDeviceIsRefused() {
        #expect(authoriseDevicePath("/dev/rdisk99", inventory: inventory) == .failure(.unknownDevice))
    }

    @Test("a non-device path is refused before any inventory lookup")
    func nonDevicePathIsRefused() {
        #expect(authoriseDevicePath("/etc/passwd", inventory: inventory) == .failure(.malformedPath))
    }

    @Test("garbage diskutil output throws rather than yielding an empty allowlist")
    func garbageInventoryThrows() {
        #expect(throws: (any Error).self) {
            try DiskInventory(diskutilPlist: Data("not a plist".utf8))
        }
    }
}

@Suite("Read length bounds")
struct ReadLengthTests {
    @Test("length must be positive and bounded", arguments: [
        (0, false), (-1, false), (Int.max, false),
        (HelperConstants.maxReadLength + 1, false),
        (1, true), (1024 * 1024, true), (HelperConstants.maxReadLength, true),
    ])
    func lengthBounds(length: Int, allowed: Bool) {
        #expect(isReadLengthAllowed(length) == allowed)
    }
}
