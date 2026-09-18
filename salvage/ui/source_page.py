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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.ios import IOSDevice
from salvage.engine.models import Device
from salvage.ui import engine_facade, ios_facade
from salvage.ui.format_utils import human_size

_SYSTEM_DISK_WARNING_FILEVAULT_ON = (
    "This is your Mac's startup disk. macOS doesn't allow raw scanning of it, and FileVault is "
    "on so the data is encrypted — a scan will find nothing. To get files back from this Mac: "
    "check the Trash, iCloud Drive → Recently Deleted, Time Machine, or APFS local snapshots. "
    "Salvage can scan external drives, USB sticks, SD cards and disk images."
)
_SYSTEM_DISK_WARNING_FILEVAULT_OFF = (
    "This is your Mac's startup disk. macOS doesn't allow raw scanning of it unless Full Disk "
    "Access is granted to Salvage (System Settings → Privacy & Security → Full Disk Access → "
    "add Salvage). To get files back from this Mac: check the Trash, iCloud Drive → Recently "
    "Deleted, Time Machine, or APFS local snapshots. Salvage can scan external drives, USB "
    "sticks, SD cards and disk images."
)


class IOSDeviceRow(QFrame):
    clicked = Signal(object)  # IOSDevice

    def __init__(self, device: IOSDevice, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.setProperty("role", "card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        icon_label = QLabel()
        style = QApplication.style()
        icon_label.setPixmap(style.standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon).pixmap(28, 28))
        layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        name_line = QHBoxLayout()
        name_label = QLabel(device.name)
        name_label.setStyleSheet("font-weight: 600;")
        name_line.addWidget(name_label)
        if device.encrypted_backups:
            badge = QLabel("Backups encrypted")
            badge.setProperty("role", "badge")
            name_line.addWidget(badge)
        name_line.addStretch()
        text_layout.addLayout(name_line)

        parts = [device.product_type, f"iOS {device.ios_version}"]
        if device.capacity_bytes:
            parts.append(human_size(device.capacity_bytes))
        sub_label = QLabel(" · ".join(parts))
        sub_label.setProperty("role", "subheading")
        text_layout.addWidget(sub_label)

        layout.addLayout(text_layout, 1)

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self.device)
        super().mousePressEvent(event)


class MediaSearchCard(QFrame):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        icon_label = QLabel()
        style = QApplication.style()
        icon_label.setPixmap(style.standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon).pixmap(28, 28))
        layout.addWidget(icon_label)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        name_label = QLabel("Photos & videos on this Mac")
        name_label.setStyleSheet("font-weight: 600;")
        text_layout.addWidget(name_label)
        sub_label = QLabel(
            "Search the Photos library, Messages, iCloud Drive, cloud folders and iPhone backups"
        )
        sub_label.setProperty("role", "subheading")
        sub_label.setWordWrap(True)
        text_layout.addWidget(sub_label)
        layout.addLayout(text_layout, 1)

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


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

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self.device)
        super().mousePressEvent(event)

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
        self._ios_rows: list[IOSDeviceRow] = []
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

        mac_heading = QLabel("This Mac")
        mac_heading.setStyleSheet("font-weight: 600;")
        outer.addWidget(mac_heading)

        self.media_search_card = MediaSearchCard()
        self.media_search_card.clicked.connect(self.controller.go_to_media_options)
        outer.addWidget(self.media_search_card)

        ios_heading = QLabel("iPhone / iPad")
        ios_heading.setStyleSheet("font-weight: 600;")
        outer.addWidget(ios_heading)

        self.ios_list_container = QWidget()
        self.ios_list_layout = QVBoxLayout(self.ios_list_container)
        self.ios_list_layout.setContentsMargins(0, 0, 0, 0)
        self.ios_list_layout.setSpacing(8)
        outer.addWidget(self.ios_list_container)

        self.ios_empty_label = QLabel("No iPhone or iPad detected. Connect one by USB and unlock it.")
        self.ios_empty_label.setProperty("role", "subheading")
        outer.addWidget(self.ios_empty_label)

        ios_bottom_row = QHBoxLayout()
        self.use_backup_btn = QPushButton("Use an existing backup folder…")
        self.use_backup_btn.clicked.connect(self._pick_backup_folder)
        ios_bottom_row.addWidget(self.use_backup_btn)
        ios_bottom_row.addStretch()
        outer.addLayout(ios_bottom_row)

        drive_heading = QLabel("Drives and disk images")
        drive_heading.setStyleSheet("font-weight: 600;")
        outer.addWidget(drive_heading)

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

        self.system_disk_warning = QLabel("")
        self.system_disk_warning.setProperty("role", "error")
        self.system_disk_warning.setWordWrap(True)
        self.system_disk_warning.hide()
        outer.addWidget(self.system_disk_warning)

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
        self._refresh_ios()

        for row in self._rows:
            row.setParent(None)
        self._rows = []
        self._selected_device = None
        self.continue_btn.setEnabled(False)
        self.system_disk_warning.hide()

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
        self._update_system_disk_warning(device)

    def _is_system_target(self, device: Device) -> bool:
        if device.is_system:
            return True
        return any(
            row.device.id == device.parent_id and row.device.is_system for row in self._rows
        )

    def _update_system_disk_warning(self, device: Device) -> None:
        if not self._is_system_target(device):
            self.system_disk_warning.hide()
            self.continue_btn.setEnabled(True)
            return

        filevault_on = engine_facade.filevault_enabled(self.controller.fake)
        if filevault_on:
            self.system_disk_warning.setText(_SYSTEM_DISK_WARNING_FILEVAULT_ON)
            self.continue_btn.setEnabled(False)
        else:
            self.system_disk_warning.setText(_SYSTEM_DISK_WARNING_FILEVAULT_OFF)
            self.continue_btn.setEnabled(True)
        self.system_disk_warning.show()

    def open_disk_image_dialog(self) -> None:
        """Public entry point for the File > Open Disk Image... menu action - same
        picker as the "Scan a disk image..." button below."""
        self._pick_image()

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

    def _refresh_ios(self) -> None:
        for row in self._ios_rows:
            row.setParent(None)
        self._ios_rows = []

        devices = ios_facade.list_ios_devices(self.controller.fake)
        for device in devices:
            row = IOSDeviceRow(device)
            row.clicked.connect(self._select_ios_device)
            self.ios_list_layout.addWidget(row)
            self._ios_rows.append(row)

        self.ios_empty_label.setVisible(len(devices) == 0)

    def _select_ios_device(self, device: IOSDevice) -> None:
        self.controller.go_to_ios_options(device=device, existing_backup_dir=None)

    def _pick_backup_folder(self) -> None:
        default_dir = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
        start_dir = str(default_dir) if default_dir.exists() else str(Path.home())
        path_str = QFileDialog.getExistingDirectory(self, "Choose an iPhone backup folder", start_dir)
        if not path_str:
            return
        path = Path(path_str)
        if not ios_facade.is_valid_backup_folder(path):
            QMessageBox.warning(
                self,
                "Not a backup folder",
                f"{path} doesn't contain a Manifest.db file, so it doesn't look like an iPhone backup.",
            )
            return
        self.controller.go_to_ios_options(device=None, existing_backup_dir=path)
