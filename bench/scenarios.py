"""Scenario mutators.

Each scenario takes a freshly-populated image (as produced by `bench.images.make_image`)
and a working copy of it, mutates that copy to simulate a real data-loss event, and
returns the `GroundTruth` of what should still be recoverable afterwards - which
corpus files, and what a good recovery engine could plausibly report as their
name/directory/modified time.

Every scenario function has the signature
    (image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth
even though most ignore `seed`/`size_mb`, so `run_scenario` can dispatch uniformly.
"""
from __future__ import annotations

import os
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bench import images
from bench.corpus import CorpusFile

SCENARIOS: tuple[str, ...] = (
    "delete_all",
    "delete_subset",
    "delete_folder_tree",
    "quick_format",
    "partial_overwrite",
    "fragmentation",
    "emptied_trash",
)


@dataclass(frozen=True)
class ExpectedFile:
    sha256: str
    name: str
    dir: str  # directory a good engine could plausibly report at scan time
    mtime: datetime
    size: int
    ext: str
    deleted: bool  # was this corpus file deleted (vs left live on the volume)?


@dataclass(frozen=True)
class GroundTruth:
    scenario: str
    fs: str
    expected: dict[str, ExpectedFile]  # keyed by sha256
    destroyed: tuple[str, ...] = ()  # sha256 of corpus files confirmed unrecoverable (informational)
    notes: str = ""


def _expected_from(f: CorpusFile, deleted: bool, dir_override: str | None = None) -> ExpectedFile:
    return ExpectedFile(
        sha256=f.sha256,
        name=f.name,
        dir=f.dir if dir_override is None else dir_override,
        mtime=f.mtime,
        size=f.size,
        ext=f.ext,
        deleted=deleted,
    )


