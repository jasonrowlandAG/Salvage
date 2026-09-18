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
import os
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

# NTFS stores a small file's data *resident*, inside its own MFT record, rather than
# in a data cluster -- so a tiny planted file leaves nothing in the data area for
# PhotoRec to carve, and a deleted resident record's attribute can be reused/cleared
# faster than a real allocated cluster. Every planted file here is comfortably over
# that threshold (order of a few hundred bytes) so this test exercises the normal,
# realistic non-resident path recovery tools actually deal with.
_PDF_MARKER = b"SALVAGE-NTFS-TEST-MARKER-3f6a1c"
_PDF_PAD_SIZE = 250_000
_DOCX_BLOB_SIZE = 250_000

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


def _log(msg: str) -> None:
    """Write straight to the real stderr, bypassing pytest's output capture, so
    diagnostics show up in CI regardless of which test (if any) ends up failing --
    plain print() would only surface if the specific test/fixture invocation that
    produced it were the one that failed, and ntfs_volume is a module-scoped
    fixture whose setup output pytest attaches to whichever test triggers it first,
    not necessarily the one that later fails."""
    print(msg, file=sys.__stderr__, flush=True)


def _free_drive_letter() -> str:
    for letter in "TUVWXYZ":
        if not Path(f"{letter}:\\").exists():
            return letter
    raise RuntimeError("no free drive letter available for the test NTFS volume")


def _win_path(p: Path) -> str:
    """A guaranteed backslash-separated path string. diskpart is a minimal, old
    command interpreter -- don't rely on however Path.__str__/__format__ happens to
    render a given Path (observed producing forward slashes for some paths in CI)."""
    return str(p).replace("/", "\\")


