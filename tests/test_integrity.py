"""Tests for salvage.engine.integrity: verdicts must come from parsing structure,
never from the extension alone.

For each format we build: a valid file, a truncated copy (cut at 60%), a copy with
a corrupted header (garbage at the start), and - where the format check can
actually catch it - a copy with a mangled middle. Several lossy/compressed
formats (JPEG, HEIC, MP4's mdat, PDF's object bodies) can absorb a localised
bit-flip in their payload without the decoder raising or the structural walk
noticing; those cases are asserted as INTACT with a comment explaining why -
that's a real, documented limitation (shared by any structural/decoder-based
check, not a bug in this module) rather than a gap in the test.
"""

from __future__ import annotations

import io
import random
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from bench.corpus import _docx_bytes, _heic_bytes, _jpeg_bytes, _mp3_bytes, _mp4_bytes, _pdf_bytes, _png_bytes
from salvage.engine import integrity
from salvage.engine.devices import Device
from salvage.engine.integrity import verify, verify_many
from salvage.engine.models import Integrity, RecoveredFile
from salvage.engine.results import mark_also_exists

SEED = 20260907


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _truncate(data: bytes, fraction: float = 0.6) -> bytes:
    return data[: int(len(data) * fraction)]


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------


def test_jpeg_valid_is_intact(tmp_path):
    data = _jpeg_bytes(random.Random(SEED), 200, 150, 80, datetime(2024, 1, 1, tzinfo=timezone.utc))
    p = _write(tmp_path, "valid.jpg", data)
    assert verify(p, "jpg") == (Integrity.INTACT, None)


def test_jpeg_truncated_is_partial(tmp_path):
    data = _jpeg_bytes(random.Random(SEED), 200, 150, 80, datetime(2024, 1, 1, tzinfo=timezone.utc))
    p = _write(tmp_path, "trunc.jpg", _truncate(data))
    verdict, reason = verify(p, "jpg")
    assert verdict == Integrity.PARTIAL
    assert "EOI" in reason


def test_jpeg_bad_soi_is_corrupt(tmp_path):
    data = bytearray(_jpeg_bytes(random.Random(SEED), 200, 150, 80, datetime(2024, 1, 1, tzinfo=timezone.utc)))
    data[0:2] = b"\x00\x00"  # no SOI marker
    p = _write(tmp_path, "corrupt_header.jpg", bytes(data))
    verdict, reason = verify(p, "jpg")
    assert verdict == Integrity.CORRUPT
    assert "SOI" in reason


def test_jpeg_bad_marker_length_is_corrupt(tmp_path):
    """A garbled *structural* value (a marker segment's length field, not the
    entropy-coded pixel data) is what our marker-chain walk can actually catch -
    see test_jpeg_bit_flip_in_scan_data_is_not_caught for the payload case it can't."""
    data = bytearray(_jpeg_bytes(random.Random(SEED), 200, 150, 80, datetime(2024, 1, 1, tzinfo=timezone.utc)))
    pos, n = 2, len(data)
    target = None
    while pos + 3 < n:
        marker = data[pos + 1]
        if marker == 0xD8 or marker == 0x01 or (0xD0 <= marker <= 0xD7):
            pos += 2
            continue
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if marker == 0xDA:
            break
        if marker in (0xE1, 0xDB, 0xC0, 0xC4):
            target = pos + 2
            break
        pos += 2 + seg_len
    assert target is not None, "fixture didn't contain an APP1/DQT/SOF0/DHT segment to corrupt"
    data[target : target + 2] = b"\xff\xf0"  # implausibly large segment length
    p = _write(tmp_path, "corrupt_marker.jpg", bytes(data))
    verdict, _reason = verify(p, "jpg")
    assert verdict == Integrity.CORRUPT


