"""Wizard step 4: browse, filter, preview, and select recovered files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from salvage.engine import thumbcache
from salvage.engine.models import Category, Integrity, RecoveredFile, ScanResult
from salvage.ui.format_utils import human_size
from salvage.ui.preview_panel import PreviewPanel
from salvage.ui.thumb_service import BackgroundThumbnailService
from salvage.ui.workers import VerifyWorker

PathRole = Qt.ItemDataRole.UserRole + 1
# Shared with media_results_page.py's MediaListModel, which supplies the same role
# for its own "Recovered"/"Deleted"/"Dup"/"iCloud" badges - one delegate, one style
# map, so both result grids draw badges identically instead of duplicating the logic.
BadgeRole = Qt.ItemDataRole.UserRole + 2

_BADGE_STYLE = {
    "Recovered": ("#0a72e8", "Recovered"),
    "Deleted": ("#c0392b", "Deleted"),
    "Dup": ("#8a6d1d", "Dup"),
    "iCloud": ("#6e6e73", "iCloud"),
    "Intact": ("#1d883c", "Intact"),
    "Partial": ("#996f09", "Partial"),
    "Corrupt": ("#c0392b", "Corrupt"),
    "On disk": ("#6e6e73", "On disk"),
}

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


def _protect_button_width(button: QPushButton) -> None:
    """Stops a footer button's own label from being compressed below its sizeHint
    when a sibling widget (e.g. a long status label) is starved for space — see the
    footer_row construction below."""
    policy = button.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Minimum)
    button.setSizePolicy(policy)


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
        if role == BadgeRole:
            badges = []
            if f.also_exists:
                badges.append("On disk")
            if f.integrity == Integrity.INTACT:
                badges.append("Intact")
            elif f.integrity == Integrity.PARTIAL:
                badges.append("Partial")
            elif f.integrity == Integrity.CORRUPT:
                badges.append("Corrupt")
            return badges
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

    def set_filter(
        self,
        category: str | None,
        search: str,
        *,
        only_intact: bool = False,
        hide_corrupt: bool = False,
        hide_on_disk: bool = False,
    ) -> None:
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, f in enumerate(self._all)
            if (category is None or f.category == category)
            and (not needle or needle in f.name.lower() or needle in f.ext.lower())
            and (not only_intact or f.integrity == Integrity.INTACT)
            and (not hide_corrupt or f.integrity != Integrity.CORRUPT)
            and (not hide_on_disk or not f.also_exists)
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

    def all_files(self) -> list[RecoveredFile]:
        return list(self._all)

    def any_on_disk(self) -> bool:
        return any(f.also_exists for f in self._all)

    def update_files(self, updated: list[RecoveredFile]) -> None:
        """Swaps in verified copies (same order/paths as before, only integrity/
        also_exists differ) once background verification finishes."""
        if len(updated) != len(self._all):
            return
        self._all = updated
        if self._visible:
            self.dataChanged.emit(
                self.index(0), self.index(len(self._visible) - 1), [BadgeRole, Qt.ItemDataRole.DecorationRole]
            )

    def select_default_recoverable(self) -> None:
        """Default recovery selection once verdicts are known: intact + partial,
        excluding corrupt and files already present on the source volume."""
        self._checked = {
            i
            for i, f in enumerate(self._all)
            if f.integrity in (Integrity.INTACT, Integrity.PARTIAL) and not f.also_exists
        }
        if self._visible:
            self.dataChanged.emit(
                self.index(0), self.index(len(self._visible) - 1), [Qt.ItemDataRole.CheckStateRole]
            )


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

        badges = index.data(BadgeRole) or []
        if badges:
            x = rect.right() - 6
            y = rect.top() + 6
            for badge in badges:
                colour, text = _BADGE_STYLE.get(badge, ("#6e6e73", badge))
                fm = painter.fontMetrics()
                w = fm.horizontalAdvance(text) + 8
                badge_rect = QRect(x - w, y, w, 14)
                painter.setBrush(QColor(colour))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(badge_rect, 3, 3)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, text)
                x -= w + 4

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
        # Background, throttled thumbnail generation shared with the Mac-media flow
        # (thumb_service.py) instead of firing one unthrottled QRunnable per image the
        # instant results land - that flood used to freeze the grid for ~20s+ at
        # realistic result counts (docs/ux-review.md finding 4.1).
        self.thumb_service = BackgroundThumbnailService(self)
        self.thumb_service.thumb_ready.connect(self._on_thumb_ready)
        self.thumb_service.progress.connect(self._on_thumb_progress)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._request_visible_thumbnails)
        self._verify_worker: VerifyWorker | None = None
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
        self.category_list.setMinimumWidth(90)
        self.category_list.setMaximumWidth(180)
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        sidebar.addWidget(self.category_list, 1)

        self.only_intact_check = QCheckBox("Only intact")
        self.only_intact_check.stateChanged.connect(self._apply_filter)
        sidebar.addWidget(self.only_intact_check)
        self.hide_corrupt_check = QCheckBox("Hide corrupt")
        self.hide_corrupt_check.stateChanged.connect(self._apply_filter)
        sidebar.addWidget(self.hide_corrupt_check)
        self.hide_on_disk_check = QCheckBox("Hide files already on disk")
        self.hide_on_disk_check.stateChanged.connect(self._apply_filter)
        sidebar.addWidget(self.hide_on_disk_check)

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
        self.list_view.verticalScrollBar().valueChanged.connect(lambda _: self._thumb_timer.start())
        self.center_stack.addWidget(self.list_view)
        self.empty_label = QLabel("No files match your filters.")
        self.empty_label.setProperty("role", "subheading")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.center_stack.addWidget(self.empty_label)
        center.addWidget(self.center_stack, 1)

        footer_row = QHBoxLayout()
        self.footer_label = QLabel("0 selected · 0 B")
        footer_row.addWidget(self.footer_label)
        self.verify_progress_label = QLabel("")
        self.verify_progress_label.setProperty("role", "subheading")
        # At large item counts this text ("Checking recovered files… 0 / 30,000") can
        # get long; let it yield space first rather than squeezing the action buttons
        # below their own label width (they were clipping to e.g. "ect all (filtere").
        self.verify_progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_row.addWidget(self.verify_progress_label)
        self.thumb_progress_label = QLabel("")
        self.thumb_progress_label.setProperty("role", "subheading")
        self.thumb_progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_row.addWidget(self.thumb_progress_label)
        footer_row.addStretch()
        select_all_btn = QPushButton("Select all (filtered)")
        select_all_btn.clicked.connect(self._select_all_filtered)
        _protect_button_width(select_all_btn)
        footer_row.addWidget(select_all_btn)
        select_none_btn = QPushButton("Select none")
        select_none_btn.clicked.connect(self._select_none)
        _protect_button_width(select_none_btn)
        footer_row.addWidget(select_none_btn)
        self.recover_btn = QPushButton("Recover selected")
        self.recover_btn.setProperty("role", "primary")
        self.recover_btn.setEnabled(False)
        self.recover_btn.clicked.connect(self._recover_clicked)
        _protect_button_width(self.recover_btn)
        footer_row.addWidget(self.recover_btn)
        center.addLayout(footer_row)

        center_widget = QWidget()
        center_widget.setLayout(center)
        body.addWidget(center_widget, 1)

        self.preview_panel = PreviewPanel(self)
        self.preview_panel.setMinimumWidth(150)
        self.preview_panel.setMaximumWidth(340)
        body.addWidget(self.preview_panel)

        outer.addLayout(body, 1)

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self._go_back)
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
        for check in (self.only_intact_check, self.hide_corrupt_check, self.hide_on_disk_check):
            check.blockSignals(True)
            check.setChecked(False)
            check.blockSignals(False)
        self._current_category = None
        self.category_list.setCurrentRow(0)
        self._apply_filter()
        self._update_preview(None)
        self._update_footer()

        thumbable = [f.path for f in self.model.image_files()]
        self.thumb_progress_label.setText(f"Thumbnails 0 / {len(thumbable):,}" if thumbable else "")
        self.thumb_service.start(thumbable)
        self._thumb_timer.start()

        self._start_verification()

    def _start_verification(self) -> None:
        if self.model is None:
            return
        files = self.model.all_files()
        if not files:
            return
        device = getattr(self.controller.session, "device", None)
        self.verify_progress_label.setText(f"Checking recovered files… 0 / {len(files):,}")
        self._verify_worker = VerifyWorker(files, device, self)
        self._verify_worker.progress.connect(self._on_verify_progress)
        self._verify_worker.finished_verify.connect(self._on_verify_finished)
        self._verify_worker.start()

    def _on_verify_progress(self, done: int, total: int) -> None:
        if self.sender() is not self._verify_worker:
            return  # a stale worker from a previous scan - ignore
        self.verify_progress_label.setText(f"Checking recovered files… {done:,} / {total:,}")

    def _on_verify_finished(self, verified_files: list[RecoveredFile]) -> None:
        if self.sender() is not self._verify_worker or self.model is None:
            return  # a stale worker from a previous scan - ignore
        self.verify_progress_label.setText("")
        self.model.update_files(verified_files)
        self.hide_on_disk_check.blockSignals(True)
        self.hide_on_disk_check.setChecked(self.model.any_on_disk())
        self.hide_on_disk_check.blockSignals(False)
        self.model.select_default_recoverable()
        self._apply_filter()
        self._update_footer()
        self._verify_worker = None

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
        self.model.set_filter(
            self._current_category,
            self.search_edit.text(),
            only_intact=self.only_intact_check.isChecked(),
            hide_corrupt=self.hide_corrupt_check.isChecked(),
            hide_on_disk=self.hide_on_disk_check.isChecked(),
        )
        empty = self.model.rowCount() == 0
        self.center_stack.setCurrentWidget(self.empty_label if empty else self.list_view)
        self._thumb_timer.start()

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid() or self.model is None:
            self._update_preview(None)
            return
        f = self.model.data(current, PathRole)
        self._update_preview(f)

    def _update_preview(self, f: RecoveredFile | None) -> None:
        if f is None:
            self.preview_panel.show_item(None, None)
            return
        meta_lines = [f"Offset: {f.offset:,}" if f.offset is not None else "Offset: unknown"]
        if f.integrity != Integrity.UNKNOWN:
            line = f"Integrity: {f.integrity.value.capitalize()}"
            if f.integrity_reason:
                line += f" — {f.integrity_reason}"
            meta_lines.append(line)
        if f.also_exists:
            meta_lines.append("Already exists on the source volume")
        self.preview_panel.show_item(
            f.path, f.category, name=f.name, size_text=human_size(f.size), meta_lines=meta_lines
        )

    def _on_thumb_ready(self, path_str: str, cache_path) -> None:
        if self.model is None or cache_path is None:
            return
        pixmap = QPixmap(str(cache_path))
        if not pixmap.isNull():
            self.model.set_thumbnail(path_str, pixmap)

    def _on_thumb_progress(self, done: int, total: int) -> None:
        self.thumb_progress_label.setText(f"Thumbnails {done:,} / {total:,}")

    def _request_visible_thumbnails(self) -> None:
        """Prioritises the thumbnail queue for whatever's currently on screen, same
        viewport-first approach as media_results_page.py's _request_visible_thumbnails."""
        if self.model is None or self.model.rowCount() == 0:
            return
        viewport = self.list_view.viewport()
        rect = viewport.rect()
        start_index = self.list_view.indexAt(rect.topLeft())
        end_index = self.list_view.indexAt(rect.bottomRight())
        start_row = start_index.row() if start_index.isValid() else 0
        end_row = end_index.row() if end_index.isValid() else self.model.rowCount() - 1
        margin = 40
        start_row = max(0, start_row - margin)
        end_row = min(self.model.rowCount() - 1, end_row + margin)
        visible_paths = []
        for row in range(start_row, end_row + 1):
            f = self.model.data(self.model.index(row), PathRole)
            if f is None or f.category != "image":
                continue
            if str(f.path) in self.model._thumbs:
                continue
            visible_paths.append(f.path)
            cached = thumbcache.get(f.path)
            if cached is not None:
                pixmap = QPixmap(str(cached))
                if not pixmap.isNull():
                    self.model.set_thumbnail(str(f.path), pixmap)
        if visible_paths:
            self.thumb_service.prioritize(visible_paths)

    def _go_back(self) -> None:
        self.thumb_service.cancel()
        self.preview_panel.release()
        self.controller.go_to_options(self.controller.session.device)

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
