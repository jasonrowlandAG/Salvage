"""Resolves the engine + device/result helpers, with fake-mode fallbacks.

salvage.engine.photorec / devices / results are being written by other
agents in parallel and may not exist yet. Everything here is imported
lazily so the UI stays testable via SALVAGE_FAKE=1 regardless of whether
those modules have landed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from salvage.engine.fake import FakeEngine, fake_devices
from salvage.engine.models import Device, RecoveredFile


def _fallback_device_from_image(path: Path) -> Device:
    size = path.stat().st_size if path.exists() else 0
    return Device(
        id=str(path),
        path=str(path),
        name=path.name,
        size_bytes=size,
        kind="image",
    )


def _fallback_is_path_on_device(path: Path, device: Device) -> bool:
    """Path-prefix check used only when the real devices.py isn't available.

    Good enough for FakeEngine devices, whose mount_point is a plain directory
    rather than a real filesystem mount point (so st_dev comparisons aren't
    meaningful for them).
    """
    if not device.mount_point:
        return False
    try:
        resolved = path.resolve()
        mount = Path(device.mount_point).resolve()
    except OSError:
        return False
    return resolved == mount or mount in resolved.parents


def _fallback_recover_files(
    files: Iterable[RecoveredFile],
    destination: Path,
    organise_by_category: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    import shutil

    files = list(files)
    written: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(files, start=1):
        target_dir = destination / f.category if organise_by_category else destination
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f.name
        shutil.copy2(f.path, target)
        written.append(target)
        if on_progress is not None:
            on_progress(i, len(files))
    return written


def make_engine(fake: bool):
    """Returns (engine, warning_message | None, needs_binary_dialog: bool)."""
    if fake:
        return FakeEngine(), None, False

    try:
        from salvage.engine.photorec import PhotoRecEngine
    except ImportError as exc:
        print(f"WARNING: salvage.engine.photorec not available yet ({exc}); using FakeEngine.")
        return FakeEngine(), None, False

    binary = PhotoRecEngine.locate_binary()
    try:
        engine = PhotoRecEngine(binary)
    except Exception as exc:  # defensive: contract says ctor may raise if unusable
        print(f"WARNING: could not construct PhotoRecEngine ({exc}); using FakeEngine.")
        return FakeEngine(), None, False

    return engine, None, binary is None


def list_devices(fake: bool) -> list[Device]:
    if fake:
        return fake_devices()
    try:
        from salvage.engine.devices import list_devices as _list_devices
    except ImportError as exc:
        print(f"WARNING: salvage.engine.devices not available yet ({exc}); using fake devices.")
        return fake_devices()
    return _list_devices()


def device_from_image(path: Path, fake: bool) -> Device:
    if fake:
        return _fallback_device_from_image(path)
    try:
        from salvage.engine.devices import device_from_image as _device_from_image
    except ImportError as exc:
        print(f"WARNING: salvage.engine.devices not available yet ({exc}); using fallback.")
        return _fallback_device_from_image(path)
    return _device_from_image(path)


def is_path_on_device(path: Path, device: Device, fake: bool) -> bool:
    if not fake:
        try:
            from salvage.engine.devices import is_path_on_device as _is_path_on_device
            return _is_path_on_device(path, device)
        except ImportError:
            pass
    return _fallback_is_path_on_device(path, device)


def recover_files(
    files: Iterable[RecoveredFile],
    destination: Path,
    organise_by_category: bool,
    on_progress: Callable[[int, int], None] | None,
) -> list[Path]:
    # Copying files is identical whether the source device was real or fake,
    # so always prefer the real implementation; it's just as safe to run
    # against FakeEngine's sample files as against real recovered ones.
    try:
        from salvage.engine.results import recover_files as _recover_files
    except ImportError as exc:
        print(f"WARNING: salvage.engine.results not available yet ({exc}); using fallback copy.")
        return _fallback_recover_files(files, destination, organise_by_category, on_progress)
    return _recover_files(files, destination, organise_by_category, on_progress)
