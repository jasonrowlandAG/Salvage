"""Filesystem-aware recovery via The Sleuth Kit (mmls/fsstat/fls/icat).

Signature carving (PhotoRec) never sees a file's original name, folder or
timestamp -- it just finds bytes that look like a JPEG. A filesystem, though,
already keeps its own record of files, including ones marked deleted (a FAT
directory entry with its first byte blanked, an NTFS MFT entry flagged
unused, ...). Reading those records back is what lets recovered files keep
their real name, folder and dates -- this is the "Quick scan": it needs an
intact, recognisable filesystem. Deep scan (PhotoRec) remains the fallback
for anything Quick can't parse (e.g. after a full/quick format wipes the old
structures) or a filesystem this engine doesn't understand.

Known, empirically-verified limitation: HFS+ removes a file's catalog entry
at delete time (no tombstone), so `fls` has nothing to recover a name from --
a normal deletion on HFS+ will honestly come back with zero results here.
FAT stores short (8.3) names with the first character overwritten by the
delete marker (surfaced by TSK as e.g. "_hoto1.jpg"); when a long-file-name
entry exists (name > 8.3), TSK reconstructs the full original name from it.
exFAT and NTFS keep the full name intact either way.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from salvage.engine.models import (
    Integrity,
    RecoveredFile,
    ScanMode,
    ScanProgress,
    ScanResult,
    category_for,
)
from salvage.engine.privileged import run_privileged

_TOOL_NAMES = ("fls", "icat", "fsstat", "mmls")
_PROGRESS_INTERVAL = 0.2  # ~5/sec, matches PhotoRecEngine
_TSK_TIMEOUT = 120  # seconds; per-subprocess-call ceiling for a hung/damaged volume
_ICAT_TIMEOUT = 120
_MAX_COMPONENT_LEN = 200
_OUTPUT_DIR_NAME = "recovered"


@dataclass(frozen=True)
class VolumeInfo:
    """A volume found on `source`, addressed the way TSK's own -o flag expects
    (a sector offset, using whatever sector size mmls reported)."""

    offset: int                # sector offset, for `-o` on fsstat/fls/icat
    length: int | None         # sector length, if known from mmls; else None
    fstype: str | None         # TSK -f code, e.g. "fat32", "hfsp"; None if unrecognised
    fstype_label: str          # human string straight from fsstat, e.g. "FAT32", "HFS+"
    label: str | None = None   # volume label, if fsstat reported one


# ---------------------------------------------------------------------------
# mmls / fsstat parsing (probe)
# ---------------------------------------------------------------------------

_MMLS_ROW_RE = re.compile(
    r"^\d+:\s+(?P<slot>\S+)\s+(?P<start>\d+)\s+(?P<end>\d+)\s+(?P<length>\d+)\s+(?P<desc>.*?)\s*$"
)
_FSSTAT_TYPE_RE = re.compile(r"File System Type:\s*(.+)")
_FSSTAT_LABEL_RE = re.compile(
    r"Volume (?:Name|Label(?: \((?:from root directory|Boot Sector)\))?)\s*:\s*(.*)"
)


def _tsk_fstype_code(label: str) -> str | None:
    """Map fsstat's human "File System Type:" string to the code `-f` wants."""
    key = label.lower().replace(" ", "").replace("+", "+")
    if key.startswith("ntfs"):
        return "ntfs"
    if key.startswith("exfat"):
        return "exfat"
    if key.startswith("fat32"):
        return "fat32"
    if key.startswith("fat16"):
        return "fat16"
    if key.startswith("fat12"):
        return "fat12"
    if key.startswith("fat"):
        return "fat"
    if key.startswith("hfs+") or key.startswith("hfsplus"):
        return "hfsp"
    if key == "hfs":
        return "hfsl"
    if key.startswith("ext4"):
        return "ext4"
    if key.startswith("ext3"):
        return "ext3"
    if key.startswith("ext2"):
        return "ext2"
    if key.startswith("ext"):
        return "ext"
    if key.startswith("ufs1"):
        return "ufs1"
    if key.startswith("ufs2"):
        return "ufs2"
    if key.startswith("ufs"):
        return "ufs"
    if key.startswith("iso9660"):
        return "iso9660"
    if key.startswith("apfs"):
        return "apfs"
    return None


