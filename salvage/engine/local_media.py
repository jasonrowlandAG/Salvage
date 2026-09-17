"""Exhaustive search of every place photos/videos hide on a Mac, deduplicated.

On a FileVault Mac, raw sector carving of the startup disk is impossible (the
disk is encrypted at rest and PhotoRec needs unallocated space in the clear).
The honest, effective alternative for "my photos disappeared" is not carving —
it's searching every place media actually lives: the Photos library
(including its Recently Deleted state), Messages/WhatsApp attachments, iCloud
Drive and other cloud-sync folders, plain file folders, and old iPhone
backups — then deduplicating by content hash so the same photo found in three
places only needs reviewing once.

This module never writes to or modifies anything it scans; `export_found` is
the only function that touches disk, and it only ever copies.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from salvage.engine.ios import BackupPasswordError, BackupReader
from salvage.engine.models import CATEGORY_BY_EXT, Category, category_for

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - exercised only when the dep is missing
    pass

MEDIA_EXTENSIONS: set[str] = {ext for ext, cat in CATEGORY_BY_EXT.items() if cat in ("image", "video")} | {"raw"}

_FULL_DISK_ACCESS_NOTE = "Grant Salvage Full Disk Access in System Settings → Privacy & Security."
_NOT_FOUND_NOTE = "Not found on this Mac."

_SKIP_DIR_NAMES = {
    "node_modules",
    "caches",
    "cache",
    "gpucache",
    "code cache",
    "dawncache",
    "cachestorage",
    "service worker",
}
_RECOVERED_DIR_RE = re.compile(r"^recovered(\.\d+)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaSource:
    key: str
    label: str
    path: Path
    kind: Literal["folder", "photos_library", "messages", "whatsapp", "ios_backup", "cloud"]
    accessible: bool
    note: str | None = None
    default_on: bool = True   # False for sources that are slow to even list (on-demand cloud mounts)


@dataclass
class FoundMedia:
    path: Path
    name: str
    ext: str
    size: int
    category: Category
    source_key: str
    taken: datetime | None
    modified: datetime
    sha256: str | None
    duplicate_of: Path | None
    in_recently_deleted: bool = False
    note: str | None = None
    recovered: bool = False
    cloud_placeholder: bool = False   # iCloud file not downloaded; reading it would trigger a download
    preview_only: bool = False        # Photos library original is iCloud-only; `path` is a local preview derivative


@dataclass
class ScanStats:
    files_seen: int = 0
    media_found: int = 0
    bytes: int = 0
    sources_done: int = 0
    sources_total: int = 0
    current: str = ""


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _check_accessible(path: Path) -> tuple[bool, str | None]:
    try:
        os.listdir(path)
        return True, None
    except PermissionError:
        return False, _FULL_DISK_ACCESS_NOTE
    except FileNotFoundError:
        return False, _NOT_FOUND_NOTE
    except OSError as exc:
        return False, str(exc)


def _find_whatsapp_media(home: Path) -> Path | None:
    candidates: list[Path] = []
    group_containers = home / "Library" / "Group Containers"
    if group_containers.is_dir():
        try:
            for entry in os.listdir(group_containers):
                if "whatsapp" in entry.lower():
                    candidates.append(group_containers / entry)
        except OSError:
            pass
    sandboxed = home / "Library" / "Containers" / "net.whatsapp.WhatsApp"
    if sandboxed.is_dir():
        candidates.append(sandboxed)

    for base in candidates:
        for sub in ("Message/Media", "Media", "Data/Message/Media", "Data/Library/Media"):
            media_path = base / sub
            if media_path.is_dir():
                return media_path
    return candidates[0] if candidates else None


def _ios_backup_label(backup_dir: Path, udid: str) -> str:
    try:
        reader = BackupReader(backup_dir)
        try:
            info = reader.info()
        finally:
            reader.close()
    except Exception:
        return f"iPhone Backup ({udid[:8]})"
    device = info.get("device_name") or "iPhone"
    date = info.get("last_backup_date")
    date_str = date.strftime("%d/%m/%Y") if isinstance(date, datetime) else (str(date) if date else "")
    label = f"{device} — {date_str}" if date_str else str(device)
    return label


def source_from_backup_dir(path: Path) -> MediaSource:
    """Builds a MediaSource(kind="ios_backup") from any backup folder.

    Unlike default_sources(), which only looks under MobileSync/Backup, this accepts any
    directory containing a Manifest.db — including a Salvage-made backup from the iPhone
    recovery flow — so it can back the "Add a folder or iPhone backup…" picker.
    """
    path = Path(path)
    if not (path / "Manifest.db").exists():
        raise ValueError(f"No Manifest.db found in {path}; this doesn't look like an iPhone backup.")
    accessible, note = _check_accessible(path)
    udid = path.name
    label = _ios_backup_label(path, udid)
    return MediaSource(key=f"ios_backup_{udid}", label=label, path=path, kind="ios_backup", accessible=accessible, note=note)


def default_sources() -> list[MediaSource]:
    """Enumerate every place media hides on this machine: the full Photos-library /
    Messages / iCloud Drive sweep on macOS, the real Windows equivalents on Windows
    (no Photos library or Messages exist there), or an empty list anywhere else."""
    if sys.platform == "darwin":
        return _default_sources_macos()
    if sys.platform == "win32":
        return _default_sources_windows()
    return []


def _add_ios_backups(backup_root: Path, add: Callable[[str, str, Path, str], None]) -> None:
    """Adds one MediaSource per iPhone backup found directly under `backup_root`
    (a MobileSync/Backup-style directory: one subfolder per device UDID)."""
    if not backup_root.is_dir():
        return
    try:
        entries = sorted(os.listdir(backup_root))
    except OSError:
        entries = []
    for entry in entries:
        backup_dir = backup_root / entry
        if not (backup_dir / "Manifest.db").exists():
            continue
        add(f"ios_backup_{entry}", _ios_backup_label(backup_dir, entry), backup_dir, "ios_backup")


def _default_sources_macos() -> list[MediaSource]:
    home = Path.home()
    sources: list[MediaSource] = []

    def add(key: str, label: str, path: Path, kind: str) -> None:
        accessible, note = _check_accessible(path)
        sources.append(MediaSource(key=key, label=label, path=path, kind=kind, accessible=accessible, note=note))

    add("pictures", "Pictures", home / "Pictures", "folder")
    add("desktop", "Desktop", home / "Desktop", "folder")
    add("documents", "Documents", home / "Documents", "folder")
    add("downloads", "Downloads", home / "Downloads", "folder")
    add("movies", "Movies", home / "Movies", "folder")
    add("icloud_drive", "iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs", "cloud")

    cloud_storage_root = home / "Library" / "CloudStorage"
    if cloud_storage_root.is_dir():
        try:
            entries = sorted(os.listdir(cloud_storage_root))
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith(".") or not (cloud_storage_root / entry).is_dir():
                continue
            src = MediaSource(
                key=f"cloudstorage_{entry}", label=entry, path=cloud_storage_root / entry, kind="cloud",
                accessible=os.access(cloud_storage_root / entry, os.R_OK),
                note="Cloud-only files are fetched on demand — listing is slow; tick to include",
                default_on=False,
            )
            sources.append(src)

    pictures_dir = home / "Pictures"
    if pictures_dir.is_dir():
        try:
            entries = sorted(os.listdir(pictures_dir))
        except OSError:
            entries = []
        for entry in entries:
            if entry.endswith(".photoslibrary"):
                add(f"photoslibrary_{entry}", entry, pictures_dir / entry, "photos_library")

    add("messages", "Messages Attachments", home / "Library" / "Messages" / "Attachments", "messages")

    whatsapp_path = _find_whatsapp_media(home)
    if whatsapp_path is not None:
        add("whatsapp", "WhatsApp Media", whatsapp_path, "whatsapp")
    else:
        sources.append(
            MediaSource(
                key="whatsapp",
                label="WhatsApp Media",
                path=home / "Library" / "Group Containers",
                kind="whatsapp",
                accessible=False,
                note="WhatsApp desktop media not found on this Mac.",
            )
        )

    _add_ios_backups(home / "Library" / "Application Support" / "MobileSync" / "Backup", add)

    volumes_root = Path("/Volumes")
    if volumes_root.is_dir():
        try:
            entries = sorted(os.listdir(volumes_root))
        except OSError:
            entries = []
        for entry in entries:
            vol_path = volumes_root / entry
            try:
                if os.path.realpath(vol_path) == "/":
                    continue
            except OSError:
                continue
            add(f"volume_{entry}", entry, vol_path, "folder")

    return sources


def _default_sources_windows() -> list[MediaSource]:
    """Real Windows equivalents of the macOS sweep above -- no Photos library or
    Messages exist on Windows, so those simply don't appear here."""
    home = Path.home()
    sources: list[MediaSource] = []

    def add(key: str, label: str, path: Path, kind: str = "folder") -> None:
        accessible, note = _check_accessible(path)
        sources.append(MediaSource(key=key, label=label, path=path, kind=kind, accessible=accessible, note=note))

    add("pictures", "Pictures", home / "Pictures")
    add("videos", "Videos", home / "Videos")
    add("desktop", "Desktop", home / "Desktop")
    add("documents", "Documents", home / "Documents")
    add("downloads", "Downloads", home / "Downloads")

    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        onedrive_path = Path(onedrive)
        accessible, note = _check_accessible(onedrive_path)
        if accessible:
            note = "Cloud-only files are fetched on demand — listing is slow; tick to include"
        sources.append(
            MediaSource(
                key="onedrive", label="OneDrive", path=onedrive_path, kind="cloud",
                accessible=accessible, note=note, default_on=False,
            )
        )

    # Apple's iCloud for Windows app puts photos here by default.
    icloud_photos = home / "Pictures" / "iCloud Photos" / "Photos"
    if icloud_photos.is_dir():
        sources.append(
            MediaSource(
                key="icloud_photos", label="iCloud Photos", path=icloud_photos, kind="cloud",
                accessible=os.access(icloud_photos, os.R_OK),
                note="Cloud-only files are fetched on demand — listing is slow; tick to include",
                default_on=False,
            )
        )

    appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    _add_ios_backups(appdata / "Apple Computer" / "MobileSync" / "Backup", add)
    _add_ios_backups(home / "Apple" / "MobileSync" / "Backup", add)

    return sources


