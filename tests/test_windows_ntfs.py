"""NTFS recovery, exercised for real on a Windows CI runner.

NTFS is the one filesystem macOS physically cannot test (`hdiutil` has no NTFS
option, and there's no formatting tool for it on macOS -- see bench/images.py's
NTFS_NOT_TESTABLE) even though it's the single most important filesystem for the
Windows market this app is launching into. This builds a real NTFS volume with
`diskpart` inside a fixed-size VHD, plants known files in nested folders, deletes
some, detaches, and scans the VHD directly as an image file -- a fixed VHD is raw
sectors plus a 512-byte footer appended at the very end, well past anything the
filesystem/partition table point at, so every engine here reads it exactly like
any other raw disk image.

Skipped entirely off Windows. Unlike tests/test_filesystem.py (which skips
quietly if sleuthkit/hdiutil happen to be missing on some third-party dev
machine), this file makes no such allowance: it's the CI-controlled proof that
Part 1's Windows tool setup actually works, so a missing tool should fail loudly,
not skip quietly.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from salvage.engine.combined import CombinedEngine
from salvage.engine.filesystem import FilesystemEngine
from salvage.engine.models import ScanMode
from salvage.engine.photorec import PhotoRecEngine

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="diskpart/VHD NTFS volumes are Windows-only")

VHD_SIZE_MB = 96

_JPEG_NAME = "vacation_photo.jpg"
_DOCX_NAME = "notes_from_trip.docx"
_PDF_NAME = "invoice_report.pdf"

# fls (Sleuth Kit) always reports paths with forward slashes, regardless of host OS
# or the filesystem being read -- these directories are planted via pathlib (which
# uses real Windows separators for the actual filesystem calls) but compared
# against fls's forward-slash-joined output.
_JPEG_DIR_PARTS = ("Photos", "2024")
_DOC_DIR_PARTS = ("Documents", "Reports")
_JPEG_DIR = "/".join(_JPEG_DIR_PARTS)
_DOC_DIR = "/".join(_DOC_DIR_PARTS)


def _run_diskpart(script_text: str, timeout: float = 90) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(script_text)
        script_path = f.name
    try:
        return subprocess.run(
            ["diskpart", "/s", script_path], capture_output=True, text=True, timeout=timeout
        )
    finally:
        Path(script_path).unlink(missing_ok=True)


def _free_drive_letter() -> str:
    for letter in "TUVWXYZ":
        if not Path(f"{letter}:\\").exists():
            return letter
    raise RuntimeError("no free drive letter available for the test NTFS volume")


def _make_minimal_docx() -> bytes:
    """A genuinely valid (if minimal) OOXML .docx -- just enough for PhotoRec's
    zip-signature carving and a real zipfile round-trip, not a full Word doc."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Salvage NTFS recovery test document.</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    return buf.getvalue()


@pytest.fixture(scope="module")
def ntfs_volume(tmp_path_factory):
    """Creates a fixed VHD, formats it NTFS, plants three known files in nested
    folders, deletes two of them (keeping one live -- same shape as
    tests/test_filesystem.py's macOS fixtures), detaches, and returns the VHD path
    plus every planted file's original bytes for byte-identical comparison."""
    base = tmp_path_factory.mktemp("ntfs")
    vhd_path = base / "test_ntfs.vhd"
    letter = _free_drive_letter()

    create_script = (
        f'create vdisk file="{vhd_path}" maximum={VHD_SIZE_MB} type=fixed\r\n'
        f'select vdisk file="{vhd_path}"\r\n'
        "attach vdisk\r\n"
        "create partition primary\r\n"
        "format fs=ntfs quick label=TESTNTFS\r\n"
        f"assign letter={letter}\r\n"
        "exit\r\n"
    )
    proc = _run_diskpart(create_script)
    assert proc.returncode == 0, f"diskpart create/attach/format failed:\n{proc.stdout}\n{proc.stderr}"

    mount = Path(f"{letter}:\\")
    for _ in range(20):
        if mount.exists():
            break
        time.sleep(0.5)
    assert mount.exists(), f"NTFS volume did not mount at {mount} after diskpart reported success"

    try:
        jpeg_dir = mount.joinpath(*_JPEG_DIR_PARTS)
        jpeg_dir.mkdir(parents=True, exist_ok=True)
        jpeg_path = jpeg_dir / _JPEG_NAME
        Image.new("RGB", (48, 32), color=(90, 140, 200)).save(jpeg_path, "JPEG")
        jpeg_bytes = jpeg_path.read_bytes()

        doc_dir = mount.joinpath(*_DOC_DIR_PARTS)
        doc_dir.mkdir(parents=True, exist_ok=True)
        docx_path = doc_dir / _DOCX_NAME
        docx_bytes = _make_minimal_docx()
        docx_path.write_bytes(docx_bytes)

        pdf_path = doc_dir / _PDF_NAME
        pdf_bytes = b"%PDF-1.4\nfake pdf content for the Windows NTFS recovery test\n"
        pdf_path.write_bytes(pdf_bytes)

        # Delete two of the three; the pdf stays live, mirroring test_filesystem.py's
        # "one never-deleted file must NOT show up in the default deleted-only scan"
        # assertion.
        jpeg_path.unlink()
        docx_path.unlink()
    finally:
        detach_script = f'select vdisk file="{vhd_path}"\r\ndetach vdisk\r\nexit\r\n'
        _run_diskpart(detach_script)

    return {
        "vhd_path": vhd_path,
        "jpeg_bytes": jpeg_bytes,
        "docx_bytes": docx_bytes,
        "pdf_bytes": pdf_bytes,
    }


