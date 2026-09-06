"""Fast unit tests for the bench harness: scoring maths, corpus determinism, and
one @pytest.mark.slow smoke test that runs the real PhotoRec engine end-to-end.
Everything except the slow test should finish in well under 90s total."""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bench.corpus import DEFAULT_SEED, build_corpus
from bench.scenarios import ExpectedFile, GroundTruth, run_scenario
from bench.score import BenchResult, error_result, score

# ---------------------------------------------------------------------------
# scoring maths
# ---------------------------------------------------------------------------


@dataclass
class FakeRecovered:
    """Duck-types the bits of RecoveredFile that score() reads."""

    path: Path
    size: int
    original_name: str | None = None
    original_dir: str | None = None
    modified: datetime | None = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _expected(name: str, content: bytes, dir_: str = "Documents", mtime: datetime | None = None) -> ExpectedFile:
    mtime = mtime or datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return ExpectedFile(
        sha256=_sha256_bytes(content),
        name=name,
        dir=dir_,
        mtime=mtime,
        size=len(content),
        ext=name.rsplit(".", 1)[-1],
        deleted=True,
    )


def _gt(*expected: ExpectedFile, scenario: str = "delete_all", fs: str = "fat32") -> GroundTruth:
    return GroundTruth(scenario=scenario, fs=fs, expected={e.sha256: e for e in expected})


def test_recall_and_precision_basic(tmp_path):
    content_a = b"alpha file contents"
    content_b = b"bravo file contents"
    content_c = b"charlie file contents - never recovered"
    exp_a = _expected("a.txt", content_a)
    exp_b = _expected("b.txt", content_b)
    exp_c = _expected("c.txt", content_c)
    gt = _gt(exp_a, exp_b, exp_c)

    recovered = [
        FakeRecovered(path=_write(tmp_path, "r1.txt", content_a), size=len(content_a)),
        FakeRecovered(path=_write(tmp_path, "r2.txt", content_b), size=len(content_b)),
        FakeRecovered(path=_write(tmp_path, "junk.bin", b"not any expected file"), size=22),
    ]

    result = score(gt, recovered, engine="fake", elapsed_s=1.0)

    assert result.expected_count == 3
    assert result.recovered_true_positives == 2
    assert result.recall == pytest.approx(2 / 3)
    assert result.precision == pytest.approx(2 / 3)
    assert result.junk_count == 1
    assert result.junk_bytes == 22


def test_empty_result_does_not_crash(tmp_path):
    gt = _gt(_expected("only.txt", b"solo content"))
    result = score(gt, [], engine="fake", elapsed_s=0.0)
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.junk_count == 0
    assert result.returned_count == 0


def test_no_expected_files_precision_is_zero_not_divide_by_zero(tmp_path):
    gt = GroundTruth(scenario="delete_all", fs="fat32", expected={})
    junk = FakeRecovered(path=_write(tmp_path, "junk.bin", b"anything"), size=8)
    result = score(gt, [junk], engine="fake", elapsed_s=0.0)
    assert result.expected_count == 0
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.junk_count == 1


def test_duplicate_results_counted_once(tmp_path):
    content = b"the exact same bytes twice"
    exp = _expected("dup.txt", content)
    gt = _gt(exp)

    recovered = [
        FakeRecovered(path=_write(tmp_path, "copy1.txt", content), size=len(content)),
        FakeRecovered(path=_write(tmp_path, "copy2.txt", content), size=len(content)),
    ]
    result = score(gt, recovered, engine="fake", elapsed_s=0.0)

    assert result.expected_count == 1
    assert result.recovered_true_positives == 1  # counted once, not twice
    assert result.recall == 1.0
    assert result.returned_count == 2
    assert result.junk_count == 1  # the second identical copy is junk, not a second hit
    assert result.precision == pytest.approx(1 / 2)


def test_name_path_and_date_accuracy(tmp_path):
    content = b"file with real metadata to check"
    mtime = datetime(2024, 3, 15, 9, 30, 0, tzinfo=timezone.utc)
    exp = _expected("summary.docx", content, dir_="Documents/Reports", mtime=mtime)
    gt = _gt(exp)

    good = FakeRecovered(
        path=_write(tmp_path, "good.docx", content),
        size=len(content),
        original_name="summary.docx",
        original_dir="Documents/Reports",
        modified=mtime + timedelta(seconds=1),  # within the 2s tolerance
    )
    result = score(gt, [good], engine="filesystem", elapsed_s=0.0)

    assert result.name_accuracy == 1.0
    assert result.path_accuracy == 1.0
    assert result.date_accuracy == 1.0


def test_name_path_and_date_accuracy_wrong_values_score_zero(tmp_path):
    content = b"file with wrong reported metadata"
    mtime = datetime(2024, 3, 15, 9, 30, 0, tzinfo=timezone.utc)
    exp = _expected("summary.docx", content, dir_="Documents/Reports", mtime=mtime)
    gt = _gt(exp)

    wrong = FakeRecovered(
        path=_write(tmp_path, "wrong.docx", content),
        size=len(content),
        original_name="f0001234.docx",  # carver-style name, not the real one
        original_dir=None,  # PhotoRec never reports this
        modified=None,
    )
    result = score(gt, [wrong], engine="photorec", elapsed_s=0.0)

    assert result.recall == 1.0  # content still matched
    assert result.name_accuracy == 0.0
    assert result.path_accuracy == 0.0
    assert result.date_accuracy == 0.0


