"""Entry point: `python -m salvage`."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from salvage.ui import engine_facade
from salvage.ui.app import MainWindow
from salvage.ui.style import STYLESHEET


def main() -> None:
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


if __name__ == "__main__":
    main()
