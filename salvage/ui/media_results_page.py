"""This-Mac media flow, step 3: browse, filter, preview, and select found media.

Reuses FileTileDelegate from results_page.py for the checkbox+thumbnail+name tile look
(results_page.py itself is untouched), adding small corner badges for recovered/deleted/
duplicate items. Thumbnails are requested lazily for the visible viewport range only (plus
a small margin), rather than for every item up front, so the grid stays smooth with tens of
thousands of items — see _request_visible_thumbnails.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QVBoxLayout,
    QWidget,
)

from salvage.engine import thumbcache
from salvage.engine.local_media import FoundMedia
from salvage.ui.format_utils import human_size
from salvage.ui.results_page import FileTileDelegate
from salvage.ui.thumb_service import BackgroundThumbnailService
from salvage.ui.thumbnails import ThumbnailLoader

FoundMediaRole = Qt.ItemDataRole.UserRole + 1
BadgeRole = Qt.ItemDataRole.UserRole + 2

_BADGE_STYLE = {
    "Recovered": ("#0a72e8", "Recovered"),
    "Deleted": ("#c0392b", "Deleted"),
    "Dup": ("#8a6d1d", "Dup"),
    "iCloud": ("#6e6e73", "iCloud"),
}

_ALL = "__all__"


class MediaTileDelegate(FileTileDelegate):
    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        badges = index.data(BadgeRole) or []
        if not badges:
            return
        painter.save()
        rect = option.rect
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


class MediaListModel(QAbstractListModel):
    def __init__(self, items: list[FoundMedia], parent=None) -> None:
        super().__init__(parent)
        self._all = sorted(items, key=lambda f: f.taken or f.modified, reverse=True)
        self._path_to_index = {str(f.path): i for i, f in enumerate(self._all)}
        self._checked: set[int] = set()
        self._visible: list[int] = list(range(len(self._all)))
        self._thumbs: dict[str, QPixmap] = {}
        self._filter_source: str | None = None
        self._filter_year: str | None = None
        self._only_recovered = False
        self._hide_duplicates = True
        self._only_recently_deleted = False
        self._search = ""

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def _item(self, row: int) -> FoundMedia:
        return self._all[self._visible[row]]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        f = self._item(index.row())
        if role == Qt.ItemDataRole.DisplayRole:
            return f.name
        if role == Qt.ItemDataRole.DecorationRole:
            pix = self._thumbs.get(str(f.path))
            if pix is not None:
                return pix
            return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if self._visible[index.row()] in self._checked else Qt.CheckState.Unchecked
        if role == FoundMediaRole:
            return f
        if role == BadgeRole:
            badges = []
            if f.recovered:
                badges.append("Recovered")
            if f.in_recently_deleted:
                badges.append("Deleted")
            if f.duplicate_of is not None:
                badges.append("Dup")
            if f.cloud_placeholder:
                badges.append("iCloud")
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

    def set_filters(
        self,
        *,
        source: str | None,
        year: str | None,
        only_recovered: bool,
        hide_duplicates: bool,
        only_recently_deleted: bool,
        search: str,
    ) -> None:
        self.beginResetModel()
        self._filter_source = source
        self._filter_year = year
        self._only_recovered = only_recovered
        self._hide_duplicates = hide_duplicates
        self._only_recently_deleted = only_recently_deleted
        self._search = search.lower().strip()
        self._visible = [i for i, f in enumerate(self._all) if self._matches(f)]
        self.endResetModel()

    def _matches(self, f: FoundMedia) -> bool:
        if self._filter_source is not None and f.source_key != self._filter_source:
            return False
        if self._filter_year is not None:
            year = str(f.taken.year) if f.taken else "Unknown"
            if year != self._filter_year:
                return False
        if self._only_recovered and not f.recovered:
            return False
        if self._hide_duplicates and f.duplicate_of is not None:
            return False
        if self._only_recently_deleted and not f.in_recently_deleted:
            return False
        if self._search and self._search not in f.name.lower():
            return False
        return True

    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._all:
            counts[f.source_key] = counts.get(f.source_key, 0) + 1
        return counts

    def year_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self._all:
            year = str(f.taken.year) if f.taken else "Unknown"
            counts[year] = counts.get(year, 0) + 1
        return counts

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

    def checked_files(self) -> list[FoundMedia]:
        return [self._all[i] for i in sorted(self._checked)]


class MediaResultsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.model: MediaListModel | None = None
        self._source_labels: dict[str, str] = {}
        self._preview_path: str | None = None
        self.preview_loader = ThumbnailLoader(self)
        self.preview_loader.ready.connect(self._on_preview_ready)
        self.thumb_service = BackgroundThumbnailService(self)
        self.thumb_service.thumb_ready.connect(self._on_bg_thumb_ready)
        self.thumb_service.progress.connect(self._on_thumb_progress)
        self.thumb_service.finished.connect(self._on_thumb_finished)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._request_visible_thumbnails)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("Photos & videos found on this Mac")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        body = QHBoxLayout()
        body.setSpacing(16)

        sidebar = QVBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name…")
        self.search_edit.textChanged.connect(self._apply_filters)
        sidebar.addWidget(self.search_edit)

        self.only_recovered_check = QCheckBox("Only recovered from backups")
        self.only_recovered_check.stateChanged.connect(self._apply_filters)
        sidebar.addWidget(self.only_recovered_check)

        self.hide_dupes_check = QCheckBox("Hide duplicates")
        self.hide_dupes_check.setChecked(True)
        self.hide_dupes_check.stateChanged.connect(self._apply_filters)
        sidebar.addWidget(self.hide_dupes_check)

        self.only_deleted_check = QCheckBox("Only Recently Deleted (Photos)")
        self.only_deleted_check.stateChanged.connect(self._apply_filters)
        sidebar.addWidget(self.only_deleted_check)

        source_label = QLabel("Source")
        source_label.setStyleSheet("font-weight: 600;")
        sidebar.addWidget(source_label)
        self.source_list = QListWidget()
        self.source_list.currentRowChanged.connect(self._apply_filters)
        sidebar.addWidget(self.source_list, 1)

        year_label = QLabel("Year")
        year_label.setStyleSheet("font-weight: 600;")
        sidebar.addWidget(year_label)
        self.year_list = QListWidget()
        self.year_list.currentRowChanged.connect(self._apply_filters)
        sidebar.addWidget(self.year_list, 1)

        sidebar_widget = QWidget()
        sidebar_widget.setLayout(sidebar)
        sidebar_widget.setFixedWidth(220)
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
        self.list_view.setItemDelegate(MediaTileDelegate(self))
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
        self.thumb_progress_label = QLabel("")
        self.thumb_progress_label.setProperty("role", "subheading")
        footer_row.addWidget(self.thumb_progress_label)
        footer_row.addStretch()
        select_all_btn = QPushButton("Select all (filtered)")
        select_all_btn.clicked.connect(self._select_all_filtered)
        footer_row.addWidget(select_all_btn)
        select_none_btn = QPushButton("Select none")
        select_none_btn.clicked.connect(self._select_none)
        footer_row.addWidget(select_none_btn)
        self.export_btn = QPushButton("Export selected")
        self.export_btn.setProperty("role", "primary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export_clicked)
        footer_row.addWidget(self.export_btn)
        center.addLayout(footer_row)

        center_widget = QWidget()
        center_widget.setLayout(center)
        body.addWidget(center_widget, 1)

        self.preview_panel = QFrame()
        self.preview_panel.setProperty("role", "panel")
        self.preview_panel.setFixedWidth(260)
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
        self.preview_taken = QLabel("")
        self.preview_taken.setProperty("role", "subheading")
        preview_layout.addWidget(self.preview_taken)
        self.preview_source = QLabel("")
        self.preview_source.setProperty("role", "subheading")
        preview_layout.addWidget(self.preview_source)
        self.preview_path = QLabel("")
        self.preview_path.setProperty("role", "subheading")
        self.preview_path.setWordWrap(True)
        preview_layout.addWidget(self.preview_path)
        self.preview_note = QLabel("")
        self.preview_note.setWordWrap(True)
        preview_layout.addWidget(self.preview_note)
        preview_layout.addStretch()
        body.addWidget(self.preview_panel)

        outer.addLayout(body, 1)

        bottom_row = QHBoxLayout()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self._go_back)
        bottom_row.addWidget(back_btn)
        bottom_row.addStretch()
        outer.addLayout(bottom_row)

    def set_items(self, items: list[FoundMedia], sources) -> None:
        self._source_labels = {s.key: s.label for s in sources}
        self.model = MediaListModel(items)
        self.model.dataChanged.connect(lambda *_: self._update_footer())
        self.list_view.setModel(self.model)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)

        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.only_recovered_check.blockSignals(True)
        self.only_recovered_check.setChecked(False)
        self.only_recovered_check.blockSignals(False)
        self.hide_dupes_check.blockSignals(True)
        self.hide_dupes_check.setChecked(True)
        self.hide_dupes_check.blockSignals(False)
        self.only_deleted_check.blockSignals(True)
        self.only_deleted_check.setChecked(False)
        self.only_deleted_check.blockSignals(False)

        self._rebuild_sidebars()
        self._apply_filters()
        self._update_preview(None)

        thumbable = [f.path for f in items if not f.cloud_placeholder and f.category in ("image", "video")]
        self.thumb_progress_label.setText(f"Thumbnails 0 / {len(thumbable):,}" if thumbable else "")
        self.thumb_service.start(thumbable)
        self._thumb_timer.start()

    def _go_back(self) -> None:
        self.thumb_service.cancel()
        self.controller.go_to_media_options()

    def _rebuild_sidebars(self) -> None:
        assert self.model is not None
        source_counts = self.model.source_counts()
        total = sum(source_counts.values())
        self.source_list.blockSignals(True)
        self.source_list.clear()
        all_item = QListWidgetItem(f"All ({total:,})")
        all_item.setData(Qt.ItemDataRole.UserRole, _ALL)
        self.source_list.addItem(all_item)
        for key, count in sorted(source_counts.items(), key=lambda kv: -kv[1]):
            label = self._source_labels.get(key, key)
            item = QListWidgetItem(f"{label} ({count:,})")
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.source_list.addItem(item)
        self.source_list.setCurrentRow(0)
        self.source_list.blockSignals(False)

        year_counts = self.model.year_counts()
        self.year_list.blockSignals(True)
        self.year_list.clear()
        year_all_item = QListWidgetItem(f"All ({total:,})")
        year_all_item.setData(Qt.ItemDataRole.UserRole, _ALL)
        self.year_list.addItem(year_all_item)
        for year in sorted(year_counts, key=lambda y: (y == "Unknown", y), reverse=True):
            item = QListWidgetItem(f"{year} ({year_counts[year]:,})")
            item.setData(Qt.ItemDataRole.UserRole, year)
            self.year_list.addItem(item)
        self.year_list.setCurrentRow(0)
        self.year_list.blockSignals(False)

    def _apply_filters(self, *_args) -> None:
        if self.model is None:
            return
        source_item = self.source_list.currentItem()
        year_item = self.year_list.currentItem()
        source = source_item.data(Qt.ItemDataRole.UserRole) if source_item else _ALL
        year = year_item.data(Qt.ItemDataRole.UserRole) if year_item else _ALL
        self.model.set_filters(
            source=None if source == _ALL else source,
            year=None if year == _ALL else year,
            only_recovered=self.only_recovered_check.isChecked(),
            hide_duplicates=self.hide_dupes_check.isChecked(),
            only_recently_deleted=self.only_deleted_check.isChecked(),
            search=self.search_edit.text(),
        )
        empty = self.model.rowCount() == 0
        self.center_stack.setCurrentWidget(self.empty_label if empty else self.list_view)
        self._update_footer()
        self._thumb_timer.start()

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid() or self.model is None:
            self._update_preview(None)
            return
        f = self.model.data(current, FoundMediaRole)
        self._update_preview(f)

    def _update_preview(self, f: FoundMedia | None) -> None:
        if f is None:
            self.preview_image.setText("Select a file to preview it.")
            self.preview_image.setPixmap(QPixmap())
            self.preview_name.setText("")
            self.preview_size.setText("")
            self.preview_taken.setText("")
            self.preview_source.setText("")
            self.preview_path.setText("")
            self.preview_note.setText("")
            self._preview_path = None
            return
        self.preview_name.setText(f.name)
        self.preview_size.setText(human_size(f.size))
        taken = f.taken.strftime("%d/%m/%Y %H:%M") if f.taken else "Unknown date"
        self.preview_taken.setText(f"Taken: {taken}")
        self.preview_source.setText(f"Source: {self._source_labels.get(f.source_key, f.source_key)}")
        self.preview_path.setText(f"Path: {f.path}")
        self.preview_note.setText(f.note or "")
        if f.cloud_placeholder:
            self._preview_path = None
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("Stored in iCloud — open it in Finder to download")
        elif f.category == "image":
            self._preview_path = str(f.path)
            cached = thumbcache.get(f.path)
            if cached is not None:
                pixmap = QPixmap(str(cached))
                if not pixmap.isNull():
                    self.preview_image.setText("")
                    self.preview_image.setPixmap(
                        pixmap.scaled(
                            240, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                        )
                    )
            else:
                self.preview_image.setText("Loading preview…")
                self.preview_image.setPixmap(QPixmap())
            self.preview_loader.request(f.path, size=240)
        else:
            self._preview_path = None
            self.preview_image.setText("No preview available")
            self.preview_image.setPixmap(QPixmap())

    def _on_bg_thumb_ready(self, path_str: str, cache_path) -> None:
        if self.model is None or cache_path is None:
            return
        pixmap = QPixmap(str(cache_path))
        if not pixmap.isNull():
            self.model.set_thumbnail(path_str, pixmap)

    def _on_thumb_progress(self, done: int, total: int) -> None:
        self.thumb_progress_label.setText(f"Thumbnails {done:,} / {total:,}")

    def _on_thumb_finished(self) -> None:
        pass

    def _on_preview_ready(self, path_str: str, pixmap: QPixmap) -> None:
        if path_str != self._preview_path:
            return
        self.preview_image.setText("")
        self.preview_image.setPixmap(
            pixmap.scaled(240, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def _request_visible_thumbnails(self) -> None:
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
            f = self.model.data(self.model.index(row), FoundMediaRole)
            if f is None or f.cloud_placeholder or f.category not in ("image", "video"):
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
        self.export_btn.setEnabled(len(selected) > 0)

    def _export_clicked(self) -> None:
        if self.model is not None:
            self.controller.export_media_results(self.model.checked_files())
