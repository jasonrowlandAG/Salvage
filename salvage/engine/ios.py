from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from Crypto.Cipher import AES

from salvage.engine.privileged import resolve_trusted_binary

try:
    import pty  # POSIX only
except ImportError:
    pty = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Binary lookup
# ---------------------------------------------------------------------------

_CANDIDATE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _tool(name: str) -> str:
    # Bundled copy and fixed/known system locations first, PATH only as a last resort -
    # see privileged.resolve_trusted_binary(). These libimobiledevice tools aren't run
    # elevated today, but resolving this way still avoids the same packaging trap
    # PhotoRec had (a frozen build silently preferring Homebrew's copy over its own),
    # and keeps this module consistent with the other two binary-resolution sites.
    bundled = Path(__file__).resolve().parent.parent / "bin" / "macos" / name  # macOS only for now
    path, _from_path = resolve_trusted_binary(name, bundled, tuple(Path(d) for d in _CANDIDATE_DIRS))
    return str(path) if path is not None else name  # subprocess will raise FileNotFoundError with a clear message


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
            if not saw_passcode_wait and on_progress is not None:
                on_progress(
                    BackupProgress(
                        percent=0.0,
                        current_file="Waiting for passcode",
                        bytes_done=None,
                        elapsed_s=time.monotonic() - start,
                    )
                )
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
# Encrypted-backup crypto (BackupKeyBag / RFC 3394 key unwrap / AES-CBC)
# ---------------------------------------------------------------------------
#
# Reference: Apple's "Manifest.plist" for an encrypted backup carries a
# BackupKeyBag (binary TLV, described below) and a ManifestKey used to
# decrypt Manifest.db; each row's Files.file blob then carries its own
# per-file EncryptionKey/ProtectionClass for decrypting the payload under
# backup_dir/<fileID[:2]>/<fileID>. This format is documented by several
# open-source projects (e.g. iphone_backup_decrypt, mvt-ios) — no third
# party device/backup libraries are used here, only that documentation.


class BackupPasswordError(Exception):
    """Raised when an encrypted backup's password is missing, wrong, or the
    keybag/manifest can't be decrypted for some other reason."""


def _parse_tlv(data: bytes) -> list[tuple[str, bytes]]:
    """Splits a keybag blob into a flat list of (4-char tag, value) pairs.

    Each entry is a 4-byte ASCII tag, a 4-byte big-endian length, then that
    many bytes of value.
    """
    entries: list[tuple[str, bytes]] = []
    pos = 0
    n = len(data)
    while pos + 8 <= n:
        tag = data[pos : pos + 4].decode("ascii", errors="replace")
        length = int.from_bytes(data[pos + 4 : pos + 8], "big")
        pos += 8
        value = data[pos : pos + length]
        pos += length
        entries.append((tag, value))
    return entries


