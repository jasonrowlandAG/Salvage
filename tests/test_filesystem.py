"""Tests for the Sleuth-Kit-backed FilesystemEngine ("Quick scan").

Builds real FAT32/exFAT/HFS+ volumes with `hdiutil` (macOS only), plants known
files in nested folders, deletes some, then converts the volume to a raw
image and scans it. Requires `fls`/`icat`/`fsstat`/`mmls` (sleuthkit) and
`hdiutil`; skipped entirely if either is missing or we're not on macOS.

hdiutil attachments are the main leak hazard here (a forgotten mount lingers
across test runs) -- every attach is paired with a `finally: hdiutil detach`.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image

from salvage.engine.filesystem import FilesystemEngine
from salvage.engine.models import ScanMode

_binaries = FilesystemEngine.locate_binaries()
_hdiutil = subprocess.run(["which", "hdiutil"], capture_output=True).returncode == 0 if platform.system() == "Darwin" else False

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or _binaries is None or not _hdiutil,
    reason="needs macOS (hdiutil) and sleuthkit (fls/icat/fsstat/mmls)",
)

_JPEG_NAME = "vacation_photo.jpg"
_TXT_NAME = "notes_from_trip.txt"
_PDF_NAME = "invoice_report.pdf"
_JPEG_DIR = "DCIM/100APPLE"
_PDF_DIR = "DCIM"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)


def _build_volume(tmp_path_factory, hdiutil_fs: str, volname: str, size_mb: int) -> dict:
    """Creates an hdiutil volume of `hdiutil_fs` type, plants known files,
    deletes two of them, converts to a raw image, and returns everything a
    test needs: the raw image path plus the planted files' original bytes."""
    base = tmp_path_factory.mktemp(volname)
    dmg_path = base / f"{volname}.dmg"
    mount_point = base / "mnt"
    mount_point.mkdir()

    create = _run(
        ["hdiutil", "create", "-size", f"{size_mb}m", "-fs", hdiutil_fs, "-volname", volname, str(dmg_path)]
    )
    assert create.returncode == 0, f"hdiutil create failed: {create.stderr}"

    attach = _run(["hdiutil", "attach", str(dmg_path), "-mountpoint", str(mount_point), "-nobrowse"])
    assert attach.returncode == 0, f"hdiutil attach failed: {attach.stderr}"

    try:
        jpeg_dir = mount_point / _JPEG_DIR
        jpeg_dir.mkdir(parents=True, exist_ok=True)
        jpeg_path = jpeg_dir / _JPEG_NAME
        Image.new("RGB", (48, 32), color=(90, 140, 200)).save(jpeg_path, "JPEG")
        jpeg_bytes = jpeg_path.read_bytes()

        txt_path = jpeg_dir / _TXT_NAME
        txt_bytes = b"notes from the trip, kept around\n"
        txt_path.write_bytes(txt_bytes)

        pdf_path = mount_point / _PDF_DIR / _PDF_NAME
        pdf_bytes = b"%PDF-1.4\nfake pdf content for FilesystemEngine tests\n"
        pdf_path.write_bytes(pdf_bytes)

        _run(["sync"])
        jpeg_path.unlink()
        pdf_path.unlink()
        _run(["sync"])
    finally:
        _run(["hdiutil", "detach", str(mount_point)])

    raw_base = base / f"{volname}.raw"
    convert = _run(["hdiutil", "convert", str(dmg_path), "-format", "UDTO", "-o", str(raw_base)])
    assert convert.returncode == 0, f"hdiutil convert failed: {convert.stderr}"
    raw_path = raw_base.with_name(raw_base.name + ".cdr")
    assert raw_path.exists()

    return {
        "raw_path": raw_path,
        "jpeg_bytes": jpeg_bytes,
        "txt_bytes": txt_bytes,
        "pdf_bytes": pdf_bytes,
    }


@pytest.fixture(scope="module")
def fat32_volume(tmp_path_factory):
    return _build_volume(tmp_path_factory, "MS-DOS FAT32", "TESTFAT32", 64)


@pytest.fixture(scope="module")
def exfat_volume(tmp_path_factory):
    return _build_volume(tmp_path_factory, "ExFAT", "TESTEXFAT", 32)


@pytest.fixture(scope="module")
def hfsplus_volume(tmp_path_factory):
    return _build_volume(tmp_path_factory, "HFS+", "TESTHFSP", 32)


def _find(files, name: str):
    return next((f for f in files if f.original_name == name), None)


# ---------------------------------------------------------------------------
# probe()
# ---------------------------------------------------------------------------


def test_probe_reports_fat32(fat32_volume):
    volumes = FilesystemEngine.probe(fat32_volume["raw_path"])
    assert len(volumes) == 1
    assert volumes[0].fstype == "fat32"
    assert "FAT32" in volumes[0].fstype_label