def test_date_accuracy_respects_tolerance_boundary(tmp_path):
    content = b"boundary check content"
    mtime = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    exp = _expected("x.txt", content, mtime=mtime)
    gt = _gt(exp)

    just_inside = FakeRecovered(
        path=_write(tmp_path, "inside.txt", content), size=len(content), modified=mtime + timedelta(seconds=2)
    )
    result_inside = score(gt, [just_inside], engine="fake", elapsed_s=0.0)
    assert result_inside.date_accuracy == 1.0

    just_outside = FakeRecovered(
        path=_write(tmp_path, "outside.txt", content), size=len(content), modified=mtime + timedelta(seconds=2.5)
    )
    result_outside = score(gt, [just_outside], engine="fake", elapsed_s=0.0)
    assert result_outside.date_accuracy == 0.0


def test_by_ext_breakdown(tmp_path):
    jpg_content = b"jpg bytes here"
    txt_content = b"txt bytes here"
    gt = _gt(_expected("a.jpg", jpg_content), _expected("b.txt", txt_content), _expected("c.txt", b"never found"))

    recovered = [
        FakeRecovered(path=_write(tmp_path, "r1.jpg", jpg_content), size=len(jpg_content)),
        FakeRecovered(path=_write(tmp_path, "r2.txt", txt_content), size=len(txt_content)),
    ]
    result = score(gt, recovered, engine="fake", elapsed_s=0.0)

    assert result.by_ext["jpg"]["expected"] == 1
    assert result.by_ext["jpg"]["recovered"] == 1
    assert result.by_ext["jpg"]["recall"] == 1.0
    assert result.by_ext["txt"]["expected"] == 2
    assert result.by_ext["txt"]["recovered"] == 1
    assert result.by_ext["txt"]["recall"] == 0.5


def test_error_result_is_a_zero_row_not_a_crash():
    gt = _gt(_expected("a.txt", b"content"), _expected("b.txt", b"more content"))
    result = error_result(gt, engine="filesystem", error="TSK does not support this filesystem")
    assert result.recall == 0.0
    assert result.precision == 0.0
    assert result.error == "TSK does not support this filesystem"
    assert result.expected_count == 2
    assert isinstance(result, BenchResult)


# ---------------------------------------------------------------------------
# corpus determinism
# ---------------------------------------------------------------------------


def test_corpus_same_seed_is_byte_identical():
    files_a = build_corpus(DEFAULT_SEED)
    files_b = build_corpus(DEFAULT_SEED)
    assert [f.sha256 for f in files_a] == [f.sha256 for f in files_b]
    assert [f.rel_path for f in files_a] == [f.rel_path for f in files_b]
    assert [f.mtime for f in files_a] == [f.mtime for f in files_b]


def test_corpus_different_seed_is_different():
    files_a = build_corpus(1)
    files_b = build_corpus(2)
    assert [f.sha256 for f in files_a] != [f.sha256 for f in files_b]


def test_corpus_covers_expected_categories():
    files = build_corpus()
    exts = {f.ext for f in files}
    assert {"jpg", "png", "heic", "pdf", "docx", "xlsx", "txt", "zip", "mp4", "mp3"} <= exts
    assert len(files) == len({f.rel_path for f in files})  # no accidental path collisions
    assert len(files) == len({f.sha256 for f in files})  # no accidental content collisions


# ---------------------------------------------------------------------------
# end-to-end smoke test (slow - real hdiutil image + real PhotoRec)
# ---------------------------------------------------------------------------

from salvage.engine.models import ScanMode  # noqa: E402
from salvage.engine.photorec import PhotoRecEngine  # noqa: E402

from bench import images  # noqa: E402

_photorec_binary = PhotoRecEngine.locate_binary()
_has_hdiutil = shutil.which("hdiutil") is not None

# FAT32's own minimum is ~33MB (newfs_msdos refuses smaller: "FAT32 is impossible
# for disk size of ..."), so this is as small/fast as this scenario can go on
# macOS - the spec's suggested 24MB doesn't actually format.
_SMOKE_SIZE_MB = 33


@pytest.mark.slow
@pytest.mark.skipif(_photorec_binary is None, reason="photorec binary not available")
@pytest.mark.skipif(not _has_hdiutil, reason="hdiutil not available (this harness is macOS-only)")
def test_smoke_delete_all_fat32_real_photorec(tmp_path):
    files = build_corpus()
    image_path = tmp_path / "fat32_delete_all.img"
    images.make_image("fat32", _SMOKE_SIZE_MB, image_path, files)

    try:
        ground_truth = run_scenario("delete_all", image_path, files, "fat32", DEFAULT_SEED, _SMOKE_SIZE_MB)

        engine = PhotoRecEngine()
        scan_workdir = tmp_path / "scan"
        scan_workdir.mkdir()
        extensions = sorted({f.ext for f in files} - {"docx", "xlsx", "mp4", "heic"} | {"zip", "mov"})
        scan_result = engine.scan(str(image_path), scan_workdir, mode=ScanMode.DEEP, extensions=extensions)

        assert scan_result.error is None
        result = score(ground_truth, scan_result.files, engine="photorec", elapsed_s=0.0)

        assert result.expected_count == len(files)
        # A loose bound, not an exact figure: real PhotoRec behaviour can vary
        # slightly across builds/OS versions. What matters is that recovery
        # actually works end to end, not a specific percentage.
        assert result.recall >= 0.5, f"expected most of a freshly-deleted, undamaged volume to carve back; got {result.recall:.0%}"
        assert result.precision > 0
    finally:
        # This harness is FAT32/small/single-scenario - no reformat, no fragmentation
        # filler - so nothing should still be attached, but check anyway.
        images.detach_leaked({str(image_path)})