def _verify_volume_via_powershell(letter: str) -> str:
    """Independent confirmation (not diskpart) that `letter:` really is the NTFS
    volume this fixture just formatted, for diagnostics if anything downstream comes
    back empty. Returns a human-readable description; raises with full detail if the
    volume isn't what's expected."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Volume -DriveLetter {letter} | Format-List | Out-String"],
        capture_output=True, text=True, timeout=30,
    )
    info = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 or "NTFS" not in info:
        raise AssertionError(f"Get-Volume -DriveLetter {letter} does not show an NTFS volume:\n{info}")
    return info


def _make_minimal_docx(blob_size: int = _DOCX_BLOB_SIZE) -> bytes:
    """A genuinely valid (if minimal) OOXML .docx -- enough for PhotoRec's zip-
    signature carving and a real zipfile round-trip, not a full Word doc. Embeds an
    incompressible, uncompressed "media" blob (docx files routinely embed images)
    so the final file size stays comfortably non-resident regardless of how well
    DEFLATE compresses the small XML parts."""
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
        z.writestr("word/media/image1.bin", os.urandom(blob_size), compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


@pytest.fixture(scope="module")
def ntfs_volume(tmp_path_factory):
    """Creates a fixed VHD, formats it NTFS, plants three known files in nested
    folders, deletes two of them (keeping one live -- same shape as
    tests/test_filesystem.py's macOS fixtures), detaches, and returns the VHD path
    plus every planted file's original bytes for byte-identical comparison."""
    return _build_ntfs_volume(tmp_path_factory)


def _build_ntfs_volume(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("ntfs")
    vhd_path = base / "test_ntfs.vhd"
    vhd_str = _win_path(vhd_path)
    letter = _free_drive_letter()

    create_script = (
        f'create vdisk file="{vhd_str}" maximum={VHD_SIZE_MB} type=fixed\r\n'
        f'select vdisk file="{vhd_str}"\r\n'
        "attach vdisk\r\n"
        "create partition primary\r\n"
        "format fs=ntfs quick label=TESTNTFS\r\n"
        f"assign letter={letter}\r\n"
        "list volume\r\n"
        "exit\r\n"
    )
    proc = _run_diskpart(create_script)
    diskpart_report = f"diskpart script:\n{create_script}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    _log(f"\n{diskpart_report}")
    # diskpart's own process exit code reflects whether the script engine ran at all,
    # not whether every individual command inside it succeeded -- a failed command
    # still typically leaves the exit code at 0, so check its own reported text too.
    assert proc.returncode == 0, f"diskpart exited {proc.returncode}\n{diskpart_report}"
    assert "error" not in proc.stdout.lower(), f"diskpart reported an error:\n{diskpart_report}"

    mount = Path(f"{letter}:\\")
    for _ in range(40):
        if mount.exists():
            break
        time.sleep(0.5)
    assert mount.exists(), f"NTFS volume did not mount at {mount} after diskpart:\n{diskpart_report}"

    # Independent confirmation (via Get-Volume, not diskpart) that this letter really
    # is the NTFS volume just formatted -- catches a silently-wrong letter/format
    # rather than leaving "recovered nothing" to be debugged blind three tests later.
    volume_info = _verify_volume_via_powershell(letter)
    _log(f"\nGet-Volume -DriveLetter {letter}:\n{volume_info}")

    try:
        jpeg_dir = mount.joinpath(*_JPEG_DIR_PARTS)
        jpeg_dir.mkdir(parents=True, exist_ok=True)
        jpeg_path = jpeg_dir / _JPEG_NAME
        # Random pixel noise (not a flat colour) so JPEG's DCT can't compress it away
        # to a resident-sized file; 600x450 lands comfortably in the hundreds-of-KB
        # range regardless of quality settings.
        Image.frombytes("RGB", (600, 450), os.urandom(600 * 450 * 3)).save(jpeg_path, "JPEG", quality=90)
        jpeg_bytes = jpeg_path.read_bytes()
        _log(f"\njpeg size: {len(jpeg_bytes)} bytes")

        doc_dir = mount.joinpath(*_DOC_DIR_PARTS)
        doc_dir.mkdir(parents=True, exist_ok=True)
        docx_path = doc_dir / _DOCX_NAME
        docx_bytes = _make_minimal_docx()
        docx_path.write_bytes(docx_bytes)
        _log(f"docx size: {len(docx_bytes)} bytes")

        pdf_path = doc_dir / _PDF_NAME
        pdf_bytes = b"%PDF-1.4\n" + _PDF_MARKER + b"\n" + (b"%" + b"x" * 78 + b"\n") * (_PDF_PAD_SIZE // 80)
        pdf_path.write_bytes(pdf_bytes)
        _log(f"pdf size: {len(pdf_bytes)} bytes")

        planted = sorted(str(p.relative_to(mount)) for p in mount.rglob("*") if p.is_file())
        _log(f"\nplanted on {mount} before delete: {planted}")
        assert len(planted) == 3, f"expected 3 planted files on {mount}, found {planted}"

        # Delete two of the three; the pdf stays live, mirroring test_filesystem.py's
        # "one never-deleted file must NOT show up in the default deleted-only scan"
        # assertion.
        jpeg_path.unlink()
        docx_path.unlink()

        remaining = sorted(str(p.relative_to(mount)) for p in mount.rglob("*") if p.is_file())
        _log(f"remaining on {mount} after delete: {remaining}")
        assert remaining == [str(Path(*_DOC_DIR_PARTS) / _PDF_NAME)]

        # Explicitly flush the volume's cached writes before detaching -- diskpart's
        # own "detach vdisk" is a controlled dismount and should already do this, but
        # this makes it an explicit, checkable step rather than an assumption.
        flush_proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Write-VolumeCache -DriveLetter {letter}"],
            capture_output=True, text=True, timeout=30,
        )
        _log(f"\nWrite-VolumeCache -DriveLetter {letter}: rc={flush_proc.returncode}\n{flush_proc.stdout}{flush_proc.stderr}")
    finally:
        detach_script = f'select vdisk file="{vhd_str}"\r\ndetach vdisk\r\nexit\r\n'
        detach_proc = _run_diskpart(detach_script)
        _log(f"\ndiskpart detach stdout:\n{detach_proc.stdout}\nstderr:\n{detach_proc.stderr}")

    # Isolate fixture-vs-engine failures before any engine ever touches this file:
    # search the VHD's own raw bytes for the deleted JPEG's signature and the PDF's
    # (still-live, so definitely present) unique marker. If either is missing here,
    # the planted content never made it into the VHD's data area at all -- a fixture
    # bug, not something FilesystemEngine/PhotoRec could ever have recovered -- and
    # this fails right here with that verdict instead of three confusing engine-level
    # "recovered nothing" failures downstream.
    raw = vhd_path.read_bytes()
    jpeg_magic_present = b"\xff\xd8\xff" in raw
    marker_present = _PDF_MARKER in raw
    _log(
        f"\nraw VHD byte scan ({len(raw)} bytes): JPEG SOI marker present={jpeg_magic_present}, "
        f"PDF marker present={marker_present}"
    )
    assert jpeg_magic_present, "JPEG signature not found anywhere in the raw VHD file -- fixture wrote nothing recoverable"
    assert marker_present, "PDF marker not found anywhere in the raw VHD file -- fixture wrote nothing recoverable"

    return {
        "vhd_path": vhd_path,
        "jpeg_bytes": jpeg_bytes,
        "docx_bytes": docx_bytes,
        "pdf_bytes": pdf_bytes,
    }


def _find(files, name: str):
    return next((f for f in files if f.original_name == name), None)


def _assert_bytes_equal(recovered: bytes, original: bytes, label: str) -> None:
    """Byte-exact comparison with a diagnostic-rich failure message -- if recovered
    content is longer than the original and starts with it, that's cluster slack
    (icat handing back a whole allocated cluster rather than truncating to the
    file's recorded logical size); anything else is a genuinely wrong recovery."""
    if recovered == original:
        return
    raise AssertionError(
        f"{label}: recovered {len(recovered)} bytes, original {len(original)} bytes "
        f"(diff {len(recovered) - len(original)}); "
        f"recovered.startswith(original)={recovered.startswith(original)}, "
        f"original.startswith(recovered)={original.startswith(recovered)}"
    )


# ---------------------------------------------------------------------------
# Diagnostic: raw `fls` output against real NTFS, not just FAT32/exFAT/HFS+
# ---------------------------------------------------------------------------


def test_diagnose_ntfs_fls_output(ntfs_volume):
    """Not a correctness test -- dumps raw `fls` output against the NTFS VHD (in two
    forms) via _log(), for anyone re-investigating this later. This is how the fix in
    filesystem.py (mode field parsing + filtering out $FILE_NAME attribute rows) was
    actually derived: a deleted NTFS entry's mode comes back "-/rrwxrwxrwx" (the
    directory entry is unallocated) rather than FAT/exFAT/HFS+'s "r/r", and NTFS
    lists a deleted file's $FILE_NAME attribute as a separate, metadata-only row
    alongside its real $DATA row.
    """
    binaries = FilesystemEngine.locate_binaries()
    assert binaries is not None, "Sleuth Kit tools not found"

    volumes = FilesystemEngine.probe(ntfs_volume["vhd_path"])
    _log(f"\nFilesystemEngine.probe(vhd_path): {volumes}")
    assert volumes, "probe() found no volumes on the NTFS VHD -- mmls/fsstat parsing itself is the problem"
    offset = volumes[0].offset
    vhd_str = str(ntfs_volume["vhd_path"])

    invocations = [
        ("fls -r -p -m / -f ntfs -o <offset> (what filesystem.py actually runs)",
         [str(binaries["fls"]), "-r", "-p", "-o", str(offset), "-f", "ntfs", "-m", "/", vhd_str]),
        ("fls -r -p -o <offset> (no -m, no -f, for comparison)",
         [str(binaries["fls"]), "-r", "-p", "-o", str(offset), vhd_str]),
    ]
    for label, args in invocations:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        matches = [
            line for line in proc.stdout.splitlines()
            if _JPEG_NAME in line or _DOCX_NAME in line or _PDF_NAME in line
        ]
        _log(
            f"\n=== {label} ===\nargs: {args}\nreturncode: {proc.returncode}\n"
            "lines matching known filenames:\n" + "\n".join(matches)
        )


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
    _assert_bytes_equal(jpeg.path.read_bytes(), ntfs_volume["jpeg_bytes"], "jpeg")

    docx = _find(result.files, _DOCX_NAME)
    assert docx is not None
    assert docx.original_dir == _DOC_DIR
    assert docx.deleted is True
    _assert_bytes_equal(docx.path.read_bytes(), ntfs_volume["docx_bytes"], "docx")

    # The never-deleted pdf is correctly excluded from the default deleted-only scan.
    assert _find(result.files, _PDF_NAME) is None


def test_filesystem_engine_include_existing_finds_live_pdf(tmp_path, ntfs_volume):
    engine = FilesystemEngine()
    result = engine.scan(ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.QUICK, include_existing=True)

    assert result.success, result.error
    pdf = _find(result.files, _PDF_NAME)
    assert pdf is not None
    assert pdf.deleted is False
    _assert_bytes_equal(pdf.path.read_bytes(), ntfs_volume["pdf_bytes"], "pdf")


# ---------------------------------------------------------------------------
# PhotoRecEngine (Deep scan): carves by signature, and proves pywinpty actually
# streams live progress rather than only reporting once at exit
# ---------------------------------------------------------------------------


def test_photorec_carves_deleted_ntfs_files_with_live_progress(tmp_path, ntfs_volume):
    engine = PhotoRecEngine()
    progress_updates = []
    result = engine.scan(
        ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.DEEP, on_progress=progress_updates.append
    )

    # PhotoRec's own log records what it thinks the source's size/partition layout
    # is -- useful to tell "opened the .vhd fine but carved nothing real" apart from
    # "never correctly read the .vhd at all" (e.g. confused by the 512-byte VHD
    # footer, or a wrong partition_none offset assumption).
    _log(
        f"\nPhotoRec result: success={result.success} cancelled={result.cancelled} error={result.error!r}\n"
        f"carved {len(result.files)} file(s): "
        + ", ".join(f"{f.name} ({f.size} bytes)" for f in result.files[:20])
        + (" ..." if len(result.files) > 20 else "")
        + f"\n--- photorec.log ---\n{result.log_text}"
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
    _log(
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


def test_combined_engine_merges_ntfs_results_without_duplicates(tmp_path, ntfs_volume):
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
    _log(
        f"\nNTFS recovery summary (Thorough/CombinedEngine): {len(result.files)} total "
        f"result(s) merged from filesystem + carve, {len(named)} with recovered original "
        f"names: {named}"
    )
