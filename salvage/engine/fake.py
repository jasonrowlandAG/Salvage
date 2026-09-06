"""In-process stand-in for PhotoRecEngine, used when SALVAGE_FAKE=1.

Lets the UI be built and tested end-to-end without root privileges or a
real PhotoRec binary. Matches PhotoRecEngine.scan's signature exactly.
"""

from __future__ import annotations

import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from salvage.engine.ios import BackupProgress, IOSBackupError, IOSDevice
from salvage.engine.ios_fixtures import build_synthetic_backup
from salvage.engine.models import (
    Device,
    RecoveredFile,
    ScanMode,
    ScanProgress,
    ScanResult,
    category_for,
)

_COLOURS = [
    (214, 69, 65),
    (66, 133, 199),
    (72, 168, 106),
    (231, 180, 58),
    (149, 97, 194),
    (60, 60, 60),
]

# (extension, generator name) for the ~30 sample files written per scan.
_FILE_PLAN = (
    ["jpg"] * 10
    + ["png"] * 6
    + ["pdf"] * 4
    + ["zip"] * 3
    + ["mp4"] * 3
    + ["txt"] * 4
)


def _write_jpeg(path: Path, colour: tuple[int, int, int]) -> None:
    Image.new("RGB", (320, 240), colour).save(path, "JPEG")


def _write_png(path: Path, colour: tuple[int, int, int]) -> None:
    Image.new("RGB", (320, 240), colour).save(path, "PNG")


def _write_pdf(path: Path) -> None:
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("notes.txt", "recovered archive contents\n")
        zf.writestr("data.csv", "a,b,c\n1,2,3\n")


def _write_mp4(path: Path) -> None:
    # Not a playable video, just plausible-looking header bytes.
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 512)


def _write_txt(path: Path, index: int) -> None:
    path.write_text(f"Recovered text fragment #{index}.\nLorem ipsum dolor sit amet.\n")


def _write_sample(path: Path, ext: str, index: int) -> None:
    colour = _COLOURS[index % len(_COLOURS)]
    if ext == "jpg":
        _write_jpeg(path, colour)
    elif ext == "png":
        _write_png(path, colour)
    elif ext == "pdf":
        _write_pdf(path)
    elif ext == "zip":
        _write_zip(path)
    elif ext == "mp4":
        _write_mp4(path)
    elif ext == "txt":
        _write_txt(path, index)


class FakeEngine:
    """Simulates a ~6 second PhotoRec scan without touching real hardware."""

    def __init__(self) -> None:
        self.binary: Path | None = None

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
        out_dir = workdir / "recovered.1"
        out_dir.mkdir(parents=True, exist_ok=True)

        wanted = {e.lower().lstrip(".") for e in extensions} if extensions else None
        plan = [ext for ext in _FILE_PLAN if wanted is None or ext in wanted]

        total_sectors = 120_000_000
        duration = 6.0
        ticks = 40
        tick_len = duration / ticks
        start = time.monotonic()

        files: list[RecoveredFile] = []
        cancelled = False

        for step in range(ticks + 1):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break

            # First tick reports an unknown total, like real PhotoRec still
            # reading the partition table.
            reported_total = 0 if step == 0 else total_sectors
            sector = int(total_sectors * step / ticks)
            phase = "Finalizing" if step == ticks else "Pass 1 - Reading sector"

            # Create the files scheduled up to this point in the plan.
            target_count = int(len(plan) * step / ticks) if plan else 0
            while len(files) < target_count:
                i = len(files)
                ext = plan[i]
                offset = 1234 + i * 160_000
                name = f"f{offset:07d}.{ext}"
                path = out_dir / name
                _write_sample(path, ext, i)
                files.append(
                    RecoveredFile(
                        path=path,
                        name=name,
                        ext=ext,
                        size=path.stat().st_size,
                        category=category_for(ext),
                        offset=offset,
                    )
                )

            if on_progress is not None:
                on_progress(
                    ScanProgress(
                        sector=sector,
                        total_sectors=reported_total,
                        files_found=len(files),
                        elapsed_s=time.monotonic() - start,
                        phase=phase,
                    )
                )

            if step < ticks:
                time.sleep(tick_len)

        # Make sure every planned file exists even if cancellation landed
        # between the loop's rounding steps (only when not cancelled).
        if not cancelled:
            while len(files) < len(plan):
                i = len(files)
                ext = plan[i]
                offset = 1234 + i * 160_000
                name = f"f{offset:07d}.{ext}"
                path = out_dir / name
                _write_sample(path, ext, i)
                files.append(
                    RecoveredFile(
                        path=path,
                        name=name,
                        ext=ext,
                        size=path.stat().st_size,
                        category=category_for(ext),
                        offset=offset,
                    )
                )

        return ScanResult(
            success=True,
            files=files,
            output_dirs=[out_dir],
            log_text=f"Fake scan complete. {len(files)} files recovered.\n",
            cancelled=cancelled,
        )


