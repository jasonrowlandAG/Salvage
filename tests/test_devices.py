from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

from salvage.engine import devices
from salvage.engine.models import Device

FIXTURES = Path(__file__).parent / "fixtures"


def _load_plist(name: str) -> dict:
    return plistlib.loads((FIXTURES / name).read_bytes())


# ---------------------------------------------------------------------------
# macOS: diskutil plist parsing
# ---------------------------------------------------------------------------


def test_macos_parses_real_diskutil_fixture(monkeypatch):
    top = _load_plist("diskutil_list.plist")
    entries = top["AllDisksAndPartitions"]

    info_by_id = {}
    for name in [
        "disk0",
        "disk0s1",
        "disk0s2",
        "disk0s3",
        "disk3",
        "disk3s1",
        "disk3s2",
        "disk3s3",
        "disk3s3s1",
        "disk3s4",
        "disk3s5",
        "disk3s6",
        "disk4",
        "disk4s1",
    ]:
        info_by_id[name] = _load_plist(f"diskutil_info_{name}.plist")

    result = devices._parse_macos_devices(entries, info_by_id)
    by_id = {d.id: d for d in result}

    # disk0s2 is the Apple_APFS physical-store placeholder and must be skipped
    assert "disk0s2" not in by_id

    # EFI/Recovery-ish partitions under disk0 are noise a non-technical user
    # can't recover anything from -> excluded even though their info was fetched
    assert "disk0s1" not in by_id  # Apple_APFS_ISC
    assert "disk0s3" not in by_id  # Apple_APFS_Recovery

    # Internal OS plumbing APFS volumes -> excluded
    assert "disk3s2" not in by_id  # Update
    assert "disk3s4" not in by_id  # Preboot
    assert "disk3s5" not in by_id  # Recovery
    assert "disk3s6" not in by_id  # VM

    disk0 = by_id["disk0"]
    assert disk0.kind == "disk"
    assert disk0.path == "/dev/rdisk0"
    assert disk0.size_bytes == 500277792768
    assert disk0.is_removable is False
    # disk0 hosts the physical store (disk0s2) backing the boot container -> is_system
    assert disk0.is_system is True

    disk3 = by_id["disk3"]
    assert disk3.kind == "disk"
    assert disk3.is_removable is False
    assert disk3.is_system is True  # synthesized container holding the boot volume
    # Same MediaName as disk0 -> must be labelled distinctly, not as a duplicate
    assert disk3.name == "APPLE SSD AP0512Z — APFS container"
    # Linked back to the physical disk that hosts its physical-store partition
    assert disk3.parent_id == "disk0"

    root_vol = by_id["disk3s3s1"]
    assert root_vol.mount_point == "/"
    assert root_vol.is_system is True
    assert root_vol.parent_id == "disk3"
    assert root_vol.filesystem == "APFS"

    data_vol = by_id["disk3s1"]
    assert data_vol.mount_point == "/System/Volumes/Data"
    assert data_vol.is_system is False
    assert data_vol.name == "Macintosh HD - Data"

    # disk4 is an ejectable/external disk image -> reads as removable
    disk4 = by_id["disk4"]
    assert disk4.is_removable is True
    assert disk4.is_system is False

    disk4s1 = by_id["disk4s1"]
    assert disk4s1.path == "/dev/rdisk4s1"
    assert disk4s1.is_removable is True
    assert disk4s1.name == "AnyUnlock - iPhone Password Unlocker Installer"

    # disk0 is the physical disk hosting the boot container's physical store;
    # the root volume (/) lives on disk3s3s1, two hops down (disk0 -> disk3 -> disk3s3s1).
    # A destination validation check against disk0 must still resolve through that chain.
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == "/")
    assert devices.is_path_on_device(Path("/Users/whoever"), disk0, result) is True


def test_macos_missing_info_is_skipped():
    entries = [
        {
            "DeviceIdentifier": "disk9",
            "OSInternal": False,
            "Partitions": [{"DeviceIdentifier": "disk9s1", "Content": "Apple_HFS"}],
        }
    ]
    # no info fetched for disk9 or disk9s1 (diskutil info failed) -> nothing returned
    assert devices._parse_macos_devices(entries, {}) == []


