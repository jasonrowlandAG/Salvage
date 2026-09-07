"""Decide how trustworthy a recovered file actually is, by parsing its structure -
never by trusting the extension alone. Carving (PhotoRec-style) can produce files
that have the right magic bytes at the front but are truncated or spliced, and this
is what tells those apart from the real thing.

Every per-format checker is deliberately bounded: full pixel/audio/stream decoding
is only attempted up to `_FULL_DECODE_CAP` bytes (structural checks - header/trailer/
box-tree walks - run regardless of size, since they only ever touch a handful of
bytes plus a bounded scan, never the whole file). This is what lets `verify()` be
called on a multi-gigabyte carved video without reading the video itself.

Public API: `verify()` for one file, `verify_many()` for a batch (returns copies -
`RecoveredFile` is frozen).
"""

from __future__ import annotations

import gzip
import sqlite3
import struct
import tarfile
import tempfile
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

from salvage.engine.models import Integrity, RecoveredFile

try:
    from PIL import Image, ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = False  # let truncation surface as OSError, not a warning
except ImportError:  # pragma: no cover - Pillow is a project dependency
    Image = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif is a project dependency
    pillow_heif = None  # type: ignore[assignment]

# Above this many bytes, skip a real decode/decompress attempt and fall back to a
# structural-only verdict (never claim INTACT off a skipped decode - see each
# checker's "large file" branch).
_FULL_DECODE_CAP = 64 * 1024 * 1024

Verdict = tuple[Integrity, "str | None"]


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

_JPEG_NO_LENGTH_MARKERS = {0x01} | set(range(0xD0, 0xD8))  # TEM, RSTn - no length field


def _verify_jpeg(path: Path, size: int) -> Verdict:
    try:
        data = path.read_bytes() if size <= _FULL_DECODE_CAP else _read_head_tail(path, size, 65536)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return Integrity.CORRUPT, "no JPEG SOI marker at start of file"

    has_eoi = False
    if size <= _FULL_DECODE_CAP:
        pos = 2
        n = len(data)
        saw_sos = False
        while pos + 1 < n:
            if data[pos] != 0xFF:
                return Integrity.CORRUPT, f"malformed marker chain at byte {pos}"
            marker = data[pos + 1]
            if marker == 0xD8:
                pos += 2
                continue
            if marker in _JPEG_NO_LENGTH_MARKERS:
                pos += 2
                continue
            if marker == 0xD9:  # EOI
                has_eoi = True
                break
            if pos + 3 >= n:
                break
            seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
            if seg_len < 2:
                return Integrity.CORRUPT, f"malformed marker chain at byte {pos}"
            if marker == 0xDA:  # SOS: entropy-coded scan data follows, no simple length
                saw_sos = True
                break
            pos += 2 + seg_len
        if not saw_sos and not has_eoi:
            return Integrity.CORRUPT, "malformed marker chain (no scan data found)"
        if not has_eoi:
            has_eoi = _tail_has_eoi(data)
    else:
        has_eoi = _tail_has_eoi(data)

    if not has_eoi:
        return Integrity.PARTIAL, "truncated: no EOI marker found (scan data cut off)"

    if size > _FULL_DECODE_CAP:
        return Integrity.INTACT, "large file: structural check passed, pixel decode skipped (>64 MB)"

    if Image is None:
        return Integrity.INTACT, None
    try:
        with Image.open(path) as im:
            im.load()
        return Integrity.INTACT, None
    except Exception as exc:
        return Integrity.CORRUPT, f"pixel data failed to decode: {exc}"


def _tail_has_eoi(data: bytes) -> bool:
    # Trailing padding (0x00/0xFF) after a genuine EOI is common; strip it before checking.
    trimmed = data.rstrip(b"\x00")
    return trimmed.endswith(b"\xff\xd9")


