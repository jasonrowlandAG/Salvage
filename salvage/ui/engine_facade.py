"""Resolves the engine + device/result helpers, with fake-mode fallbacks.

salvage.engine.photorec / devices / results are being written by other
agents in parallel and may not exist yet. Everything here is imported
lazily so the UI stays testable via SALVAGE_FAKE=1 regardless of whether
those modules have landed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from salvage.engine.fake import FakeEngine, fake_devices
from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanProgress, ScanResult


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


class _ModeRoutingEngine:
    """Presents PhotoRecEngine + FilesystemEngine + CombinedEngine as one engine
    object, picking which to run per `.scan()` call based on the requested
    ScanMode -- Quick scan reads the filesystem's own deleted-file records (real
    names, folders, dates); Deep scan carves by signature; Thorough scan runs
    both (CombinedEngine) for the best of each. This lets the rest of the UI
    (which holds a single `controller.engine`, chosen once at startup) route
    each scan without needing to know which mode maps to which class.
    """

    def __init__(self, filesystem_engine, photorec_engine, combined_engine=None) -> None:
        self._filesystem_engine = filesystem_engine
        self._photorec_engine = photorec_engine
        self._combined_engine = combined_engine

    def scan(
        self,
        source,
        workdir: Path,
        mode: ScanMode = ScanMode.DEEP,
        extensions: list[str] | None = None,
        on_progress: Callable[[ScanProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> ScanResult:
        if mode == ScanMode.THOROUGH:
            if self._combined_engine is None:
                return ScanResult(
                    success=False,
                    error=(
                        "Thorough scan needs both The Sleuth Kit and PhotoRec installed. "
                        "Install `brew install sleuthkit testdisk`, or use Quick/Deep scan instead."
                    ),
                )
            return self._combined_engine.scan(
                source, workdir, mode=mode, extensions=extensions, on_progress=on_progress, cancel=cancel
            )
        if mode == ScanMode.QUICK:
            if self._filesystem_engine is None:
                return ScanResult(
                    success=False,
                    error=(
                        "Quick scan needs The Sleuth Kit (fls/icat/fsstat/mmls). "
                        "Install it with `brew install sleuthkit`, or use Deep scan instead."
                    ),
                )
            return self._filesystem_engine.scan(
                source, workdir, mode=mode, extensions=extensions, on_progress=on_progress, cancel=cancel
            )
        if self._photorec_engine is None:
            return ScanResult(success=False, error="PhotoRec is not installed.")
        return self._photorec_engine.scan(
            source, workdir, mode=mode, extensions=extensions, on_progress=on_progress, cancel=cancel
        )


def filesystem_available(fake: bool) -> bool:
    """Whether Quick scan (FilesystemEngine / Sleuth Kit) can run. Used by
    OptionsPage to grey out the Quick radio button when the tools aren't
    installed, rather than letting the user pick it and fail at scan time."""
    if fake:
        return True
    try:
        from salvage.engine.filesystem import FilesystemEngine
    except ImportError:
        return False
    return FilesystemEngine.locate_binaries() is not None


def make_engine(fake: bool):
    """Returns (engine, warning_message | None, needs_binary_dialog: bool)."""
    if fake:
        return FakeEngine(), None, False

    photorec_engine = None
    needs_binary_dialog = False
    try:
        from salvage.engine.photorec import PhotoRecEngine
    except ImportError as exc:
        print(f"WARNING: salvage.engine.photorec not available yet ({exc}); Deep scan disabled.")
    else:
        binary = PhotoRecEngine.locate_binary()
        if binary is None:
            # No PhotoRec binary anywhere; let __main__ show the install-prompt dialog.
            needs_binary_dialog = True
        else:
            try:
                photorec_engine = PhotoRecEngine(binary)
            except Exception as exc:  # defensive: contract says ctor may raise if unusable
                print(f"WARNING: could not construct PhotoRecEngine ({exc}); Deep scan disabled.")

    filesystem_engine = None
    try:
        from salvage.engine.filesystem import FilesystemEngine
    except ImportError as exc:
        print(f"WARNING: salvage.engine.filesystem not available yet ({exc}); Quick scan disabled.")
    else:
        binaries = FilesystemEngine.locate_binaries()
        if binaries is None:
            print("WARNING: sleuthkit binaries not found; Quick scan disabled.")
        else:
            try:
                filesystem_engine = FilesystemEngine(binaries)
            except Exception as exc:
                print(f"WARNING: could not construct FilesystemEngine ({exc}); Quick scan disabled.")

    if photorec_engine is None and filesystem_engine is None:
        return FakeEngine(), None, needs_binary_dialog

    combined_engine = None
    if photorec_engine is not None or filesystem_engine is not None:
        # Thorough scan degrades gracefully with just one sub-engine present --
        # CombinedEngine itself only reports "both stages failed" if neither ran.
        try:
            from salvage.engine.combined import CombinedEngine
        except ImportError as exc:
            print(f"WARNING: salvage.engine.combined not available yet ({exc}); Thorough scan disabled.")
        else:
            combined_engine = CombinedEngine(filesystem_engine, photorec_engine)

    return _ModeRoutingEngine(filesystem_engine, photorec_engine, combined_engine), None, needs_binary_dialog


def list_devices(fake: bool) -> list[Device]:
    if fake:
        return fake_devices()
    try:
        from salvage.engine.devices import list_devices as _list_devices
    except ImportError as exc:
        print(f"WARNING: salvage.engine.devices not available yet ({exc}); using fake devices.")
        return fake_devices()
    return _list_devices()


def filevault_enabled(fake: bool) -> bool | None:
    if fake:
        return None
    try:
        from salvage.engine.devices import filevault_enabled as _filevault_enabled
    except ImportError as exc:
        print(f"WARNING: salvage.engine.devices not available yet ({exc}); assuming unknown.")
        return None
    return _filevault_enabled()


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
