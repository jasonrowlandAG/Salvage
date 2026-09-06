from __future__ import annotations

import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import pty  # POSIX only
except ImportError:
    pty = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Binary lookup
# ---------------------------------------------------------------------------

_CANDIDATE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _tool(name: str) -> str:
    which = shutil.which(name)
    if which:
        return which
    for base in _CANDIDATE_DIRS:
        candidate = Path(base) / name
        if candidate.exists():
            return str(candidate)
    # Bundled path, same layout as PhotoRecEngine.locate_binary() (macOS only for now).
    bundled = Path(__file__).resolve().parent.parent / "bin" / "macos" / name
    if bundled.exists():
        return str(bundled)
    return name  # subprocess will raise FileNotFoundError with a clear message


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IOSDevice:
    udid: str
    name: str
    product_type: str
    ios_version: str
    capacity_bytes: int | None
    encrypted_backups: bool


class IOSBackupError(Exception):
    """Raised for any user-facing backup failure (locked device, no trust, password needed, ...)."""


def list_ios_devices() -> list[IOSDevice]:
    try:
        out = _run([_tool("idevice_id"), "-l"])
    except (OSError, subprocess.SubprocessError):
        return []
    udids = [line.strip() for line in out.stdout.splitlines() if line.strip()]

    devices: list[IOSDevice] = []
    for udid in udids:
        device = _device_info(udid)
        if device is not None:
            devices.append(device)
    return devices


def _ideviceinfo_value(udid: str, key: str, domain: str | None = None) -> str | None:
    args = [_tool("ideviceinfo"), "-u", udid]
    if domain:
        args += ["-q", domain]
    args += ["-k", key]
    try:
        out = _run(args)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _device_info(udid: str) -> IOSDevice | None:
    name = _ideviceinfo_value(udid, "DeviceName")
    product_type = _ideviceinfo_value(udid, "ProductType")
    ios_version = _ideviceinfo_value(udid, "ProductVersion")
    if name is None or product_type is None or ios_version is None:
        return None  # device vanished / not paired / lockdownd unreachable

    capacity_raw = _ideviceinfo_value(udid, "TotalDiskCapacity", domain="com.apple.disk_usage")
    capacity_bytes = int(capacity_raw) if capacity_raw and capacity_raw.isdigit() else None

    will_encrypt = _ideviceinfo_value(udid, "WillEncrypt", domain="com.apple.mobile.backup")
    encrypted_backups = (will_encrypt or "").strip().lower() == "true"

    return IOSDevice(
        udid=udid,
        name=name,
        product_type=product_type,
        ios_version=ios_version,
        capacity_bytes=capacity_bytes,
        encrypted_backups=encrypted_backups,
    )


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


@dataclass
class BackupProgress:
    percent: float
    current_file: str
    bytes_done: int | None
    elapsed_s: float


# idevicebackup2 draws a bar like:
#   [==================================================] 100% (14.1 MB/13.1 MB)
# and, right as a file finishes, briefly:
#   [============                                      ]  22% Finished
# It resets the "total" per internal batch of files rather than for the whole
# backup, so percent/bytes here reflect the current batch, not a global fraction.
_PROGRESS_RE = re.compile(
    r"\[[=\s]*\]\s*(\d+)%\s*(?:\(([\d.]+\s*[KMGT]?B)/([\d.]+\s*[KMGT]?B)\)|Finished)"
)
_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?)B")
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

_WAITING_PASSCODE = "Waiting for passcode to be entered on the device"
_LOCKDOWND_ERROR = "Could not connect to lockdownd"
_REFUSED = "device refused to start the backup process"
_VERSION_MISMATCH = "backup protocol version mismatch"
_NEEDS_PASSWORD = "Can't get password input in non-interactive mode"
_ENCRYPTED_BACKUP = "This is an encrypted backup"


def _parse_size(text: str) -> int | None:
    m = _SIZE_RE.match(text.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _SIZE_MULT.get(m.group(2), 1))


