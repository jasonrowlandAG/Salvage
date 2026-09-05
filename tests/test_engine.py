from __future__ import annotations

from pathlib import Path

import pytest

from salvage.engine.models import ScanMode
from salvage.engine.photorec import PhotoRecEngine
from salvage.engine.results import recover_files

TEST_IMAGE = Path("/Users/jasonrowland/Salvage/.probe/test_partition.raw")

_binary = PhotoRecEngine.locate_binary()

pytestmark = pytest.mark.skipif(
    _binary is None or not TEST_IMAGE.exists(),
    reason="photorec binary or .probe/test_partition.raw not available",
)


def test_deep_scan_recovers_jpg(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path, ScanMode.DEEP)

    assert result.success
    assert result.error is None
    assert not result.cancelled
    assert result.output_dirs
    assert any(f.ext == "jpg" for f in result.files)


def test_extension_filter_limits_results(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path, ScanMode.DEEP, extensions=["jpg"])

    assert result.success
    assert result.files
    assert all(f.ext == "jpg" for f in result.files)


def test_recover_files_copies_and_organises(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path / "scan", ScanMode.DEEP, extensions=["jpg"])
    assert result.files

    dest = tmp_path / "out"
    copied = recover_files(result.files, dest, organise_by_category=True)

    assert len(copied) == len(result.files)
    assert all(p.exists() for p in copied)
    assert (dest / "Image").is_dir()