def test_macos_os_internal_disks_skipped():
    entries = [{"DeviceIdentifier": "disk1", "OSInternal": True}]
    info_by_id = {"disk1": {"Size": 524288000}}
    assert devices._parse_macos_devices(entries, info_by_id) == []


def test_macos_list_devices_returns_empty_on_diskutil_failure(monkeypatch):
    monkeypatch.setattr(devices, "_diskutil_list", lambda: None)
    assert devices._list_devices_macos() == []


# ---------------------------------------------------------------------------
# Windows: Get-Disk / Get-Partition / Get-Volume JSON parsing
# ---------------------------------------------------------------------------


def test_windows_parses_realistic_fixture(monkeypatch):
    monkeypatch.setenv("SystemDrive", "C:")
    disks_raw = (FIXTURES / "windows_disks.json").read_text()
    partitions_raw = (FIXTURES / "windows_partitions.json").read_text()
    volumes_raw = (FIXTURES / "windows_volumes.json").read_text()

    result = devices._parse_windows_devices(disks_raw, partitions_raw, volumes_raw)
    by_id = {d.id: d for d in result}

    disk0 = by_id["disk0"]
    assert disk0.kind == "disk"
    assert disk0.path == "\\\\.\\PhysicalDrive0"
    assert disk0.is_removable is False
    assert disk0.is_system is True  # hosts the C: system partition

    disk1 = by_id["disk1"]
    assert disk1.is_removable is True  # USB bus type
    assert disk1.is_system is False

    c_part = by_id["disk0-part3"]
    assert c_part.path == "\\\\.\\C:"
    assert c_part.mount_point == "C:\\"
    assert c_part.filesystem == "NTFS"
    assert c_part.name == "Windows"
    assert c_part.is_system is True
    assert c_part.parent_id == "disk0"

    e_part = by_id["disk1-part1"]
    assert e_part.path == "\\\\.\\E:"
    assert e_part.name == "SANDISK"
    assert e_part.filesystem == "exFAT"
    assert e_part.is_removable is True
    assert e_part.is_system is False

    # unlettered Recovery/Reserved partitions on disk0 are skipped
    assert "disk0-part1" not in by_id
    assert "disk0-part2" not in by_id


def test_windows_handles_single_disk_as_bare_object(monkeypatch):
    monkeypatch.setenv("SystemDrive", "C:")
    disks_raw = '{"Number": 0, "FriendlyName": "Solo Disk", "Size": 1000, "BusType": "SATA"}'
    result = devices._parse_windows_devices(disks_raw, "[]", "[]")
    assert len(result) == 1
    assert result[0].id == "disk0"


def test_windows_handles_malformed_json():
    assert devices._parse_windows_devices("not json", "[]", "[]") == []


# ---------------------------------------------------------------------------
# Linux: lsblk JSON parsing
# ---------------------------------------------------------------------------


def test_linux_parses_realistic_lsblk_fixture():
    raw = (FIXTURES / "linux_lsblk.json").read_text()
    result = devices._parse_linux_devices(raw)
    by_id = {d.id: d for d in result}

    # loop devices (snaps, ISOs) are not real drives
    assert "loop0" not in by_id

    sda = by_id["sda"]
    assert sda.kind == "disk"
    assert sda.path == "/dev/sda"
    assert sda.is_removable is False
    assert sda.is_system is True  # hosts the root partition
    assert sda.name == "Samsung SSD 860"

    sda2 = by_id["sda2"]
    assert sda2.mount_point == "/"
    assert sda2.is_system is True
    assert sda2.parent_id == "sda"
    assert sda2.filesystem == "ext4"

    sdb = by_id["sdb"]
    assert sdb.is_removable is True
    assert sdb.is_system is False
    assert sdb.name == "SanDisk Ultra"

    sdb1 = by_id["sdb1"]
    assert sdb1.is_removable is True
    assert sdb1.name == "SANDISK"
    assert sdb1.mount_point == "/media/user/SANDISK"
    assert sdb1.parent_id == "sdb"


def test_linux_handles_malformed_json():
    assert devices._parse_linux_devices("not json") == []


def test_linux_handles_empty_blockdevices():
    assert devices._parse_linux_devices('{"blockdevices": []}') == []


# ---------------------------------------------------------------------------
# device_from_image / system_volume_mount
# ---------------------------------------------------------------------------


