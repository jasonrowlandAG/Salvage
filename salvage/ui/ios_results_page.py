"""iPhone wizard step 4: browse parsed Messages/WhatsApp/Contacts/Notes/Photos."""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QAbstractListModel, QAbstractTableModel, QModelIndex, QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.ios_parsers import Call, Contact, Message, Note, TrashedPhoto, Visit
from salvage.ui.ios_facade import IOSParsedData

_MIN_DATE = datetime.min.replace(tzinfo=timezone.utc)


class MessageTableModel(QAbstractTableModel):
    HEADERS = ["Chat", "Sender", "Text", "Date", "Service", "Status"]

    def __init__(self, messages: list[Message], parent=None) -> None:
        super().__init__(parent)
        self._all = sorted(messages, key=lambda m: m.date or _MIN_DATE)
        self._visible: list[int] = list(range(len(self._all)))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        m = self._all[self._visible[index.row()]]
        col = index.column()
        if col == 0:
            return m.chat
        if col == 1:
            return m.sender
        if col == 2:
            return m.text
        if col == 3:
            return m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
        if col == 4:
            return m.service
        if col == 5:
            return "Recently Deleted" if m.deleted else ""
        return None

    def set_filter(self, search: str, only_deleted: bool) -> None:
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, m in enumerate(self._all)
            if (not only_deleted or m.deleted)
            and (
                not needle
                or needle in m.text.lower()
                or needle in m.chat.lower()
                or needle in m.sender.lower()
            )
        ]
        self.endResetModel()


class ContactTableModel(QAbstractTableModel):
    HEADERS = ["Name", "Phones", "Emails", "Organisation", "Status"]

    def __init__(self, contacts: list[Contact], parent=None) -> None:
        super().__init__(parent)
        self._all = contacts
        self._visible: list[int] = list(range(len(contacts)))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        c = self._all[self._visible[index.row()]]
        col = index.column()
        if col == 0:
            return c.name
        if col == 1:
            return ", ".join(c.phones)
        if col == 2:
            return ", ".join(c.emails)
        if col == 3:
            return c.organisation or ""
        if col == 4:
            return "Recovered" if c.deleted else ""
        return None

    def set_filter(self, search: str, only_deleted: bool) -> None:
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, c in enumerate(self._all)
            if (not only_deleted or c.deleted) and (not needle or needle in c.name.lower())
        ]
        self.endResetModel()


class NoteTableModel(QAbstractTableModel):
    HEADERS = ["Title", "Folder", "Modified", "Body", "Status"]

    def __init__(self, notes: list[Note], parent=None) -> None:
        super().__init__(parent)
        self._all = notes
        self._visible: list[int] = list(range(len(notes)))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        n = self._all[self._visible[index.row()]]
        col = index.column()
        if col == 0:
            return n.title or "(untitled)"
        if col == 1:
            return n.folder or ""
        if col == 2:
            return n.modified.strftime("%Y-%m-%d %H:%M") if n.modified else ""
        if col == 3:
            return n.body.replace("\n", " ")[:200]
        if col == 4:
            return "Recovered" if n.deleted else ""
        return None

    def set_filter(self, search: str, only_deleted: bool) -> None:
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, n in enumerate(self._all)
            if (not only_deleted or n.deleted)
            and (not needle or needle in n.title.lower() or needle in n.body.lower())
        ]
        self.endResetModel()


class CallTableModel(QAbstractTableModel):
    HEADERS = ["Number/Address", "Date", "Duration", "Direction", "Answered", "Service"]

    def __init__(self, calls: list[Call], parent=None) -> None:
        super().__init__(parent)
        self._all = sorted(calls, key=lambda c: c.date or _MIN_DATE, reverse=True)
        self._visible: list[int] = list(range(len(self._all)))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        c = self._all[self._visible[index.row()]]
        col = index.column()
        if col == 0:
            return c.address
        if col == 1:
            return c.date.strftime("%Y-%m-%d %H:%M") if c.date else ""
        if col == 2:
            minutes, seconds = divmod(c.duration_s, 60)
            return f"{minutes}:{seconds:02d}"
        if col == 3:
            return "Outgoing" if c.outgoing else "Incoming"
        if col == 4:
            return "Yes" if c.answered else "No"
        if col == 5:
            return c.service
        return None

    def set_filter(self, search: str, only_deleted: bool) -> None:
        # only_deleted has no meaning for call history; kept for _CategoryView's uniform API.
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i for i, c in enumerate(self._all) if not needle or needle in c.address.lower()
        ]
        self.endResetModel()


class SafariHistoryTableModel(QAbstractTableModel):
    HEADERS = ["Title", "URL", "Visited", "Visit count"]

    def __init__(self, visits: list[Visit], parent=None) -> None:
        super().__init__(parent)
        self._all = sorted(visits, key=lambda v: v.date or _MIN_DATE, reverse=True)
        self._visible: list[int] = list(range(len(self._all)))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._visible)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        v = self._all[self._visible[index.row()]]
        col = index.column()
        if col == 0:
            return v.title
        if col == 1:
            return v.url
        if col == 2:
            return v.date.strftime("%Y-%m-%d %H:%M") if v.date else ""
        if col == 3:
            return v.visit_count
        return None

    def set_filter(self, search: str, only_deleted: bool) -> None:
        # only_deleted has no meaning for Safari history; kept for _CategoryView's API.
        self.beginResetModel()
        needle = search.lower().strip()
        self._visible = [
            i
            for i, v in enumerate(self._all)
            if not needle or needle in v.title.lower() or needle in v.url.lower()
        ]
        self.endResetModel()


