"""Wizard step 2: scan mode, file types, and recovery destination."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.models import CATEGORY_BY_EXT, Device, ScanMode
from salvage.ui import engine_facade

CATEGORY_LABELS = [
    ("image", "Photos"),
    ("document", "Documents"),
    ("video", "Videos"),
    ("audio", "Audio"),
    ("archive", "Archives"),
]


def _extensions_for_category(category: str) -> list[str]:
    return [ext for ext, cat in CATEGORY_BY_EXT.items() if cat == category]


class OptionsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.device: Device | None = None
        self._destination: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("Scan options")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.device_label = QLabel("")
        self.device_label.setProperty("role", "subheading")
        outer.addWidget(self.device_label)

        mode_panel = QFrame()
        mode_panel.setProperty("role", "panel")
        mode_layout = QVBoxLayout(mode_panel)
        mode_title = QLabel("Scan mode")
        mode_title.setStyleSheet("font-weight: 600;")
        mode_layout.addWidget(mode_title)
        self.quick_radio = QRadioButton(
            "Quick — deleted files with original names and folders.\n"
            "Needs an intact filesystem."
        )
        self.deep_radio = QRadioButton(
            "Deep — scans every sector for file signatures.\n"
            "Finds more after a format, but names are lost."
        )
        self.thorough_radio = QRadioButton(
            "Thorough — filesystem records first, then every sector.\n"
            "Best results, slowest."
        )
        self.thorough_radio.setChecked(True)
        mode_layout.addWidget(self.quick_radio)
        mode_layout.addWidget(self.deep_radio)
        mode_layout.addWidget(self.thorough_radio)
        self.quick_unavailable_label = QLabel(
            "Quick scan needs The Sleuth Kit — install with `brew install sleuthkit`."
        )
        self.quick_unavailable_label.setProperty("role", "error")
        self.quick_unavailable_label.setWordWrap(True)
        self.quick_unavailable_label.hide()
        mode_layout.addWidget(self.quick_unavailable_label)
        if not engine_facade.filesystem_available(getattr(self.controller, "fake", False)):
            # Quick itself is unavailable without Sleuth Kit, but Thorough (the
            # default) degrades gracefully to carving-only via CombinedEngine,
            # so it's left checked rather than forced over to Deep.
            self.quick_radio.setEnabled(False)
            self.quick_unavailable_label.show()
        outer.addWidget(mode_panel)

        types_panel = QFrame()
        types_panel.setProperty("role", "panel")
        types_layout = QVBoxLayout(types_panel)
        types_title = QLabel("File types to recover")
        types_title.setStyleSheet("font-weight: 600;")
        types_layout.addWidget(types_title)

        self.everything_check = QCheckBox("Everything")
        self.everything_check.setChecked(True)
        self.everything_check.stateChanged.connect(self._on_everything_toggled)
        types_layout.addWidget(self.everything_check)

        self.category_checks: dict[str, QCheckBox] = {}
        for row_index, row_labels in enumerate((CATEGORY_LABELS[:3], CATEGORY_LABELS[3:])):
            row = QHBoxLayout()
            for cat, label in row_labels:
                cb = QCheckBox(label)
                cb.stateChanged.connect(self._on_category_toggled)
                self.category_checks[cat] = cb
                row.addWidget(cb)
            if row_index == 0:
                row.addStretch()
            types_layout.addLayout(row)
        outer.addWidget(types_panel)

        dest_panel = QFrame()
        dest_panel.setProperty("role", "panel")
        dest_layout = QVBoxLayout(dest_panel)
        dest_title = QLabel("Recovery destination")
        dest_title.setStyleSheet("font-weight: 600;")
        dest_layout.addWidget(dest_title)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Choose a folder to save recovered files to…")
        dest_row.addWidget(self.dest_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_destination)
        dest_row.addWidget(browse_btn)
        dest_layout.addLayout(dest_row)
        self.dest_error = QLabel("")
        self.dest_error.setProperty("role", "error")
        self.dest_error.setWordWrap(True)
        self.dest_error.hide()
        dest_layout.addWidget(self.dest_error)
        outer.addWidget(dest_panel)

        outer.addStretch()

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.controller.go_to_source)
        bottom_row.addWidget(back_btn)
        bottom_row.addStretch()
        self.start_btn = QPushButton("Start scan")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_scan)
        bottom_row.addWidget(self.start_btn)
        outer.addLayout(bottom_row)

    def set_device(self, device: Device) -> None:
        self.device = device
        self.device_label.setText(f"Source: {device.name}")
        self._destination = None
        self.dest_edit.clear()
        self.dest_error.hide()

        desktop = Path.home() / "Desktop"
        if desktop.exists() and not engine_facade.is_path_on_device(desktop, device, self.controller.fake):
            self._set_destination(desktop)
        self._update_start_enabled()

    def _on_everything_toggled(self) -> None:
        if self.everything_check.isChecked():
            self._set_categories_checked(False)
        self._update_start_enabled()

    def _on_category_toggled(self) -> None:
        # Picking a specific type means "not everything"; picking nothing falls back
        # to Everything so the selection is never empty.
        any_checked = any(cb.isChecked() for cb in self.category_checks.values())
        self.everything_check.blockSignals(True)
        self.everything_check.setChecked(not any_checked)
        self.everything_check.blockSignals(False)
        self._update_start_enabled()

    def _set_categories_checked(self, checked: bool) -> None:
        for cb in self.category_checks.values():
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

    def _browse_destination(self) -> None:
        start_dir = self.dest_edit.text() or str(Path.home())
        path_str = QFileDialog.getExistingDirectory(self, "Choose recovery destination", start_dir)
        if not path_str:
            return
        self._set_destination(Path(path_str))

    def _set_destination(self, path: Path) -> None:
        self.dest_edit.setText(str(path))
        self._destination = path
        on_source = self.device is not None and engine_facade.is_path_on_device(
            path, self.device, self.controller.fake
        )
        if on_source:
            self.dest_error.setText(
                "Choose a folder on a different drive — writing to the drive you're recovering "
                "from can overwrite the files you're trying to get back."
            )
            self.dest_error.show()
        else:
            self.dest_error.hide()
        self._update_start_enabled()

    def _selected_extensions(self) -> list[str] | None:
        if self.everything_check.isChecked():
            return None
        exts: list[str] = []
        for cat, cb in self.category_checks.items():
            if cb.isChecked():
                exts.extend(_extensions_for_category(cat))
        return exts

    def _update_start_enabled(self, *_args) -> None:
        dest_ok = self._destination is not None and not self.dest_error.isVisible()
        types_ok = self.everything_check.isChecked() or any(
            cb.isChecked() for cb in self.category_checks.values()
        )
        self.start_btn.setEnabled(bool(dest_ok and types_ok))

    def _start_scan(self) -> None:
        if self.quick_radio.isChecked():
            mode = ScanMode.QUICK
        elif self.thorough_radio.isChecked():
            mode = ScanMode.THOROUGH
        else:
            mode = ScanMode.DEEP
        self.controller.go_to_scan(mode, self._selected_extensions(), self._destination)
