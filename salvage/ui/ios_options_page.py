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

from salvage.engine.ios import BackupPasswordError, BackupReader, IOSDevice
from salvage.ui import ios_facade
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

# Only ever populated in encrypted backups — Apple omits them otherwise.
ENCRYPTED_ONLY_CHECKS: list[tuple[str, str]] = [
    ("call_history", "Call history"),
    ("safari_history", "Safari history"),
]


class IOSOptionsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.device: IOSDevice | None = None
        self.existing_backup_dir: Path | None = None
        self._destination: Path | None = None
        self._encrypted = False
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

        self.password_panel = QFrame()
        self.password_panel.setProperty("role", "panel")
        password_layout = QVBoxLayout(self.password_panel)
        password_title = QLabel("Backup password")
        password_title.setStyleSheet("font-weight: 600;")
        password_layout.addWidget(password_title)
        password_hint = QLabel(
            "This backup is encrypted. Your backup password stays on this Mac and is only "
            "used to decrypt the backup."
        )
        password_hint.setProperty("role", "subheading")
        password_hint.setWordWrap(True)
        password_layout.addWidget(password_hint)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Backup password")
        self.password_edit.textChanged.connect(self._on_password_changed)
        password_layout.addWidget(self.password_edit)
        self.password_error_label = QLabel("")
        self.password_error_label.setProperty("role", "error")
        self.password_error_label.setWordWrap(True)
        self.password_error_label.hide()
        password_layout.addWidget(self.password_error_label)
        self.password_panel.hide()
        outer.addWidget(self.password_panel)

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
        for key, label in ENCRYPTED_ONLY_CHECKS:
            cb = QCheckBox(label)
            cb.setChecked(False)
            cb.setEnabled(False)
            cb.stateChanged.connect(self._update_start_enabled)
            self.category_checks[key] = cb
            types_layout.addWidget(cb)

        encrypted_only_note = QLabel(
            "Call history and Safari history are only included in encrypted backups — enable "
            "'Encrypt local backup' in Finder to recover them."
        )
        encrypted_only_note.setProperty("role", "subheading")
        encrypted_only_note.setWordWrap(True)
        types_layout.addWidget(encrypted_only_note)
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
        self.password_edit.clear()
        self._clear_password_error()

        if device is not None:
            parts = [device.product_type, f"iOS {device.ios_version}"]
            if device.capacity_bytes:
                parts.append(human_size(device.capacity_bytes))
            self.device_label.setText(f"Source: {device.name} · {' · '.join(parts)}")
            self._encrypted = bool(device.encrypted_backups)
        else:
            self.device_label.setText(f"Source: existing backup at {existing_backup_dir}")
            self._encrypted = existing_backup_dir is not None and ios_facade.is_encrypted_backup_folder(
                existing_backup_dir
            )

        self.password_panel.setVisible(self._encrypted)
        for key, _label in ENCRYPTED_ONLY_CHECKS:
            cb = self.category_checks[key]
            cb.setEnabled(self._encrypted)
            cb.setChecked(self._encrypted)

        desktop = Path.home() / "Desktop"
        if desktop.exists():
            self._set_destination(desktop)
        self._update_start_enabled()

    def show_password_error(self, message: str) -> None:
        """Called by the controller when a backup created after this page (a live-device
        backup) turns out to need a different password — keeps the user on this flow
        instead of a dead-end error dialog.
        """
        self.password_error_label.setText(message)
        self.password_error_label.show()

    def _clear_password_error(self) -> None:
        self.password_error_label.hide()
        self.password_error_label.setText("")

    def _on_password_changed(self, _text: str) -> None:
        self._clear_password_error()
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
        dest_ok = self._destination is not None
        types_ok = bool(self._selected_categories())
        password_ok = not self._encrypted or bool(self.password_edit.text())
        self.start_btn.setEnabled(bool(dest_ok and types_ok and password_ok))

    def _start(self) -> None:
        assert self._destination is not None
        password = self.password_edit.text() if self._encrypted else None
        self._clear_password_error()

        # An existing (already-backed-up) encrypted folder can be checked right now,
        # before leaving this page — no need to wait for a fresh device backup.
        if self.existing_backup_dir is not None and self._encrypted:
            try:
                reader = BackupReader(self.existing_backup_dir, password=password)
                reader.close()
            except BackupPasswordError as exc:
                self.show_password_error(str(exc))
                return

        self.controller.go_to_ios_start(
            device=self.device,
            existing_backup_dir=self.existing_backup_dir,
            categories=self._selected_categories(),
            destination=self._destination,
            password=password,
        )
