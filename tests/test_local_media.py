from __future__ import annotations

import sqlite3
import struct
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from salvage.engine import local_media as lm
from salvage.engine.local_media import FoundMedia, MediaSource, ScanStats, export_found, scan_sources

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_jpeg_with_exif(path: Path, taken: str, size: tuple[int, int] = (20, 20)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (200, 50, 50))
    exif = img.getexif()
    exif[306] = taken  # DateTime tag; local_media checks this as a fallback to DateTimeOriginal
    img.save(path, "JPEG", exif=exif)


def _box(box_type: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + box_type + body


def _build_mvhd_body(unix_dt: datetime) -> bytes:
    mac_epoch_creation = int(unix_dt.timestamp()) + lm._MAC_EPOCH_OFFSET
    body = bytes([0]) + b"\x00\x00\x00"  # version 0, flags
    body += struct.pack(">I", mac_epoch_creation)  # creation_time
    body += struct.pack(">I", 0)  # modification_time
    body += struct.pack(">I", 600)  # timescale
    body += struct.pack(">I", 0)  # duration
    body += b"\x00" * 80  # rate/volume/matrix/etc - unused by the parser
    return body


def _write_mp4_stub(path: Path, taken: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00")
    mvhd = _box(b"mvhd", _build_mvhd_body(taken))
    moov = _box(b"moov", mvhd)
    mdat = _box(b"mdat", b"\xab" * 64)  # non-moov box the walker must skip over
    path.write_bytes(ftyp + moov + mdat)


def _make_photos_library(root: Path) -> tuple[Path, str, str]:
    lib = root / "Test.photoslibrary"
    (lib / "originals" / "A").mkdir(parents=True)
    (lib / "database").mkdir(parents=True)

    trashed_uuid = "AAAA1111-2222-3333-4444-555555555555"
    kept_uuid = "AAAA2222-3333-4444-5555-666666666666"
    (lib / "originals" / "A" / f"{trashed_uuid}.HEIC").write_bytes(b"trashed-photo-bytes")
    (lib / "originals" / "A" / f"{kept_uuid}.HEIC").write_bytes(b"kept-photo-bytes")

    conn = sqlite3.connect(lib / "database" / "Photos.sqlite")
    conn.execute(
        "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZFILENAME TEXT, ZTRASHEDSTATE INTEGER)"
    )
    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZTRASHEDSTATE) VALUES (?, ?, 1)",
        (trashed_uuid, "IMG_0001.HEIC"),
    )
    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZTRASHEDSTATE) VALUES (?, ?, 0)",
        (kept_uuid, "IMG_0002.HEIC"),
    )
    conn.commit()
    conn.close()
    return lib, trashed_uuid, kept_uuid


# ---------------------------------------------------------------------------
# EXIF / mvhd date parsing
# ---------------------------------------------------------------------------


def test_exif_taken_reads_datetime_tag(tmp_path):
    jpg = tmp_path / "photo.jpg"
    _write_jpeg_with_exif(jpg, "2019:06:15 08:30:00")
    taken = lm._exif_taken(jpg)
    assert taken == datetime(2019, 6, 15, 8, 30, 0)


def test_exif_taken_returns_none_without_exif(tmp_path):
    jpg = tmp_path / "plain.jpg"
    Image.new("RGB", (10, 10)).save(jpg, "JPEG")
    assert lm._exif_taken(jpg) is None