class BackupKeyBag:
    """Parsed BackupKeyBag: a header (keybag-wide fields) plus one dict per
    protection class (CLAS/WRAP/WPKY/KTYP), split on TLV "UUID" boundaries —
    the first UUID belongs to the keybag itself, every UUID after that opens
    a new class entry.
    """

    def __init__(self, header: dict[str, bytes], classes: list[dict[str, bytes]]) -> None:
        self.header = header
        self.classes = classes

    @classmethod
    def parse(cls, data: bytes) -> "BackupKeyBag":
        entries = _parse_tlv(data)
        uuid_indexes = [i for i, (tag, _) in enumerate(entries) if tag == "UUID"]
        if not uuid_indexes:
            raise BackupPasswordError("Backup key bag has no UUID entries — can't parse it.")
        header_end = uuid_indexes[1] if len(uuid_indexes) > 1 else len(entries)
        header = dict(entries[:header_end])
        bounds = uuid_indexes[1:] + [len(entries)]
        classes = [dict(entries[bounds[i] : bounds[i + 1]]) for i in range(len(bounds) - 1)]
        return cls(header, classes)

    def derive_class_keys(self, password: str) -> dict[int, bytes]:
        """Derives the passcode key from `password` and unwraps every
        passcode-protected class key with it. Raises BackupPasswordError if
        the keybag is missing required fields or the password is wrong.
        """
        dpsl = self.header.get("DPSL")
        dpic_raw = self.header.get("DPIC")
        salt = self.header.get("SALT")
        iter_raw = self.header.get("ITER")
        if not dpsl or not dpic_raw or not salt or not iter_raw:
            raise BackupPasswordError(
                "This backup's key bag is missing passcode-derivation parameters."
            )
        dpic = int.from_bytes(dpic_raw, "big")
        iterations = int.from_bytes(iter_raw, "big")

        intermediate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), dpsl, dpic, dklen=32)
        passcode_key = hashlib.pbkdf2_hmac("sha1", intermediate, salt, iterations, dklen=32)

        class_keys: dict[int, bytes] = {}
        checked_any = False
        for entry in self.classes:
            clas_raw = entry.get("CLAS")
            wrap_raw = entry.get("WRAP")
            wpky = entry.get("WPKY")
            if clas_raw is None or wrap_raw is None or wpky is None:
                continue
            wrap = int.from_bytes(wrap_raw, "big")
            if not (wrap & 2):
                continue  # not protected by the passcode key (e.g. device-key-only classes)
            checked_any = True
            try:
                key = _aes_key_unwrap(passcode_key, wpky)
            except ValueError:
                raise BackupPasswordError("Incorrect backup password.") from None
            class_keys[int.from_bytes(clas_raw, "big")] = key

        if not checked_any:
            raise BackupPasswordError(
                "No passcode-protected class keys found in this backup's key bag."
            )
        return class_keys


def _aes_key_unwrap(kek: bytes, wrapped: bytes) -> bytes:
    """RFC 3394 AES key unwrap. `wrapped` must be a multiple of 8 bytes; raises
    ValueError if the integrity check fails (wrong key or corrupted input).
    """
    if len(wrapped) % 8 != 0 or len(wrapped) < 16:
        raise ValueError("wrapped key has an invalid length")
    n = len(wrapped) // 8 - 1
    a = wrapped[:8]
    r = [wrapped[8 * (i + 1) : 8 * (i + 1) + 8] for i in range(n)]
    cipher = AES.new(kek, AES.MODE_ECB)
    for j in reversed(range(6)):
        for i in reversed(range(1, n + 1)):
            t = n * j + i
            a_int = int.from_bytes(a, "big") ^ t
            block = cipher.decrypt(a_int.to_bytes(8, "big") + r[i - 1])
            a = block[:8]
            r[i - 1] = block[8:]
    if a != b"\xa6\xa6\xa6\xa6\xa6\xa6\xa6\xa6":
        raise ValueError("key unwrap integrity check failed")
    return b"".join(r)


def _aes_cbc_decrypt_zero_iv(key: bytes, ciphertext: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16)
    return cipher.decrypt(ciphertext)


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or pad > len(data):
        return data  # not validly padded; hand back as-is rather than mangling it
    return data[:-pad]


def _manifest_file_keys(file_blob: bytes | None) -> tuple[bytes | None, int | None]:
    """Pulls EncryptionKey (4-byte class + 40-byte wrapped key) and
    ProtectionClass out of a Files.file NSKeyedArchiver blob, for encrypted
    backups only. Mirrors _manifest_file_size's approach: the MBFile record
    is an NSDictionary that plistlib exposes directly inside $objects, with
    simple values (ints, short NSData) inlined rather than UID-referenced.
    """
    if not file_blob:
        return None, None
    try:
        data = plistlib.loads(file_blob)
    except Exception:
        return None, None
    objects = data.get("$objects")
    if not isinstance(objects, list):
        return None, None
    for obj in objects:
        if not (isinstance(obj, dict) and "Size" in obj):
            continue
        enc_key = obj.get("EncryptionKey")
        if isinstance(enc_key, plistlib.UID):
            idx = enc_key.data
            enc_key = objects[idx] if 0 <= idx < len(objects) else None
        if isinstance(enc_key, dict):
            enc_key = enc_key.get("NS.data")
        if not isinstance(enc_key, (bytes, bytearray)):
            enc_key = None

        prot_class = obj.get("ProtectionClass")
        if isinstance(prot_class, plistlib.UID):
            idx = prot_class.data
            resolved = objects[idx] if 0 <= idx < len(objects) else None
            prot_class = resolved if isinstance(resolved, int) else None
        elif not isinstance(prot_class, int):
            prot_class = None

        return (bytes(enc_key) if enc_key is not None else None), prot_class
    return None, None


