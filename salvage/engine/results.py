from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from salvage.engine.devices import Device
from salvage.engine.models import RecoveredFile, category_for

_OFFSET_RE = re.compile(r"^f(\d+)")


def find_output_dirs(workdir: Path, base_name: str = "recovered") -> list[Path]:
    """PhotoRec appends .1, .2, ... to the /d path. Return the dirs that exist, in order."""
    workdir = Path(workdir)
    dirs: list[Path] = []
    n = 1
    while True:
        d = workdir / f"{base_name}.{n}"
        if not d.is_dir():
            break
        dirs.append(d)
        n += 1
    return dirs


def collect_recovered(output_dirs: list[Path]) -> list[RecoveredFile]:
    """Build RecoveredFile entries for every carved file, skipping PhotoRec's report.xml."""
    files: list[RecoveredFile] = []
    for d in output_dirs:
        for entry in sorted(Path(d).iterdir()):
            if not entry.is_file() or entry.name.lower() == "report.xml":
                continue
            ext = entry.suffix.lstrip(".").lower()
            m = _OFFSET_RE.match(entry.name)
            offset = int(m.group(1)) if m else None
            files.append(
                RecoveredFile(
                    path=entry,
                    name=entry.name,
                    ext=ext,
                    size=entry.stat().st_size,
                    category=category_for(ext),
                    offset=offset,
                )
            )
    return files


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def recover_files(
    files: Iterable[RecoveredFile],
    destination: Path,
    organise_by_category: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """Copy (never move) recovered files to destination, optionally sorted into category folders."""
    destination = Path(destination)
    files = list(files)
    total = len(files)
    copied: list[Path] = []
    for i, f in enumerate(files, start=1):
        target_dir = destination / f.category.capitalize() if organise_by_category else destination
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = _unique_path(target_dir / f.name)
        shutil.copy2(f.path, dest_path)
        copied.append(dest_path)
        if on_progress is not None:
            on_progress(i, total)
    return copied


# ---------------------------------------------------------------------------
# also_exists: flag carved results that are byte-identical to a file still
# present on the mounted source volume - noise a carver produces for anything
# it didn't realise was never actually deleted.
# ---------------------------------------------------------------------------

_MAX_LIVE_FILES = 200_000    # skip entirely on volumes bigger than this
_TIME_BUDGET_S = 20.0        # skip entirely if indexing the volume takes longer than this


def _sha256_of(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def mark_also_exists(
    files: Iterable[RecoveredFile],
    device: Device | None,
    max_files: int = _MAX_LIVE_FILES,
    time_budget_s: float = _TIME_BUDGET_S,
) -> list[RecoveredFile]:
    """Returns copies of `files` with `.also_exists` set for any recovered file
    whose content is byte-identical to a file still present on `device`'s mounted
    volume. Optional and time-bounded: if the volume isn't mounted, isn't
    readable, or has more than `max_files` entries (or indexing them takes
    longer than `time_budget_s`), this returns `files` unchanged rather than
    blocking. Live files are indexed by size only; hashing (of both the live
    candidate and the recovered file) happens only on a size collision."""
    files = list(files)
    if device is None or not device.mount_point:
        return files
    mount = Path(device.mount_point)
    if not mount.is_dir():
        return files

    by_size: dict[int, list[Path]] = {}
    start = time.monotonic()
    count = 0
    aborted = False
    try:
        for root, _dirs, filenames in os.walk(mount, onerror=lambda _e: None):
            for name in filenames:
                count += 1
                if count > max_files or (time.monotonic() - start) > time_budget_s:
                    aborted = True
                    break
                p = Path(root) / name
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                by_size.setdefault(size, []).append(p)
            if aborted:
                break
    except OSError:
        return files
    if aborted:
        return files

    live_hash_cache: dict[str, str | None] = {}
    result: list[RecoveredFile] = []
    for f in files:
        candidates = by_size.get(f.size)
        if not candidates:
            result.append(f)
            continue
        recovered_hash = _sha256_of(f.path)
        matched = False
        if recovered_hash is not None:
            for cand in candidates:
                key = str(cand)
                if key not in live_hash_cache:
                    live_hash_cache[key] = _sha256_of(cand)
                if live_hash_cache[key] == recovered_hash:
                    matched = True
                    break
        result.append(replace(f, also_exists=True) if matched else f)
    return result