class PhotoGridModel(QAbstractListModel):
    def __init__(self, photos: list[TrashedPhoto], parent=None) -> None:
        super().__init__(parent)
        self._photos = photos
        self._pixmaps: dict[int, QPixmap] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._photos)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self._photos[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return p.filename
        if role == Qt.ItemDataRole.DecorationRole:
            row = index.row()
            if row not in self._pixmaps:
                pix = QPixmap()
                if p.thumbnail_path is not None:
                    pix.load(str(p.thumbnail_path))
                self._pixmaps[row] = pix
            return self._pixmaps[row]
        return None


class _CategoryView(QWidget):
    """A filter box + 'show only deleted' toggle over a QTableView."""

    def __init__(self, model, deleted_label: str | None) -> None:
        super().__init__()
        self._model = model
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter…")
        self.search_edit.textChanged.connect(self._apply_filter)
        controls.addWidget(self.search_edit, 1)
        self.deleted_check = QCheckBox(f"Show only {deleted_label}" if deleted_label else "")
        self.deleted_check.stateChanged.connect(self._apply_filter)
        self.deleted_check.setVisible(deleted_label is not None)
        controls.addWidget(self.deleted_check)
        layout.addLayout(controls)

        self.table = QTableView()
        self.table.setModel(model)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        for col in range(model.columnCount()):
            self.table.setColumnWidth(col, 160)
        layout.addWidget(self.table, 1)

    def _apply_filter(self) -> None:
        self._model.set_filter(self.search_edit.text(), self.deleted_check.isChecked())


class IOSResultsPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self._data: IOSParsedData | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        heading = QLabel("iPhone recovery results")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        body = QHBoxLayout()
        body.setSpacing(16)

        self.category_list = QListWidget()
        self.category_list.setMinimumWidth(150)
        self.category_list.setMaximumWidth(220)
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        body.addWidget(self.category_list)

        self.center_stack = QStackedWidget()
        body.addWidget(self.center_stack, 1)

        outer.addLayout(body, 1)

        footer_row = QHBoxLayout()
        self.footer_label = QLabel("")
        self.footer_label.setProperty("role", "subheading")
        footer_row.addWidget(self.footer_label)
        footer_row.addStretch()
        back_btn = QPushButton("Back")
        back_btn.clicked.connect(self.controller.go_to_source)
        footer_row.addWidget(back_btn)
        self.export_btn = QPushButton("Export all")
        self.export_btn.setProperty("role", "primary")
        self.export_btn.clicked.connect(self._export_all)
        footer_row.addWidget(self.export_btn)
        outer.addLayout(footer_row)

    def set_data(self, data: IOSParsedData) -> None:
        self._data = data
        self.category_list.clear()
        while self.center_stack.count():
            w = self.center_stack.widget(0)
            self.center_stack.removeWidget(w)
            w.deleteLater()

        if data.messages:
            deleted = sum(1 for m in data.messages if m.deleted)
            self._add_tab(
                f"Messages ({len(data.messages):,} · {deleted:,} Recently Deleted)",
                _CategoryView(MessageTableModel(data.messages), "Recently Deleted"),
            )
        if data.whatsapp:
            self._add_tab(
                f"WhatsApp ({len(data.whatsapp):,})",
                _CategoryView(MessageTableModel(data.whatsapp), "Recently Deleted"),
            )
        if data.contacts:
            deleted = sum(1 for c in data.contacts if c.deleted)
            self._add_tab(
                f"Contacts ({len(data.contacts):,} · {deleted:,} recovered)",
                _CategoryView(ContactTableModel(data.contacts), "recovered"),
            )
        if data.notes:
            deleted = sum(1 for n in data.notes if n.deleted)
            self._add_tab(
                f"Notes ({len(data.notes):,} · {deleted:,} recovered)",
                _CategoryView(NoteTableModel(data.notes), "recovered"),
            )
        if data.calls:
            self._add_tab(
                f"Calls ({len(data.calls):,})",
                _CategoryView(CallTableModel(data.calls), None),
            )
        if data.safari_history:
            self._add_tab(
                f"Safari history ({len(data.safari_history):,})",
                _CategoryView(SafariHistoryTableModel(data.safari_history), None),
            )
        if data.trashed_photos:
            self._add_tab(
                f"Recently Deleted photos ({len(data.trashed_photos):,})",
                self._build_photo_grid(data.trashed_photos),
            )

        if self.category_list.count():
            self.category_list.setCurrentRow(0)

        total = (
            len(data.messages)
            + len(data.whatsapp)
            + len(data.contacts)
            + len(data.notes)
            + len(data.calls)
            + len(data.safari_history)
            + len(data.trashed_photos)
        )
        self.footer_label.setText(f"{total:,} items recovered")

    def _add_tab(self, label: str, widget: QWidget) -> None:
        self.category_list.addItem(QListWidgetItem(label))
        self.center_stack.addWidget(widget)

    def _build_photo_grid(self, photos: list[TrashedPhoto]) -> QWidget:
        view = QListView()
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setIconSize(QSize(120, 120))
        view.setGridSize(QSize(140, 150))
        view.setSpacing(8)
        view.setModel(PhotoGridModel(photos))
        return view

    def _on_category_changed(self, row: int) -> None:
        if row >= 0:
            self.center_stack.setCurrentIndex(row)

    def _export_all(self) -> None:
        if self._data is None:
            return
        self.controller.export_ios_results(self._data)