def test_mvhd_creation_time_parses_hand_built_atom(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    when = datetime(2021, 3, 4, 12, 0, 0)
    _write_mp4_stub(mp4, when)
    assert lm._mvhd_creation_time(mp4) == when


def test_mvhd_creation_time_none_for_garbage(tmp_path):
    junk = tmp_path / "not_a_video.mp4"
    junk.write_bytes(b"not a real mp4 file at all")
    assert lm._mvhd_creation_time(junk) is None


def test_mvhd_creation_time_bounds_deeply_nested_moov_boxes(tmp_path):
    # A hostile/corrupt .mov can nest "moov" boxes indefinitely. Before the
    # fix, _find_mvhd_bytes recursed once per nesting level with no depth
    # limit -- a ~32 KB file with 4,000 nested "moov" headers reliably raised
    # an uncaught RecursionError (a RuntimeError subclass that
    # _mvhd_creation_time's own `except (OSError, OverflowError, ValueError)`
    # does not catch), which propagated straight out of the media scan.
    nested = b""
    for _ in range(4000):
        nested = _box(b"moov", nested)
    hostile = tmp_path / "hostile.mov"
    hostile.write_bytes(nested)

    assert lm._mvhd_creation_time(hostile) is None


# ---------------------------------------------------------------------------
# scan_sources: generic folder walking, skip rules, dedup, min_size
# ---------------------------------------------------------------------------


def test_scan_sources_walks_nested_folders_and_skips_junk_dirs(tmp_path):
    root = tmp_path / "root"
    _write_jpeg_with_exif(root / "sub" / "deeper" / "a.jpg", "2020:01:01 00:00:00")
    (root / ".hidden").mkdir(parents=True)
    (root / ".hidden" / "secret.jpg").write_bytes(b"x" * 30_000)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "icon.png").write_bytes(b"y" * 30_000)
    (root / "Library" / "Caches").mkdir(parents=True)
    (root / "Library" / "Caches" / "thumb.jpg").write_bytes(b"z" * 30_000)
    (root / "recovered.1").mkdir(parents=True)
    (root / "recovered.1" / "f0.jpg").write_bytes(b"w" * 30_000)
    (root / "notes.txt").write_bytes(b"not media")

    source = MediaSource(key="t", label="Test", path=root, kind="folder", accessible=True)
    results = scan_sources([source], min_size=0, hash_dupes=False)

    names = {fm.name for fm in results}
    assert names == {"a.jpg"}
    assert results[0].taken == datetime(2020, 1, 1, 0, 0, 0)
    assert results[0].category == "image"


def test_scan_sources_skips_photoslibrary_bundles_in_generic_folders(tmp_path):
    # A .photoslibrary package can sit inside a plain folder source (e.g. ~/Pictures).
    # It must be left to the dedicated photos_library source, not re-walked as a
    # generic folder (which would miss trash-state and pick up internal derivatives).
    root = tmp_path / "root"
    (root / "Vacation.photoslibrary" / "originals" / "A").mkdir(parents=True)
    (root / "Vacation.photoslibrary" / "originals" / "A" / "photo.jpg").write_bytes(b"x" * 30_000)
    (root / "plain.jpg").write_bytes(b"y" * 30_000)

    source = MediaSource(key="t", label="Test", path=root, kind="folder", accessible=True)
    results = scan_sources([source], min_size=20_000, hash_dupes=False)

    assert {fm.name for fm in results} == {"plain.jpg"}


def test_scan_sources_respects_min_size_for_generic_folders(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "small.jpg").write_bytes(b"x" * 100)
    (root / "big.jpg").write_bytes(b"y" * 30_000)

    source = MediaSource(key="t", label="Test", path=root, kind="folder", accessible=True)

    filtered = scan_sources([source], min_size=20_000, hash_dupes=False)
    assert {fm.name for fm in filtered} == {"big.jpg"}

    unfiltered = scan_sources([source], min_size=0, hash_dupes=False)
    assert {fm.name for fm in unfiltered} == {"small.jpg", "big.jpg"}