def _read_head_tail(path: Path, size: int, chunk: int) -> bytes:
    """For files too big to fully load: enough of the head to parse the marker
    chain up to SOS, plus enough of the tail to check for EOI."""
    with open(path, "rb") as f:
        head = f.read(chunk)
        f.seek(max(0, size - chunk))
        tail = f.read(chunk)
    return head + tail


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _verify_png(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != _PNG_SIG:
                return Integrity.CORRUPT, "no PNG signature"
            first_type = None
            saw_iend = False
            pos = 8
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                length, ctype = struct.unpack(">I4s", header)
                if first_type is None:
                    first_type = ctype
                    if first_type != b"IHDR":
                        return Integrity.CORRUPT, "IHDR is not the first chunk"
                body = f.read(length)
                crc_bytes = f.read(4)
                if len(body) < length or len(crc_bytes) < 4:
                    return Integrity.PARTIAL, "truncated: a chunk declares more bytes than the file has"
                stored_crc = struct.unpack(">I", crc_bytes)[0]
                actual_crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
                if stored_crc != actual_crc:
                    return Integrity.CORRUPT, f"CRC32 mismatch in {ctype.decode('latin-1')} chunk"
                pos += 12 + length
                if ctype == b"IEND":
                    saw_iend = True
                    break
            if first_type is None:
                return Integrity.CORRUPT, "no chunks found after signature"
            if not saw_iend:
                return Integrity.PARTIAL, "truncated: no IEND chunk found"
        return Integrity.INTACT, None
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"


# ---------------------------------------------------------------------------
# GIF / BMP / TIFF / WebP / HEIC - header sniff + Pillow (pillow_heif for HEIC)
# ---------------------------------------------------------------------------


def _has_gif_header(head: bytes) -> bool:
    return head[:6] in (b"GIF87a", b"GIF89a")


def _has_bmp_header(head: bytes) -> bool:
    return head[:2] == b"BM"


def _has_tiff_header(head: bytes) -> bool:
    return head[:4] in (b"II*\x00", b"MM\x00*")


def _has_webp_header(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


_HEIC_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1")


def _has_heic_header(head: bytes) -> bool:
    if head[4:8] != b"ftyp":
        return False
    if head[8:12] in _HEIC_BRANDS:
        return True
    # else check the compatible-brands list that follows major_brand+minor_version
    compatible = head[16:32]
    return any(compatible[i : i + 4] in _HEIC_BRANDS for i in range(0, len(compatible) - 3, 4))


_HEADER_CHECKS = {
    "gif": (_has_gif_header, "no GIF signature"),
    "bmp": (_has_bmp_header, "no BMP signature"),
    "tif": (_has_tiff_header, "no TIFF byte-order marker"),
    "tiff": (_has_tiff_header, "no TIFF byte-order marker"),
    "webp": (_has_webp_header, "no RIFF/WEBP signature"),
    "heic": (_has_heic_header, "no HEIC ftyp box"),
    "heif": (_has_heic_header, "no HEIC ftyp box"),
}


def _verify_pillow_image(path: Path, size: int, ext: str) -> Verdict:
    check, reason = _HEADER_CHECKS[ext]
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"
    if not check(head):
        return Integrity.CORRUPT, reason

    if ext in ("heic", "heif") and pillow_heif is None:
        return Integrity.UNKNOWN, "ftyp box present; pillow_heif not available to confirm"

    if Image is None:
        return Integrity.UNKNOWN, "Pillow not available to confirm"

    if size > _FULL_DECODE_CAP:
        return Integrity.PARTIAL, f"not fully decoded: file exceeds size cap ({_mb(size)})"

    try:
        with Image.open(path) as im:
            im.load()
        return Integrity.INTACT, None
    except Exception as exc:
        return Integrity.PARTIAL, f"image data failed to decode: {exc}"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _verify_pdf(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
            if not head.startswith(b"%PDF-"):
                return Integrity.CORRUPT, "no %PDF- header"
            tail_len = min(size, 4096)
            f.seek(size - tail_len)
            tail = f.read(tail_len)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    if b"%%EOF" not in tail:
        return Integrity.PARTIAL, "truncated: no %%EOF trailer found"

    idx = tail.rfind(b"startxref")
    if idx == -1:
        return Integrity.PARTIAL, "trailer present but no startxref found"
    rest = tail[idx + len(b"startxref") :].lstrip()
    digits = b""
    for b in rest:
        if 48 <= b <= 57:
            digits += bytes([b])
        else:
            break
    if not digits:
        return Integrity.PARTIAL, "startxref found but has no offset"
    offset = int(digits)
    if offset < 0 or offset >= size:
        return Integrity.PARTIAL, f"startxref points outside the file (offset {offset}, size {size})"
    return Integrity.INTACT, None


# ---------------------------------------------------------------------------
# ZIP family (zip/docx/xlsx/pptx/odt/ods/odp/epub/jar)
# ---------------------------------------------------------------------------


def _verify_zip(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            tail_len = min(size, 65557)  # max comment length (65535) + EOCD record (22)
            f.seek(max(0, size - tail_len))
            tail = f.read(tail_len)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    starts_with_local_header = head == b"PK\x03\x04"
    has_eocd = b"PK\x05\x06" in tail

    if not starts_with_local_header and not has_eocd:
        return Integrity.CORRUPT, "no zip signature found"
    if not has_eocd:
        return Integrity.PARTIAL, "missing central directory; local file headers present"

    try:
        with zipfile.ZipFile(path) as zf:
            if size <= _FULL_DECODE_CAP:
                try:
                    bad = zf.testzip()
                except (OSError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
                    # testzip() only *returns* a name for a CRC mismatch - a member whose
                    # compressed stream is itself broken (e.g. bit-flipped deflate data)
                    # raises instead, and that's just as much a CORRUPT verdict.
                    return Integrity.CORRUPT, f"member data failed to decompress: {exc}"
                if bad is not None:
                    return Integrity.CORRUPT, f"CRC check failed for member {bad!r}"
                return Integrity.INTACT, None
            zf.namelist()  # cheap: reads the central directory only
            return Integrity.PARTIAL, f"not fully verified: archive exceeds size cap ({_mb(size)}), CRC check skipped"
    except zipfile.BadZipFile as exc:
        return Integrity.CORRUPT, f"bad zip: {exc}"


# ---------------------------------------------------------------------------
# ISO-BMFF (mp4/mov/m4v/m4a) - walk the atom/box tree without reading mdat
# ---------------------------------------------------------------------------


def _iter_boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            break
        size = int.from_bytes(header[0:4], "big")
        box_type = header[4:8]
        header_len = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                break
            size = int.from_bytes(ext, "big")
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            break
        yield pos, box_type, size, header_len
        pos += size


def _verify_isobmff(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            boxes = list(_iter_boxes(f, 0, size))
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    if not boxes:
        return Integrity.CORRUPT, "no valid box header found"
    if boxes[0][1] != b"ftyp":
        return Integrity.CORRUPT, "missing ftyp box at start of file"

    types = {t for _, t, _, _ in boxes}
    if b"moov" not in types:
        if b"mdat" in types:
            return Integrity.PARTIAL, "missing moov atom (metadata not recovered, media data present)"
        return Integrity.CORRUPT, "missing moov atom"

    for pos, box_type, box_size, header_len in boxes:
        if box_type == b"mdat":
            declared = box_size - header_len
            available = size - (pos + header_len)
            if available < declared:
                return (
                    Integrity.PARTIAL,
                    f"truncated: mdat declares {_mb(declared)}, file has {_mb(max(available, 0))}",
                )

    last_pos, _last_type, last_size, _last_hlen = boxes[-1]
    if last_pos + last_size > size:
        return Integrity.PARTIAL, "truncated: final box extends past end of file"

    return Integrity.INTACT, None


# ---------------------------------------------------------------------------
# MP3
# ---------------------------------------------------------------------------

_MPEG_VERSION = {0b00: 2.5, 0b10: 2.0, 0b11: 1.0}
_LAYER = {0b01: 3, 0b10: 2, 0b11: 1}
_BITRATES_KBPS = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, None],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, None],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (2, 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
}
_SAMPLE_RATES = {1.0: [44100, 48000, 32000, None], 2.0: [22050, 24000, 16000, None], 2.5: [11025, 12000, 8000, None]}


def _mp3_frame_length(data: bytes, pos: int) -> int | None:
    if pos + 4 > len(data):
        return None
    b1, b2, b3, _b4 = data[pos : pos + 4]
    if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
        return None
    version = _MPEG_VERSION.get((b2 >> 3) & 0b11)
    layer = _LAYER.get((b2 >> 1) & 0b11)
    if version is None or layer is None:
        return None
    bitrate_idx = (b3 >> 4) & 0x0F
    samplerate_idx = (b3 >> 2) & 0x03
    padding = (b3 >> 1) & 0x01
    ver_key = 1 if version == 1.0 else 2
    bitrate_table = _BITRATES_KBPS.get((ver_key, layer))
    if bitrate_table is None or not (0 < bitrate_idx < 15):
        return None
    bitrate = bitrate_table[bitrate_idx]
    samplerate_table = _SAMPLE_RATES.get(version)
    if samplerate_table is None or samplerate_idx == 3:
        return None
    samplerate = samplerate_table[samplerate_idx]
    if not bitrate or not samplerate:
        return None
    if layer == 1:
        return (12 * bitrate * 1000 // samplerate + padding) * 4
    coeff = 144 if ver_key == 1 else 72
    return coeff * bitrate * 1000 // samplerate + padding


def _mp3_start_offset(data: bytes) -> int:
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return 10 + size
    return 0


def _verify_mp3(path: Path, size: int) -> Verdict:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    pos = _mp3_start_offset(data)
    if pos >= len(data):
        return Integrity.CORRUPT, "ID3 tag claims to cover the whole file; no audio frames"

    first_len = _mp3_frame_length(data, pos)
    if first_len is None:
        return Integrity.CORRUPT, "no MP3 frame sync found"

    n = len(data)
    frames = 0
    while pos < n:
        remaining = n - pos
        if remaining < 4:
            break
        frame_len = _mp3_frame_length(data, pos)
        if frame_len is None:
            if remaining > 32:  # more than trailing slack left - sync was lost mid-stream
                return Integrity.PARTIAL, "frame sync lost before end of file (truncated or spliced)"
            break
        frames += 1
        if frame_len > remaining:
            return Integrity.PARTIAL, f"truncated: last frame declares {frame_len}B, only {remaining}B remain"
        pos += frame_len

    if frames == 0:
        return Integrity.CORRUPT, "no complete MP3 frames found"
    return Integrity.INTACT, None


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _verify_sqlite(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            header = f.read(100)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"

    if len(header) < 100 or header[:16] != _SQLITE_MAGIC:
        return Integrity.CORRUPT, "no SQLite header magic"

    page_size = struct.unpack(">H", header[16:18])[0]
    if page_size == 1:
        page_size = 65536
    page_count = struct.unpack(">I", header[28:32])[0]
    if page_size > 0 and page_count > 0:
        declared_size = page_size * page_count
        if size < declared_size:
            return (
                Integrity.PARTIAL,
                f"truncated: header expects ~{_mb(declared_size)} ({page_count} pages), file has ~{_mb(size)}",
            )

    if size > _FULL_DECODE_CAP:
        return Integrity.INTACT, "large file: header/page-count check passed, integrity_check skipped (>64 MB)"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(path.read_bytes())
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if row is not None and row[0] == "ok":
            return Integrity.INTACT, None
        return Integrity.CORRUPT, f"integrity_check: {row[0] if row else 'failed'}"
    except sqlite3.Error as exc:
        return Integrity.CORRUPT, f"integrity_check failed: {exc}"
    except OSError as exc:
        return Integrity.INTACT, f"header/page-count check passed; integrity_check unavailable ({exc})"
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# GZIP / tar / 7z / rar
# ---------------------------------------------------------------------------

_GZIP_MAGIC = b"\x1f\x8b"


def _verify_gzip(path: Path, size: int) -> Verdict:
    try:
        with open(path, "rb") as f:
            magic = f.read(2)
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"
    if magic != _GZIP_MAGIC:
        return Integrity.CORRUPT, "no gzip magic bytes"

    if size > _FULL_DECODE_CAP:
        return Integrity.PARTIAL, f"not fully decoded: file exceeds size cap ({_mb(size)})"

    try:
        with gzip.open(path, "rb") as gz:
            while gz.read(1024 * 1024):
                pass
        return Integrity.INTACT, None
    except (OSError, EOFError, zlib.error) as exc:
        return Integrity.PARTIAL, f"truncated or corrupt gzip stream: {exc}"


def _verify_tar(path: Path, size: int) -> Verdict:
    try:
        with tarfile.open(path, mode="r:*") as tf:
            tf.getmembers()
        return Integrity.INTACT, None
    except tarfile.ReadError as exc:
        return Integrity.CORRUPT, f"bad tar: {exc}"
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"


def _verify_magic_only(path: Path, magics: tuple[bytes, ...], label: str) -> Verdict:
    """7z/rar: no bundled decompressor, so this only sniffs the signature - it can
    say CORRUPT with confidence but never INTACT with confidence (a valid-looking
    header says nothing about a mangled middle), so it reports UNKNOWN rather than
    risk a false-INTACT. Documented as a known weak spot."""
    try:
        with open(path, "rb") as f:
            head = f.read(max(len(m) for m in magics))
    except OSError as exc:
        return Integrity.CORRUPT, f"could not read file: {exc}"
    if not any(head.startswith(m) for m in magics):
        return Integrity.CORRUPT, f"no {label} signature"
    return Integrity.UNKNOWN, f"{label} signature present; contents not verified (no {label} decoder)"


def _verify_7z(path: Path, size: int) -> Verdict:
    return _verify_magic_only(path, (b"7z\xbc\xaf\x27\x1c",), "7z")


def _verify_rar(path: Path, size: int) -> Verdict:
    return _verify_magic_only(path, (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"), "RAR")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_ZIP_EXTS = {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "jar"}
_ISOBMFF_EXTS = {"mp4", "mov", "m4v", "m4a"}
_PILLOW_EXTS = {"gif", "bmp", "tif", "tiff", "webp", "heic", "heif"}
_SQLITE_EXTS = {"sqlite", "sqlite3", "db"}


def verify(path: Path, ext: str | None = None) -> Verdict:
    """Parse `path`'s own structure and return (verdict, short human reason).
    `ext` overrides the extension read from `path` (lowercase, no dot) - useful
    for carved files whose name doesn't reflect their real format."""
    path = Path(path)
    ext = (ext if ext is not None else path.suffix.lstrip(".")).lower()

    try:
        size = path.stat().st_size
    except OSError as exc:
        return Integrity.UNKNOWN, f"could not stat file: {exc}"

    try:
        if ext in ("jpg", "jpeg"):
            return _verify_jpeg(path, size)
        if ext == "png":
            return _verify_png(path, size)
        if ext in _PILLOW_EXTS:
            return _verify_pillow_image(path, size, ext)
        if ext == "pdf":
            return _verify_pdf(path, size)
        if ext in _ZIP_EXTS:
            return _verify_zip(path, size)
        if ext in _ISOBMFF_EXTS:
            return _verify_isobmff(path, size)
        if ext == "mp3":
            return _verify_mp3(path, size)
        if ext in _SQLITE_EXTS:
            return _verify_sqlite(path, size)
        if ext == "gz":
            return _verify_gzip(path, size)
        if ext == "tar":
            return _verify_tar(path, size)
        if ext == "7z":
            return _verify_7z(path, size)
        if ext == "rar":
            return _verify_rar(path, size)
    except Exception as exc:  # never let one bad file crash a batch verification
        return Integrity.UNKNOWN, f"verification error: {type(exc).__name__}: {exc}"

    return Integrity.UNKNOWN, None  # text/unknown formats: don't guess


def verify_many(
    files: Iterable[RecoveredFile],
    max_workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[RecoveredFile]:
    """Verify a batch of `RecoveredFile`s concurrently. Returns copies (the model
    is frozen) with `integrity`/`integrity_reason` populated; everything else is
    unchanged. Order matches the input."""
    from dataclasses import replace

    files = list(files)
    total = len(files)
    if total == 0:
        return []

    results: list[Verdict | None] = [None] * total
    done = 0

    def _work(i: int) -> None:
        results[i] = verify(files[i].path, files[i].ext)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, _ in enumerate(executor.map(_work, range(total))):
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    return [
        replace(f, integrity=verdict[0], integrity_reason=verdict[1])
        for f, verdict in zip(files, results)
    ]
