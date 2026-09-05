"""Wizard step 1: choose the device, partition, or disk image to recover from."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.models import Device
from salvage.ui import engine_facade
from salvage.ui.format_utils import human_size


class DeviceRow(QFrame):
    clicked = Signal(object)  # Device

    def __init__(self, device: Device, indent: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.setProperty("role", "card")
        self.setProperty("selected", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        if indent:
            layout.addSpacing(28)

        icon_label = QLabel()
        style = QApplication.style()
        pix_enum = (
            QStyle.StandardPixmap.SP_DriveFDIcon
            if device.is_removable
            else QStyle.StandardPixmap.SP_DriveHDIcon
        )
        icon_label.setPixmap(style.standardIcon(pix_enum).pixmap(28, 28))
        layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        name_line = QHBoxLayout()
        name_label = QLabel(device.name)
        name_label.setStyleSheet("font-weight: 600;")
        name_line.addWidget(name_label)
        if device.is_system:
            badge = QLabel("System")
            badge.setProperty("role", "badge")
            name_line.addWidget(badge)
        name_line.addStretch()
        text_layout.addLayout(name_line)

        parts = [human_size(device.size_bytes)]
        if device.filesystem:
            parts.append(device.filesystem)
        if device.mount_point:
            parts.append(device.mount_point)
        sub_label = QLabel(" · ".join(parts))
        sub_label.setProperty("role", "subheading")
        text_layout.addWidget(sub_label)

        layout.addLayout(text_layout, 1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class SourcePage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self._rows: list[DeviceRow] = []
        self._selected_device: Device | None = None
        self._image_device: Device | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)

        header_row = QHBoxLayout()
        heading = QLabel("Select a source to recover from")
        heading.setProperty("role", "heading")
        header_row.addWidget(heading)
        header_row.addStretch()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        header_row.addWidget(self.refresh_btn)
        outer.addLayout(header_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_container)
        outer.addWidget(self.scroll, 1)

        self.empty_label = QLabel("No devices found. Connect a drive and click Refresh.")
        self.empty_label.setProperty("role", "subheading")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        outer.addWidget(self.empty_label)

        note = QLabel(
            "Recovering from a partition (rather than the whole disk) is usually best — "
            "it is faster and avoids duplicate results from unrelated data."
        )
        note.setProperty("role", "subheading")
        note.setWordWrap(True)
        outer.addWidget(note)

        bottom_row = QHBoxLayout()
        self.image_btn = QPushButton("Scan a disk image…")
        self.image_btn.clicked.connect(self._pick_image)
        bottom_row.addWidget(self.image_btn)
        bottom_row.addStretch()
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setProperty("role", "primary")
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self._continue)
        bottom_row.addWidget(self.continue_btn)
        outer.addLayout(bottom_row)

    def refresh(self) -> None:
        for row in self._rows:
            row.setParent(None)
        self._rows = []
        self._selected_device = None
        self.continue_btn.setEnabled(False)

        devices = engine_facade.list_devices(self.controller.fake)
        if self._image_device is not None:
            devices = [self._image_device, *devices]

        disks = [d for d in devices if d.kind in ("disk", "image")]
        for disk in disks:
            self._add_row(disk, indent=False)
            for part in devices:
                if part.kind == "partition" and part.parent_id == disk.id:
                    self._add_row(part, indent=True)

        self.empty_label.setVisible(len(devices) == 0)
        self.scroll.setVisible(len(devices) > 0)

    def _add_row(self, device: Device, indent: bool) -> None:
        row = DeviceRow(device, indent=indent)
        row.clicked.connect(self._select)
        self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        self._rows.append(row)
        if device.kind == "partition" and device.is_removable and self._selected_device is None:
            self._select(device)

    def _select(self, device: Device) -> None:
        self._selected_device = device
        for row in self._rows:
            row.set_selected(row.device.id == device.id)
        self.continue_btn.setEnabled(True)

    def _pick_image(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, "Choose a disk image")
        if not path_str:
            return
        device = engine_facade.device_from_image(Path(path_str), self.controller.fake)
        self._image_device = device
        self.refresh()
        self._select(device)

    def _continue(self) -> None:
        if self._selected_device is not None:
            self.controller.go_to_options(self._selected_device)
