from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from bench.corpus import HEIC_FIXTURE
from salvage.engine import thumbcache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(thumbcache, "CACHE_DIR", cache_dir)
    return cache_dir


@dataclass
class _FakeMedia:
    path: Path
    cloud_placeholder: bool = False


def test_ensure_generates_thumbnail_for_plain_image(tmp_path):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (800, 600), (10, 120, 200)).save(img_path, "JPEG")

    result = thumbcache.ensure(img_path)

    assert result is not None
    assert result.exists()
    assert result.parent == thumbcache.CACHE_DIR
    with Image.open(result) as thumb:
        assert max(thumb.size) <= thumbcache.THUMB_SIZE


def test_ensure_is_idempotent_and_cached_on_second_call(tmp_path):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (400, 300), (5, 5, 5)).save(img_path, "JPEG")

    first = thumbcache.ensure(img_path)
    assert thumbcache.get(img_path) == first

    second = thumbcache.ensure(img_path)
    assert second == first


def test_get_returns_none_when_nothing_cached(tmp_path):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 100)).save(img_path, "JPEG")
    assert thumbcache.get(img_path) is None


def test_ensure_generates_thumbnail_for_heic(tmp_path):
    # A checked-in real HEIC rather than one encoded here: the app ships a decode-only
    # libheif (no HEVC encoder - see packaging/THIRD_PARTY.md), so nothing in this repo
    # can encode one any more. importorskip still guards the *decoder* being present.
    pytest.importorskip("pi_heif")
    heic_path = tmp_path / "photo.heic"
    heic_path.write_bytes(HEIC_FIXTURE.read_bytes())

    result = thumbcache.ensure(heic_path)

    assert result is not None
    assert result.exists()
    with Image.open(result) as thumb:
        assert max(thumb.size) <= thumbcache.THUMB_SIZE


def _write_mp4_stub(path: Path) -> None:
    # A minimal-but-not-really-playable mp4: valid enough box structure to have a .mp4
    # extension and pass basic sniffing, but not guaranteed to contain real video frames.
    # QuickLook may or may not be able to render a poster frame from it; either a rendered
    # thumbnail or a clean None (icon fallback) is an acceptable outcome for a stub this thin.
    def box(box_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + box_type + body

    ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00")
    mvhd = box(b"mvhd", bytes(100))
    moov = box(b"moov", mvhd)
    path.write_bytes(ftyp + moov)


def test_ensure_mp4_stub_returns_quicklook_thumb_or_none(tmp_path):
    mp4_path = tmp_path / "clip.mp4"
    _write_mp4_stub(mp4_path)

    result = thumbcache.ensure(mp4_path)

    # A hand-built stub isn't real video data, so QuickLook may legitimately fail to render
    # it — the contract is just that we never raise, and any success is a real cached file.
    if result is not None:
        assert result.exists()
        assert result.parent == thumbcache.CACHE_DIR


def test_ensure_for_skips_cloud_placeholder(tmp_path):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (200, 200)).save(img_path, "JPEG")
    placeholder = _FakeMedia(path=img_path, cloud_placeholder=True)

    result = thumbcache.ensure_for(placeholder)

    assert result is None
    assert thumbcache.get(img_path) is None  # never touched the file


def test_ensure_for_generates_when_not_placeholder(tmp_path):
    img_path = tmp_path / "photo.jpg"
    Image.new("RGB", (200, 200)).save(img_path, "JPEG")
    media = _FakeMedia(path=img_path, cloud_placeholder=False)

    result = thumbcache.ensure_for(media)

    assert result is not None
    assert result.exists()


def test_generate_batch_handles_missing_files_gracefully(tmp_path):
    missing = tmp_path / "does_not_exist.jpg"
    real = tmp_path / "real.jpg"
    Image.new("RGB", (100, 100)).save(real, "JPEG")

    results = thumbcache.generate_batch([missing, real])

    assert results[missing] is None
    assert results[real] is not None
    assert results[real].exists()
