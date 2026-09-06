"""Scoring core: compare a ground truth against what an engine actually recovered.

Content match (sha256 of recovered bytes == sha256 of the original corpus file) is
the *only* thing that counts as a true recovery. A same-size, same-name file with
the wrong bytes is a miss, not a partial credit - PhotoRec-style carvers can and do
produce files that merely look right.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench.scenarios import ExpectedFile, GroundTruth

DATE_TOLERANCE_S = 2.0  # FAT's own mtime resolution is 2s; TSK/carvers shouldn't do worse


@dataclass
class TypeBreakdown:
    ext: str
    expected: int = 0
    recovered: int = 0

    @property
    def recall(self) -> float | None:
        return None if self.expected == 0 else self.recovered / self.expected


@dataclass
class BenchResult:
    engine: str
    scenario: str
    fs: str
    recall: float
    precision: float
    name_accuracy: float
    path_accuracy: float
    date_accuracy: float
    expected_count: int
    recovered_true_positives: int
    returned_count: int
    junk_count: int
    junk_bytes: int
    elapsed_s: float
    by_ext: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def score(
    ground_truth: GroundTruth,
    recovered_files: list[Any],
    engine: str,
    elapsed_s: float,
    hash_fn=_sha256_file,
) -> BenchResult:
    """`recovered_files` is a list of objects with `.path`, `.size`, and optionally
    `.original_name`, `.original_dir`, `.modified` (i.e. `RecoveredFile` instances,
    or anything duck-typed the same way)."""
    expected = ground_truth.expected
    expected_count = len(expected)

    # Multiple returned files can hash to the same expected file (e.g. a carver
    # splitting one file into fragments that happen to reassemble identically, or
    # simple duplicates) - a duplicate return should count once as a true positive
    # and the rest as junk, per "duplicate results counted once".
    matched_expected: dict[str, Any] = {}  # sha256 -> the RecoveredFile that matched it (first one)
    junk_count = 0
    junk_bytes = 0
    returned_count = len(recovered_files)

    for rf in recovered_files:
        path = getattr(rf, "path", None)
        digest = hash_fn(Path(path)) if path is not None else None
        exp = expected.get(digest) if digest else None
        if exp is not None and exp.sha256 not in matched_expected:
            matched_expected[exp.sha256] = rf
        else:
            junk_count += 1
            junk_bytes += int(getattr(rf, "size", 0) or 0)

    recovered_true_positives = len(matched_expected)
    recall = recovered_true_positives / expected_count if expected_count else 0.0
    precision = recovered_true_positives / returned_count if returned_count else 0.0

    name_hits = 0
    path_hits = 0
    date_hits = 0
    by_ext: dict[str, TypeBreakdown] = {e: TypeBreakdown(ext=e) for e in {ef.ext for ef in expected.values()}}
    for ef in expected.values():
        by_ext.setdefault(ef.ext, TypeBreakdown(ext=ef.ext))
        by_ext[ef.ext].expected += 1

    for sha, rf in matched_expected.items():
        exp = expected[sha]
        by_ext[exp.ext].recovered += 1

        original_name = getattr(rf, "original_name", None)
        if original_name is not None and original_name == exp.name:
            name_hits += 1

        original_dir = getattr(rf, "original_dir", None)
        if original_dir is not None and _normalize_dir(original_dir) == _normalize_dir(exp.dir):
            path_hits += 1

        modified = getattr(rf, "modified", None)
        if modified is not None and _within_tolerance(modified, exp.mtime):
            date_hits += 1

    name_accuracy = name_hits / recovered_true_positives if recovered_true_positives else 0.0
    path_accuracy = path_hits / recovered_true_positives if recovered_true_positives else 0.0
    date_accuracy = date_hits / recovered_true_positives if recovered_true_positives else 0.0

    return BenchResult(
        engine=engine,
        scenario=ground_truth.scenario,
        fs=ground_truth.fs,
        recall=recall,
        precision=precision,
        name_accuracy=name_accuracy,
        path_accuracy=path_accuracy,
        date_accuracy=date_accuracy,
        expected_count=expected_count,
        recovered_true_positives=recovered_true_positives,
        returned_count=returned_count,
        junk_count=junk_count,
        junk_bytes=junk_bytes,
        elapsed_s=elapsed_s,
        by_ext={ext: {"expected": tb.expected, "recovered": tb.recovered, "recall": tb.recall} for ext, tb in sorted(by_ext.items())},
        notes=ground_truth.notes,
    )


def error_result(ground_truth: GroundTruth, engine: str, error: str, elapsed_s: float = 0.0) -> BenchResult:
    """A zero row for an engine that failed outright on this fs/scenario combo,
    e.g. TSK refusing a filesystem it doesn't recognise. Records why, doesn't crash."""
    return BenchResult(
        engine=engine,
        scenario=ground_truth.scenario,
        fs=ground_truth.fs,
        recall=0.0,
        precision=0.0,
        name_accuracy=0.0,
        path_accuracy=0.0,
        date_accuracy=0.0,
        expected_count=len(ground_truth.expected),
        recovered_true_positives=0,
        returned_count=0,
        junk_count=0,
        junk_bytes=0,
        elapsed_s=elapsed_s,
        error=error,
    )


def _normalize_dir(d: str) -> str:
    return d.strip("/").replace("\\", "/").lower()


def _within_tolerance(a: datetime, b: datetime) -> bool:
    a_ts = a.timestamp() if a.tzinfo else a.replace(tzinfo=timezone.utc).timestamp()
    b_ts = b.timestamp() if b.tzinfo else b.replace(tzinfo=timezone.utc).timestamp()
    return abs(a_ts - b_ts) <= DATE_TOLERANCE_S


def results_to_json(results: list[BenchResult]) -> str:
    return json.dumps([r.to_dict() for r in results], indent=2, default=str)
