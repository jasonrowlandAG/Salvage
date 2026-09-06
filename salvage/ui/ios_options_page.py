"""iPhone wizard step 2: what to recover and where to save it."""

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
    QVBoxLayout,
    QWidget,
)

from salvage.engine.ios import IOSDevice
from salvage.ui.format_utils import human_size

CATEGORY_CHECKS: list[tuple[str, str]] = [
    ("messages", "Messages (iMessage/SMS)"),
    ("whatsapp", "WhatsApp"),
    ("contacts", "Contacts"),
    ("notes", "Notes"),
    (
        "photos",
        "Recently Deleted photos (thumbnails only — full-size originals are in "
        "iCloud Photos → Recently Deleted)",
    ),
]


class IOSOptionsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.device: IOSDevice | None = None
        self.existing_backup_dir: Path | None = None
        self._destination: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("iPhone options")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.device_label = QLabel("")
        self.device_label.setProperty("role", "subheading")
        outer.addWidget(self.device_label)

        self.encrypted_warning = QLabel(
            "This backup is encrypted. Salvage can't read encrypted iPhone backups yet — "
            "turn off backup encryption on the device (Settings > General > Transfer or "
            "Reset iPhone > Encrypted Backup) and back up again."
        )
        self.encrypted_warning.setProperty("role", "error")
        self.encrypted_warning.setWordWrap(True)
        self.encrypted_warning.hide()
        outer.addWidget(self.encrypted_warning)

        types_panel = QFrame()
        types_panel.setProperty("role", "panel")
        types_layout = QVBoxLayout(types_panel)
        types_title = QLabel("What to recover")
        types_title.setStyleSheet("font-weight: 600;")
        types_layout.addWidget(types_title)

        self.category_checks: dict[str, QCheckBox] = {}
        for key, label in CATEGORY_CHECKS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_start_enabled)
            self.category_checks[key] = cb
            types_layout.addWidget(cb)

        call_history_note = QLabel(
            "Call history is only included in encrypted backups — enable 'Encrypt local "
            "backup' in Finder to recover it."
        )
        call_history_note.setProperty("role", "subheading")
        call_history_note.setWordWrap(True)
        types_layout.addWidget(call_history_note)
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
        self.dest_edit.setPlaceholderText("Choose a folder to save recovered data to…")
        dest_row.addWidget(self.dest_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_destination)
        dest_row.addWidget(browse_btn)
        dest_layout.addLayout(dest_row)
        outer.addWidget(dest_panel)

        outer.addStretch()

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.controller.go_to_source)
        bottom_row.addWidget(back_btn)
        bottom_row.addStretch()
        self.start_btn = QPushButton("Start")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        bottom_row.addWidget(self.start_btn)
        outer.addLayout(bottom_row)

    def set_source(self, device: IOSDevice | None, existing_backup_dir: Path | None) -> None:
        self.device = device
        self.existing_backup_dir = existing_backup_dir
        self._destination = None
        self.dest_edit.clear()

        if device is not None:
            parts = [device.product_type, f"iOS {device.ios_version}"]
            if device.capacity_bytes:
                parts.append(human_size(device.capacity_bytes))
            self.device_label.setText(f"Source: {device.name} · {' · '.join(parts)}")
        else:
            self.device_label.setText(f"Source: existing backup at {existing_backup_dir}")

        encrypted = bool(device is not None and device.encrypted_backups)
        self.encrypted_warning.setVisible(encrypted)

        desktop = Path.home() / "Desktop"
        if desktop.exists():
            self._set_destination(desktop)
        self._update_start_enabled()

    def _browse_destination(self) -> None:
        start_dir = self.dest_edit.text() or str(Path.home())
        path_str = QFileDialog.getExistingDirectory(self, "Choose recovery destination", start_dir)
        if not path_str:
            return
        self._set_destination(Path(path_str))

    def _set_destination(self, path: Path) -> None:
        self.dest_edit.setText(str(path))
        self._destination = path
        self._update_start_enabled()

    def _selected_categories(self) -> set[str]:
        return {key for key, cb in self.category_checks.items() if cb.isChecked()}

    def _update_start_enabled(self, *_args) -> None:
        encrypted = bool(self.device is not None and self.device.encrypted_backups)
        dest_ok = self._destination is not None
        types_ok = bool(self._selected_categories())
        self.start_btn.setEnabled(bool(dest_ok and types_ok and not encrypted))

    def _start(self) -> None:
        assert self._destination is not None
        self.controller.go_to_ios_start(
            device=self.device,
            existing_backup_dir=self.existing_backup_dir,
            categories=self._selected_categories(),
            destination=self._destination,
        )
