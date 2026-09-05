from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Iterable

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
