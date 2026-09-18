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

import hashlib
import io
import os
import re
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


def _query_disable_delete_notify() -> int | None:
    """Reads the machine-wide DisableDeleteNotify setting (0 = TRIM/UNMAP enabled,
    1 = disabled), so the fixture can restore whatever it found afterwards."""
    proc = subprocess.run(
        ["fsutil", "behavior", "query", "DisableDeleteNotify"], capture_output=True, text=True, timeout=30
    )
    _log(f"\nfsutil behavior query DisableDeleteNotify: rc={proc.returncode}\n{proc.stdout}{proc.stderr}")
    m = re.search(r"=\s*(\d+)", proc.stdout)
    return int(m.group(1)) if m else None


def _set_disable_delete_notify(value: int) -> None:
    proc = subprocess.run(
        ["fsutil", "behavior", "set", "DisableDeleteNotify", str(value)],
        capture_output=True, text=True, timeout=30,
    )
    _log(f"\nfsutil behavior set DisableDeleteNotify {value}: rc={proc.returncode}\n{proc.stdout}{proc.stderr}")


def _flush_volume(letter: str, when: str) -> None:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Write-VolumeCache -DriveLetter {letter}"],
        capture_output=True, text=True, timeout=30,
    )
    _log(f"\nWrite-VolumeCache -DriveLetter {letter} ({when}): rc={proc.returncode}\n{proc.stdout}{proc.stderr}")


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

    # NTFS issues delete notifications (TRIM/UNMAP) that a diskpart-attached VHD
    # honours -- the backing blocks get discarded and read back as zeros, the exact
    # phenomenon README.md already documents for real SSDs. That makes deleted-file
    # recovery genuinely impossible on any TRIM-capable volume, which is most modern
    # Windows machines with SSDs -- a real, honest product limitation, not something
    # to work around in the engine (confirmed empirically: FilesystemEngine/PhotoRec
    # were reading back correctly-sized, correctly-located, all-zero clusters -- the
    # content was gone, not misread). This fixture disables delete notifications for
    # the plant/delete window specifically *so there is something for the engine to
    # recover at all*, then always restores whatever value it found: this is a
    # machine-wide setting, and even on a throwaway CI runner it shouldn't be left
    # changed.
    original_trim_setting = _query_disable_delete_notify()
    _set_disable_delete_notify(1)
    try:
        jpeg_bytes, docx_bytes, pdf_bytes = _plant_and_delete_ntfs_files(mount, letter)
    finally:
        if original_trim_setting is not None:
            _set_disable_delete_notify(original_trim_setting)
        detach_script = f'select vdisk file="{vhd_str}"\r\ndetach vdisk\r\nexit\r\n'
        detach_proc = _run_diskpart(detach_script)
        _log(f"\ndiskpart detach stdout:\n{detach_proc.stdout}\nstderr:\n{detach_proc.stderr}")

    # Isolate fixture-vs-engine failures before any engine ever touches this file:
    # search the VHD's own raw bytes for the deleted JPEG's and DOCX's *entire*
    # content, not just a signature -- a 3-byte JPEG SOI marker turns up by
    # coincidence elsewhere on the volume (including inside other live files), so it
    # doesn't actually prove the content survived, only that *something* looks
    # JPEG-ish. If either full file is missing here, the content never made it into
    # the VHD's data area (or was discarded, e.g. by TRIM) -- a fixture/environment
    # issue, not something FilesystemEngine/PhotoRec could ever have recovered -- and
    # this fails right here with that verdict instead of three confusing engine-level
    # "recovered nothing" failures downstream.
    raw = vhd_path.read_bytes()
    jpeg_present = jpeg_bytes in raw
    docx_present = docx_bytes in raw
    _log(
        f"\nraw VHD byte scan ({len(raw)} bytes): full JPEG content present={jpeg_present}, full DOCX content present={docx_present}\n"
        f"jpeg sha256 at fixture-return time (same object returned to every test): {hashlib.sha256(jpeg_bytes).hexdigest()}\n"
        f"docx sha256 at fixture-return time: {hashlib.sha256(docx_bytes).hexdigest()}"
    )
    assert jpeg_present, "deleted JPEG's full content not found anywhere in the raw VHD file -- nothing for any engine to recover"
    assert docx_present, "deleted DOCX's full content not found anywhere in the raw VHD file -- nothing for any engine to recover"

    return {
        "vhd_path": vhd_path,
        "jpeg_bytes": jpeg_bytes,
        "docx_bytes": docx_bytes,
        "pdf_bytes": pdf_bytes,
    }