def test_probe_reports_exfat(exfat_volume):
    volumes = FilesystemEngine.probe(exfat_volume["raw_path"])
    assert len(volumes) == 1
    assert volumes[0].fstype == "exfat"
    assert "exFAT" in volumes[0].fstype_label


def test_probe_reports_hfsplus(hfsplus_volume):
    volumes = FilesystemEngine.probe(hfsplus_volume["raw_path"])
    assert len(volumes) == 1
    assert volumes[0].fstype == "hfsp"
    assert "HFS+" in volumes[0].fstype_label


def test_probe_unreadable_source_returns_empty(tmp_path):
    blank = tmp_path / "blank.raw"
    blank.write_bytes(b"\x00" * (1024 * 1024))
    assert FilesystemEngine.probe(blank) == []


# ---------------------------------------------------------------------------
# scan(): recovers deleted files with real names/folders/dates
# ---------------------------------------------------------------------------


def test_quick_scan_recovers_deleted_fat32(tmp_path, fat32_volume):
    engine = FilesystemEngine()
    result = engine.scan(fat32_volume["raw_path"], tmp_path, mode=ScanMode.QUICK)

    assert result.success
    assert result.error is None
    assert not result.cancelled

    jpeg = _find(result.files, _JPEG_NAME)
    assert jpeg is not None, [f.original_name for f in result.files]
    assert jpeg.original_dir == _JPEG_DIR
    assert jpeg.deleted is True
    assert jpeg.source_engine == "sleuthkit"
    assert jpeg.path.read_bytes() == fat32_volume["jpeg_bytes"]
    assert jpeg.modified is not None

    pdf = _find(result.files, _PDF_NAME)
    assert pdf is not None
    assert pdf.original_dir == _PDF_DIR
    assert pdf.path.read_bytes() == fat32_volume["pdf_bytes"]

    # The live (never-deleted) file is not part of the default, deleted-only result.
    assert _find(result.files, _TXT_NAME) is None


def test_quick_scan_recovers_deleted_exfat(tmp_path, exfat_volume):
    engine = FilesystemEngine()
    result = engine.scan(exfat_volume["raw_path"], tmp_path, mode=ScanMode.QUICK)

    assert result.success
    assert result.error is None

    jpeg = _find(result.files, _JPEG_NAME)
    assert jpeg is not None
    assert jpeg.original_dir == _JPEG_DIR
    assert jpeg.path.read_bytes() == exfat_volume["jpeg_bytes"]
    assert jpeg.modified is not None

    pdf = _find(result.files, _PDF_NAME)
    assert pdf is not None
    assert pdf.path.read_bytes() == exfat_volume["pdf_bytes"]


def test_quick_scan_hfsplus_deletion_loses_catalog_entry(tmp_path, hfsplus_volume):
    """Documented Sleuth Kit / HFS+ limitation: HFS+ removes a file's catalog
    record at delete time (no tombstone), so `fls` has no name to recover from
    for an ordinary deletion. This must come back as a valid, honest empty
    result -- not a scan failure."""
    engine = FilesystemEngine()
    result = engine.scan(hfsplus_volume["raw_path"], tmp_path, mode=ScanMode.QUICK)

    assert result.success
    assert result.error is None
    assert result.files == []


# ---------------------------------------------------------------------------
# include_existing, extensions filter, cancellation
# ---------------------------------------------------------------------------


def test_include_existing_adds_live_files(tmp_path, exfat_volume):
    engine = FilesystemEngine()
    result = engine.scan(
        exfat_volume["raw_path"], tmp_path, mode=ScanMode.QUICK, include_existing=True
    )
    assert result.success
    txt = _find(result.files, _TXT_NAME)
    assert txt is not None
    assert txt.deleted is False
    assert txt.path.read_bytes() == exfat_volume["txt_bytes"]


def test_extension_filter_limits_results(tmp_path, exfat_volume):
    engine = FilesystemEngine()
    result = engine.scan(
        exfat_volume["raw_path"], tmp_path, mode=ScanMode.QUICK, extensions=["jpg"]
    )
    assert result.success
    assert result.files
    assert all(f.ext == "jpg" for f in result.files)


def test_cancel_stops_scan(tmp_path, exfat_volume):
    engine = FilesystemEngine()
    cancel = threading.Event()
    cancel.set()
    result = engine.scan(
        exfat_volume["raw_path"], tmp_path, mode=ScanMode.QUICK, cancel=cancel
    )
    assert result.cancelled
    assert not result.success


def test_scan_no_filesystem_suggests_deep_scan(tmp_path):
    blank = tmp_path / "blank.raw"
    blank.write_bytes(b"\x00" * (2 * 1024 * 1024))
    engine = FilesystemEngine()
    result = engine.scan(blank, tmp_path / "work", mode=ScanMode.QUICK)
    assert not result.success
    assert not result.cancelled
    assert result.error is not None
    assert "Deep scan" in result.error