def test_scan_sources_marks_duplicates_by_content_hash(tmp_path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    content = b"identical-bytes" * 2000  # keep it above the default min_size
    (root / "a" / "one.jpg").write_bytes(content)
    (root / "b" / "two.jpg").write_bytes(content)
    (root / "a" / "unique.jpg").write_bytes(b"different-bytes" * 2000)

    source = MediaSource(key="t", label="Test", path=root, kind="folder", accessible=True)
    results = scan_sources([source], min_size=0, hash_dupes=True)

    assert len(results) == 3
    dupes = [fm for fm in results if fm.duplicate_of is not None]
    originals = [fm for fm in results if fm.duplicate_of is None]
    assert len(dupes) == 1
    assert len(originals) == 2
    assert dupes[0].duplicate_of in {fm.path for fm in originals}
    assert all(fm.sha256 for fm in results)


def test_scan_sources_skips_inaccessible_sources(tmp_path):
    source = MediaSource(
        key="t", label="Blocked", path=tmp_path / "does_not_exist", kind="folder",
        accessible=False, note="Grant Full Disk Access",
    )
    results = scan_sources([source])
    assert results == []


def test_scan_sources_progress_and_cancel(tmp_path):
    root = tmp_path / "root"
    for i in range(5):
        (root).mkdir(exist_ok=True)
        (root / f"f{i}.jpg").write_bytes(b"x" * 30_000)
    source = MediaSource(key="t", label="Test", path=root, kind="folder", accessible=True)

    seen: list[ScanStats] = []
    cancel = threading.Event()
    cancel.set()  # cancel immediately; scan_sources must stop cleanly, not hang or error
    results = scan_sources([source], on_progress=seen.append, cancel=cancel, min_size=0)
    assert results == []


# ---------------------------------------------------------------------------
# Photos library scanning
# ---------------------------------------------------------------------------


def test_scan_photos_library_marks_recently_deleted(tmp_path):
    lib, trashed_uuid, kept_uuid = _make_photos_library(tmp_path)
    source = MediaSource(key="photos", label="Photos Library", path=lib, kind="photos_library", accessible=True)

    results = scan_sources([source], min_size=20_000, hash_dupes=False)  # min_size must be ignored for this kind

    assert len(results) == 2  # both kept despite being smaller than min_size
    by_uuid = {fm.path.stem: fm for fm in results}
    assert by_uuid[trashed_uuid].in_recently_deleted is True
    assert by_uuid[kept_uuid].in_recently_deleted is False
    assert by_uuid[trashed_uuid].preview_only is False
    assert by_uuid[kept_uuid].preview_only is False


def _make_icloud_optimised_photos_library(root: Path) -> tuple[Path, str]:
    """An iCloud-optimised library: `originals/` exists but is empty for this asset — only
    downsized preview derivatives are actually on disk, the way Jay's real library looks."""
    lib = root / "Optimised.photoslibrary"
    (lib / "originals" / "B").mkdir(parents=True)
    (lib / "database").mkdir(parents=True)
    (lib / "resources" / "derivatives" / "B").mkdir(parents=True)
    (lib / "resources" / "derivatives" / "masters" / "B").mkdir(parents=True)

    uuid = "BBBB1111-2222-3333-4444-555555555555"
    # A small low-quality legacy thumbnail and a larger, better preview — the scan must
    # pick the larger one.
    (lib / "resources" / "derivatives" / "masters" / "B" / f"{uuid}_4_5005_c.jpeg").write_bytes(b"x" * 50)
    (lib / "resources" / "derivatives" / "B" / f"{uuid}_1_105_c.jpeg").write_bytes(b"y" * 500)
    # A transcode slice that must never be picked even though it's larger.
    (lib / "resources" / "derivatives" / "B" / f"{uuid}_cvt_t0000.jpeg").write_bytes(b"z" * 5000)

    conn = sqlite3.connect(lib / "database" / "Photos.sqlite")
    conn.execute(
        "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZFILENAME TEXT, "
        "ZDIRECTORY TEXT, ZDATECREATED REAL, ZKIND INTEGER, ZTRASHEDSTATE INTEGER, ZFAVORITE INTEGER)"
    )
    taken_core_data = datetime(2023, 8, 1, 10, 0, 0).timestamp() - lm._CORE_DATA_EPOCH_OFFSET
    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZDIRECTORY, ZDATECREATED, ZKIND, ZTRASHEDSTATE, ZFAVORITE) "
        "VALUES (?, ?, ?, ?, 0, 0, 1)",
        (uuid, "IMG_9999.HEIC", "20230801/somewhere", taken_core_data),
    )
    conn.commit()
    conn.close()
    return lib, uuid