def test_jpeg_bit_flip_in_scan_data_is_not_caught(tmp_path):
    """Known limitation: JPEG's entropy-coded scan data has no checksum, and
    libjpeg (via Pillow) is deliberately tolerant of bit errors there (it's
    designed to degrade gracefully, not fail loudly) - so a localised bit-flip
    inside the *pixel* data, with header/EOI both intact, is invisible to both
    our structural walk and the Pillow confirming step. Documented, not fixed."""
    data = bytearray(_jpeg_bytes(random.Random(SEED), 200, 150, 80, datetime(2024, 1, 1, tzinfo=timezone.utc)))
    mid = len(data) // 2
    for i in range(mid, mid + 300):
        data[i] = 0
    p = _write(tmp_path, "bitflip.jpg", bytes(data))
    assert verify(p, "jpg") == (Integrity.INTACT, None)


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def test_png_valid_is_intact(tmp_path):
    data = _png_bytes(random.Random(SEED), 100, 80)
    p = _write(tmp_path, "valid.png", data)
    assert verify(p, "png") == (Integrity.INTACT, None)


def test_png_truncated_is_partial(tmp_path):
    data = _png_bytes(random.Random(SEED), 100, 80)
    p = _write(tmp_path, "trunc.png", _truncate(data))
    verdict, _reason = verify(p, "png")
    assert verdict == Integrity.PARTIAL


def test_png_bad_signature_is_corrupt(tmp_path):
    data = bytearray(_png_bytes(random.Random(SEED), 100, 80))
    data[0:8] = b"garbage!"
    p = _write(tmp_path, "corrupt_header.png", bytes(data))
    verdict, reason = verify(p, "png")
    assert verdict == Integrity.CORRUPT
    assert "signature" in reason


def test_png_flipped_bytes_fail_crc(tmp_path):
    """Unlike JPEG, PNG's per-chunk CRC32 makes middle corruption cheap and decisive."""
    data = bytearray(_png_bytes(random.Random(SEED), 100, 80))
    mid = len(data) // 2
    for i in range(mid, mid + 20):
        data[i] ^= 0xFF
    p = _write(tmp_path, "corrupt_middle.png", bytes(data))
    verdict, reason = verify(p, "png")
    assert verdict == Integrity.CORRUPT
    assert "CRC" in reason


# ---------------------------------------------------------------------------
# GIF / BMP - representative of the header+Pillow group (TIFF/WebP share the code path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pillow_fmt,ext", [("GIF", "gif"), ("BMP", "bmp")])
def test_pillow_image_family(tmp_path, pillow_fmt, ext):
    img = Image.new("RGB", (60, 40), color=(120, 50, 200))
    buf = io.BytesIO()
    img.save(buf, pillow_fmt)
    data = bytearray(buf.getvalue())

    p = _write(tmp_path, f"valid.{ext}", bytes(data))
    assert verify(p, ext) == (Integrity.INTACT, None)

    p = _write(tmp_path, f"trunc.{ext}", _truncate(bytes(data)))
    verdict, _reason = verify(p, ext)
    assert verdict == Integrity.PARTIAL

    corrupt_header = bytearray(data)
    corrupt_header[0:4] = b"XXXX"
    p = _write(tmp_path, f"corrupt_header.{ext}", bytes(corrupt_header))
    verdict, _reason = verify(p, ext)
    assert verdict == Integrity.CORRUPT


# ---------------------------------------------------------------------------
# HEIC (real file from bench/fixtures/sample.heic)
# ---------------------------------------------------------------------------


def test_heic_valid_is_intact(tmp_path):
    data = _heic_bytes(random.Random(SEED), 200, 150)
    p = _write(tmp_path, "valid.heic", data)
    assert verify(p, "heic") == (Integrity.INTACT, None)


def test_heic_truncated_is_partial(tmp_path):
    data = _heic_bytes(random.Random(SEED), 200, 150)
    p = _write(tmp_path, "trunc.heic", _truncate(data))
    verdict, _reason = verify(p, "heic")
    assert verdict == Integrity.PARTIAL


def test_heic_bad_ftyp_is_corrupt(tmp_path):
    data = bytearray(_heic_bytes(random.Random(SEED), 200, 150))
    data[4:12] = b"XXXXXXXX"  # clobber the "ftyp" box type + major brand
    p = _write(tmp_path, "corrupt_header.heic", bytes(data))
    verdict, reason = verify(p, "heic")
    assert verdict == Integrity.CORRUPT
    assert "ftyp" in reason


# ---------------------------------------------------------------------------
# MP4 (hand-built ftyp+moov+mdat, matching bench/corpus.py's own fixture builder)
# ---------------------------------------------------------------------------


