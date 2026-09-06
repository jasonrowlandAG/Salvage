"""This-Mac media flow, step 1: choose search locations, options, and destination."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.local_media import MediaSource, source_from_backup_dir
from salvage.ui import media_facade

_KIND_ICON = {
    "folder": QStyle.StandardPixmap.SP_DirIcon,
    "photos_library": QStyle.StandardPixmap.SP_DriveHDIcon,
    "messages": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "whatsapp": QStyle.StandardPixmap.SP_ComputerIcon,
    "ios_backup": QStyle.StandardPixmap.SP_DriveNetIcon,
    "cloud": QStyle.StandardPixmap.SP_DriveNetIcon,
}

_FULL_DISK_ACCESS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def _accessible(path: Path) -> tuple[bool, str | None]:
    try:
        os.listdir(path)
        return True, None
    except PermissionError:
        return False, "Grant Salvage Full Disk Access in System Settings → Privacy & Security."
    except FileNotFoundError:
        return False, "Not found on this Mac."
    except OSError as exc:
        return False, str(exc)


class MediaSourceRow(QFrame):
    def __init__(self, source: MediaSource, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = source
        self.setProperty("role", "card")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.check = QCheckBox()
        self.check.setChecked(source.accessible)
        self.check.setEnabled(source.accessible)
        layout.addWidget(self.check)

        icon_label = QLabel()
        style = QApplication.style()
        icon_label.setPixmap(style.standardIcon(_KIND_ICON.get(source.kind, QStyle.StandardPixmap.SP_FileIcon)).pixmap(24, 24))
        layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        name_label = QLabel(source.label)
        name_label.setStyleSheet("font-weight: 600;")
        text_layout.addWidget(name_label)
        sub_text = str(source.path) if source.accessible else (source.note or "Not accessible")
        sub_label = QLabel(sub_text)
        sub_label.setProperty("role", "subheading")
        sub_label.setWordWrap(True)
        text_layout.addWidget(sub_label)
        layout.addLayout(text_layout, 1)

        if not source.accessible:
            self.setEnabled(False)


class MediaOptionsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self._destination: Path | None = None
        self._rows: list[MediaSourceRow] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)

        heading = QLabel("Search this Mac for photos & videos")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        subheading = QLabel(
            "Salvage searches these places, deduplicates by content, and cross-references "
            "Messages attachments against any iPhone backup for files that vanished from "
            "this Mac but still exist on the phone."
        )
        subheading.setProperty("role", "subheading")
        subheading.setWordWrap(True)
        outer.addWidget(subheading)

        fda_row = QHBoxLayout()
        self.fda_label = QLabel("Some locations need Full Disk Access to search.")
        self.fda_label.setProperty("role", "error")
        fda_row.addWidget(self.fda_label)
        self.fda_btn = QPushButton("Open Full Disk Access settings")
        self.fda_btn.clicked.connect(self._open_fda_settings)
        fda_row.addWidget(self.fda_btn)
        fda_row.addStretch()
        outer.addLayout(fda_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_container)
        outer.addWidget(self.scroll, 1)

        add_row = QHBoxLayout()
        add_btn = QPushButton("Add a folder or iPhone backup…")
        add_btn.clicked.connect(self._add_folder)
        add_row.addWidget(add_btn)
        add_row.addStretch()
        outer.addLayout(add_row)

        options_panel = QFrame()
        options_panel.setProperty("role", "panel")
        options_layout = QVBoxLayout(options_panel)
        options_title = QLabel("Options")
        options_title.setStyleSheet("font-weight: 600;")
        options_layout.addWidget(options_title)
        self.skip_small_check = QCheckBox("Skip files smaller than 20 KB")
        self.skip_small_check.setChecked(True)
        options_layout.addWidget(self.skip_small_check)
        self.dupes_check = QCheckBox("Find duplicates (slower)")
        self.dupes_check.setChecked(True)
        options_layout.addWidget(self.dupes_check)
        outer.addWidget(options_panel)

        dest_panel = QFrame()
        dest_panel.setProperty("role", "panel")
        dest_layout = QVBoxLayout(dest_panel)
        dest_title = QLabel("Save recovered photos to")
        dest_title.setStyleSheet("font-weight: 600;")
        dest_layout.addWidget(dest_title)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Choose a destination folder…")
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

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.controller.go_to_source)
        bottom_row.addWidget(back_btn)
        bottom_row.addStretch()
        self.start_btn = QPushButton("Search")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        bottom_row.addWidget(self.start_btn)
        outer.addLayout(bottom_row)

    def refresh(self) -> None:
        for row in self._rows:
            row.setParent(None)
        self._rows = []

        sources = media_facade.default_sources(self.controller.fake)
        any_inaccessible = any(not s.accessible for s in sources)
        self.fda_label.setVisible(any_inaccessible)
        self.fda_btn.setVisible(any_inaccessible)

        for source in sources:
            self._add_row(source)

        self._destination = None
        self.dest_edit.clear()
        self.dest_error.hide()
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            self._set_destination(desktop)
        self._update_start_enabled()

    def _add_row(self, source: MediaSource) -> None:
        row = MediaSourceRow(source)
        row.check.stateChanged.connect(self._on_row_toggled)
        self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self._rows.append(row)

    def _on_row_toggled(self, *_args) -> None:
        if self._destination is not None:
            self._set_destination(self._destination)
        else:
            self._update_start_enabled()

    def _open_fda_settings(self) -> None:
        try:
            subprocess.run(["open", _FULL_DISK_ACCESS_URL], check=False)
        except OSError:
            pass

    def _add_folder(self) -> None:
        path_str = QFileDialog.getExistingDirectory(self, "Choose a folder or iPhone backup", str(Path.home()))
        if not path_str:
            return
        path = Path(path_str)
        if (path / "Manifest.db").exists():
            try:
                source = source_from_backup_dir(path)
            except ValueError as exc:
                QMessageBox.warning(self, "Not a backup folder", str(exc))
                return
        else:
            accessible, note = _accessible(path)
            source = MediaSource(
                key=f"custom_{path}", label=path.name, path=path, kind="folder", accessible=accessible, note=note
            )
        self._add_row(source)
        self._update_start_enabled()

    def _browse_destination(self) -> None:
        start_dir = self.dest_edit.text() or str(Path.home())
        path_str = QFileDialog.getExistingDirectory(self, "Choose destination folder", start_dir)
        if not path_str:
            return
        self._set_destination(Path(path_str))

    def _selected_sources(self) -> list[MediaSource]:
        return [row.source for row in self._rows if row.source.accessible and row.check.isChecked()]

    def _set_destination(self, path: Path) -> None:
        self.dest_edit.setText(str(path))
        self._destination = path
        error = self._validate_destination(path)
        if error:
            self.dest_error.setText(error)
            self.dest_error.show()
        else:
            self.dest_error.hide()
        self._update_start_enabled()

    def _validate_destination(self, path: Path) -> str | None:
        if path.exists() and not os.access(path, os.W_OK):
            return "This folder isn't writable. Choose another destination."
        try:
            resolved_dest = path.resolve()
        except OSError:
            return None
        for source in self._selected_sources():
            try:
                resolved_src = Path(source.path).resolve()
            except OSError:
                continue
            if resolved_dest == resolved_src or resolved_src in resolved_dest.parents:
                return (
                    f"This destination is inside a location being searched ({source.label}). "
                    "Choose a folder outside the places you're scanning."
                )
        return None

    def _update_start_enabled(self, *_args) -> None:
        dest_ok = self._destination is not None and not self.dest_error.isVisible()
        sources_ok = len(self._selected_sources()) > 0
        self.start_btn.setEnabled(bool(dest_ok and sources_ok))

    def _start(self) -> None:
        assert self._destination is not None
        min_size = 20_000 if self.skip_small_check.isChecked() else 0
        self.controller.go_to_media_scan(
            self._selected_sources(), min_size, self.dupes_check.isChecked(), self._destination
        )