def _find(files, name: str):
    return next((f for f in files if f.original_name == name), None)


# ---------------------------------------------------------------------------
# FilesystemEngine (Quick scan): real names, real folders, byte-identical
# ---------------------------------------------------------------------------


def test_filesystem_engine_recovers_deleted_ntfs_files_byte_identical(tmp_path, ntfs_volume):
    engine = FilesystemEngine()
    result = engine.scan(ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.QUICK)

    assert result.success, result.error
    assert not result.cancelled

    jpeg = _find(result.files, _JPEG_NAME)
    assert jpeg is not None, f"jpeg not recovered; got {[f.original_name for f in result.files]}"
    assert jpeg.original_dir == _JPEG_DIR
    assert jpeg.deleted is True
    assert jpeg.source_engine == "sleuthkit"
    assert jpeg.path.read_bytes() == ntfs_volume["jpeg_bytes"]

    docx = _find(result.files, _DOCX_NAME)
    assert docx is not None
    assert docx.original_dir == _DOC_DIR
    assert docx.deleted is True
    assert docx.path.read_bytes() == ntfs_volume["docx_bytes"]

    # The never-deleted pdf is correctly excluded from the default deleted-only scan.
    assert _find(result.files, _PDF_NAME) is None


def test_filesystem_engine_include_existing_finds_live_pdf(tmp_path, ntfs_volume):
    engine = FilesystemEngine()
    result = engine.scan(ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.QUICK, include_existing=True)

    assert result.success, result.error
    pdf = _find(result.files, _PDF_NAME)
    assert pdf is not None
    assert pdf.deleted is False
    assert pdf.path.read_bytes() == ntfs_volume["pdf_bytes"]


# ---------------------------------------------------------------------------
# PhotoRecEngine (Deep scan): carves by signature, and proves pywinpty actually
# streams live progress rather than only reporting once at exit
# ---------------------------------------------------------------------------


def test_photorec_carves_deleted_ntfs_files_with_live_progress(tmp_path, ntfs_volume, capsys):
    engine = PhotoRecEngine()
    progress_updates = []
    result = engine.scan(
        ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.DEEP, on_progress=progress_updates.append
    )

    assert result.success, result.error
    assert not result.cancelled
    assert result.files, "PhotoRec carved nothing from the NTFS image"

    carved_bytes = {f.path.read_bytes() for f in result.files}
    assert ntfs_volume["jpeg_bytes"] in carved_bytes, "carved JPEG bytes not found among PhotoRec's results"
    assert ntfs_volume["docx_bytes"] in carved_bytes, "carved DOCX bytes not found among PhotoRec's results"

    # If pywinpty silently isn't working and photorec.py fell back to a plain,
    # fully block-buffered pipe (the bug this exists to catch -- see
    # salvage/engine/photorec.py), PhotoRec's progress line only ever shows up in
    # one final flush right before exit, so at most one distinct non-zero sector
    # reading is ever observed. A real streaming scan reports many.
    sector_values = sorted({p.sector for p in progress_updates if p.sector > 0})
    with capsys.disabled():
        print(
            f"\nphotorec live-progress check: {len(progress_updates)} on_progress call(s), "
            f"{len(sector_values)} distinct non-zero sector reading(s)"
        )
    assert len(sector_values) >= 2, (
        "expected PhotoRec's sector count to advance across multiple live progress "
        f"updates via pywinpty, not jump once at exit; observed {sector_values}"
    )


# ---------------------------------------------------------------------------
# CombinedEngine (Thorough scan): filesystem + carve merged, no duplicates
# ---------------------------------------------------------------------------


def test_combined_engine_merges_ntfs_results_without_duplicates(tmp_path, ntfs_volume, capsys):
    combined = CombinedEngine(FilesystemEngine(), PhotoRecEngine())
    result = combined.scan(ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.THOROUGH)

    assert result.success, result.error

    jpeg_matches = [f for f in result.files if f.path.read_bytes() == ntfs_volume["jpeg_bytes"]]
    assert len(jpeg_matches) == 1, (
        "the deleted JPEG's filesystem record and carved copy are the same bytes and "
        f"must merge into exactly one result, got {len(jpeg_matches)}"
    )
    assert jpeg_matches[0].original_name == _JPEG_NAME, "merge should keep the named filesystem copy, not the anonymous carve"

    docx_matches = [f for f in result.files if f.path.read_bytes() == ntfs_volume["docx_bytes"]]
    assert len(docx_matches) == 1
    assert docx_matches[0].original_name == _DOCX_NAME

    named = sorted(f.original_name for f in result.files if f.original_name)
    with capsys.disabled():
        print(
            f"\nNTFS recovery summary (Thorough/CombinedEngine): {len(result.files)} total "
            f"result(s) merged from filesystem + carve, {len(named)} with recovered original "
            f"names: {named}"
        )