def test_mp4_valid_is_intact(tmp_path):
    data = _mp4_bytes(random.Random(SEED), datetime(2024, 1, 1, tzinfo=timezone.utc), 50_000)
    p = _write(tmp_path, "valid.mp4", data)
    assert verify(p, "mp4") == (Integrity.INTACT, None)


def test_mp4_truncated_mdat_is_partial(tmp_path):
    data = _mp4_bytes(random.Random(SEED), datetime(2024, 1, 1, tzinfo=timezone.utc), 50_000)
    p = _write(tmp_path, "trunc.mp4", _truncate(data))
    verdict, reason = verify(p, "mp4")
    assert verdict == Integrity.PARTIAL
    assert "mdat" in reason


def test_mp4_bad_ftyp_is_corrupt(tmp_path):
    data = bytearray(_mp4_bytes(random.Random(SEED), datetime(2024, 1, 1, tzinfo=timezone.utc), 50_000))
    data[4:8] = b"XXXX"
    p = _write(tmp_path, "corrupt_header.mp4", bytes(data))
    verdict, reason = verify(p, "mp4")
    assert verdict == Integrity.CORRUPT
    assert "ftyp" in reason


def test_mp4_bit_flip_inside_mdat_is_not_caught(tmp_path):
    """Known limitation: the atom-tree walk (by design, per the whole point of
    checking a multi-GB video without reading it) never looks *inside* mdat -
    a spliced/garbled video payload with an otherwise-valid box tree verifies as
    INTACT. A real preview/thumbnail check would be needed to catch this."""
    data = bytearray(_mp4_bytes(random.Random(SEED), datetime(2024, 1, 1, tzinfo=timezone.utc), 50_000))
    mid = len(data) - 20_000  # well inside mdat's body
    for i in range(mid, mid + 500):
        data[i] ^= 0xFF
    p = _write(tmp_path, "bitflip.mp4", bytes(data))
    assert verify(p, "mp4") == (Integrity.INTACT, None)


# ---------------------------------------------------------------------------
# MP3
# ---------------------------------------------------------------------------


def test_mp3_valid_is_intact(tmp_path):
    data = _mp3_bytes(random.Random(SEED), 200, "Test Track")
    p = _write(tmp_path, "valid.mp3", data)
    assert verify(p, "mp3") == (Integrity.INTACT, None)


def test_mp3_truncated_is_partial(tmp_path):
    data = _mp3_bytes(random.Random(SEED), 200, "Test Track")
    p = _write(tmp_path, "trunc.mp3", _truncate(data))
    verdict, reason = verify(p, "mp3")
    assert verdict == Integrity.PARTIAL
    assert "frame" in reason


def test_mp3_no_sync_is_corrupt(tmp_path):
    data = bytearray(_mp3_bytes(random.Random(SEED), 200, "Test Track"))
    data[0:4] = b"\x00\x00\x00\x00"  # clobbers the ID3 tag AND the first frame sync
    p = _write(tmp_path, "corrupt_header.mp3", bytes(data))
    verdict, reason = verify(p, "mp3")
    assert verdict == Integrity.CORRUPT
    assert "sync" in reason


def test_mp3_desync_in_middle_is_partial(tmp_path):
    data = bytearray(_mp3_bytes(random.Random(SEED), 200, "Test Track"))
    mid = len(data) // 2
    for i in range(mid, mid + 50):
        data[i] = 0  # breaks the sync word for whichever frame starts here
    p = _write(tmp_path, "corrupt_middle.mp3", bytes(data))
    verdict, reason = verify(p, "mp3")
    assert verdict == Integrity.PARTIAL
    assert "sync" in reason


# ---------------------------------------------------------------------------
# PDF (hand-built, matching bench/corpus.py's own fixture builder)
# ---------------------------------------------------------------------------


def test_pdf_valid_is_intact(tmp_path):
    data = _pdf_bytes("Salvage integrity test")
    p = _write(tmp_path, "valid.pdf", data)
    assert verify(p, "pdf") == (Integrity.INTACT, None)


def test_pdf_truncated_is_partial(tmp_path):
    data = _pdf_bytes("Salvage integrity test")
    p = _write(tmp_path, "trunc.pdf", _truncate(data))
    verdict, reason = verify(p, "pdf")
    assert verdict == Integrity.PARTIAL
    assert "EOF" in reason