def _list_partitions(mmls_bin: Path, source: str) -> list[tuple[int, int | None, str]]:
    """Returns [(sector_offset, sector_length, description), ...] for real
    partitions, skipping Meta/Unallocated rows. Empty list if `source` has no
    partition table mmls can parse (e.g. it's a single already-carved volume,
    or the image is unreadable)."""
    try:
        proc = subprocess.run(
            [str(mmls_bin), source], capture_output=True, text=True, timeout=_TSK_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    rows: list[tuple[int, int | None, str]] = []
    for line in proc.stdout.splitlines():
        m = _MMLS_ROW_RE.match(line)
        if not m:
            continue
        if m.group("slot") in ("Meta", "-------"):
            continue
        rows.append((int(m.group("start")), int(m.group("length")), m.group("desc").strip()))
    return rows


def _probe_volume(fsstat_bin: Path, source: str, offset: int, length: int | None) -> VolumeInfo | None:
    try:
        proc = subprocess.run(
            [str(fsstat_bin), "-o", str(offset), source],
            capture_output=True,
            text=True,
            timeout=_TSK_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    type_match = _FSSTAT_TYPE_RE.search(proc.stdout)
    if not type_match:
        return None
    fstype_label = type_match.group(1).strip()
    label_match = _FSSTAT_LABEL_RE.search(proc.stdout)
    label = label_match.group(1).strip() if label_match else None
    return VolumeInfo(
        offset=offset,
        length=length,
        fstype=_tsk_fstype_code(fstype_label),
        fstype_label=fstype_label,
        label=label or None,
    )


def _probe_volumes(binaries: dict[str, Path], source: str) -> list[VolumeInfo]:
    partitions = _list_partitions(binaries["mmls"], source)
    if not partitions:
        # No partition table mmls recognises -- treat the whole source as one volume.
        partitions = [(0, None, "volume")]
    volumes = []
    for offset, length, _desc in partitions:
        vol = _probe_volume(binaries["fsstat"], source, offset, length)
        if vol is not None:
            volumes.append(vol)
    return volumes


# ---------------------------------------------------------------------------
# fls / icat parsing and extraction
# ---------------------------------------------------------------------------

# `fls -m /` mactime bodyfile columns: md5|name|inode|mode|uid|gid|size|atime|mtime|ctime|crtime
_FLS_LINE_RE = re.compile(
    r"^(?P<md5>[^|]*)\|(?P<name>[^|]*)\|(?P<inode>[^|]*)\|(?P<mode>[^|]*)\|"
    r"(?P<uid>[^|]*)\|(?P<gid>[^|]*)\|(?P<size>[^|]*)\|(?P<atime>[^|]*)\|"
    r"(?P<mtime>[^|]*)\|(?P<ctime>[^|]*)\|(?P<crtime>[^|]*)$"
)
_DELETED_SUFFIX_RE = re.compile(r"\s*\(deleted[^)]*\)\s*$")
_UNSAFE_CHARS_RE = re.compile(r'[<>:"|?*\\/\x00-\x1f]')


def _sanitize_component(name: str) -> str:
    name = name.replace("\x00", "").strip()
    if name in ("", ".", ".."):
        return "_"
    name = _UNSAFE_CHARS_RE.sub("_", name)
    return name[:_MAX_COMPONENT_LEN] or "_"


def _sanitize_relpath(original_dir: str, original_name: str) -> Path:
    parts = [_sanitize_component(p) for p in original_dir.split("/") if p not in ("", ".", "..")]
    parts.append(_sanitize_component(original_name))
    return Path(*parts)


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _timestamp(field: str) -> datetime | None:
    try:
        value = int(field)
    except ValueError:
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value)
    except (OSError, OverflowError, ValueError):
        return None


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _run_streaming_to_file(
    args: list[str], out_path: Path, cancel: threading.Event | None, timeout: float
) -> int | None:
    """Runs `args` with stdout redirected straight to a file (never a pipe --
    fls output on a large volume can exceed the OS pipe buffer, which would
    deadlock a wait()-then-read() pattern). Returns the exit code, or None if
    cancelled or timed out."""
    with open(out_path, "wb") as fh:
        proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.DEVNULL)
        start = time.monotonic()
        while True:
            try:
                proc.wait(timeout=0.1)
                return proc.returncode
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    _terminate(proc)
                    return None
                if time.monotonic() - start > timeout:
                    _terminate(proc)
                    return None


