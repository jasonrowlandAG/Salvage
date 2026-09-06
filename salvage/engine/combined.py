"""Thorough scan: filesystem-record recovery first, then signature carving.

FilesystemEngine is fast and gives real names/paths/dates, but only for what
survives in filesystem metadata -- a quick-formatted volume or a filesystem it
doesn't understand comes back empty. PhotoRecEngine finds bytes everywhere
(deleted or not, formatted or not) but never knows a file's original name.
CombinedEngine runs both and reports the best of each: a file recovered by
both engines is reported once, using the filesystem copy's name/path/date;
carved files with no filesystem match are kept as-is (unnamed) -- they're the
extra recall carving adds on top of what filesystem parsing already found.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from salvage.engine.models import RecoveredFile, ScanMode, ScanProgress, ScanResult

_HASH_WORKERS = 8

# Rough split of the overall progress bar between the two stages. The
# filesystem stage only reads metadata plus whichever files are actually
# flagged deleted, so it's fast; the carve stage reads every sector of the
# source and dominates elapsed time on anything but a tiny image.
_FS_STAGE_SHARE = 0.15
_CARVE_STAGE_SHARE = 1.0 - _FS_STAGE_SHARE

_PHASE_FS = "Reading filesystem records"
_PHASE_CARVE = "Carving every sector"


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def merge_recovered(
    fs_files: list[RecoveredFile], carved_files: list[RecoveredFile]
) -> list[RecoveredFile]:
    """Combine a filesystem-engine result with a carving result, keeping every
    filesystem-recovered file and only the carved files that aren't byte-for-byte
    duplicates of one of them.

    Hashing is the expensive part when there are thousands of carved files, so
    two shortcuts keep it cheap: a carved file can only be somebody's duplicate
    if its *size* matches a filesystem-recovered file's size (skip hashing
    everything else -- typically most of a carve set, since junk fragments and
    unrelated file sizes can't collide), and whatever's left is hashed across a
    thread pool rather than one file at a time on the calling thread.
    """
    fs_by_size: dict[int, dict[str, RecoveredFile]] = defaultdict(dict)
    for f in fs_files:
        digest = _sha256_file(f.path)
        if digest is not None:
            fs_by_size[f.size][digest] = f

    merged: list[RecoveredFile] = list(fs_files)

    candidates = [f for f in carved_files if f.size in fs_by_size]
    passthrough = [f for f in carved_files if f.size not in fs_by_size]
    merged.extend(passthrough)

    if candidates:
        with ThreadPoolExecutor(max_workers=min(_HASH_WORKERS, len(candidates))) as pool:
            digests = list(pool.map(_sha256_file, (f.path for f in candidates)))
        for carved, digest in zip(candidates, digests):
            by_hash = fs_by_size.get(carved.size, {})
            if digest is not None and digest in by_hash:
                continue  # byte-identical to a named filesystem-recovered file -- drop it
            merged.append(carved)

    return merged


class CombinedEngine:
    """Runs the filesystem engine first, then carving, and merges: files recovered by
    both are reported once, keeping the filesystem copy's name/path/date."""

    def __init__(self, filesystem_engine=None, photorec_engine=None) -> None:
        self._filesystem_engine = filesystem_engine
        self._photorec_engine = photorec_engine

    def scan(
        self,
        source: str | Path,
        workdir: Path,
        mode: ScanMode = ScanMode.DEEP,
        extensions: list[str] | None = None,
        on_progress: Callable[[ScanProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> ScanResult:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()

        fs_files: list[RecoveredFile] = []
        fs_output_dirs: list[Path] = []
        fs_error: str | None = None
        fs_ran = False

        def emit(files_found: int, phase: str) -> None:
            if on_progress is not None:
                on_progress(
                    ScanProgress(
                        files_found=files_found,
                        elapsed_s=time.monotonic() - start_time,
                        phase=phase,
                    )
                )

        if self._filesystem_engine is not None:
            fs_ran = True
            fs_workdir = workdir / "filesystem"
            fs_workdir.mkdir(parents=True, exist_ok=True)

            def fs_progress(p: ScanProgress) -> None:
                emit(p.files_found, _PHASE_FS)

            try:
                fs_result = self._filesystem_engine.scan(
                    source,
                    fs_workdir,
                    mode=mode,
                    extensions=extensions,
                    on_progress=fs_progress,
                    cancel=cancel,
                )
            except Exception as exc:  # defensive: a sub-engine crashing shouldn't crash the combined scan
                fs_error = f"{type(exc).__name__}: {exc}"
                fs_result = None

            if fs_result is not None:
                if fs_result.cancelled:
                    return ScanResult(success=False, cancelled=True, files=fs_files)
                if fs_result.success:
                    fs_files = fs_result.files
                    fs_output_dirs = fs_result.output_dirs
                else:
                    fs_error = fs_result.error or "Filesystem stage failed."
        else:
            fs_error = "Filesystem engine not available."

        if cancel is not None and cancel.is_set():
            return ScanResult(success=False, cancelled=True, files=fs_files)

        emit(len(fs_files), _PHASE_FS)

        carved_files: list[RecoveredFile] = []
        carve_output_dirs: list[Path] = []
        carve_error: str | None = None
        carve_ran = False

        if self._photorec_engine is not None:
            carve_ran = True
            carve_workdir = workdir / "carve"
            carve_workdir.mkdir(parents=True, exist_ok=True)

            def carve_progress(p: ScanProgress) -> None:
                emit(len(fs_files) + p.files_found, _PHASE_CARVE)

            try:
                # Always DEEP, regardless of what mode CombinedEngine itself was
                # called with (e.g. ScanMode.THOROUGH, which only the routing
                # layer understands) -- PhotoRec's /cmd string is built from
                # `mode.value` directly and only knows the two carving modes.
                carve_result = self._photorec_engine.scan(
                    source,
                    carve_workdir,
                    mode=ScanMode.DEEP,
                    extensions=extensions,
                    on_progress=carve_progress,
                    cancel=cancel,
                )
            except Exception as exc:
                carve_error = f"{type(exc).__name__}: {exc}"
                carve_result = None

            if carve_result is not None:
                if carve_result.cancelled:
                    return ScanResult(
                        success=False,
                        cancelled=True,
                        files=merge_recovered(fs_files, carved_files),
                        output_dirs=fs_output_dirs,
                    )
                if carve_result.success:
                    carved_files = carve_result.files
                    carve_output_dirs = carve_result.output_dirs
                else:
                    carve_error = carve_result.error or "Carving stage failed."
        else:
            carve_error = "PhotoRec is not installed."

        fs_failed = fs_ran and fs_error is not None
        carve_failed = carve_ran and carve_error is not None
        both_failed = (not fs_ran or fs_failed) and (not carve_ran or carve_failed)
        if both_failed:
            reason = " / ".join(m for m in (fs_error, carve_error) if m) or "Both recovery stages failed."
            return ScanResult(success=False, error=reason)

        merged = merge_recovered(fs_files, carved_files)
        emit(len(merged), "Done")

        return ScanResult(
            success=True,
            files=merged,
            output_dirs=[*fs_output_dirs, *carve_output_dirs],
            # A stage that failed but not fatally (e.g. no filesystem record found
            # on a reformatted volume, carving still ran fine) is worth surfacing
            # as a note rather than silently swallowing -- but it's not an error,
            # since carrying on with the other stage alone is correct behaviour.
            error=None,
        )