def create_backup(
    udid: str,
    backup_root: Path,
    on_progress: Callable[[BackupProgress], None] | None = None,
    cancel: threading.Event | None = None,
    full: bool = True,
) -> Path:
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)

    args = [_tool("idevicebackup2"), "-u", udid, "backup"]
    if full:
        args.append("--full")
    args.append(str(backup_root))

    # idevicebackup2's progress bar is only redrawn when stdout is a tty, so a plain
    # pipe delivers almost nothing until the process exits (see photorec.py for the
    # same issue). A pty makes progress stream as it's produced.
    if pty is not None:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=slave_fd, stderr=slave_fd, close_fds=True
        )
        os.close(slave_fd)
    else:
        # No pty on this platform (Windows): idevicebackup2 is POSIX-only via
        # libimobiledevice/Homebrew, so this path is untested/unused today.
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        master_fd = None

    start = time.monotonic()
    buf = ""
    tail = ""
    cancelled = False
    saw_passcode_wait = False

    def _read_chunk() -> str | None:
        if master_fd is not None:
            try:
                raw = os.read(master_fd, 8192)
            except OSError:
                return None
        else:
            assert proc.stdout is not None
            raw = proc.stdout.read(8192)
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace")

    while True:
        if cancel is not None and cancel.is_set():
            cancelled = True
            _terminate(proc)
            break

        chunk = _read_chunk()
        if chunk is None:
            break
        buf += chunk
        tail = (tail + chunk)[-4096:]

        if _WAITING_PASSCODE in tail:
            saw_passcode_wait = True

        match = None
        for match in _PROGRESS_RE.finditer(tail):
            pass  # keep the last match found in the tail window
        if match is not None and on_progress is not None:
            percent = float(match.group(1))
            bytes_done = _parse_size(match.group(2)) if match.group(2) else None
            on_progress(
                BackupProgress(
                    percent=percent,
                    current_file="Receiving files",
                    bytes_done=bytes_done,
                    elapsed_s=time.monotonic() - start,
                )
            )

    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(proc)

    if cancelled:
        raise IOSBackupError("Backup cancelled.")

    if proc.returncode != 0:
        if _NEEDS_PASSWORD in buf or _ENCRYPTED_BACKUP in buf:
            raise IOSBackupError(
                "This backup is password-protected. Salvage cannot read encrypted iPhone "
                "backups yet — turn off backup encryption on the device (Settings > General "
                "> Transfer or Reset iPhone > Encrypted Backup) and try again."
            )
        if _LOCKDOWND_ERROR in buf:
            raise IOSBackupError(
                "Could not connect to the iPhone. Unlock it, tap Trust if prompted, and make "
                "sure it's still connected by USB."
            )
        if _REFUSED in buf or _VERSION_MISMATCH in buf:
            raise IOSBackupError(
                "The iPhone refused the backup request. Unlock the device, tap Trust if "
                "prompted, then try again."
            )
        raise IOSBackupError(f"idevicebackup2 exited with an error (code {proc.returncode}).")

    if saw_passcode_wait and "Backup Failed" in buf:
        raise IOSBackupError(
            "The iPhone is waiting for its passcode to be entered on the device. Unlock the "
            "phone and enter its passcode, then try again."
        )

    return backup_root / udid


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# Backup reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackupFile:
    file_id: str
    domain: str
    relative_path: str
    size: int
    path_on_disk: Path


class BackupReader:
    KNOWN: dict[str, tuple[str, str]] = {
        "sms": ("HomeDomain", "Library/SMS/sms.db"),
        "contacts": ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb"),
        "contact_images": ("HomeDomain", "Library/AddressBook/AddressBookImages.sqlitedb"),
        "notes": ("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite"),
        "call_history": ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata"),
        "photos_db": ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
        "whatsapp": ("AppDomainGroup-group.net.whatsapp.WhatsApp.shared", "ChatStorage.sqlite"),
        "whatsapp_contacts": (
            "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
            "ContactsV2.sqlite",
        ),
    }

    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = Path(backup_dir)
        manifest_db = self.backup_dir / "Manifest.db"
        if not manifest_db.exists():
            raise FileNotFoundError(f"No Manifest.db found in {self.backup_dir}")
        self._conn = sqlite3.connect(f"file:{manifest_db}?mode=ro", uri=True)

    def close(self) -> None:
        self._conn.close()

    def info(self) -> dict:
        result: dict = {}
        info_plist = self.backup_dir / "Info.plist"
        if info_plist.exists():
            with open(info_plist, "rb") as f:
                data = plistlib.load(f)
            result["device_name"] = data.get("Device Name")
            result["product_type"] = data.get("Product Type")
            result["product_version"] = data.get("Product Version")
            result["last_backup_date"] = data.get("Last Backup Date")

        manifest_plist = self.backup_dir / "Manifest.plist"
        is_encrypted = False
        if manifest_plist.exists():
            with open(manifest_plist, "rb") as f:
                mdata = plistlib.load(f)
            is_encrypted = bool(mdata.get("IsEncrypted", False))
        result["is_encrypted"] = is_encrypted
        return result

    def find(
        self, domain: str | None = None, relative_path_like: str | None = None
    ) -> list[BackupFile]:
        # flags: 1 = regular file, 2 = directory, 4 = symlink (only files carry payload bytes).
        query = "SELECT fileID, domain, relativePath, file FROM Files WHERE flags = 1"
        params: list[str] = []
        if domain is not None:
            query += " AND domain = ?"
            params.append(domain)
        if relative_path_like is not None:
            query += " AND relativePath LIKE ?"
            params.append(relative_path_like)

        results: list[BackupFile] = []
        cur = self._conn.execute(query, params)
        for file_id, file_domain, relative_path, file_blob in cur.fetchall():
            if not relative_path:
                continue  # domain-root pseudo-entries have no relativePath
            path_on_disk = self.backup_dir / file_id[:2] / file_id
            if not path_on_disk.exists():
                continue
            # The manifest's recorded Size can drift from the actual payload (observed on a
            # real device backup, e.g. sms.db resized between manifest write and transfer);
            # the on-disk copy is what extract() actually hands back, so trust it first.
            size = path_on_disk.stat().st_size
            if size == 0:
                manifest_size = _manifest_file_size(file_blob)
                if manifest_size is not None:
                    size = manifest_size
            results.append(
                BackupFile(
                    file_id=file_id,
                    domain=file_domain,
                    relative_path=relative_path,
                    size=size,
                    path_on_disk=path_on_disk,
                )
            )
        return results

    def extract(self, bf: BackupFile, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bf.path_on_disk, dest)
        return dest

    def extract_known(self, key: str, dest_dir: Path) -> Path | None:
        if key not in self.KNOWN:
            raise KeyError(f"unknown key: {key}")
        domain, relative_path = self.KNOWN[key]
        matches = self.find(domain=domain, relative_path_like=relative_path)
        exact = [m for m in matches if m.relative_path == relative_path]
        if not exact:
            return None
        bf = exact[0]

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(relative_path).name
        self.extract(bf, dest)

        for suffix in ("-wal", "-shm"):
            sidecar_matches = self.find(domain=domain, relative_path_like=relative_path + suffix)
            sidecar_exact = [m for m in sidecar_matches if m.relative_path == relative_path + suffix]
            if sidecar_exact:
                self.extract(sidecar_exact[0], dest_dir / (dest.name + suffix))

        return dest


