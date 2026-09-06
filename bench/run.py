"""CLI entry point: run the recovery-accuracy benchmark matrix.

    python -m bench.run --engines photorec,filesystem --filesystems fat32,exfat,hfs+ \\
        --scenarios all --out bench/results/<timestamp>.json [--quick]

For each (engine x filesystem x scenario) combination this builds a fresh disk
image from the corpus, applies the scenario's mutation, runs the engine's scan(),
scores the result against ground truth, and cleans up before moving on - so disk
usage stays bounded to one image/scan at a time rather than growing with the
matrix size.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from bench import images
from bench.corpus import DEFAULT_SEED, build_corpus
from bench.scenarios import SCENARIOS, run_scenario
from bench.score import BenchResult, error_result, results_to_json, score

from salvage.engine.models import ScanMode
from salvage.engine.photorec import PhotoRecEngine

try:
    from salvage.engine.filesystem import FilesystemEngine  # type: ignore[import-not-found]
except ImportError:
    FilesystemEngine = None  # not built yet - skip gracefully everywhere below

ALL_FILESYSTEMS = ["fat32", "exfat", "hfs+", "apfs"]  # ntfs excluded - see bench/README.md
ALL_ENGINES = ["photorec", "filesystem"]
QUICK_SCENARIOS = ("delete_all", "delete_subset", "quick_format")

QUICK_SIZE_MB = 64
NORMAL_SIZE_MB = 96

# PhotoRec's /cmd fileopt toggles aren't simply the file extension: several of our
# corpus formats are containers that PhotoRec recognises under a different family
# name (verified empirically against this build - `docx`/`xlsx`/`heic`/`mp4` are
# rejected as unknown toggle names with a "PhotoRec syntax error", even though
# PhotoRec *does* carve and correctly rename files of those types once the
# container family they ride on is enabled).
_EXT_TO_PHOTOREC_TOGGLE = {
    "docx": "zip",  # OOXML is a zip archive; PhotoRec detects+renames it from "zip"
    "xlsx": "zip",
    "mp4": "mov",  # PhotoRec bundles the ISO-BMFF/QuickTime family under "mov"
    "heic": "mov",  # HEIC is ISO-BMFF too, and rides the same "mov" detector
}


def _photorec_toggles(extensions: list[str]) -> list[str]:
    toggles = {_EXT_TO_PHOTOREC_TOGGLE.get(e, e) for e in extensions}
    return sorted(toggles)


def _engine_factory(name: str):
    if name == "photorec":
        return PhotoRecEngine()
    if name == "filesystem":
        return None if FilesystemEngine is None else FilesystemEngine()
    raise ValueError(f"unknown engine {name!r}; choose from {ALL_ENGINES}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Salvage recovery-accuracy benchmark harness")
    p.add_argument("--engines", default="photorec", help="comma-separated: photorec,filesystem")
    p.add_argument("--filesystems", default=",".join(ALL_FILESYSTEMS))
    p.add_argument("--scenarios", default="all")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--quick", action="store_true", help="smaller images and a short scenario list, for CI speed")
    p.add_argument("--size-mb", type=int, default=None, help="override the image size in MB (default: 64 with --quick, else 96)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="scratch dir for images/scans (default: a temp dir, removed at the end)",
    )
    return p.parse_args(argv)


def _skip_row(scenario: str, fs: str, engine_name: str, error: str) -> BenchResult:
    return BenchResult(
        engine=engine_name,
        scenario=scenario,
        fs=fs,
        recall=0.0,
        precision=0.0,
        name_accuracy=0.0,
        path_accuracy=0.0,
        date_accuracy=0.0,
        expected_count=0,
        recovered_true_positives=0,
        returned_count=0,
        junk_count=0,
        junk_bytes=0,
        elapsed_s=0.0,
        error=error,
    )


def run_matrix(
    engines: list[str],
    filesystems: list[str],
    scenarios: list[str],
    seed: int,
    quick: bool,
    workdir: Path | None,
    size_mb: int | None = None,
) -> list[BenchResult]:
    files = build_corpus(seed)
    corpus_extensions = sorted({f.ext for f in files})
    if size_mb is None:
        size_mb = QUICK_SIZE_MB if quick else NORMAL_SIZE_MB
    results: list[BenchResult] = []

    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="salvage_bench_"))
    workdir.mkdir(parents=True, exist_ok=True)
    tracked_images: set[str] = set()

    engine_objs = {}
    for name in engines:
        try:
            engine_objs[name] = _engine_factory(name)
        except Exception as exc:
            print(f"! engine {name!r} unavailable: {exc}", file=sys.stderr)
            engine_objs[name] = None
        if engine_objs[name] is None and name == "filesystem":
            print("! salvage.engine.filesystem.FilesystemEngine not found yet - skipping 'filesystem' rows", file=sys.stderr)

    total = len(filesystems) * len(scenarios) * len(engines)
    done = 0

    try:
        for fs in filesystems:
            for scenario in scenarios:
                image_path = workdir / f"{fs}_{scenario}.img"
                tracked_images.add(str(image_path))

                try:
                    images.make_image(fs, size_mb, image_path, files)
                except Exception as exc:
                    for engine_name in engines:
                        done += 1
                        print(f"[{done}/{total}] {engine_name:10s} {fs:6s} {scenario:20s} SKIP (image build failed)")
                        results.append(_skip_row(scenario, fs, engine_name, f"image build failed: {exc}"))
                    continue

                try:
                    ground_truth = run_scenario(scenario, image_path, files, fs, seed, size_mb)
                except Exception as exc:
                    for engine_name in engines:
                        done += 1
                        print(f"[{done}/{total}] {engine_name:10s} {fs:6s} {scenario:20s} SKIP (scenario failed)")
                        results.append(_skip_row(scenario, fs, engine_name, f"scenario mutation failed: {exc}"))
                    images.detach_leaked(tracked_images)
                    image_path.unlink(missing_ok=True)
                    continue

                for engine_name in engines:
                    done += 1
                    engine = engine_objs.get(engine_name)
                    if engine is None:
                        print(f"[{done}/{total}] {engine_name:10s} {fs:6s} {scenario:20s} SKIP (engine unavailable)")
                        results.append(error_result(ground_truth, engine_name, "engine unavailable"))
                        continue

                    scan_workdir = workdir / f"scan_{fs}_{scenario}_{engine_name}"
                    shutil.rmtree(scan_workdir, ignore_errors=True)
                    scan_workdir.mkdir(parents=True, exist_ok=True)

                    scan_extensions = _photorec_toggles(corpus_extensions) if engine_name == "photorec" else corpus_extensions

                    t0 = time.monotonic()
                    try:
                        scan_result = engine.scan(
                            str(image_path), scan_workdir, mode=ScanMode.DEEP, extensions=scan_extensions
                        )
                        elapsed = time.monotonic() - t0
                        if not scan_result.success and not scan_result.files:
                            result = error_result(ground_truth, engine_name, scan_result.error or "scan failed", elapsed)
                        else:
                            result = score(ground_truth, scan_result.files, engine_name, elapsed)
                    except Exception as exc:
                        elapsed = time.monotonic() - t0
                        traceback.print_exc()
                        result = error_result(ground_truth, engine_name, f"{type(exc).__name__}: {exc}", elapsed)

                    results.append(result)
                    if result.error:
                        print(f"[{done}/{total}] {engine_name:10s} {fs:6s} {scenario:20s} ERROR: {result.error} ({result.elapsed_s:.1f}s)")
                    else:
                        print(
                            f"[{done}/{total}] {engine_name:10s} {fs:6s} {scenario:20s} "
                            f"recall={result.recall:.0%} precision={result.precision:.0%} "
                            f"name={result.name_accuracy:.0%} path={result.path_accuracy:.0%} "
                            f"junk={result.junk_count} ({result.elapsed_s:.1f}s)"
                        )
                    shutil.rmtree(scan_workdir, ignore_errors=True)

                images.detach_leaked(tracked_images)
                image_path.unlink(missing_ok=True)
    finally:
        images.detach_leaked(tracked_images)
        shutil.rmtree(workdir, ignore_errors=True)

    return results


def print_table(results: list[BenchResult]) -> None:
    header = (
        f"{'engine':10s} {'fs':6s} {'scenario':20s} {'recall':>7s} {'precis.':>8s} "
        f"{'name':>6s} {'path':>6s} {'date':>6s} {'junk':>5s} {'time':>7s}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        if r.error:
            print(f"{r.engine:10s} {r.fs:6s} {r.scenario:20s} ERROR: {r.error}")
            continue
        print(
            f"{r.engine:10s} {r.fs:6s} {r.scenario:20s} {r.recall:7.0%} {r.precision:8.0%} "
            f"{r.name_accuracy:6.0%} {r.path_accuracy:6.0%} {r.date_accuracy:6.0%} {r.junk_count:5d} {r.elapsed_s:6.1f}s"
        )


def write_markdown(results: list[BenchResult], out_path: Path) -> None:
    lines = [
        "# Salvage bench results",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| Engine | FS | Scenario | Recall | Precision | Name acc. | Path acc. | Date acc. | Junk | Time (s) | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.engine} | {r.fs} | {r.scenario} | - | - | - | - | - | - | - | ERROR: {r.error} |")
        else:
            lines.append(
                f"| {r.engine} | {r.fs} | {r.scenario} | {r.recall:.0%} | {r.precision:.0%} | "
                f"{r.name_accuracy:.0%} | {r.path_accuracy:.0%} | {r.date_accuracy:.0%} | {r.junk_count} | "
                f"{r.elapsed_s:.1f} | {r.notes} |"
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    filesystems = [f.strip() for f in args.filesystems.split(",") if f.strip()]

    explicit_scenarios = args.scenarios != "all"
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()] if explicit_scenarios else list(SCENARIOS)
    if args.quick and not explicit_scenarios:
        scenarios = list(QUICK_SCENARIOS)

    results = run_matrix(engines, filesystems, scenarios, args.seed, args.quick, args.workdir, args.size_mb)

    print()
    print_table(results)

    out = args.out
    if out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(__file__).resolve().parent / "results" / f"{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(results_to_json(results))
    print(f"\nwrote {out}")

    md_path = out.parent / "latest.md"
    write_markdown(results, md_path)
    print(f"wrote {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
