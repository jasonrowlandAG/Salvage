"""Build test disk images on macOS with hdiutil and populate them from the corpus.

PhotoRec and Sleuth Tool Kit engines both read a raw sequence of sectors, so every
image this module produces ends up as a plain raw image file (extension ``.img``)
containing exactly what a real block device would hold - MBR/GPT + filesystem +
data, nothing hdiutil-specific. ``.img`` was chosen (over the more traditional
`.raw`/`.dd`) because hdiutil's own attach command sniffs image type from the file
extension and refuses anything it doesn't recognise (`.raw` -> "image not
recognized") - `.img` is one of the extensions it accepts, which lets us re-attach
the very same file later for scenario mutation without an intermediate copy.

The main hazard here is a leaked attach: if a scenario crashes between attach and
detach, the disk image stays mounted and macOS won't let anyone delete or resize
the file underneath it. Every attach in this module goes through the `attached`
context manager, which detaches in a `finally` block, and callers can double check
with `list_leaked_attaches` / `detach_all_created` at the end of a run.
"""
from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator

from bench.corpus import CorpusFile

# hdiutil -fs names, see `hdiutil create -help`.
FILESYSTEMS: dict[str, str] = {
    "fat32": "MS-DOS FAT32",
    "exfat": "ExFAT",
    "hfs+": "Journaled HFS+",
    "apfs": "APFS",
}

# `diskutil eraseVolume <format> ...` names for the quick_format scenario - these
# differ from the hdiutil create -fs names above.
_ERASE_FORMAT: dict[str, str] = {
    "fat32": "FAT32",
    "exfat": "ExFAT",
    "hfs+": "JHFS+",
    "apfs": "APFS",
}

# FAT32 needs enough clusters that newfs_msdos will actually lay out a FAT32 (not
# FAT16) table; below this, `hdiutil create -fs "MS-DOS FAT32"` fails outright with
# "FAT32 is impossible for disk size of ...".
MIN_FAT32_MB = 33

NTFS_NOT_TESTABLE = (
    "NTFS was not exercised on this host: `hdiutil create -fs` has no NTFS option, and neither "
    "mkntfs nor mkfs.ntfs is installed (Homebrew's ntfs-3g formula is not installed, and even "
    "installed it ships no formatting tool on macOS - it's a FUSE driver, not a mkfs). Testing "
    "NTFS would need a Linux host with ntfs-3g/ntfsprogs, or a Windows host."
)