def _extract_file(
    args: list[str], target: Path, cancel: threading.Event | None, timeout: float
) -> bool | None:
    """Runs icat writing straight to `target`. True on success, False if icat
    failed for this file (skip it, not a scan failure), None if cancelled."""
    with open(target, "wb") as fh:
        proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.DEVNULL)
        start = time.monotonic()
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    _terminate(proc)
                    return None
                if time.monotonic() - start > timeout:
                    _terminate(proc)
                    return False
    if proc.returncode != 0:
        target.unlink(missing_ok=True)
        return False
    return True


class FilesystemEngine:
    def __init__(self, binaries: dict[str, Path] | None = None) -> None:
        located = binaries if binaries is not None else self.locate_binaries()
        if located is None:
            raise FileNotFoundError(
                "Sleuth Kit tools (fls/icat/fsstat/mmls) not found; install with "
                "`brew install sleuthkit` (macOS) or your distro's sleuthkit package."
            )
        self.binaries = located

    @staticmethod
    def locate_binaries() -> dict[str, Path] | None:
        system = platform.system()
        exe_suffix = ".exe" if system == "Windows" else ""
        plat_dir = "macos" if system == "Darwin" else ("windows" if system == "Windows" else "linux")
        bundled_dir = Path(__file__).resolve().parent.parent / "bin" / plat_dir
        search_dirs = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]

        found: dict[str, Path] = {}
        for name in _TOOL_NAMES:
            which = shutil.which(name)
            path: Path | None = Path(which) if which else None
            if path is None:
                for d in search_dirs:
                    candidate = d / f"{name}{exe_suffix}"
                    if candidate.exists():
                        path = candidate
                        break
            if path is None:
                candidate = bundled_dir / f"{name}{exe_suffix}"
                if candidate.exists():
                    path = candidate
            if path is None:
                return None
            found[name] = path
        return found

    @staticmethod
    def probe(source: str | Path) -> list[VolumeInfo]:
        binaries = FilesystemEngine.locate_binaries()
        if binaries is None:
            return []
        try:
            return _probe_volumes(binaries, str(source))
        except OSError:
            return []

    def scan(
        self,
        source: str | Path,
        workdir: Path,
        mode: ScanMode = ScanMode.QUICK,
        extensions: list[str] | None = None,
        on_progress: Callable[[ScanProgress], None] | None = None,
        cancel: threading.Event | None = None,
        *,
        include_existing: bool = False,
    ) -> ScanResult:
        # `mode` is accepted for interface parity with PhotoRecEngine (the UI's
        # ScanWorker calls both engines the same way); this engine always does
        # the filesystem-record recovery that makes it "Quick" in the first
        # place, regardless of which ScanMode value is passed.
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        needs_privilege = (
            platform.system() in ("Darwin", "Linux")
            and os.geteuid() != 0
            and str(source).startswith("/dev/")
        )
        if needs_privilege:
            return self._scan_privileged(source, workdir, extensions, on_progress, cancel, include_existing)
        return self._scan_impl(str(source), workdir, extensions, on_progress, cancel, include_existing)

    # -- non-privileged core, also invoked (as root) by the privileged worker --

    def _scan_impl(
        self,
        source: str,
        workdir: Path,
        extensions: list[str] | None,
        on_progress: Callable[[ScanProgress], None] | None,
        cancel: threading.Event | None,
        include_existing: bool,
    ) -> ScanResult:
        recovered_root = workdir / _OUTPUT_DIR_NAME
        recovered_root.mkdir(parents=True, exist_ok=True)

        ext_filter = {e.lower().lstrip(".") for e in extensions} if extensions else None

        try:
            volumes = _probe_volumes(self.binaries, source)
        except OSError as exc:
            return ScanResult(success=False, error=f"Could not read {source}: {exc}")

        if not volumes:
            return ScanResult(
                success=False,
                error=(
                    "No recognisable filesystem found on this source -- it may have been "
                    "quick-formatted, badly damaged, or use a filesystem Sleuth Kit doesn't "
                    "support. Try Deep scan instead."
                ),
            )

        files: list[RecoveredFile] = []
        start_time = time.monotonic()
        last_emit = 0.0
        cancelled = False

        def emit(phase: str) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if on_progress is None or now - last_emit < _PROGRESS_INTERVAL:
                return
            on_progress(ScanProgress(files_found=len(files), elapsed_s=now - start_time, phase=phase))
            last_emit = now

        for vol_index, vol in enumerate(volumes, start=1):
            if cancelled:
                break
            phase = (
                "Reading MFT"
                if vol.fstype == "ntfs"
                else f"Scanning {vol.fstype_label} volume {vol_index} of {len(volumes)}"
            )
            emit(phase)

            fls_args = [str(self.binaries["fls"]), "-r", "-p", "-o", str(vol.offset)]
            if vol.fstype:
                fls_args += ["-f", vol.fstype]
            fls_args += ["-m", "/", source]

            fls_out = workdir / f"fls_vol{vol_index}.txt"
            rc = _run_streaming_to_file(fls_args, fls_out, cancel, _TSK_TIMEOUT)
            if rc is None:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                continue  # timed out reading this volume; not fatal to the whole scan
            if rc != 0:
                continue  # damaged/unreadable volume; skip it, keep going

            try:
                lines = fls_out.read_text(errors="replace").splitlines()
            except OSError:
                continue

            for line in lines:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                m = _FLS_LINE_RE.match(line)
                if not m:
                    continue
                if not m.group("mode").startswith("r/r"):
                    continue  # skip directories (d/d) and virtual entries (v/v, V/V)

                raw_name = m.group("name")
                deleted = "(deleted" in raw_name
                if not include_existing and not deleted:
                    continue
                name = _DELETED_SUFFIX_RE.sub("", raw_name).lstrip("/")
                if not name:
                    continue

                try:
                    size = int(m.group("size"))
                except ValueError:
                    size = 0
                if size <= 0:
                    continue

                if "/" in name:
                    original_dir, original_name = name.rsplit("/", 1)
                else:
                    original_dir, original_name = "", name

                ext = Path(original_name).suffix.lstrip(".").lower()
                if ext_filter is not None and ext not in ext_filter:
                    continue

                inode = m.group("inode").strip()
                if not inode:
                    continue

                rel_path = _sanitize_relpath(original_dir, original_name)
                target = _unique_target(recovered_root / rel_path)
                target.parent.mkdir(parents=True, exist_ok=True)

                icat_args = [str(self.binaries["icat"]), "-o", str(vol.offset)]
                if vol.fstype:
                    icat_args += ["-f", vol.fstype]
                icat_args += [source, inode]

                extracted = _extract_file(icat_args, target, cancel, _ICAT_TIMEOUT)
                if extracted is None:
                    cancelled = True
                    break
                if not extracted:
                    continue  # icat failed for this one file; skip it, scan continues

                actual_size = target.stat().st_size if target.exists() else 0
                if actual_size == 0:
                    target.unlink(missing_ok=True)
                    continue

                files.append(
                    RecoveredFile(
                        path=target,
                        name=target.name,
                        ext=ext,
                        size=actual_size,
                        category=category_for(ext),
                        original_name=original_name,
                        original_dir=original_dir,
                        modified=_timestamp(m.group("mtime")),
                        created=_timestamp(m.group("crtime")) or _timestamp(m.group("ctime")),
                        deleted=deleted,
                        inode=inode,
                        source_engine="sleuthkit",
                        integrity=Integrity.INTACT if actual_size == size else Integrity.PARTIAL,
                    )
                )
                emit(phase)

            if cancelled:
                break

        if on_progress is not None:
            on_progress(ScanProgress(files_found=len(files), elapsed_s=time.monotonic() - start_time, phase="Done"))

        return ScanResult(
            success=not cancelled,
            files=files,
            output_dirs=[recovered_root] if files else [],
            cancelled=cancelled,
        )

    # -- privileged path: raw device access needs root on macOS/Linux --

    def _scan_privileged(
        self,
        source: str | Path,
        workdir: Path,
        extensions: list[str] | None,
        on_progress: Callable[[ScanProgress], None] | None,
        cancel: threading.Event | None,
        include_existing: bool,
    ) -> ScanResult:
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--root-worker",
            "--source",
            str(source),
            "--workdir",
            str(workdir),
        ]
        if extensions:
            args += ["--extensions", ",".join(extensions)]
        if include_existing:
            args.append("--include-existing")

        try:
            priv_proc = run_privileged(args, workdir, linux=platform.system() == "Linux")
        except PermissionError as exc:
            return ScanResult(success=False, error=str(exc))

        start = time.monotonic()
        cancelled = False
        while priv_proc.poll() is None:
            if cancel is not None and cancel.is_set():
                priv_proc.cancel()
                cancelled = True
            if on_progress is not None:
                phase = "Cancelling…" if cancelled else "Waiting for administrator authorisation…"
                on_progress(ScanProgress(elapsed_s=time.monotonic() - start, phase=phase))
            time.sleep(0.2)
        priv_proc.wait(timeout=15)

        manifest_path = workdir / "fs_manifest.json"
        if not manifest_path.exists():
            if cancelled:
                return ScanResult(success=False, cancelled=True)
            return ScanResult(success=False, error="Filesystem scan did not complete.")

        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return ScanResult(success=False, error=f"Could not read scan results: {exc}")

        if manifest.get("error"):
            return ScanResult(success=False, error=manifest["error"])

        files = [
            RecoveredFile(
                path=workdir / entry["rel_path"],
                name=entry["name"],
                ext=entry["ext"],
                size=entry["size"],
                category=entry["category"],
                original_name=entry["original_name"],
                original_dir=entry["original_dir"],
                modified=datetime.fromisoformat(entry["modified"]) if entry["modified"] else None,
                created=datetime.fromisoformat(entry["created"]) if entry["created"] else None,
                deleted=entry["deleted"],
                inode=entry["inode"],
                source_engine="sleuthkit",
                integrity=Integrity(entry["integrity"]),
            )
            for entry in manifest.get("files", [])
        ]
        return ScanResult(
            success=manifest.get("success", False),
            files=files,
            output_dirs=[workdir / _OUTPUT_DIR_NAME] if files else [],
            cancelled=manifest.get("cancelled", False),
        )