def test_scan_photos_library_falls_back_to_largest_derivative_when_optimised(tmp_path):
    lib, uuid = _make_icloud_optimised_photos_library(tmp_path)
    source = MediaSource(key="photos", label="Photos Library", path=lib, kind="photos_library", accessible=True)

    results = scan_sources([source], hash_dupes=False)

    assert len(results) == 1
    fm = results[0]
    assert fm.preview_only is True
    assert fm.name == "IMG_9999.HEIC"  # original filename preserved even though we use a preview
    assert fm.path.name.endswith("_1_105_c.jpeg")  # the larger, non-cvt derivative was picked
    assert fm.note == "Original is in iCloud Photos — only a preview is on this Mac"
    assert fm.category == "image"
    assert fm.taken == datetime(2023, 8, 1, 10, 0, 0)


# ---------------------------------------------------------------------------
# ios_backup scanning against the real probe backup, if present
# ---------------------------------------------------------------------------


def test_scan_ios_backup_source_against_probe_backup_if_present():
    probe = Path(__file__).resolve().parent.parent / ".probe" / "ios_backup" / "00008101-001C60C12E52001E"
    if not probe.exists():
        pytest.skip("no probe iOS backup available in this checkout")
    source = MediaSource(key="ios", label="Probe Backup", path=probe, kind="ios_backup", accessible=True)
    results = scan_sources([source], hash_dupes=False)
    assert isinstance(results, list)
    for fm in results:
        assert fm.category in ("image", "video")
        assert fm.note and "from iPhone backup" in fm.note


# ---------------------------------------------------------------------------
# export_found
# ---------------------------------------------------------------------------


def test_export_found_copies_by_year_and_skips_duplicates(tmp_path):
    a = FoundMedia(
        path=tmp_path / "src" / "a.jpg",
        name="a.jpg",
        ext="jpg",
        size=10,
        category="image",
        source_key="t",
        taken=datetime(2018, 5, 1),
        modified=datetime(2018, 5, 1),
        sha256="hash1",
        duplicate_of=None,
    )
    b = FoundMedia(
        path=tmp_path / "src" / "b.jpg",
        name="b.jpg",
        ext="jpg",
        size=10,
        category="image",
        source_key="t",
        taken=datetime(2021, 8, 1),
        modified=datetime(2021, 8, 1),
        sha256="hash1",
        duplicate_of=a.path,
    )
    a.path.parent.mkdir(parents=True)
    a.path.write_bytes(b"content-a")
    b.path.write_bytes(b"content-b")

    dest = tmp_path / "export"
    copied = export_found([a, b], dest)
    assert copied == [dest / "2018" / "a.jpg"]
    assert (dest / "2018" / "a.jpg").read_bytes() == b"content-a"
    assert not (dest / "2021").exists()

    copied_all = export_found([a, b], dest, include_duplicates=True)
    assert len(copied_all) == 2
    assert (dest / "2021" / "b.jpg").exists()


# ---------------------------------------------------------------------------
# default_sources smoke test
# ---------------------------------------------------------------------------


def test_default_sources_returns_list_on_macos():
    sources = lm.default_sources()
    if sys.platform != "darwin":
        assert sources == []
        return
    assert isinstance(sources, list)
    keys = [s.key for s in sources]
    assert len(keys) == len(set(keys))  # keys unique
    for s in sources:
        assert isinstance(s, MediaSource)
        if not s.accessible:
            assert s.note
