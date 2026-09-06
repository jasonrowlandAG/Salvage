"""Deterministic corpus of real, valid files used to populate benchmark disk images.

Everything here is seeded so that ``build_corpus(seed)`` produces byte-identical
files (and therefore identical sha256 hashes) across runs and machines. That
determinism is what lets bench scores be compared run over run.
"""
from __future__ import annotations

import hashlib
import io
import random
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - pillow-heif is a project dependency
    pillow_heif = None  # type: ignore[assignment]

DEFAULT_SEED = 20260906


@dataclass(frozen=True)
class CorpusFile:
    """One planted file: its bytes, hash, and where/when it should live on a volume."""

    rel_path: str  # POSIX-style path within the volume, e.g. "DCIM/100APPLE/IMG_0001.JPG"
    content: bytes
    sha256: str
    size: int
    mtime: datetime  # planted modified time (UTC, second resolution)

    @property
    def name(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]

    @property
    def dir(self) -> str:
        parts = self.rel_path.rsplit("/", 1)
        return parts[0] if len(parts) > 1 else ""

    @property
    def ext(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


def corpus_by_path(files: list[CorpusFile]) -> dict[str, CorpusFile]:
    return {f.rel_path: f for f in files}


def total_size(files: list[CorpusFile]) -> int:
    return sum(f.size for f in files)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mk(rel_path: str, content: bytes, mtime: datetime) -> CorpusFile:
    return CorpusFile(rel_path=rel_path, content=content, sha256=_sha256(content), size=len(content), mtime=mtime)


def _photo_like_image(rnd: random.Random, w: int, h: int) -> Image.Image:
    """A believable photo stand-in: a small random tile upscaled to (w, h) for
    low-frequency structure, blended with a touch of full-resolution noise for
    grain. Pure per-pixel noise (maximum entropy) compresses so unpredictably
    that PhotoRec's own end-of-image scan can find a false EOI marker inside the
    entropy-coded data and truncate the carve; real photos don't have that
    problem because they aren't literally random, so this doesn't either."""
    tile_w, tile_h = 48, 32
    tile = Image.frombytes("RGB", (tile_w, tile_h), rnd.randbytes(tile_w * tile_h * 3))
    smooth = tile.resize((w, h), Image.BICUBIC)
    noise = Image.frombytes("RGB", (w, h), rnd.randbytes(w * h * 3))
    return Image.blend(smooth, noise, 0.12)


_EXIF_IFD_OFFSET_TAG = 0x8769  # ExifOffset: points IFD0 at the nested Exif SubIFD
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DATETIME_DIGITIZED = 0x9004
_IFD0_DATETIME = 0x0132


def _jpeg_bytes(rnd: random.Random, w: int, h: int, quality: int, taken: datetime) -> bytes:
    img = _photo_like_image(rnd, w, h)
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S")
    exif = img.getexif()
    exif[_IFD0_DATETIME] = stamp
    # Real cameras always park the interesting timestamps in a nested Exif SubIFD
    # (reached via IFD0's ExifOffset pointer), not directly in IFD0. A flat IFD0
    # with no SubIFD is still spec-valid EXIF, but this PhotoRec build silently
    # fails to detect *any* JPEG carrying it (see bench/README.md) - so this
    # mirrors real camera output both for realism and to exercise that carver path.
    exif_ifd = exif.get_ifd(_EXIF_IFD_OFFSET_TAG)
    exif_ifd[_EXIF_DATETIME_ORIGINAL] = stamp
    exif_ifd[_EXIF_DATETIME_DIGITIZED] = stamp
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, exif=exif)
    return buf.getvalue()


def _heic_bytes(rnd: random.Random, w: int, h: int) -> bytes:
    if pillow_heif is None:
        raise RuntimeError("pillow_heif is required to build the HEIC corpus fixture")
    data = rnd.randbytes(w * h * 3)
    img = Image.frombytes("RGB", (w, h), data)
    buf = io.BytesIO()
    img.save(buf, format="HEIF", quality=45)
    return buf.getvalue()


def _png_bytes(rnd: random.Random, w: int, h: int) -> bytes:
    """A real, valid PNG: gradient plus light per-pixel noise so it compresses
    to a modest size (a full-noise image would bloat a lossless format)."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 255) // max(w - 1, 1), (y * 255) // max(h - 1, 1), rnd.randint(0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _pdf_bytes(title: str) -> bytes:
    """A minimal but structurally valid single-page PDF: correct object numbering,
    xref table with real byte offsets, and a trailer. Hand-built (no PDF library)."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    safe_title = title.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream_body = f"BT /F1 18 Tf 72 700 Td ({safe_title}) Tj ET".encode("latin-1", "replace")
    objects.append(b"<< /Length %d >>\nstream\n" % len(stream_body) + stream_body + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
    ).encode()
    return bytes(out)


_ZIP_FIXED_DATE = (2024, 1, 1, 0, 0, 0)  # fixed so zip bytes (and their sha256) are deterministic


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def _docx_bytes(body_text: str) -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ).encode()
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    ).encode()
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    ).encode()
    return _make_zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "word/document.xml": document,
        }
    )


def _xlsx_bytes() -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    ).encode()
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    ).encode()
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    ).encode()
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    ).encode()
    sheet1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Budget</t></is></c></row></sheetData>'
        "</worksheet>"
    ).encode()
    return _make_zip(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/worksheets/sheet1.xml": sheet1,
        }
    )


def _backup_zip_bytes() -> bytes:
    return _make_zip(
        {
            "readme.txt": b"Backup of the Q3 planning docs.\n",
            "checklist.csv": b"item,done\nbrief,yes\nbudget,yes\n",
        }
    )