def test_device_from_image(tmp_path):
    img = tmp_path / "recovery.dd"
    img.write_bytes(b"x" * 1024)
    dev = devices.device_from_image(img)
    assert dev.kind == "image"
    assert dev.size_bytes == 1024
    assert dev.name == "recovery.dd"
    assert dev.path == str(img.resolve())


def test_system_volume_mount_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert devices.system_volume_mount() == "/"


def test_system_volume_mount_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemDrive", "D:")
    assert devices.system_volume_mount() == "D:\\"


# ---------------------------------------------------------------------------
# filevault_enabled
# ---------------------------------------------------------------------------


def test_filevault_enabled_true_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Result:
        stdout = "FileVault is On.\n"

    monkeypatch.setattr(devices.subprocess, "run", lambda *a, **k: _Result())
    assert devices.filevault_enabled() is True


def test_filevault_enabled_false_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    class _Result:
        stdout = "FileVault is Off.\n"

    monkeypatch.setattr(devices.subprocess, "run", lambda *a, **k: _Result())
    assert devices.filevault_enabled() is False


def test_filevault_enabled_none_on_non_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert devices.filevault_enabled() is None


def test_filevault_enabled_none_when_fdesetup_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def _raise(*a, **k):
        raise OSError("fdesetup not found")

    monkeypatch.setattr(devices.subprocess, "run", _raise)
    assert devices.filevault_enabled() is None


# ---------------------------------------------------------------------------
# is_path_on_device
# ---------------------------------------------------------------------------


def test_is_path_on_device_posix_matches_mount_point(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    device = Device(
        id="disk2s1",
        path="/dev/rdisk2s1",
        name="Test",
        size_bytes=1000,
        kind="partition",
        mount_point=str(tmp_path),
    )
    # tmp_path itself is unlikely to be a real mount point, so fake ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(tmp_path))
    target = tmp_path / "sub" / "file.txt"
    assert devices.is_path_on_device(target, device) is True


def test_is_path_on_device_posix_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    device = Device(
        id="disk2s1",
        path="/dev/rdisk2s1",
        name="Test",
        size_bytes=1000,
        kind="partition",
        mount_point="/Volumes/Other",
    )
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(tmp_path))
    target = tmp_path / "file.txt"
    assert devices.is_path_on_device(target, device) is False


def test_is_path_on_device_posix_disk_checks_partitions(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    disk = Device(id="disk2", path="/dev/rdisk2", name="Disk", size_bytes=1000, kind="disk")
    partition = Device(
        id="disk2s1",
        path="/dev/rdisk2s1",
        name="Vol",
        size_bytes=500,
        kind="partition",
        mount_point=str(tmp_path),
        parent_id="disk2",
    )
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(tmp_path))
    target = tmp_path / "file.txt"
    assert devices.is_path_on_device(target, disk, all_devices=[disk, partition]) is True


def test_is_path_on_device_windows_drive_letter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    device = Device(
        id="disk1-part1",
        path="\\\\.\\E:",
        name="USB",
        size_bytes=1000,
        kind="partition",
        mount_point="E:\\",
        parent_id="disk1",
    )

    monkeypatch.setattr(devices, "_drive_letter_of_path", lambda p: "E")
    assert devices.is_path_on_device(Path("E:\\photos"), device) is True


def test_is_path_on_device_windows_no_match(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    device = Device(
        id="disk1-part1",
        path="\\\\.\\E:",
        name="USB",
        size_bytes=1000,
        kind="partition",
        mount_point="E:\\",
    )
    monkeypatch.setattr(devices, "_drive_letter_of_path", lambda p: "C")
    assert devices.is_path_on_device(Path("C:\\Users"), device) is False


def test_is_path_on_device_windows_disk_checks_partitions(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    disk = Device(id="disk1", path="\\\\.\\PhysicalDrive1", name="Disk", size_bytes=1000, kind="disk")
    partition = Device(
        id="disk1-part1",
        path="\\\\.\\E:",
        name="USB",
        size_bytes=500,
        kind="partition",
        mount_point="E:\\",
        parent_id="disk1",
    )
    monkeypatch.setattr(devices, "_drive_letter_of_path", lambda p: "E")
    assert devices.is_path_on_device(Path("E:\\photos"), disk, all_devices=[disk, partition]) is True