class ImageError(RuntimeError):
    pass


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _run_ok(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    proc = _run(cmd)
    if proc.returncode != 0:
        raise ImageError(f"{what} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


# ---------------------------------------------------------------------------
# attach / detach
# ---------------------------------------------------------------------------


def attached_images() -> list[dict]:
    """Raw `hdiutil info -plist` view of every image attached system-wide right now."""
    proc = _run(["hdiutil", "info", "-plist"])
    if proc.returncode != 0 or not proc.stdout:
        return []
    return plistlib.loads(proc.stdout.encode()).get("images", [])


def _mounted_entity(plist_bytes: bytes) -> tuple[str, Path]:
    data = plistlib.loads(plist_bytes)
    for ent in data.get("system-entities", []):
        if ent.get("mount-point"):
            return ent["dev-entry"], Path(ent["mount-point"])
    raise ImageError(f"hdiutil attach produced no mountable volume: {data}")


def attach(image_path: Path, mount_point: Path | None = None) -> tuple[str, Path]:
    """Attach `image_path`, returning (device-node, mount-point). `image_path` must
    have an extension hdiutil recognises (.img/.cdr/.dmg/.sparseimage all work)."""
    args = ["hdiutil", "attach", str(image_path), "-plist", "-nobrowse"]
    if mount_point is not None:
        mount_point.mkdir(parents=True, exist_ok=True)
        args += ["-mountpoint", str(mount_point)]
    proc = _run(args)
    if proc.returncode != 0:
        raise ImageError(f"hdiutil attach failed for {image_path}: {proc.stderr.strip()}")
    return _mounted_entity(proc.stdout.encode())


def detach(device_or_mountpoint: str | Path, force: bool = True) -> None:
    args = ["hdiutil", "detach", str(device_or_mountpoint)]
    if force:
        args.append("-force")
    # Best-effort: a failed detach here almost always means "already detached",
    # which is exactly the state we want, so don't raise.
    _run(args)


@contextlib.contextmanager
def attached(image_path: Path, mount_point: Path | None = None) -> Iterator[tuple[str, Path]]:
    """Attach `image_path` for the duration of the `with` block and always detach,
    even if the block raises."""
    device, mp = attach(image_path, mount_point)
    try:
        yield device, mp
    finally:
        detach(device)


def detach_leaked(tracked_image_paths: set[str]) -> list[str]:
    """Detach any image in `tracked_image_paths` hdiutil still thinks is attached.
    Call this after each scenario/run as a safety net against a crash that skipped
    a `finally`. Returns the device nodes it had to detach."""
    resolved = {str(Path(p).resolve()) for p in tracked_image_paths}
    detached = []
    for img in attached_images():
        image_path = img.get("image-path", "")
        try:
            image_path = str(Path(image_path).resolve())
        except OSError:
            pass
        if image_path in resolved:
            for ent in img.get("system-entities", []):
                dev = ent.get("dev-entry")
                if dev:
                    detach(dev)
                    detached.append(dev)
    return detached


# ---------------------------------------------------------------------------
# building images
# ---------------------------------------------------------------------------


def _populate(mount_point: Path, files: list[CorpusFile]) -> None:
    for f in files:
        dest = mount_point / f.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.content)
        ts = f.mtime.timestamp()
        os.utime(dest, (ts, ts), follow_symlinks=True)


def make_image(
    fs: str,
    size_mb: int,
    out: Path,
    files: list[CorpusFile],
    volname: str = "BENCH",
) -> Path:
    """Create a `fs` volume of `size_mb`, populate it from `files` (preserving
    directory structure and mtimes), detach, and convert to a raw image at `out`
    (which must end in `.img`). Returns `out`."""
    if fs not in FILESYSTEMS:
        raise ValueError(f"unsupported filesystem {fs!r}; choose from {sorted(FILESYSTEMS)}")
    if fs == "fat32" and size_mb < MIN_FAT32_MB:
        raise ValueError(f"FAT32 needs at least {MIN_FAT32_MB} MB, got {size_mb} MB")

    out = Path(out)
    if out.suffix != ".img":
        raise ValueError(f"out must end in .img (hdiutil needs a recognised extension), got {out}")
    out.parent.mkdir(parents=True, exist_ok=True)

    work_base = out.parent / f".{out.stem}_work"
    sparse_path = work_base.with_suffix(".sparseimage")
    mount_point = out.parent / f".{out.stem}_mnt"
    convert_base = out.parent / f".{out.stem}_conv"
    cdr_path = convert_base.with_suffix(".cdr")

    device = None
    try:
        for p in (sparse_path, cdr_path):
            if p.exists():
                p.unlink()
        _run_ok(
            [
                "hdiutil",
                "create",
                "-size",
                f"{size_mb}m",
                "-fs",
                FILESYSTEMS[fs],
                "-volname",
                volname,
                "-type",
                "SPARSE",
                str(work_base),
            ],
            f"hdiutil create ({fs}, {size_mb}MB)",
        )

        device, mp = attach(sparse_path, mount_point)
        try:
            _populate(mp, files)
        finally:
            detach(device)
            device = None

        _run_ok(
            ["hdiutil", "convert", str(sparse_path), "-format", "UDTO", "-o", str(convert_base)],
            f"hdiutil convert ({fs})",
        )
        if out.exists():
            out.unlink()
        cdr_path.rename(out)
        return out
    finally:
        if device is not None:
            detach(device)
        if sparse_path.exists():
            sparse_path.unlink()
        if cdr_path.exists():
            cdr_path.unlink()
        if mount_point.exists():
            shutil.rmtree(mount_point, ignore_errors=True)


def reformat_volume(device: str, fs: str, volname: str = "BENCH") -> tuple[str, Path]:
    """Reformat the volume at `device` with the same filesystem (used by the
    quick_format scenario). `diskutil eraseVolume` remounts afterwards at a
    default location, not necessarily the caller's original mount point, so this
    returns the (possibly new) device and mount point."""
    if fs not in _ERASE_FORMAT:
        raise ValueError(f"unsupported filesystem {fs!r}")
    _run_ok(["diskutil", "eraseVolume", _ERASE_FORMAT[fs], volname, device], f"diskutil eraseVolume ({fs})")
    # eraseVolume can take a beat to settle before `diskutil info` reflects it.
    for attempt in range(20):
        proc = _run(["diskutil", "info", "-plist", device])
        if proc.returncode == 0:
            info = plistlib.loads(proc.stdout.encode())
            mp = info.get("MountPoint")
            if mp:
                return device, Path(mp)
        time.sleep(0.25 * (attempt + 1))
    raise ImageError(f"volume at {device} did not remount after reformat")


def overwrite_region(image_path: Path, offset: int, length: int, seed: int) -> None:
    """Write `length` deterministic-random bytes into the raw image file at
    `offset`, bypassing any filesystem entirely (used by partial_overwrite). The
    image must be detached first - this is plain file I/O on our own filesystem."""
    import random

    rnd = random.Random(seed)
    with open(image_path, "r+b") as fh:
        fh.seek(offset)
        remaining = length
        chunk = 4 * 1024 * 1024
        while remaining > 0:
            n = min(chunk, remaining)
            fh.write(rnd.randbytes(n))
            remaining -= n