# ---------------------------------------------------------------------------
# Root-worker entry point: re-invoked via `run_privileged` as `sys.executable
# <this file> --root-worker ...` so the whole scan (mmls/fsstat/fls/icat) runs
# under one elevation instead of prompting per tool. Writes its result as
# JSON to workdir/fs_manifest.json for the non-privileged parent to read back
# once run_privileged's helper script has chowned the workdir to the real user.
# ---------------------------------------------------------------------------


def _root_worker_main(argv: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--extensions", default="")
    parser.add_argument("--include-existing", action="store_true")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    manifest_path = workdir / "fs_manifest.json"
    extensions = [e for e in args.extensions.split(",") if e] or None

    binaries = FilesystemEngine.locate_binaries()
    if binaries is None:
        manifest_path.write_text(json.dumps({"error": "Sleuth Kit tools not found on this system."}))
        return

    engine = FilesystemEngine(binaries)
    result = engine._scan_impl(args.source, workdir, extensions, None, None, args.include_existing)

    manifest = {
        "success": result.success,
        "error": result.error,
        "cancelled": result.cancelled,
        "files": [
            {
                "rel_path": str(f.path.relative_to(workdir)),
                "name": f.name,
                "ext": f.ext,
                "size": f.size,
                "category": f.category,
                "original_name": f.original_name,
                "original_dir": f.original_dir,
                "modified": f.modified.isoformat() if f.modified else None,
                "created": f.created.isoformat() if f.created else None,
                "deleted": f.deleted,
                "inode": f.inode,
                "integrity": f.integrity.value,
            }
            for f in result.files
        ],
    }
    manifest_path.write_text(json.dumps(manifest))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--root-worker":
        _root_worker_main(sys.argv[2:])
