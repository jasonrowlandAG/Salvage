"""Entry point: `python -m salvage`."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from salvage.engine.ios import cleanup_stale_ios_caches
from salvage.ui import engine_facade
from salvage.ui.app import MainWindow
from salvage.ui.style import STYLESHEET


def main() -> None:
    if "--print-engine" in sys.argv[1:]:
        _print_engine_info()
        return

    cleanup_stale_ios_caches()  # best-effort sweep of any cache left by a prior crash

    fake = os.environ.get("SALVAGE_FAKE") == "1"

    app = QApplication(sys.argv)
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
        from salvage.engine.ios import _tool

        print(f"idevice_id={_tool('idevice_id')}")
    except ImportError as exc:
        print(f"idevice_id=<unavailable: {exc}>")


if __name__ == "__main__":
    main()