# ---------------------------------------------------------------------------
# Backup reading
# ---------------------------------------------------------------------------

_CACHE_DIR_PREFIX = "salvage_ios_"


def cleanup_stale_ios_caches() -> None:
    """Best-effort sweep of decrypted-manifest cache dirs left behind by a
    previous run that crashed or was killed before BackupReader.close() ran
    (e.g. a force-quit while a backup was open). Safe to call on every
    startup: only ever removes tempfile.mkdtemp() dirs this module created
    (the _CACHE_DIR_PREFIX prefix), never anything else in the temp root.
    """
    try:
        temp_root = Path(tempfile.gettempdir())
        for entry in temp_root.iterdir():
            if entry.name.startswith(_CACHE_DIR_PREFIX) and entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass  # never let a cleanup sweep block startup


@dataclass(frozen=True)
class BackupFile:
    file_id: str
    domain: str
    relative_path: str
    size: int
    path_on_disk: Path
    encryption_key: bytes | None = None  # 4-byte class + 40-byte wrapped key (encrypted backups only)
    protection_class: int | None = None


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
        "safari_history": ("HomeDomain", "Library/Safari/History.db"),
        "health": ("HomeDomain", "Library/Health/healthdb_secure.sqlite"),
        "voicemail": ("HomeDomain", "Library/Voicemail/voicemail.db"),
    }

    def __init__(self, backup_dir: Path, password: str | None = None) -> None:
        self.backup_dir = Path(backup_dir)
        manifest_db = self.backup_dir / "Manifest.db"
        if not manifest_db.exists():
            raise FileNotFoundError(f"No Manifest.db found in {self.backup_dir}")

        manifest_plist_path = self.backup_dir / "Manifest.plist"
        manifest_plist: dict = {}
        if manifest_plist_path.exists():
            with open(manifest_plist_path, "rb") as f:
                manifest_plist = plistlib.load(f)
        self._manifest_plist = manifest_plist
        self.is_encrypted = bool(manifest_plist.get("IsEncrypted", False))
        self._class_keys: dict[int, bytes] = {}
        self._conn: sqlite3.Connection | None = None
        self._cache_dir: Path | None = None  # holds the decrypted Manifest.db, if any; see close()

        if not self.is_encrypted:
            self.needs_password = False
            self._conn = sqlite3.connect(f"file:{manifest_db}?mode=ro", uri=True)
            return

        # Encrypted backup: needs_password stays True until a correct password unlocks
        # it. Constructing without a password (or with one that's wrong) still leaves
        # a usable object — info() works, find()/extract() raise BackupPasswordError —
        # so callers can probe .is_encrypted before asking the user for a password.
        self.needs_password = True
        if password is None:
            return

        keybag_data = manifest_plist.get("BackupKeyBag")
        if not keybag_data:
            raise BackupPasswordError("This backup is encrypted but has no key bag to unlock it.")
        keybag = BackupKeyBag.parse(bytes(keybag_data))
        self._class_keys = keybag.derive_class_keys(password)
        self.needs_password = False

        decrypted_manifest = self._decrypt_manifest_db()
        self._conn = sqlite3.connect(f"file:{decrypted_manifest}?mode=ro", uri=True)

    def _decrypt_manifest_db(self) -> Path:
        manifest_key = self._manifest_plist.get("ManifestKey")
        if not manifest_key or len(manifest_key) < 5:
            raise BackupPasswordError("Manifest.plist has no usable ManifestKey.")
        manifest_key = bytes(manifest_key)
        clas = int.from_bytes(manifest_key[:4], "little")
        wrapped = manifest_key[4:]
        class_key = self._class_keys.get(clas)
        if class_key is None:
            raise BackupPasswordError(
                f"No unlocked class key for Manifest.db (protection class {clas})."
            )
        file_key = _aes_key_unwrap(class_key, wrapped)

        encrypted = (self.backup_dir / "Manifest.db").read_bytes()
        plaintext = _strip_pkcs7(_aes_cbc_decrypt_zero_iv(file_key, encrypted))
        if not plaintext.startswith(b"SQLite format 3\x00"):
            raise BackupPasswordError("Incorrect backup password.")

        # This holds a full plaintext copy of the backup's manifest (SMS/contacts/
        # notes index) - it must never land somewhere world-readable or predictable,
        # and never as a sibling of backup_dir (which can be any folder a file picker
        # reached, e.g. a USB stick or network share - not somewhere Salvage should be
        # writing). mkdtemp() gives a per-user, 0700 directory regardless of umask;
        # the file itself is opened with an explicit 0600 mode at creation, not
        # chmod'd afterward. Deleted in close() once the caller is done with it.
        cache_dir = Path(tempfile.mkdtemp(prefix=_CACHE_DIR_PREFIX))
        self._cache_dir = cache_dir
        dest = cache_dir / "Manifest.decrypted.db"
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            output = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with output:
            output.write(plaintext)
        return dest

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._cache_dir is not None:
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None

    def __enter__(self) -> "BackupReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

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
        result["is_encrypted"] = self.is_encrypted
        return result

    def find(
        self, domain: str | None = None, relative_path_like: str | None = None
    ) -> list[BackupFile]:
        if self._conn is None:
            raise BackupPasswordError(
                "This backup is encrypted; provide the correct backup password first."
            )
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

            encryption_key: bytes | None = None
            protection_class: int | None = None
            if self.is_encrypted:
                # On-disk size is the padded ciphertext, not the real size — always
                # prefer the manifest's recorded Size for encrypted backups.
                encryption_key, protection_class = _manifest_file_keys(file_blob)
                manifest_size = _manifest_file_size(file_blob)
                size = manifest_size if manifest_size is not None else path_on_disk.stat().st_size
            else:
                # The manifest's recorded Size can drift from the actual payload (observed
                # on a real device backup, e.g. sms.db resized between manifest write and
                # transfer); the on-disk copy is what extract() actually hands back, so
                # trust it first.
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
                    encryption_key=encryption_key,
                    protection_class=protection_class,
                )
            )
        return results

    def extract(self, bf: BackupFile, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not self.is_encrypted:
            shutil.copy2(bf.path_on_disk, dest)
            return dest

        if bf.encryption_key is None or len(bf.encryption_key) < 5:
            raise BackupPasswordError(f"No encryption key recorded for {bf.relative_path}.")
        clas = int.from_bytes(bf.encryption_key[:4], "little")
        wrapped = bf.encryption_key[4:]
        class_key = self._class_keys.get(clas)
        if class_key is None:
            raise BackupPasswordError(
                f"No unlocked class key for {bf.relative_path} (protection class {clas})."
            )
        file_key = _aes_key_unwrap(class_key, wrapped)
        ciphertext = bf.path_on_disk.read_bytes()
        plaintext = _aes_cbc_decrypt_zero_iv(file_key, ciphertext)
        dest.write_bytes(plaintext[: bf.size])  # trim CBC padding using the manifest's real size
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


def _afc_quote(value: str) -> str:
    """Quote a value for afcclient's own interactive command line (it reads commands
    newline-delimited from stdin and tokenises each line itself). Escapes backslashes
    and double quotes - same order as privileged._applescript_escape, backslashes
    first - so a device-supplied filename can't break out of the quoted argument and
    smuggle in a second afcclient command. Embedded CR/LF are stripped outright:
    those would start a new command line regardless of quoting, since afcclient's
    input is newline-delimited."""
    value = value.replace("\r", "").replace("\n", "")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


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
    raw = _afc_run(udid, [f"ls -l {_afc_quote(path)}"])
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
    _afc_run(udid, [f"get {_afc_quote(remote)} {_afc_quote(str(dest))}"], timeout=120)
    if not dest.exists():
        raise IOSBackupError(f"Failed to pull {remote} from device via AFC.")
    return dest
