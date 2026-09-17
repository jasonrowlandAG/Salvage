"""Wizard step 5: recovery complete."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class DonePage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self._recover_dir: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("Recovery complete")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "subheading")
        self.summary_label.setWordWrap(True)
        outer.addWidget(self.summary_label)

        self.cleanup_check = QCheckBox("Delete scan working files to free up space")
        self.cleanup_check.setChecked(True)
        outer.addWidget(self.cleanup_check)

        outer.addStretch()

        button_row = QHBoxLayout()
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self._open_folder)
        button_row.addWidget(open_btn)
        button_row.addStretch()
        new_scan_btn = QPushButton("Start new scan")
        new_scan_btn.setProperty("role", "primary")
        new_scan_btn.clicked.connect(self._start_new_scan)
        button_row.addWidget(new_scan_btn)
        outer.addLayout(button_row)

    def set_result(self, recovered_paths: list[Path], recover_dir: Path, note: str | None = None) -> None:
        self._recover_dir = recover_dir
        text = f"{len(recovered_paths)} files recovered to {recover_dir}"
        if note:
            text += f"\n{note}"
        self.summary_label.setText(text)
        self.cleanup_check.setChecked(True)

    def _open_folder(self) -> None:
        if self._recover_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._recover_dir)))

    def _start_new_scan(self) -> None:
        if self.cleanup_check.isChecked():
            workdir = self.controller.session.workdir
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
        self.controller.reset_and_go_to_source()