def test_pdf_bad_header_is_corrupt(tmp_path):
    data = _pdf_bytes("Salvage integrity test")
    corrupted = b"XXXXX" + data[5:]
    p = _write(tmp_path, "corrupt_header.pdf", corrupted)
    verdict, reason = verify(p, "pdf")
    assert verdict == Integrity.CORRUPT
    assert "PDF" in reason


# ---------------------------------------------------------------------------
# DOCX (real zip/OOXML file, via zipfile - same as bench/corpus.py)
# ---------------------------------------------------------------------------


def test_docx_valid_is_intact(tmp_path):
    data = _docx_bytes("Assembly Growth integrity test docx")
    p = _write(tmp_path, "valid.docx", data)
    assert verify(p, "docx") == (Integrity.INTACT, None)


def test_docx_truncated_is_partial(tmp_path):
    data = _docx_bytes("Assembly Growth integrity test docx")
    p = _write(tmp_path, "trunc.docx", _truncate(data))
    verdict, reason = verify(p, "docx")
    assert verdict == Integrity.PARTIAL
    assert "central directory" in reason


def test_docx_corrupt_local_header_is_corrupt(tmp_path):
    data = bytearray(_docx_bytes("Assembly Growth integrity test docx"))
    for i in range(30):
        data[i] = 0  # clobbers the first local file header's signature/fields
    p = _write(tmp_path, "corrupt_header.docx", bytes(data))
    verdict, _reason = verify(p, "docx")
    assert verdict == Integrity.CORRUPT


def test_docx_flipped_bytes_fail_crc(tmp_path):
    data = bytearray(_docx_bytes("Assembly Growth integrity test docx"))
    for i in range(50, 80):
        data[i] ^= 0xFF  # inside the first member's compressed data
    p = _write(tmp_path, "corrupt_middle.docx", bytes(data))
    verdict, _reason = verify(p, "docx")
    assert verdict == Integrity.CORRUPT


# ---------------------------------------------------------------------------
# SQLite (real database file)
# ---------------------------------------------------------------------------


def _build_sqlite(tmp_path: Path) -> bytes:
    db_path = tmp_path / "_source.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(200):
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row-{i}" * 5,))
    conn.commit()
    conn.close()
    data = db_path.read_bytes()
    db_path.unlink()
    return data


def test_sqlite_valid_is_intact(tmp_path):
    data = _build_sqlite(tmp_path)
    p = _write(tmp_path, "valid.sqlite", data)
    assert verify(p, "sqlite") == (Integrity.INTACT, None)


def test_sqlite_truncated_is_partial(tmp_path):
    data = _build_sqlite(tmp_path)
    p = _write(tmp_path, "trunc.sqlite", _truncate(data))
    verdict, reason = verify(p, "sqlite")
    assert verdict == Integrity.PARTIAL
    assert "truncated" in reason


def test_sqlite_bad_magic_is_corrupt(tmp_path):
    data = bytearray(_build_sqlite(tmp_path))
    data[0:16] = b"X" * 16
    p = _write(tmp_path, "corrupt_header.sqlite", bytes(data))
    verdict, reason = verify(p, "sqlite")
    assert verdict == Integrity.CORRUPT
    assert "magic" in reason


def test_sqlite_flipped_bytes_fail_integrity_check(tmp_path):
    data = bytearray(_build_sqlite(tmp_path))
    mid = len(data) // 2
    for i in range(mid, mid + 200):
        data[i] ^= 0xFF
    p = _write(tmp_path, "corrupt_middle.sqlite", bytes(data))
    verdict, reason = verify(p, "sqlite")
    assert verdict == Integrity.CORRUPT
    assert "integrity_check" in reason


# ---------------------------------------------------------------------------
# unknown/text formats: never guess
# ---------------------------------------------------------------------------


def test_unknown_extension_is_unknown(tmp_path):
    p = _write(tmp_path, "notes.txt", b"just some plain text notes\n")
    assert verify(p, "txt") == (Integrity.UNKNOWN, None)