# ---------------------------------------------------------------------------
# Walking / filtering
# ---------------------------------------------------------------------------


def _skip_dir(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if name.lower() in _SKIP_DIR_NAMES:
        return True
    if name.endswith(".app"):
        return True
    # .photoslibrary packages are walked by the dedicated photos_library source
    # (which also reads Photos.sqlite for Recently Deleted state); walking them
    # again here as a plain folder would re-find the same originals with no
    # trash-state, plus every internal derivative/preview/cache file Photos.app
    # keeps alongside them.
    if name.lower().endswith((".photoslibrary", ".photoslibrary/")):
        return True
    if name == "Developer" and path.parent.name == "Library":
        return True
    if _RECOVERED_DIR_RE.match(name):
        return True
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    return False


def _iter_files(root: Path, cancel: threading.Event | None):
    seen_dirs: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dp = Path(dirpath)
        try:
            st = os.stat(dirpath)
        except OSError:
            dirnames[:] = []
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(key)
        dirnames[:] = [d for d in dirnames if not _skip_dir(dp / d)]
        for name in filenames:
            if cancel is not None and cancel.is_set():
                return
            file_path = dp / name
            try:
                if file_path.is_symlink():
                    continue
            except OSError:
                continue
            yield file_path


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------


def _exif_taken(path: Path) -> datetime | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            raw = exif.get(36867) or exif.get(306)
            if not raw and hasattr(exif, "get_ifd"):
                try:
                    ifd = exif.get_ifd(0x8769)
                except Exception:
                    ifd = {}
                raw = ifd.get(36867)
            if not raw:
                return None
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


def _read_box_header(f, end: int) -> tuple[int, bytes, int] | None:
    header = f.read(8)
    if len(header) < 8:
        return None
    size = int.from_bytes(header[0:4], "big")
    box_type = header[4:8]
    header_len = 8
    if size == 1:
        size64 = f.read(8)
        if len(size64) < 8:
            return None
        size = int.from_bytes(size64, "big")
        header_len = 16
    elif size == 0:
        size = end - f.tell() + header_len
    if size < header_len:
        return None
    return size, box_type, header_len


def _find_mvhd_bytes(f, start: int, end: int) -> bytes | None:
    pos = start
    while pos < end:
        f.seek(pos)
        parsed = _read_box_header(f, end)
        if parsed is None:
            return None
        size, box_type, header_len = parsed
        if box_type == b"mvhd":
            f.seek(pos + header_len)
            return f.read(size - header_len)
        if box_type == b"moov":
            found = _find_mvhd_bytes(f, pos + header_len, pos + size)
            if found is not None:
                return found
        pos += size
    return None


_MAC_EPOCH_OFFSET = 2082844800  # seconds between 1904-01-01 and 1970-01-01


def _mvhd_creation_time(path: Path) -> datetime | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            data = _find_mvhd_bytes(f, 0, end)
            if not data or len(data) < 8:
                return None
            version = data[0]
            if version == 1:
                if len(data) < 12:
                    return None
                creation = int.from_bytes(data[4:12], "big")
            else:
                creation = int.from_bytes(data[4:8], "big")
            if creation <= 0:
                return None
            unix_ts = creation - _MAC_EPOCH_OFFSET
            if unix_ts < 0:
                return None
            return datetime.fromtimestamp(unix_ts)
    except (OSError, OverflowError, ValueError):
        return None


def _extract_taken(path: Path, ext: str, category: Category, fallback_mtime: float) -> datetime | None:
    taken: datetime | None = None
    if category == "image":
        taken = _exif_taken(path)
    elif category == "video":
        taken = _mvhd_creation_time(path)
    if taken is not None:
        return taken
    try:
        return datetime.fromtimestamp(fallback_mtime)
    except (OSError, OverflowError, ValueError):
        return None


_SF_DATALESS = 0x40000000

# Windows equivalent of macOS's dataless-file flag: set on a OneDrive/iCloud "cloud-only"
# placeholder whose bytes aren't actually on disk yet. Checking st_file_attributes (not
# opening the file) is what lets us skip it without triggering a download, exactly like
# the SF_DATALESS check does on macOS.
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000


def _is_dataless(st: os.stat_result) -> bool:
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & (_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | _FILE_ATTRIBUTE_RECALL_ON_OPEN))
    return bool(getattr(st, "st_flags", 0) & _SF_DATALESS)