def fake_devices() -> list[Device]:
    """A fixed pair of devices (one internal, one removable) for UI testing.

    The removable partition's mount point is a real directory on disk so
    is_path_on_device fallback logic has something concrete to compare
    against.
    """
    mount_point = Path(tempfile.gettempdir()) / "salvage_fake_removable"
    mount_point.mkdir(parents=True, exist_ok=True)

    return [
        Device(
            id="disk0",
            path="/dev/rdisk0",
            name="Macintosh HD",
            size_bytes=512_000_000_000,
            kind="disk",
            is_removable=False,
            is_system=True,
        ),
        Device(
            id="disk0s2",
            path="/dev/rdisk0s2",
            name="Macintosh HD",
            size_bytes=494_000_000_000,
            kind="partition",
            is_removable=False,
            is_system=True,
            filesystem="APFS",
            mount_point="/",
            parent_id="disk0",
        ),
        Device(
            id="disk4",
            path="/dev/rdisk4",
            name="SanDisk Ultra",
            size_bytes=64_000_000_000,
            kind="disk",
            is_removable=True,
            is_system=False,
        ),
        Device(
            id="disk4s1",
            path="/dev/rdisk4s1",
            name="SanDisk Ultra — PHOTOS",
            size_bytes=63_500_000_000,
            kind="partition",
            is_removable=True,
            is_system=False,
            filesystem="ExFAT",
            mount_point=str(mount_point),
            parent_id="disk4",
        ),
    ]


FAKE_IOS_UDID = "00008101-FAKE0001DEV1234"


def fake_ios_devices() -> list[IOSDevice]:
    return [
        IOSDevice(
            udid=FAKE_IOS_UDID,
            name="Jay's iPhone (Fake)",
            product_type="iPhone13,2",
            ios_version="17.4",
            capacity_bytes=128_000_000_000,
            encrypted_backups=False,
        )
    ]


class FakeIOSBackup:
    """Simulates idevicebackup2 by fabricating a tiny synthetic backup on disk.

    Matches ios.create_backup's signature and return contract (Path to
    backup_root/udid) so the UI layer can drive fake and real backups the
    same way, and the resulting directory is a genuine BackupReader target —
    the same extraction and parsing code runs against it as against a real
    device backup.
    """

    def run(
        self,
        udid: str,
        backup_root: Path,
        on_progress: Callable[[BackupProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        backup_root = Path(backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        ticks = 8
        for step in range(1, ticks + 1):
            if cancel is not None and cancel.is_set():
                raise IOSBackupError("Backup cancelled.")
            time.sleep(0.12)
            if on_progress is not None:
                on_progress(
                    BackupProgress(
                        percent=(step / ticks) * 100,
                        current_file="Receiving files",
                        bytes_done=step * 4_000_000,
                        elapsed_s=time.monotonic() - start,
                    )
                )
        return build_synthetic_backup(backup_root, udid)