def _rm(mount: Path, files: list[CorpusFile]) -> None:
    for f in files:
        p = mount / f.rel_path
        if p.exists():
            p.unlink()
        # macOS leaves an AppleDouble sidecar (._name) for resource forks/xattrs on
        # filesystems without native xattr support (FAT32/exFAT); clean it up too
        # so it doesn't linger as an orphan the engine might carve as "junk".
        sidecar = p.parent / f"._{p.name}"
        if sidecar.exists():
            sidecar.unlink()


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def delete_all(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    with images.attached(image_path) as (_device, mount):
        _rm(mount, files)
    return GroundTruth(
        scenario="delete_all",
        fs=fs,
        expected={f.sha256: _expected_from(f, deleted=True) for f in files},
    )


def delete_subset(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    ordered = sorted(files, key=lambda f: f.rel_path)
    to_delete = ordered[0::2]  # half deleted, half left live - tests false positives too
    with images.attached(image_path) as (_device, mount):
        _rm(mount, to_delete)
    deleted_sha = {f.sha256 for f in to_delete}
    expected = {f.sha256: _expected_from(f, deleted=f.sha256 in deleted_sha) for f in files}
    return GroundTruth(scenario="delete_subset", fs=fs, expected=expected)


_TREE_ROOT = "Documents/Reports"


def delete_folder_tree(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    with images.attached(image_path) as (_device, mount):
        target_dir = mount / _TREE_ROOT
        if target_dir.exists():
            shutil.rmtree(target_dir)
    in_tree = {f.sha256 for f in files if f.rel_path == _TREE_ROOT or f.rel_path.startswith(_TREE_ROOT + "/")}
    expected = {f.sha256: _expected_from(f, deleted=f.sha256 in in_tree) for f in files}
    return GroundTruth(scenario="delete_folder_tree", fs=fs, expected=expected, notes=f"rm -r {_TREE_ROOT}")


def quick_format(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    with images.attached(image_path) as (device, _mount):
        images.reformat_volume(device, fs, volname="BENCH")
        # Deliberately no other action: the volume is now empty from the fs's own
        # point of view. Original file bytes should still sit in the data region
        # of the image (a "quick" format rewrites metadata, not data), but the
        # original name/directory only survives if remnants of the old metadata
        # are still findable in unallocated space - which is exactly the harder,
        # more valuable thing filesystem-aware recovery is being measured on here.
    return GroundTruth(
        scenario="quick_format",
        fs=fs,
        expected={f.sha256: _expected_from(f, deleted=True) for f in files},
        notes=(
            "filesystem metadata was replaced by the reformat; name/path accuracy here measures "
            "whether an engine can dig up remnants of the old metadata, not just carve data"
        ),
    )


def partial_overwrite(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    with images.attached(image_path) as (_device, mount):
        _rm(mount, files)

    rnd = random.Random(seed)
    total_bytes = size_mb * 1024 * 1024
    overwrite_len = int(total_bytes * 0.25)
    # Stay clear of the first slice of the image (partition table/boot sector/
    # superblock) so we're overwriting data blocks, not instantly destroying the
    # whole filesystem structure (that's quick_format's job) - and randomise where
    # in the rest of the volume the overwrite lands, so which files survive isn't
    # always "whatever happened to be written first".
    header_reserve = min(2 * 1024 * 1024, total_bytes // 10)
    max_start = max(total_bytes - overwrite_len, header_reserve)
    offset = rnd.randint(header_reserve, max_start) if max_start > header_reserve else header_reserve
    images.overwrite_region(image_path, offset, overwrite_len, seed=seed)

    image_bytes = Path(image_path).read_bytes()
    expected: dict[str, ExpectedFile] = {}
    destroyed: list[str] = []
    for f in files:
        if f.content in image_bytes:
            expected[f.sha256] = _expected_from(f, deleted=True)
        else:
            destroyed.append(f.sha256)
    return GroundTruth(
        scenario="partial_overwrite",
        fs=fs,
        expected=expected,
        destroyed=tuple(destroyed),
        notes=(
            f"overwrote {overwrite_len} bytes ({overwrite_len / total_bytes:.0%} of the volume) "
            f"starting at offset {offset}; ground truth was computed by re-scanning the raw image "
            f"for each file's exact bytes after the overwrite, not by guessing"
        ),
    )


# Corpus files big enough to span several clusters, so rewriting them into
# scattered holes actually forces non-contiguous placement.
_FRAG_TARGETS = (
    "Videos/Clips/clip_01.mp4",
    "Videos/Clips/clip_02.mp4",
    "DCIM/100APPLE/IMG_0006.JPG",
)


def fragmentation(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    rnd = random.Random(seed)
    by_path = {f.rel_path: f for f in files}
    targets = [by_path[p] for p in _FRAG_TARGETS if p in by_path]

    with images.attached(image_path) as (_device, mount):
        free_bytes = shutil.disk_usage(mount).free
        filler_dir = mount / "_filler"
        filler_dir.mkdir(exist_ok=True)

        # 1. Fill most of what's left with small filler files.
        chunk = 512 * 1024
        budget = int(free_bytes * 0.85)
        filler_paths: list[Path] = []
        written = 0
        i = 0
        while written < budget:
            i += 1
            n = min(chunk, budget - written)
            if n <= 0:
                break
            p = filler_dir / f"junk_{i:05d}.bin"
            p.write_bytes(rnd.randbytes(n))
            filler_paths.append(p)
            written += n

        # 2. Delete every other filler file, leaving scattered holes smaller
        #    than a whole target file.
        for p in filler_paths[0::2]:
            p.unlink()

        # 3. Delete then rewrite each target file: with only small scattered
        #    holes free, the allocator is forced to split it non-contiguously.
        for f in targets:
            dest = mount / f.rel_path
            if dest.exists():
                dest.unlink()
            dest.write_bytes(f.content)
            ts = f.mtime.timestamp()
            os.utime(dest, (ts, ts))

        # 4. Delete the (now fragmented) target files, and clean up filler.
        _rm(mount, targets)
        for p in filler_dir.glob("junk_*.bin"):
            p.unlink()
        if filler_dir.exists():
            filler_dir.rmdir()

    target_sha = {f.sha256 for f in targets}
    expected = {f.sha256: _expected_from(f, deleted=f.sha256 in target_sha) for f in files}
    return GroundTruth(
        scenario="fragmentation",
        fs=fs,
        expected=expected,
        notes=f"deliberately fragmented then deleted: {list(_FRAG_TARGETS)}",
    )


_TRASH_TARGETS = (
    "DCIM/100APPLE/IMG_0002.JPG",
    "Music/song_01.mp3",
    "Documents/notes.txt",
)
_TRASH_DIR = ".Trashes/501"


def emptied_trash(image_path: Path, files: list[CorpusFile], fs: str, seed: int, size_mb: int) -> GroundTruth:
    by_path = {f.rel_path: f for f in files}
    targets = [by_path[p] for p in _TRASH_TARGETS if p in by_path]

    with images.attached(image_path) as (_device, mount):
        trash_dir = mount / _TRASH_DIR
        trash_dir.mkdir(parents=True, exist_ok=True)
        for f in targets:
            src = mount / f.rel_path
            if src.exists():
                shutil.move(str(src), str(trash_dir / f.name))
        # "Empty trash."
        for f in targets:
            p = trash_dir / f.name
            if p.exists():
                p.unlink()

    target_sha = {f.sha256 for f in targets}
    expected: dict[str, ExpectedFile] = {}
    for f in files:
        deleted = f.sha256 in target_sha
        expected[f.sha256] = _expected_from(f, deleted=deleted, dir_override=_TRASH_DIR if deleted else None)
    return GroundTruth(
        scenario="emptied_trash",
        fs=fs,
        expected=expected,
        notes=f"moved to {_TRASH_DIR} then deleted: {list(_TRASH_TARGETS)}",
    )


SCENARIO_FUNCS = {
    "delete_all": delete_all,
    "delete_subset": delete_subset,
    "delete_folder_tree": delete_folder_tree,
    "quick_format": quick_format,
    "partial_overwrite": partial_overwrite,
    "fragmentation": fragmentation,
    "emptied_trash": emptied_trash,
}


def run_scenario(
    name: str,
    image_path: Path,
    files: list[CorpusFile],
    fs: str,
    seed: int,
    size_mb: int,
) -> GroundTruth:
    if name not in SCENARIO_FUNCS:
        raise ValueError(f"unknown scenario {name!r}; choose from {SCENARIOS}")
    return SCENARIO_FUNCS[name](image_path, files, fs, seed, size_mb)