def _build_found_media(
    path: Path, size: int, mtime: float, source_key: str, ext: str, display_name: str | None = None,
    dataless: bool = False,
) -> FoundMedia:
    category = category_for(ext)
    if dataless:
        taken = _safe_mtime(mtime)
    else:
        taken = _extract_taken(path, ext, category, mtime)
    try:
        modified = datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        modified = datetime.fromtimestamp(0)
    return FoundMedia(
        path=path,
        name=display_name or path.name,
        ext=ext,
        size=size,
        category=category,
        source_key=source_key,
        taken=taken,
        modified=modified,
        sha256=None,
        duplicate_of=None,
        cloud_placeholder=dataless,
        note="In iCloud — not downloaded to this Mac" if dataless else None,
    )


def _safe_mtime(mtime: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Photos library (originals/Masters + trash state from a copy of Photos.sqlite)
# ---------------------------------------------------------------------------


_CORE_DATA_EPOCH_OFFSET = 978307200  # seconds between 2001-01-01 (Core Data reference date) and 1970-01-01

_PHOTOS_ASSET_COLUMNS = ("ZUUID", "ZFILENAME", "ZDIRECTORY", "ZDATECREATED", "ZKIND", "ZTRASHEDSTATE", "ZFAVORITE")


def _photos_asset_rows(lib_root: Path) -> list[dict]:
    """Reads every ZASSET row (one per photo/video Photos knows about) off a copy of the DB.

    An iCloud-optimised library keeps `originals/` empty and stores only downsized preview
    derivatives under `resources/derivatives/` — so we can't just walk `originals/` for media
    the way older/local libraries allow. Reading ZASSET directly is the only way to enumerate
    every asset regardless of whether its original bytes are actually on this Mac.
    The live database is locked (WAL) while Photos.app may have it open, so we always work
    off a copy.
    """
    db_path = lib_root / "database" / "Photos.sqlite"
    if not db_path.exists():
        return []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "Photos.sqlite"
        try:
            shutil.copy2(db_path, tmp_db)
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                if side.exists():
                    shutil.copy2(side, Path(tmp) / (tmp_db.name + suffix))
            conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(ZASSET)").fetchall()}
                select_cols = [c for c in _PHOTOS_ASSET_COLUMNS if c in cols]
                if "ZUUID" not in select_cols or "ZFILENAME" not in select_cols:
                    return []
                cur = conn.execute(f"SELECT {', '.join(select_cols)} FROM ZASSET")
                return [dict(zip(select_cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return []


def _find_photos_original(lib_root: Path, uuid: str, filename: str) -> Path | None:
    folder = lib_root / "originals" / uuid[0].upper()
    if not folder.is_dir():
        return None
    ext = Path(filename).suffix
    candidate = folder / f"{uuid}{ext}"
    if candidate.exists():
        return candidate
    matches = sorted(folder.glob(f"{uuid}.*"))
    return matches[0] if matches else None


def _find_photos_best_derivative(lib_root: Path, uuid: str) -> Path | None:
    """Picks the largest (highest-quality) preview derivative for an asset.

    Derivatives live under resources/derivatives/<first-uuid-char>/ (and an older
    .../masters/<first-uuid-char>/ location) named like <UUID>_1_105_c.jpeg,
    <UUID>_1_102_o.jpeg, <UUID>_4_5005_c.jpeg — the numeric codes vary by asset kind and
    Photos version, so rather than hardcode them we just take the largest file that starts
    with the asset's UUID, skipping the per-frame video transcode slices (`_cvt_`).
    """
    c = uuid[0].upper()
    best: Path | None = None
    best_size = -1
    for sub in (f"resources/derivatives/{c}", f"resources/derivatives/masters/{c}"):
        folder = lib_root / sub
        if not folder.is_dir():
            continue
        try:
            entries = os.scandir(folder)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if "_cvt_" in entry.name or not entry.name.startswith(uuid):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    size = entry.stat().st_size
                except OSError:
                    continue
                if size > best_size:
                    best, best_size = Path(entry.path), size
    return best


def _scan_photos_library(source: MediaSource, found: list[FoundMedia], stats: ScanStats, cancel, emit) -> None:
    for row in _photos_asset_rows(source.path):
        if cancel is not None and cancel.is_set():
            return
        uuid = row.get("ZUUID")
        filename = row.get("ZFILENAME")
        if not uuid or not filename:
            continue
        stats.current = str(filename)
        stats.files_seen += 1

        media_path = _find_photos_original(source.path, uuid, filename)
        preview_only = media_path is None
        if media_path is None:
            media_path = _find_photos_best_derivative(source.path, uuid)
        if media_path is None:
            emit()
            continue
        try:
            st = media_path.stat()
        except OSError:
            emit()
            continue

        ext = Path(filename).suffix.lstrip(".").lower()
        category: Category = "video" if row.get("ZKIND") == 1 else "image"
        zdate = row.get("ZDATECREATED")
        taken = None
        if zdate is not None:
            try:
                taken = datetime.fromtimestamp(float(zdate) + _CORE_DATA_EPOCH_OFFSET)
            except (OSError, OverflowError, ValueError):
                taken = None
        if taken is None:
            taken = _safe_mtime(st.st_mtime)

        fm = FoundMedia(
            path=media_path,
            name=filename,
            ext=ext,
            size=st.st_size,
            category=category,
            source_key=source.key,
            taken=taken,
            modified=_safe_mtime(st.st_mtime) or datetime.fromtimestamp(0),
            sha256=None,
            duplicate_of=None,
            in_recently_deleted=bool(row.get("ZTRASHEDSTATE")),
            note="Original is in iCloud Photos — only a preview is on this Mac" if preview_only else None,
            preview_only=preview_only,
        )
        found.append(fm)
        stats.media_found += 1
        stats.bytes += st.st_size
        emit()


def _scan_generic_dir(
    source: MediaSource, found: list[FoundMedia], stats: ScanStats, cancel, emit, min_size: int
) -> None:
    for path in _iter_files(source.path, cancel):
        if cancel is not None and cancel.is_set():
            return
        ext = path.suffix.lstrip(".").lower()
        if ext not in MEDIA_EXTENSIONS:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        stats.current = str(path)
        stats.files_seen += 1
        if min_size > 0 and st.st_size < min_size:
            emit()
            continue
        fm = _build_found_media(path, st.st_size, st.st_mtime, source.key, ext, dataless=_is_dataless(st))
        found.append(fm)
        stats.media_found += 1
        stats.bytes += st.st_size
        emit()


def messages_missing_on_mac(chat_db: Path | None = None) -> dict[str, str | None]:
    """Cross-references Messages attachments against files still present on disk.

    Returns {basename: year_string_or_None} for every image/video attachment recorded in
    chat.db whose file no longer exists at its recorded path. scan_sources uses this set to
    flag matching files found inside an iPhone backup as `recovered` — i.e. the attachment
    vanished from this Mac but still exists on the phone.
    """
    if chat_db is None:
        chat_db = Path.home() / "Library" / "Messages" / "chat.db"
    chat_db = Path(chat_db)
    if not chat_db.exists():
        return {}

    home = Path.home()
    missing: dict[str, str | None] = {}
    try:
        conn = sqlite3.connect(f"file:{chat_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        query = (
            "SELECT a.filename, strftime('%Y', max(m.date)/1000000000+978307200,'unixepoch') "
            "FROM attachment a "
            "LEFT JOIN message_attachment_join j ON j.attachment_id = a.ROWID "
            "LEFT JOIN message m ON m.ROWID = j.message_id "
            "WHERE (a.mime_type LIKE 'image/%' OR a.mime_type LIKE 'video/%') "
            "AND a.filename IS NOT NULL "
            "GROUP BY a.ROWID"
        )
        try:
            rows = conn.execute(query).fetchall()
        except sqlite3.Error:
            return {}
    finally:
        conn.close()

    for filename, year in rows:
        if not filename:
            continue
        path = Path(filename.replace("~", str(home), 1)) if filename.startswith("~") else Path(filename)
        if not path.exists():
            missing.setdefault(path.name, year)
    return missing


def _scan_ios_backup(
    source: MediaSource,
    found: list[FoundMedia],
    stats: ScanStats,
    cancel,
    emit,
    missing_basenames: dict[str, str | None] | None = None,
) -> None:
    missing_basenames = missing_basenames or {}
    try:
        reader = BackupReader(source.path)
        if getattr(reader, "is_encrypted", False):
            return  # needs a password; the iPhone flow handles encrypted backups
    except (FileNotFoundError, sqlite3.Error, BackupPasswordError):
        return
    try:
        sms_matches = reader.find(domain="MediaDomain", relative_path_like="Library/SMS/Attachments/%")
        camera_matches = reader.find(domain="CameraRollDomain", relative_path_like="Media/DCIM/%")
        for bf, is_sms in [(m, True) for m in sms_matches] + [(m, False) for m in camera_matches]:
            if cancel is not None and cancel.is_set():
                return
            display_name = Path(bf.relative_path).name
            ext = Path(display_name).suffix.lstrip(".").lower()
            if ext not in MEDIA_EXTENSIONS:
                continue
            try:
                mtime = bf.path_on_disk.stat().st_mtime
            except OSError:
                continue
            stats.current = display_name
            stats.files_seen += 1
            fm = _build_found_media(bf.path_on_disk, bf.size, mtime, source.key, ext, display_name=display_name)
            if is_sms and display_name in missing_basenames:
                fm.note = "Missing from this Mac — recovered from iPhone backup"
                fm.recovered = True
            else:
                fm.note = f"from iPhone backup {source.label}"
            found.append(fm)
            stats.media_found += 1
            stats.bytes += bf.size
            emit()
    finally:
        reader.close()


# ---------------------------------------------------------------------------
# Hashing / deduplication
# ---------------------------------------------------------------------------


def _sha256_of(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _dedupe(found: list[FoundMedia], cancel: threading.Event | None) -> None:
    def hash_one(fm: FoundMedia) -> tuple[FoundMedia, str | None]:
        if (cancel is not None and cancel.is_set()) or fm.cloud_placeholder:
            return fm, None
        return fm, _sha256_of(fm.path)

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(hash_one, found))

    seen: dict[str, Path] = {}
    for fm, digest in results:
        fm.sha256 = digest
        if digest is None:
            continue
        first = seen.get(digest)
        if first is None:
            seen[digest] = fm.path
        else:
            fm.duplicate_of = first


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scan_sources(
    sources: list[MediaSource],
    on_progress: Callable[[ScanStats], None] | None = None,
    cancel: threading.Event | None = None,
    min_size: int = 20_000,
    hash_dupes: bool = True,
) -> list[FoundMedia]:
    stats = ScanStats(sources_total=len(sources))
    last_emit = 0.0

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if force or now - last_emit >= 0.2:
            on_progress(replace(stats))
            last_emit = now

    found: list[FoundMedia] = []
    missing_basenames: dict[str, str | None] | None = None
    for source in sources:
        if cancel is not None and cancel.is_set():
            break
        stats.current = f"Preparing: {source.label}"
        emit()
        if source.accessible:
            try:
                if source.kind == "photos_library":
                    _scan_photos_library(source, found, stats, cancel, emit)
                elif source.kind == "messages":
                    _scan_generic_dir(source, found, stats, cancel, emit, min_size=0)
                elif source.kind == "ios_backup":
                    if missing_basenames is None:
                        missing_basenames = messages_missing_on_mac()
                    _scan_ios_backup(source, found, stats, cancel, emit, missing_basenames)
                else:
                    _scan_generic_dir(source, found, stats, cancel, emit, min_size=min_size)
            except (OSError, PermissionError, sqlite3.Error):
                pass
        stats.sources_done += 1
        emit()

    if not (cancel is not None and cancel.is_set()) and hash_dupes:
        _dedupe(found, cancel)

    emit(force=True)
    return found


# ---------------------------------------------------------------------------
# Export (copy only, never move)
# ---------------------------------------------------------------------------


def _unique_export_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def export_found(
    items: list[FoundMedia],
    destination: Path,
    on_progress: Callable[[int, int], None] | None = None,
    include_duplicates: bool = False,
) -> list[Path]:
    destination = Path(destination)
    to_copy = [it for it in items if include_duplicates or it.duplicate_of is None]
    total = len(to_copy)
    copied: list[Path] = []
    for i, fm in enumerate(to_copy, start=1):
        best_date = fm.taken or fm.modified
        year = str(best_date.year) if best_date else "Unknown"
        target_dir = destination / year
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = _unique_export_path(target_dir / fm.name)
        shutil.copy2(fm.path, dest_path)
        copied.append(dest_path)
        if on_progress is not None:
            on_progress(i, total)
    return copied