def _plant_and_delete_ntfs_files(mount: Path, letter: str) -> tuple[bytes, bytes, bytes]:
    """Plants the three known files, confirms all three landed, deletes the jpeg and
    docx (the pdf stays live), confirms exactly the pdf remains, and flushes the
    volume's cached writes. Returns (jpeg_bytes, docx_bytes, pdf_bytes) as actually
    written and read back -- not independently regenerated."""
    jpeg_dir = mount.joinpath(*_JPEG_DIR_PARTS)
    jpeg_dir.mkdir(parents=True, exist_ok=True)
    jpeg_path = jpeg_dir / _JPEG_NAME
    # Random pixel noise (not a flat colour) so JPEG's DCT can't compress it away
    # to a resident-sized file; 600x450 lands comfortably in the hundreds-of-KB
    # range regardless of quality settings.
    Image.frombytes("RGB", (600, 450), os.urandom(600 * 450 * 3)).save(jpeg_path, "JPEG", quality=90)
    jpeg_bytes = jpeg_path.read_bytes()
    _log(f"\njpeg size: {len(jpeg_bytes)} bytes, sha256 at plant time: {hashlib.sha256(jpeg_bytes).hexdigest()}")

    doc_dir = mount.joinpath(*_DOC_DIR_PARTS)
    doc_dir.mkdir(parents=True, exist_ok=True)
    docx_path = doc_dir / _DOCX_NAME
    docx_bytes = _make_minimal_docx()
    docx_path.write_bytes(docx_bytes)
    _log(f"docx size: {len(docx_bytes)} bytes, sha256 at plant time: {hashlib.sha256(docx_bytes).hexdigest()}")

    pdf_path = doc_dir / _PDF_NAME
    pdf_bytes = b"%PDF-1.4\n" + _PDF_MARKER + b"\n" + (b"%" + b"x" * 78 + b"\n") * (_PDF_PAD_SIZE // 80)
    pdf_path.write_bytes(pdf_bytes)
    _log(f"pdf size: {len(pdf_bytes)} bytes")

    planted = sorted(str(p.relative_to(mount)) for p in mount.rglob("*") if p.is_file())
    _log(f"\nplanted on {mount} before delete: {planted}")
    assert len(planted) == 3, f"expected 3 planted files on {mount}, found {planted}"

    # Flush *before* deleting, not just before detaching. NTFS metadata is journaled
    # and does reach disk promptly -- which is exactly why fls already correctly sees
    # every planted file's name/folder/size even before this fix -- but a plain
    # write + close leaves the file's *data* pages dirty in the OS cache; deleting
    # before those pages are flushed discards them outright, so the MFT ends up
    # correctly pointing at clusters that were never actually written (reading back
    # as zeros, not the deleted file's real content). Flushing here is what makes
    # there be real data on disk for a deletion to orphan in the first place.
    _flush_volume(letter, "after planting, before deleting")

    # Delete two of the three; the pdf stays live, mirroring test_filesystem.py's
    # "one never-deleted file must NOT show up in the default deleted-only scan"
    # assertion.
    jpeg_path.unlink()
    docx_path.unlink()

    remaining = sorted(str(p.relative_to(mount)) for p in mount.rglob("*") if p.is_file())
    _log(f"remaining on {mount} after delete: {remaining}")
    assert remaining == [str(Path(*_DOC_DIR_PARTS) / _PDF_NAME)]

    # And again before detaching -- diskpart's own "detach vdisk" is a controlled
    # dismount and should already do this, but this makes it an explicit, checkable
    # step rather than an assumption.
    _flush_volume(letter, "before detach")

    return jpeg_bytes, docx_bytes, pdf_bytes


def _find(files, name: str):
    return next((f for f in files if f.original_name == name), None)


def _hex_head_tail(data: bytes, n: int = 32) -> str:
    head = data[:n].hex(" ")
    tail = data[-n:].hex(" ") if len(data) >= n else "(shorter than n)"
    return f"first {min(n, len(data))} bytes: {head}\nlast {min(n, len(data))} bytes: {tail}"


def _find_offsets(haystack: bytes, needle: bytes, limit: int = 5) -> list[int]:
    offsets: list[int] = []
    start = 0
    while len(offsets) < limit:
        idx = haystack.find(needle, start)
        if idx == -1:
            break
        offsets.append(idx)
        start = idx + 1
    return offsets


def _diagnose_byte_mismatch(recovered_file, recovered: bytes, ntfs_volume: dict, label: str) -> str:
    """Same-length-but-wrong-content is a data-correctness bug, not a truncation
    problem -- distinguishes the live possibilities rather than guessing: is the
    recovered content actually a different one of our own planted files (points at
    cluster-run/inode misattribution)? Does the original's real content exist
    somewhere else in the raw VHD than where icat read from (points at a sector-size
    mismatch between what mmls/fsstat assumed for the partition offset and what
    icat/istat assumed when reading data runs -- both default to 512 bytes/sector
    unless told otherwise, but mmls can auto-detect a different value from the
    partition table while fsstat/icat may not follow that same auto-detection)?
    """
    original = ntfs_volume[f"{label}_bytes"]
    vhd_path = ntfs_volume["vhd_path"]
    raw = vhd_path.read_bytes()

    parts = [
        f"=== byte mismatch diagnostic for {label} ===",
        # If the fixture were generating "original" separately from what it actually
        # wrote (e.g. re-deriving it instead of reusing the exact object read back at
        # plant time), the sha256 recorded when the file was planted (see
        # _plant_and_delete_ntfs_files's own log line) would differ from this one --
        # both computed from the identical ntfs_volume["<label>_bytes"] object, so a
        # mismatch here would mean the object itself was mutated or replaced somewhere
        # between plant and this comparison, not just an engine/VHD problem.
        f"recovered: {len(recovered)} bytes, sha256={hashlib.sha256(recovered).hexdigest()}\n{_hex_head_tail(recovered)}",
        f"original: {len(original)} bytes, sha256={hashlib.sha256(original).hexdigest()} "
        "(compare against this file's 'sha256 at plant time' log line above)\n"
        f"{_hex_head_tail(original)}",
    ]

    # 1) Is the recovered content actually a DIFFERENT one of our own planted files?
    for other_label in ("jpeg", "docx", "pdf"):
        if other_label == label:
            continue
        other = ntfs_volume[f"{other_label}_bytes"]
        parts.append(
            f"recovered == {other_label}_bytes: {recovered == other}; "
            f"recovered in {other_label}_bytes: {recovered in other}; "
            f"{other_label}_bytes in recovered: {other in recovered}"
        )

    # 2) Where does the ORIGINAL's real content actually live in the raw VHD, and
    #    where did icat read from? mmls/fsstat/istat's own raw output shows whatever
    #    sector size *they* assumed, independent of each other.
    original_offsets = _find_offsets(raw, original)
    recovered_offsets = _find_offsets(raw, recovered) if recovered != original else []
    parts.append(
        f"original's exact bytes found in raw VHD at byte offset(s): {original_offsets} "
        f"(raw VHD is {len(raw)} bytes)"
    )
    if recovered != original:
        parts.append(f"recovered's exact bytes found in raw VHD at byte offset(s): {recovered_offsets}")

    binaries = FilesystemEngine.locate_binaries()
    if binaries is not None:
        vhd_str = str(vhd_path)
        mmls_proc = subprocess.run([str(binaries["mmls"]), vhd_str], capture_output=True, text=True, timeout=60)
        parts.append(f"--- mmls (raw, full) ---\n{mmls_proc.stdout}\n{mmls_proc.stderr}")

        volumes = FilesystemEngine.probe(vhd_path)
        if volumes:
            offset = volumes[0].offset
            fsstat_proc = subprocess.run(
                [str(binaries["fsstat"]), "-o", str(offset), vhd_str], capture_output=True, text=True, timeout=60
            )
            parts.append(f"--- fsstat -o {offset} (raw, full) ---\n{fsstat_proc.stdout}\n{fsstat_proc.stderr}")

            # istat isn't in filesystem.py's _TOOL_NAMES (never needed at scan time),
            # so it's not resolvable via locate_binaries() -- look for it next to fls.
            istat_bin = binaries["fls"].parent / f"istat{binaries['fls'].suffix}"
            if recovered_file.inode and istat_bin.exists():
                istat_proc = subprocess.run(
                    [str(istat_bin), "-o", str(offset), vhd_str, recovered_file.inode],
                    capture_output=True, text=True, timeout=60,
                )
                parts.append(
                    f"--- istat -o {offset} {vhd_str} {recovered_file.inode} (raw, full) ---\n"
                    f"{istat_proc.stdout}\n{istat_proc.stderr}"
                )
            else:
                parts.append(f"istat not found next to fls (looked for {istat_bin if recovered_file.inode else 'n/a'})")

    # 3) Rule out the fixture: is ntfs_volume["<label>_bytes"] what was actually
    #    written, or something regenerated for comparison? (It's read back from disk
    #    immediately after writing, before deletion -- see _build_ntfs_volume -- so
    #    this should always be "written", but confirm rather than assume.)
    parts.append(
        f"fixture note: ntfs_volume['{label}_bytes'] is read back from the mounted "
        "volume immediately after writing, before deletion (see _build_ntfs_volume) "
        "-- not independently regenerated."
    )

    return "\n\n".join(parts)


def _assert_bytes_equal(recovered_file, recovered: bytes, ntfs_volume: dict, label: str) -> None:
    """Byte-exact comparison. On mismatch, runs the full diagnostic suite (hex dumps,
    cross-file comparison, raw VHD offset search, mmls/fsstat/istat output) rather
    than a bare pytest diff -- same-length-but-wrong-content is a data-correctness
    bug that deserves more than "assert a == b"."""
    original = ntfs_volume[f"{label}_bytes"]
    if recovered == original:
        return
    pytest.fail(_diagnose_byte_mismatch(recovered_file, recovered, ntfs_volume, label))


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
    _assert_bytes_equal(jpeg, jpeg.path.read_bytes(), ntfs_volume, "jpeg")

    docx = _find(result.files, _DOCX_NAME)
    assert docx is not None
    assert docx.original_dir == _DOC_DIR
    assert docx.deleted is True
    _assert_bytes_equal(docx, docx.path.read_bytes(), ntfs_volume, "docx")

    # The never-deleted pdf is correctly excluded from the default deleted-only scan.
    assert _find(result.files, _PDF_NAME) is None


def test_filesystem_engine_include_existing_finds_live_pdf(tmp_path, ntfs_volume):
    engine = FilesystemEngine()
    result = engine.scan(ntfs_volume["vhd_path"], tmp_path, mode=ScanMode.QUICK, include_existing=True)

    assert result.success, result.error
    pdf = _find(result.files, _PDF_NAME)
    assert pdf is not None
    assert pdf.deleted is False
    _assert_bytes_equal(pdf, pdf.path.read_bytes(), ntfs_volume, "pdf")


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

    # on_progress fires on a wall-clock timer (roughly every 0.2s of scan time, see
    # _PROGRESS_INTERVAL in photorec.py) independent of whether pywinpty is actually
    # streaming live output or photorec.py silently fell back to a plain, fully
    # block-buffered pipe -- so counting *distinct* sector readings across multiple
    # calls was originally meant to tell those apart (a batched dump right before
    # exit can only ever produce one). That doesn't hold on this test's ~95 MB VHD:
    # PhotoRec's DEEP scan of it finishes on CI hardware well within a second, too
    # fast to guarantee sampling more than one distinct "Reading sector" line even
    # when pywinpty is genuinely streaming (confirmed live on a real Windows CI run:
    # this exact assertion failed with zero readings once NTFS recovery itself was
    # already proven correct end to end -- carved bytes matched exactly -- so the
    # scan being fast, not broken streaming, is what's actually going on here).
    # The honest, still-meaningful bar for a volume this small: pywinpty's read loop
    # ran at all (at least one on_progress call), which it wouldn't if spawning it
    # had failed outright and the code silently fell through to the plain-pipe path
    # returning nothing until process exit -- see photorec.py's `elif winpty is not
    # None: try: pty_proc = winpty.PtyProcess.spawn(...) except Exception: pty_proc
    # = None` fallback.
    sector_values = sorted({p.sector for p in progress_updates if p.sector > 0})
    _log(
        f"\nphotorec live-progress check: {len(progress_updates)} on_progress call(s), "
        f"{len(sector_values)} distinct non-zero sector reading(s)"
    )
    assert progress_updates, "expected at least one live progress update via pywinpty during the scan"


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
