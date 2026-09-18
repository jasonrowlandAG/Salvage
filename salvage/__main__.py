"""Entry point: `python -m salvage`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from salvage.engine.ios import cleanup_stale_ios_caches
from salvage.ui import engine_facade
from salvage.ui.about import app_version
from salvage.ui.app import MainWindow
from salvage.ui.style import STYLESHEET


def main() -> None:
    if "--print-engine" in sys.argv[1:]:
        _print_engine_info()
        return

    cleanup_stale_ios_caches()  # best-effort sweep of any cache left by a prior crash

    fake = os.environ.get("SALVAGE_FAKE") == "1"

    app = QApplication(sys.argv)
    app.setApplicationName("Salvage")
    app.setApplicationDisplayName("Salvage")
    app.setApplicationVersion(app_version())
    app.setOrganizationName("Assembly Growth")
    _set_window_icon(app)
    app.setStyleSheet(STYLESHEET)

    engine, _warning, needs_binary_dialog = engine_facade.make_engine(fake)

    if needs_binary_dialog:
        QMessageBox.information(
            None,
            "PhotoRec not found",
            "Salvage needs the PhotoRec tool from TestDisk to recover files.\n\n"
            "Install it with:\n  brew install testdisk\n\n"
            "or download it from https://www.cgsecurity.org/wiki/TestDisk_Download",
        )

    window = MainWindow(fake, engine)
    window.show()
    sys.exit(app.exec())


def _set_window_icon(app: QApplication) -> None:
    """Best-effort app icon for the running window/taskbar - the .app bundle's own
    Dock/Finder icon is already set via packaging/salvage.spec's BUNDLE(icon=...)
    independently of this, so this only matters for running from source and for
    Windows taskbar/alt-tab, where nothing else supplies one."""
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = [
        Path(meipass) / "assets" / "icon_1024.png" if meipass else None,
        Path(__file__).resolve().parent.parent / "packaging" / "assets" / "icon_1024.png",
    ]
    for path in candidates:
        if path is not None and path.exists():
            app.setWindowIcon(QIcon(str(path)))
            return


def _print_engine_info() -> None:
    """Diagnostic for packaging: report where the engine locates its binaries, with
    no GUI. Used to verify a frozen build finds the bundled photorec/libimobiledevice
    tools rather than falling through to Homebrew."""
    meipass = getattr(sys, "_MEIPASS", None)
    print(f"frozen={getattr(sys, 'frozen', False)} MEIPASS={meipass}")
    try:
        from salvage.engine.photorec import PhotoRecEngine

        print(f"photorec={PhotoRecEngine.locate_binary()}")
    except ImportError as exc:
        print(f"photorec=<unavailable: {exc}>")
    try:
        from salvage.engine.filesystem import FilesystemEngine

        print(f"sleuthkit={FilesystemEngine.locate_binaries()}")
    except ImportError as exc:
        print(f"sleuthkit=<unavailable: {exc}>")
    try:
        from salvage.engine.ios import _tool

        print(f"idevice_id={_tool('idevice_id')}")
    except ImportError as exc:
        print(f"idevice_id=<unavailable: {exc}>")


if __name__ == "__main__":
    main()