_MAC_EPOCH_OFFSET = 2_082_844_800  # seconds between 1904-01-01 and 1970-01-01


def _box(box_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + box_type + body


def _mvhd_body(taken: datetime) -> bytes:
    mac_time = int(taken.timestamp()) + _MAC_EPOCH_OFFSET
    body = bytes([0]) + b"\x00\x00\x00"  # version 0, flags
    body += struct.pack(">I", mac_time)  # creation_time
    body += struct.pack(">I", mac_time)  # modification_time
    body += struct.pack(">I", 600)  # timescale
    body += struct.pack(">I", 0)  # duration
    body += b"\x00" * 80  # rate/volume/matrix/etc, unused by parsers we test against
    return body


def _mp4_bytes(rnd: random.Random, taken: datetime, mdat_size: int) -> bytes:
    """A hand-built ftyp+moov+mdat MP4: no real video, but a structurally valid
    box tree with a large mdat so the file spans many disk clusters."""
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    mvhd = _box(b"mvhd", _mvhd_body(taken))
    moov = _box(b"moov", mvhd)
    mdat = _box(b"mdat", rnd.randbytes(mdat_size))
    return ftyp + moov + mdat


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3_tit2_frame(title: str) -> bytes:
    text = b"\x00" + title.encode("latin-1")
    return b"TIT2" + struct.pack(">I", len(text)) + b"\x00\x00" + text


def _mp3_bytes(rnd: random.Random, n_frames: int, title: str) -> bytes:
    """ID3v2 tag followed by real MPEG-1 Layer III frame headers (128kbps/44.1kHz,
    sync word + valid header fields) with filler payload bytes."""
    frame_size = 417  # floor(144 * 128000 / 44100), no padding
    header = bytes([0xFF, 0xFB, 0x90, 0x02])
    frames = bytearray()
    for _ in range(n_frames):
        frames += header + rnd.randbytes(frame_size - len(header))

    id3_body = _id3_tit2_frame(title)
    id3_header = b"ID3" + bytes([0x03, 0x00, 0x00]) + _syncsafe(len(id3_body))
    return id3_header + id3_body + bytes(frames)


_NOTES_TXT = (
    b"Assembly Growth - bench corpus notes.\n"
    b"This file exists to exercise plain-text recovery paths.\n"
    b"Nothing sensitive here: just fixture content for the benchmark harness.\n"
)


# ---------------------------------------------------------------------------
# corpus assembly
# ---------------------------------------------------------------------------


def build_corpus(seed: int = DEFAULT_SEED) -> list[CorpusFile]:
    """Build the deterministic file corpus. Same seed -> byte-identical files."""
    rnd = random.Random(seed)
    base_date = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def planted_mtime() -> datetime:
        return base_date + timedelta(days=rnd.randint(0, 620), seconds=rnd.randint(0, 86_399))

    files: list[CorpusFile] = []

    # JPEGs, 20 KB - 3 MB, with EXIF DateTime
    jpeg_specs = [
        (300, 200, 70),
        (600, 400, 75),
        (1000, 700, 80),
        (1600, 1150, 82),
        (2200, 1600, 85),
        (3000, 2150, 90),
    ]
    for i, (w, h, q) in enumerate(jpeg_specs, start=1):
        mtime = planted_mtime()
        content = _jpeg_bytes(rnd, w, h, q, mtime)
        files.append(_mk(f"DCIM/100APPLE/IMG_{i:04d}.JPG", content, mtime))

    # HEIC
    mtime = planted_mtime()
    files.append(_mk("DCIM/100APPLE/IMG_0007.HEIC", _heic_bytes(rnd, 400, 300), mtime))

    # PNGs
    for i, (w, h) in enumerate([(320, 200), (500, 350)], start=1):
        mtime = planted_mtime()
        files.append(_mk(f"Pictures/Screenshots/screenshot_{i:02d}.png", _png_bytes(rnd, w, h), mtime))
    mtime = planted_mtime()
    files.append(_mk("Pictures/Wallpapers/wallpaper_01.png", _png_bytes(rnd, 640, 400), mtime))

    # Documents
    mtime = planted_mtime()
    files.append(_mk("Documents/Reports/q3.pdf", _pdf_bytes("Assembly Growth - Q3 Report"), mtime))

    mtime = planted_mtime()
    files.append(
        _mk(
            "Documents/Reports/summary.docx",
            _docx_bytes("Assembly Growth Q3 summary placeholder for bench corpus."),
            mtime,
        )
    )

    mtime = planted_mtime()
    files.append(_mk("Documents/Reports/Drafts/draft_q3.txt", b"Draft notes for the Q3 report.\n", mtime))

    mtime = planted_mtime()
    files.append(_mk("Documents/Finance/budget.xlsx", _xlsx_bytes(), mtime))

    mtime = planted_mtime()
    files.append(_mk("Documents/notes.txt", _NOTES_TXT, mtime))

    mtime = planted_mtime()
    files.append(_mk("Documents/Archive/backup.zip", _backup_zip_bytes(), mtime))

    # Videos - large mdat so files span many clusters
    for i, size in enumerate([3_000_000, 1_200_000], start=1):
        mtime = planted_mtime()
        files.append(_mk(f"Videos/Clips/clip_{i:02d}.mp4", _mp4_bytes(rnd, mtime, size), mtime))

    # Audio
    for i, n_frames in enumerate([1500, 750], start=1):
        mtime = planted_mtime()
        title = f"Bench Corpus Track {i}"
        files.append(_mk(f"Music/song_{i:02d}.mp3", _mp3_bytes(rnd, n_frames, title), mtime))

    return files
