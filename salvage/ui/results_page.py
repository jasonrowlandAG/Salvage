"""Wizard step 4: browse, filter, preview, and select recovered files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.models import Category, RecoveredFile, ScanResult
from salvage.ui.format_utils import human_size
from salvage.ui.thumbnails import ThumbnailLoader

PathRole = Qt.ItemDataRole.UserRole + 1

CATEGORY_FILTER_LABELS: list[tuple[str | None, str]] = [
    (None, "All"),
    ("image", "Photos"),
    ("document", "Documents"),
    ("video", "Videos"),
    ("audio", "Audio"),
    ("archive", "Archives"),
    ("other", "Other"),
]

_CATEGORY_ICON_PIXMAP = {
    "image": QStyle.StandardPixmap.SP_FileIcon,
    "document": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "video": QStyle.StandardPixmap.SP_MediaPlay,
    "audio": QStyle.StandardPixmap.SP_MediaVolume,
    "archive": QStyle.StandardPixmap.SP_DirIcon,
    "other": QStyle.StandardPixmap.SP_FileIcon,
}


class FileListModel(QAbstractListModel):
    def __init__(self, files: list[RecoveredFile], parent=None) -> None:
        super().__init__(parent)
        self._all = files
        self._checked: set[int] = set()
        self._visible: list[int] = list(range(len(files)))
        self._thumbs: dict[str, QPixmap] = {}
        self._path_to_index = {str(f.path): i for i, f in enumerate(files)}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def _file(self, row: int) -> RecoveredFile:
        return self._all[self._visible[row]]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        f = self._file(index.row())
        if role == Qt.ItemDataRole.DisplayRole:
            return f.name
        if role == Qt.ItemDataRole.DecorationRole:
            pix = self._thumbs.get(str(f.path))
            if pix is not None:
                return pix
            return QApplication.style().standardIcon(_CATEGORY_ICON_PIXMAP.get(f.category, QStyle.StandardPixmap.SP_FileIcon))
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if self._visible[index.row()] in self._checked else Qt.CheckState.Unchecked
        if role == PathRole:
            return f
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.isValid():
            real_idx = self._visible[index.row()]
            if value == Qt.CheckState.Checked:
                self._checked.add(real_idx)
            else:
                self._checked.discard(real_idx)
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable

    def set_filter(self, category: str | None, search: str) -> None:
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, f in enumerate(self._all)
            if (category is None or f.category == category)
            and (not needle or needle in f.name.lower() or needle in f.ext.lower())
        ]
        self.endResetModel()

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._all:
            counts[f.category] = counts.get(f.category, 0) + 1
        return counts

    def image_files(self) -> list[RecoveredFile]:
        return [f for f in self._all if f.category == "image"]

    def set_thumbnail(self, path_str: str, pixmap: QPixmap) -> None:
        self._thumbs[path_str] = pixmap
        real_idx = self._path_to_index.get(path_str)
        if real_idx is None or real_idx not in self._visible:
            return
        row = self._visible.index(real_idx)
        model_index = self.index(row)
        self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])

    def select_all_filtered(self) -> None:
        self._checked.update(self._visible)
        if self._visible:
            self.dataChanged.emit(self.index(0), self.index(len(self._visible) - 1), [Qt.ItemDataRole.CheckStateRole])

    def clear_all_checked(self) -> None:
        self._checked.clear()
        if self._visible:
            self.dataChanged.emit(self.index(0), self.index(len(self._visible) - 1), [Qt.ItemDataRole.CheckStateRole])

    def checked_files(self) -> list[RecoveredFile]:
        return [self._all[i] for i in sorted(self._checked)]


class FileTileDelegate(QStyledItemDelegate):
    TILE_SIZE = QSize(140, 140)
    CHECKBOX_SIZE = 18

    def sizeHint(self, option, index) -> QSize:
        return self.TILE_SIZE

    def paint(self, painter, option, index) -> None:
        painter.save()
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor("#dce8fb"))

        thumb_rect = QRect(rect.x() + 10, rect.y() + 8, rect.width() - 20, 88)
        pix = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pix, QPixmap) and not pix.isNull():
            scaled = pix.scaled(
                thumb_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            x = thumb_rect.x() + (thumb_rect.width() - scaled.width()) // 2
            y = thumb_rect.y() + (thumb_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        elif isinstance(pix, QIcon):
            pix.paint(painter, thumb_rect, Qt.AlignmentFlag.AlignCenter)

        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        text_rect = QRect(rect.x() + 4, rect.y() + 100, rect.width() - 8, 34)
        painter.setPen(QColor("#1d1d1f"))
        fm = painter.fontMetrics()
        elided = fm.elidedText(name, Qt.TextElideMode.ElideMiddle, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)

        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        cb_rect = QRect(rect.x() + 6, rect.y() + 6, self.CHECKBOX_SIZE, self.CHECKBOX_SIZE)
        opt = QStyleOptionButton()
        opt.rect = cb_rect
        opt.state = QStyle.StateFlag.State_Enabled
        opt.state |= QStyle.StateFlag.State_On if checked else QStyle.StateFlag.State_Off
        QApplication.style().drawControl(QStyle.ControlElement.CE_CheckBox, opt, painter)
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease:
            cb_rect = QRect(option.rect.x() + 6, option.rect.y() + 6, self.CHECKBOX_SIZE, self.CHECKBOX_SIZE)
            if cb_rect.contains(event.pos()):
                current = index.data(Qt.ItemDataRole.CheckStateRole)
                new_state = Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
                model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole)
                return True
        return False


class ResultsPage(QWidget):
    recover_requested = Signal(list)  # list[RecoveredFile]

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.model: FileListModel | None = None
        self._current_category: str | None = None
        self._preview_path: str | None = None
        self.thumb_loader = ThumbnailLoader(self)
        self.thumb_loader.ready.connect(self._on_thumb_ready)
        self.preview_loader = ThumbnailLoader(self)
        self.preview_loader.ready.connect(self._on_preview_ready)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Recovered files")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        body = QHBoxLayout()
        body.setSpacing(16)

        sidebar = QVBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or extension…")
        self.search_edit.textChanged.connect(self._apply_filter)
        sidebar.addWidget(self.search_edit)
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(180)
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        sidebar.addWidget(self.category_list, 1)
        sidebar_widget = QWidget()
        sidebar_widget.setLayout(sidebar)
        body.addWidget(sidebar_widget)

        center = QVBoxLayout()
        self.center_stack = QStackedWidget()
        self.list_view = QListView()
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setSpacing(8)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.list_view.setItemDelegate(FileTileDelegate(self))
        self.center_stack.addWidget(self.list_view)
        self.empty_label = QLabel("No files match your filters.")
        self.empty_label.setProperty("role", "subheading")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.center_stack.addWidget(self.empty_label)
        center.addWidget(self.center_stack, 1)

        footer_row = QHBoxLayout()
        self.footer_label = QLabel("0 selected · 0 B")
        footer_row.addWidget(self.footer_label)
        footer_row.addStretch()
        select_all_btn = QPushButton("Select all (filtered)")
        select_all_btn.clicked.connect(self._select_all_filtered)
        footer_row.addWidget(select_all_btn)
        select_none_btn = QPushButton("Select none")
        select_none_btn.clicked.connect(self._select_none)
        footer_row.addWidget(select_none_btn)
        self.recover_btn = QPushButton("Recover selected")
        self.recover_btn.setProperty("role", "primary")
        self.recover_btn.setEnabled(False)
        self.recover_btn.clicked.connect(self._recover_clicked)
        footer_row.addWidget(self.recover_btn)
        center.addLayout(footer_row)

        center_widget = QWidget()
        center_widget.setLayout(center)
        body.addWidget(center_widget, 1)

        self.preview_panel = QFrame()
        self.preview_panel.setProperty("role", "panel")
        self.preview_panel.setFixedWidth(240)
        preview_layout = QVBoxLayout(self.preview_panel)
        self.preview_image = QLabel("Select a file to preview it.")
        self.preview_image.setProperty("role", "subheading")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setFixedHeight(180)
        self.preview_image.setWordWrap(True)
        preview_layout.addWidget(self.preview_image)
        self.preview_name = QLabel("")
        self.preview_name.setWordWrap(True)
        self.preview_name.setStyleSheet("font-weight: 600;")
        preview_layout.addWidget(self.preview_name)
        self.preview_size = QLabel("")
        self.preview_size.setProperty("role", "subheading")
        preview_layout.addWidget(self.preview_size)
        self.preview_offset = QLabel("")
        self.preview_offset.setProperty("role", "subheading")
        preview_layout.addWidget(self.preview_offset)
        preview_layout.addStretch()
        body.addWidget(self.preview_panel)

        outer.addLayout(body, 1)

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(lambda: self.controller.go_to_options(self.controller.session.device))
        bottom_row.addWidget(back_btn)
        bottom_row.addStretch()
        outer.addLayout(bottom_row)

    def set_result(self, scan_result: ScanResult) -> None:
        self.model = FileListModel(scan_result.files)
        self.model.dataChanged.connect(lambda *_: self._update_footer())
        self.list_view.setModel(self.model)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)

        self._rebuild_category_list()
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._current_category = None
        self.category_list.setCurrentRow(0)
        self._apply_filter()
        self._update_preview(None)
        self._update_footer()

        for f in self.model.image_files():
            self.thumb_loader.request(f.path)

    def _rebuild_category_list(self) -> None:
        assert self.model is not None
        counts = self.model.category_counts()
        total = sum(counts.values())
        self.category_list.blockSignals(True)
        self.category_list.clear()
        for category, label in CATEGORY_FILTER_LABELS:
            n = total if category is None else counts.get(category, 0)
            item = QListWidgetItem(f"{label} ({n})")
            item.setData(Qt.ItemDataRole.UserRole, category)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)

    def _on_category_changed(self, row: int) -> None:
        if row < 0:
            return
        self._current_category = self.category_list.item(row).data(Qt.ItemDataRole.UserRole)
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self.model is None:
            return
        self.model.set_filter(self._current_category, self.search_edit.text())
        empty = self.model.rowCount() == 0
        self.center_stack.setCurrentWidget(self.empty_label if empty else self.list_view)

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid() or self.model is None:
            self._update_preview(None)
            return
        f = self.model.data(current, PathRole)
        self._update_preview(f)

    def _update_preview(self, f: RecoveredFile | None) -> None:
        if f is None:
            self.preview_image.setText("Select a file to preview it.")
            self.preview_image.setPixmap(QPixmap())
            self.preview_name.setText("")
            self.preview_size.setText("")
            self.preview_offset.setText("")
            self._preview_path = None
            return
        self.preview_name.setText(f.name)
        self.preview_size.setText(human_size(f.size))
        self.preview_offset.setText(f"Offset: {f.offset:,}" if f.offset is not None else "Offset: unknown")
        if f.category == "image":
            self._preview_path = str(f.path)
            self.preview_image.setText("Loading preview…")
            self.preview_image.setPixmap(QPixmap())
            self.preview_loader.request(f.path, size=220)
        else:
            self._preview_path = None
            self.preview_image.setText("No preview available")
            self.preview_image.setPixmap(QPixmap())

    def _on_thumb_ready(self, path_str: str, pixmap: QPixmap) -> None:
        if self.model is not None:
            self.model.set_thumbnail(path_str, pixmap)

    def _on_preview_ready(self, path_str: str, pixmap: QPixmap) -> None:
        if path_str != self._preview_path:
            return
        self.preview_image.setText("")
        self.preview_image.setPixmap(
            pixmap.scaled(220, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def _select_all_filtered(self) -> None:
        if self.model is not None:
            self.model.select_all_filtered()
            self._update_footer()

    def _select_none(self) -> None:
        if self.model is not None:
            self.model.clear_all_checked()
            self._update_footer()

    def _update_footer(self) -> None:
        if self.model is None:
            return
        selected = self.model.checked_files()
        total_bytes = sum(f.size for f in selected)
        self.footer_label.setText(f"{len(selected)} selected · {human_size(total_bytes)}")
        self.recover_btn.setEnabled(len(selected) > 0)

    def _recover_clicked(self) -> None:
        if self.model is not None:
            self.controller.start_recovery(self.model.checked_files())