def _manifest_file_size(file_blob: bytes | None) -> int | None:
    if not file_blob:
        return None
    try:
        data = plistlib.loads(file_blob)
    except Exception:
        return None
    # NSKeyedArchiver plist: top-level $objects array holds an NSDictionary whose
    # "Size" entry is the file size; only present (and meaningful) for regular files.
    objects = data.get("$objects")
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if isinstance(obj, dict) and "Size" in obj:
            size = obj["Size"]
            if isinstance(size, int):
                return size
    return None


# ---------------------------------------------------------------------------
# AFC (live device filesystem, e.g. DCIM)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AFCEntry:
    path: str
    size: int
    mtime: float


# A real `ls -l` line, e.g.:
#   -rw-r--r--    1 mobile mobile    1118573 22 Mar 2025 13:21:26 IMG_5716.PNG
#   drwxr-xr-x    2 mobile mobile        128 06 Sep 2026 08:29:25 109APPLE
_AFC_LS_RE = re.compile(
    r"^([dl-])\S{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d{1,2}\s+\w{3}\s+\d{4}\s+[\d:]{8})\s+(.+)$"
)
_AFC_MTIME_FMT = "%d %b %Y %H:%M:%S"


def _afc_run(udid: str, commands: list[str], timeout: float = 30) -> str:
    proc_input = "\n".join(commands) + "\nquit\n"
    out = subprocess.run(
        [_tool("afcclient"), "-u", udid],
        input=proc_input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return out.stdout


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PROMPT_RE = re.compile(r"afc:.*?>\s*")


def _strip_afc_chrome(raw: str) -> list[str]:
    cleaned = _ANSI_RE.sub("", raw)
    cleaned = _PROMPT_RE.sub("", cleaned)
    return [line for line in cleaned.splitlines() if line.strip()]


def _list_dir(udid: str, path: str) -> list[tuple[str, bool, int, float]]:
    """Return [(name, is_dir, size, mtime)] for one directory via `ls -l`."""
    raw = _afc_run(udid, [f'ls -l "{path}"'])
    entries = []
    for line in _strip_afc_chrome(raw):
        m = _AFC_LS_RE.match(line)
        if not m:
            continue
        kind, size_str, mtime_str, name = m.groups()
        if name in (".", ".."):
            continue
        try:
            mtime = time.mktime(time.strptime(mtime_str, _AFC_MTIME_FMT))
        except ValueError:
            mtime = 0.0
        entries.append((name, kind == "d", int(size_str), mtime))
    return entries


def list_dcim(udid: str) -> list[AFCEntry]:
    entries: list[AFCEntry] = []
    _walk_afc_dir(udid, "/DCIM", entries)
    return entries


def _walk_afc_dir(udid: str, path: str, out: list[AFCEntry]) -> None:
    for name, is_dir, size, mtime in _list_dir(udid, path):
        child = f"{path}/{name}"
        if is_dir:
            _walk_afc_dir(udid, child, out)
        else:
            out.append(AFCEntry(path=child, size=size, mtime=mtime))


def pull_afc_file(udid: str, remote: str, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _afc_run(udid, [f'get "{remote}" "{dest}"'], timeout=120)
    if not dest.exists():
        raise IOSBackupError(f"Failed to pull {remote} from device via AFC.")
    return dest
