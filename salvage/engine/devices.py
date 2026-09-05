from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .models import Device

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_devices() -> list[Device]:
    if sys.platform == "darwin":
        return _list_devices_macos()
    if sys.platform == "win32":
        return _list_devices_windows()
    if sys.platform.startswith("linux"):
        return _list_devices_linux()
    return []


def device_from_image(path: Path) -> Device:
    resolved = Path(path)
    size = resolved.stat().st_size
    return Device(
        id=str(resolved),
        path=str(resolved),
        name=resolved.name,
        size_bytes=size,
        kind="image",
    )


def system_volume_mount() -> str:
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def is_path_on_device(
    path: Path, device: Device, all_devices: list[Device] | None = None
) -> bool:
    if sys.platform == "win32":
        return _is_path_on_device_windows(path, device, all_devices)
    return _is_path_on_device_posix(path, device, all_devices)


# ---------------------------------------------------------------------------
# POSIX path-to-device matching
# ---------------------------------------------------------------------------


def _mount_point_of(path: Path) -> str:
    current = Path(path).resolve()
    while not os.path.ismount(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return str(current)


def _is_path_on_device_posix(
    path: Path, device: Device, all_devices: list[Device] | None
) -> bool:
    mp = _mount_point_of(path)
    if device.mount_point and mp == device.mount_point:
        return True
    if device.kind == "disk" and all_devices:
        for other in all_devices:
            if other.parent_id == device.id and other.mount_point and mp == other.mount_point:
                return True
    return False


# ---------------------------------------------------------------------------
# Windows path-to-device matching
# ---------------------------------------------------------------------------


def _drive_letter_of_path(path: Path) -> str | None:
    drive = Path(path).resolve().drive
    return drive.rstrip(":").upper() if drive else None


def _drive_letter_of_device(device: Device) -> str | None:
    if device.mount_point:
        stripped = device.mount_point.rstrip("\\")
        if len(stripped) >= 2 and stripped[1] == ":":
            return stripped[0].upper()
    if device.path.startswith("\\\\.\\") and len(device.path) >= 6 and device.path[5] == ":":
        return device.path[4].upper()
    return None


def _is_path_on_device_windows(
    path: Path, device: Device, all_devices: list[Device] | None
) -> bool:
    letter = _drive_letter_of_path(path)
    if letter is None:
        return False
    if letter == _drive_letter_of_device(device):
        return True
    if device.kind == "disk" and all_devices:
        for other in all_devices:
            if other.parent_id == device.id and letter == _drive_letter_of_device(other):
                return True
    return False


# ---------------------------------------------------------------------------
# macOS: diskutil
# ---------------------------------------------------------------------------


def _diskutil_list() -> dict | None:
    try:
        out = subprocess.run(
            ["diskutil", "list", "-plist"], capture_output=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return plistlib.loads(out.stdout)
    except Exception:
        return None


def _diskutil_info(device_id: str) -> dict | None:
    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", device_id],
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return plistlib.loads(out.stdout)
    except Exception:
        return None


def _list_devices_macos() -> list[Device]:
    top = _diskutil_list()
    if not top:
        return []
    entries = top.get("AllDisksAndPartitions", []) or []

    ids_needed: list[str] = []
    for entry in entries:
        if entry.get("OSInternal"):
            continue
        ids_needed.append(entry["DeviceIdentifier"])
        for part in entry.get("Partitions", []) or []:
            if part.get("Content") == "Apple_APFS":
                continue  # physical-store placeholder; real data lives in the synthesized container below
            ids_needed.append(part["DeviceIdentifier"])
        for vol in entry.get("APFSVolumes", []) or []:
            ids_needed.append(vol["DeviceIdentifier"])

    info_by_id: dict[str, dict] = {}
    for device_id in ids_needed:
        info = _diskutil_info(device_id)
        if info is not None:
            info_by_id[device_id] = info

    return _parse_macos_devices(entries, info_by_id)


def _parse_macos_devices(entries: list[dict], info_by_id: dict[str, dict]) -> list[Device]:
    # (device_id, kind, parent_id)
    raw: list[tuple[str, str, str | None]] = []
    for entry in entries:
        if entry.get("OSInternal"):
            continue
        whole_id = entry["DeviceIdentifier"]
        if whole_id in info_by_id:
            raw.append((whole_id, "disk", None))
        for part in entry.get("Partitions", []) or []:
            if part.get("Content") == "Apple_APFS":
                continue
            pid = part["DeviceIdentifier"]
            if pid in info_by_id:
                raw.append((pid, "partition", whole_id))
        for vol in entry.get("APFSVolumes", []) or []:
            vid = vol["DeviceIdentifier"]
            if vid in info_by_id:
                raw.append((vid, "partition", whole_id))

    entry_by_id = {e["DeviceIdentifier"]: e for e in entries}
    root_mount = system_volume_mount()
    system_ids: set[str] = set()
    for device_id, _kind, parent_id in raw:
        info = info_by_id[device_id]
        if info.get("MountPoint") != root_mount:
            continue
        system_ids.add(device_id)
        if not parent_id:
            continue
        system_ids.add(parent_id)
        # The system volume's synthesized container sits atop a physical-store
        # partition (e.g. disk0s2); mark that partition's whole disk too.
        parent_entry = entry_by_id.get(parent_id)
        if not parent_entry:
            continue
        for store in parent_entry.get("APFSPhysicalStores", []) or []:
            store_id = store.get("DeviceIdentifier")
            if not store_id:
                continue
            system_ids.add(store_id)
            for other_entry in entries:
                for other_part in other_entry.get("Partitions", []) or []:
                    if other_part.get("DeviceIdentifier") == store_id:
                        system_ids.add(other_entry["DeviceIdentifier"])

    devices = []
    for device_id, kind, parent_id in raw:
        devices.append(
            _macos_device(device_id, info_by_id[device_id], kind, parent_id, device_id in system_ids)
        )
    return devices


def _macos_device(
    device_id: str, info: dict, kind: str, parent_id: str | None, is_system: bool
) -> Device:
    node = info.get("DeviceNode") or f"/dev/{device_id}"
    raw_path = "/dev/r" + node[len("/dev/") :] if node.startswith("/dev/") else node

    media_name = (info.get("MediaName") or "").strip()
    vol_name = (info.get("VolumeName") or "").strip()
    if media_name and vol_name:
        name = f"{media_name} — {vol_name}"
    else:
        name = vol_name or media_name or device_id

    removable = (
        bool(info.get("RemovableMediaOrExternalDevice"))
        or bool(info.get("Ejectable"))
        or info.get("Internal") is False
    )

    return Device(
        id=device_id,
        path=raw_path,
        name=name,
        size_bytes=int(info.get("Size") or info.get("TotalSize") or 0),
        kind=kind,  # type: ignore[arg-type]
        is_removable=removable,
        is_system=is_system,
        filesystem=info.get("FilesystemName") or None,
        mount_point=info.get("MountPoint") or None,
        parent_id=parent_id,
    )


# ---------------------------------------------------------------------------
# Windows: PowerShell Get-Disk / Get-Partition / Get-Volume
# ---------------------------------------------------------------------------


def _run_powershell(command: str) -> str | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _list_devices_windows() -> list[Device]:
    disks_raw = _run_powershell("Get-Disk | ConvertTo-Json -Depth 4")
    partitions_raw = _run_powershell("Get-Partition | ConvertTo-Json -Depth 4")
    volumes_raw = _run_powershell("Get-Volume | ConvertTo-Json -Depth 4")
    if disks_raw is None or partitions_raw is None:
        return []
    return _parse_windows_devices(disks_raw, partitions_raw, volumes_raw or "")


def _as_list(parsed: object) -> list[dict]:
    if parsed is None:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    return list(parsed)  # type: ignore[arg-type]


def _parse_windows_devices(disks_raw: str, partitions_raw: str, volumes_raw: str) -> list[Device]:
    try:
        disks = _as_list(json.loads(disks_raw)) if disks_raw.strip() else []
        partitions = _as_list(json.loads(partitions_raw)) if partitions_raw.strip() else []
        volumes = _as_list(json.loads(volumes_raw)) if volumes_raw.strip() else []
    except json.JSONDecodeError:
        return []

    volume_by_letter = {
        str(v["DriveLetter"]).upper(): v for v in volumes if v.get("DriveLetter")
    }
    system_drive = os.environ.get("SystemDrive", "C:").rstrip(":\\").upper()

    partitions_by_disk: dict[object, list[dict]] = {}
    for part in partitions:
        partitions_by_disk.setdefault(part.get("DiskNumber"), []).append(part)

    system_disk_numbers = {
        part.get("DiskNumber")
        for part in partitions
        if part.get("DriveLetter") and str(part["DriveLetter"]).upper() == system_drive
    }

    devices: list[Device] = []
    for disk in disks:
        number = disk.get("Number")
        if number is None:
            continue
        disk_id = f"disk{number}"
        bus = (disk.get("BusType") or "").upper()
        removable = bus in {"USB", "SD", "MMC"}
        devices.append(
            Device(
                id=disk_id,
                path=f"\\\\.\\PhysicalDrive{number}",
                name=disk.get("FriendlyName") or disk_id,
                size_bytes=int(disk.get("Size") or 0),
                kind="disk",
                is_removable=removable,
                is_system=number in system_disk_numbers,
                filesystem=None,
                mount_point=None,
                parent_id=None,
            )
        )
        for part in partitions_by_disk.get(number, []):
            letter = part.get("DriveLetter")
            if not letter:
                continue  # unlettered partitions (EFI/Recovery/Reserved) aren't scan/recover targets
            letter = str(letter).upper()
            vol = volume_by_letter.get(letter, {})
            devices.append(
                Device(
                    id=f"{disk_id}-part{part.get('PartitionNumber')}",
                    path=f"\\\\.\\{letter}:",
                    name=vol.get("FileSystemLabel") or f"{letter}:",
                    size_bytes=int(part.get("Size") or vol.get("Size") or 0),
                    kind="partition",
                    is_removable=removable,
                    is_system=(letter == system_drive),
                    filesystem=vol.get("FileSystem") or None,
                    mount_point=f"{letter}:\\",
                    parent_id=disk_id,
                )
            )
    return devices


# ---------------------------------------------------------------------------
# Linux: lsblk
# ---------------------------------------------------------------------------


def _list_devices_linux() -> list[Device]:
    try:
        out = subprocess.run(
            [
                "lsblk",
                "-J",
                "-b",
                "-o",
                "NAME,PATH,SIZE,TYPE,RM,FSTYPE,MOUNTPOINT,MODEL,LABEL,PKNAME",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_linux_devices(out.stdout)


def _flatten_lsblk(items: list[dict], parent: str | None = None) -> list[dict]:
    flat: list[dict] = []
    for item in items:
        entry = dict(item)
        if parent and not entry.get("pkname"):
            entry["pkname"] = parent
        children = entry.pop("children", None)
        flat.append(entry)
        if children:
            flat.extend(_flatten_lsblk(children, parent=entry.get("name")))
    return flat


def _parse_linux_devices(raw: str) -> list[Device]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    items = _flatten_lsblk(data.get("blockdevices", []) or [])

    system_ids: set[str] = set()
    for item in items:
        if item.get("mountpoint") == "/":
            system_ids.add(item["name"])
            if item.get("pkname"):
                system_ids.add(item["pkname"])

    devices: list[Device] = []
    for item in items:
        if item.get("type") == "loop":
            continue  # loop-mounted images (snaps, ISOs), not real drives
        name = item["name"]
        rm = item.get("rm")
        removable = rm if isinstance(rm, bool) else str(rm) == "1"
        devices.append(
            Device(
                id=name,
                path=item.get("path") or f"/dev/{name}",
                name=item.get("label") or item.get("model") or name,
                size_bytes=int(item.get("size") or 0),
                kind="disk" if item.get("type") == "disk" else "partition",
                is_removable=removable,
                is_system=name in system_ids,
                filesystem=item.get("fstype") or None,
                mount_point=item.get("mountpoint") or None,
                parent_id=item.get("pkname") or None,
            )
        )
    return devices
