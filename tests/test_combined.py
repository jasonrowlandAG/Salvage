"""Tests for CombinedEngine (filesystem-record recovery + carving, merged).

Uses tiny stub engines instead of real Sleuth Kit / PhotoRec so the merge
logic, error handling, and cancellation are exercised fast and portably --
the real end-to-end path is already covered by tests/test_filesystem.py and
bench/.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from salvage.engine.combined import CombinedEngine, merge_recovered
from salvage.engine.models import RecoveredFile, ScanMode, ScanResult, category_for


def _rf(path: Path, content: bytes, **kwargs) -> RecoveredFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ext = path.suffix.lstrip(".")
    return RecoveredFile(
        path=path,
        name=path.name,
        ext=ext,
        size=len(content),
        category=category_for(ext),
        **kwargs,
    )


class _StubEngine:
    """A `.scan()` that just returns a canned ScanResult, ignoring its arguments."""

    def __init__(self, result: ScanResult | Exception) -> None:
        self._result = result
        self.calls = 0

    def scan(self, source, workdir, mode=ScanMode.DEEP, extensions=None, on_progress=None, cancel=None):
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# ---------------------------------------------------------------------------
# merge_recovered: pure merge logic, no engines involved
# ---------------------------------------------------------------------------


def test_merge_content_match_keeps_named_filesystem_copy(tmp_path):
    fs_file = _rf(
        tmp_path / "fs" / "IMG_0001.JPG",
        b"same bytes",
        original_name="IMG_0001.JPG",
        original_dir="DCIM/100APPLE",
        deleted=True,
        source_engine="sleuthkit",
    )
    carved_dupe = _rf(tmp_path / "carve" / "f0001234.jpg", b"same bytes", source_engine="photorec")

    merged = merge_recovered([fs_file], [carved_dupe])

    assert len(merged) == 1
    assert merged[0] is fs_file
    assert merged[0].original_name == "IMG_0001.JPG"


def test_merge_unmatched_carved_files_survive(tmp_path):
    fs_file = _rf(tmp_path / "fs" / "a.jpg", b"aaa", original_name="a.jpg", source_engine="sleuthkit")
    carved_unique = _rf(tmp_path / "carve" / "f0002.jpg", b"different bytes entirely", source_engine="photorec")

    merged = merge_recovered([fs_file], [carved_unique])

    assert len(merged) == 2
    assert carved_unique in merged


def test_merge_same_size_different_content_is_not_deduped(tmp_path):
    # Same size as the filesystem copy, but different bytes -- must NOT be
    # treated as a duplicate just because the cheap size pre-filter matched.
    fs_file = _rf(tmp_path / "fs" / "a.txt", b"aaaa", original_name="a.txt", source_engine="sleuthkit")
    carved = _rf(tmp_path / "carve" / "f0003.txt", b"bbbb", source_engine="photorec")

    merged = merge_recovered([fs_file], [carved])

    assert len(merged) == 2


def test_merge_no_filesystem_files_keeps_all_carved(tmp_path):
    carved = [
        _rf(tmp_path / "carve" / "f1.jpg", b"one", source_engine="photorec"),
        _rf(tmp_path / "carve" / "f2.jpg", b"two", source_engine="photorec"),
    ]
    merged = merge_recovered([], carved)
    assert merged == carved


# ---------------------------------------------------------------------------
# CombinedEngine.scan: orchestration, degraded stages, cancellation
# ---------------------------------------------------------------------------


def test_filesystem_stage_failure_still_yields_carved_results(tmp_path):
    carved = _rf(tmp_path / "carve_src" / "f1.jpg", b"carved bytes", source_engine="photorec")
    fs_engine = _StubEngine(ScanResult(success=False, error="No recognisable filesystem found."))
    carve_engine = _StubEngine(ScanResult(success=True, files=[carved]))

    engine = CombinedEngine(fs_engine, carve_engine)
    result = engine.scan("source.img", tmp_path / "workdir")

    assert result.success is True
    assert result.error is None
    assert result.files == [carved]


def test_both_stages_failing_is_a_real_error(tmp_path):
    fs_engine = _StubEngine(ScanResult(success=False, error="fs broke"))
    carve_engine = _StubEngine(ScanResult(success=False, error="carve broke"))

    engine = CombinedEngine(fs_engine, carve_engine)
    result = engine.scan("source.img", tmp_path / "workdir")

    assert result.success is False
    assert result.cancelled is False
    assert "fs broke" in result.error
    assert "carve broke" in result.error


def test_carving_stage_exception_does_not_crash_scan(tmp_path):
    fs_file = _rf(tmp_path / "fs" / "a.jpg", b"aaa", original_name="a.jpg", source_engine="sleuthkit")
    fs_engine = _StubEngine(ScanResult(success=True, files=[fs_file]))
    carve_engine = _StubEngine(RuntimeError("photorec crashed"))

    engine = CombinedEngine(fs_engine, carve_engine)
    result = engine.scan("source.img", tmp_path / "workdir")

    # Filesystem stage alone succeeded, so this is not a total failure.
    assert result.success is True
    assert result.files == [fs_file]


def test_cancel_during_filesystem_stage_stops_before_carving(tmp_path):
    fs_engine = _StubEngine(ScanResult(success=False, cancelled=True))
    carve_engine = _StubEngine(ScanResult(success=True, files=[]))

    engine = CombinedEngine(fs_engine, carve_engine)
    cancel = threading.Event()
    cancel.set()
    result = engine.scan("source.img", tmp_path / "workdir", cancel=cancel)

    assert result.cancelled is True
    assert result.success is False
    assert carve_engine.calls == 0  # never got to the carving stage


def test_cancel_during_carving_stage_reports_cancelled(tmp_path):
    fs_file = _rf(tmp_path / "fs" / "a.jpg", b"aaa", original_name="a.jpg", source_engine="sleuthkit")
    fs_engine = _StubEngine(ScanResult(success=True, files=[fs_file]))
    carve_engine = _StubEngine(ScanResult(success=False, cancelled=True, files=[]))

    engine = CombinedEngine(fs_engine, carve_engine)
    result = engine.scan("source.img", tmp_path / "workdir")

    assert result.cancelled is True
    assert result.success is False
    # The filesystem stage's own results aren't lost even though the scan overall
    # didn't complete -- useful if a caller wants "what did we get before cancel".
    assert fs_file in result.files


def test_missing_photorec_engine_still_succeeds_with_filesystem_results(tmp_path):
    fs_file = _rf(tmp_path / "fs" / "a.jpg", b"aaa", original_name="a.jpg", source_engine="sleuthkit")
    fs_engine = _StubEngine(ScanResult(success=True, files=[fs_file]))

    engine = CombinedEngine(fs_engine, None)
    result = engine.scan("source.img", tmp_path / "workdir")

    assert result.success is True
    assert result.files == [fs_file]


def test_missing_both_engines_is_an_error(tmp_path):
    engine = CombinedEngine(None, None)
    result = engine.scan("source.img", tmp_path / "workdir")

    assert result.success is False
    assert result.error is not None


def test_photorec_always_called_with_deep_mode_regardless_of_requested_mode(tmp_path):
    captured_modes = []

    class _ModeCapturingCarver:
        def scan(self, source, workdir, mode=ScanMode.DEEP, extensions=None, on_progress=None, cancel=None):
            captured_modes.append(mode)
            return ScanResult(success=True, files=[])

    fs_engine = _StubEngine(ScanResult(success=True, files=[]))
    engine = CombinedEngine(fs_engine, _ModeCapturingCarver())
    engine.scan("source.img", tmp_path / "workdir", mode=ScanMode.THOROUGH)

    assert captured_modes == [ScanMode.DEEP]