def test_no_extension_hint_falls_back_to_path_suffix(tmp_path):
    p = _write(tmp_path, "notes.md", b"# heading\n")
    assert verify(p) == (Integrity.UNKNOWN, None)


# ---------------------------------------------------------------------------
# verify_many
# ---------------------------------------------------------------------------


def test_verify_many_populates_copies_and_reports_progress(tmp_path):
    jpeg_data = _jpeg_bytes(random.Random(SEED), 100, 80, 75, datetime(2024, 1, 1, tzinfo=timezone.utc))
    png_data = _png_bytes(random.Random(SEED), 50, 40)
    p1 = _write(tmp_path, "a.jpg", jpeg_data)
    p2 = _write(tmp_path, "b.png", png_data)
    files = [
        RecoveredFile(path=p1, name="a.jpg", ext="jpg", size=p1.stat().st_size, category="image"),
        RecoveredFile(path=p2, name="b.png", ext="png", size=p2.stat().st_size, category="image"),
    ]
    progress_calls = []
    verified = verify_many(files, on_progress=lambda done, total: progress_calls.append((done, total)))

    assert len(verified) == 2
    assert all(f.integrity == Integrity.INTACT for f in verified)
    # originals untouched - RecoveredFile is frozen and verify_many must not mutate in place
    assert files[0].integrity == Integrity.UNKNOWN
    assert progress_calls[-1] == (2, 2)


def test_verify_many_empty_list():
    assert verify_many([]) == []


# ---------------------------------------------------------------------------
# also_exists (Part B)
# ---------------------------------------------------------------------------


def test_mark_also_exists_flags_byte_identical_live_file(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "a.txt").write_bytes(b"hello world" * 10)
    (mount / "b.txt").write_bytes(b"different content entirely" * 5)

    recovered_dir = tmp_path / "recovered"
    recovered_dir.mkdir()
    r_dupe = recovered_dir / "f100.txt"
    r_dupe.write_bytes(b"hello world" * 10)  # byte-identical to a.txt
    r_unique = recovered_dir / "f200.txt"
    r_unique.write_bytes(b"totally unique carved bytes, not on the volume")

    files = [
        RecoveredFile(path=r_dupe, name="f100.txt", ext="txt", size=r_dupe.stat().st_size, category="document"),
        RecoveredFile(path=r_unique, name="f200.txt", ext="txt", size=r_unique.stat().st_size, category="document"),
    ]
    device = Device(id="d1", path=str(mount), name="test volume", size_bytes=4096, kind="partition", mount_point=str(mount))

    result = mark_also_exists(files, device)
    by_name = {f.name: f for f in result}
    assert by_name["f100.txt"].also_exists is True
    assert by_name["f200.txt"].also_exists is False
    # unchanged originals still say False - mark_also_exists must return copies
    assert files[0].also_exists is False


def test_mark_also_exists_skips_when_not_mounted(tmp_path):
    files = [
        RecoveredFile(path=tmp_path / "x.txt", name="x.txt", ext="txt", size=5, category="document"),
    ]
    device = Device(id="d1", path="/dev/disk9", name="unmounted", size_bytes=4096, kind="partition", mount_point=None)
    result = mark_also_exists(files, device)
    assert result == files


def test_mark_also_exists_no_device_is_a_noop(tmp_path):
    files = [
        RecoveredFile(path=tmp_path / "x.txt", name="x.txt", ext="txt", size=5, category="document"),
    ]
    assert mark_also_exists(files, None) == files


def test_mark_also_exists_aborts_over_file_cap(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    for i in range(5):
        (mount / f"f{i}.txt").write_bytes(f"content-{i}".encode())

    recovered_dir = tmp_path / "recovered"
    recovered_dir.mkdir()
    r = recovered_dir / "f0.txt"
    r.write_bytes(b"content-0")
    files = [RecoveredFile(path=r, name="f0.txt", ext="txt", size=r.stat().st_size, category="document")]
    device = Device(id="d1", path=str(mount), name="test", size_bytes=4096, kind="partition", mount_point=str(mount))

    # A cap smaller than the number of live files should abort and return unchanged,
    # even though f0.txt (which does match) would otherwise have been flagged.
    result = mark_also_exists(files, device, max_files=2)
    assert result[0].also_exists is False
