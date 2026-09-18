"""Tests for the OS-accent-colour-leak fix (docs/ux-review.md finding 3.6): the
grid-tile checkbox (drawn via native QStyle.drawControl) and a QLineEdit's text
selection both followed the *system* accent colour instead of the app's own brand
blue - confirmed rendering in system red on a Mac set to a non-blue accent. Checks
that checked/selected UI renders the pinned brand blue (#0a72e8) regardless of
what the platform theme would otherwise supply.

Runs offscreen. Pixel-sampling based (renders real widgets/delegates to a QPixmap
and reads colours back) rather than parsing the stylesheet string, so it catches
regressions in whichever mechanism actually controls the colour (QSS for real
widgets, hand-painted QPainter calls for the custom delegate).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QLineEdit,
    QRadioButton,
    QStyleOptionViewItem,
)

from salvage.ui.results_page import FileTileDelegate
from salvage.ui.style import STYLESHEET

_BRAND_BLUE = QColor("#0a72e8")


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(STYLESHEET)
    yield application
    # QApplication is a process-wide singleton shared with every other test file in
    # this run - leaving the stylesheet applied here would silently change what
    # widgets look like (and their size hints) in tests that run afterwards.
    application.setStyleSheet("")


def _render(widget, size=(60, 30)) -> QPixmap:
    widget.resize(*size)
    pix = widget.grab()
    return pix


def _center_color(pix: QPixmap) -> QColor:
    img = pix.toImage()
    return QColor(img.pixel(pix.width() // 2, pix.height() // 2))


def _is_close(a: QColor, b: QColor, tol: int = 12) -> bool:
    return abs(a.red() - b.red()) <= tol and abs(a.green() - b.green()) <= tol and abs(a.blue() - b.blue()) <= tol


def test_checked_checkbox_indicator_is_brand_blue(app):
    cb = QCheckBox("test")
    cb.setChecked(True)
    pix = _render(cb, (20, 20))
    # The indicator is drawn at the left edge - sample there, not the widget centre.
    color = QColor(pix.toImage().pixel(8, 10))
    assert _is_close(color, _BRAND_BLUE), f"expected brand blue near {color.name()}"


def test_checked_radio_indicator_is_brand_blue(app):
    rb = QRadioButton("test")
    rb.setChecked(True)
    pix = _render(rb, (20, 20))
    color = QColor(pix.toImage().pixel(8, 10))
    assert _is_close(color, _BRAND_BLUE), f"expected brand blue near {color.name()}"


def test_lineedit_selection_background_is_brand_blue(app):
    le = QLineEdit("hello")
    le.resize(80, 24)
    le.selectAll()
    le.show()
    app.processEvents()
    pix = le.grab()
    # Sample a pixel under the selected text, away from the border.
    color = QColor(pix.toImage().pixel(15, 12))
    assert _is_close(color, _BRAND_BLUE), f"expected brand blue near {color.name()}"
    le.hide()


def _paint_tile_checkbox(checked: bool) -> QPixmap:
    """Renders just FileTileDelegate's checkbox glyph for one tile, checked or not."""
    from unittest.mock import MagicMock

    delegate = FileTileDelegate()
    pix = QPixmap(FileTileDelegate.TILE_SIZE)
    pix.fill(Qt.GlobalColor.white)
    from PySide6.QtGui import QPainter

    painter = QPainter(pix)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, FileTileDelegate.TILE_SIZE.width(), FileTileDelegate.TILE_SIZE.height())

    index = MagicMock(spec=QModelIndex)

    def data(role):
        from salvage.ui.results_page import BadgeRole

        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return "file.jpg"
        if role == BadgeRole:
            return []
        return None

    index.data.side_effect = data
    delegate.paint(painter, option, index)
    painter.end()
    return pix


def test_grid_tile_checkbox_checked_is_brand_blue_not_native_style(app):
    pix = _paint_tile_checkbox(checked=True)
    # Checkbox glyph sits at (rect.x()+6, rect.y()+6, 18, 18) = (6,6)-(24,24).
    # Sample its top-left corner fill, away from the diagonal checkmark stroke
    # (which runs roughly (10,15)->(20,11) and would otherwise land white).
    color = QColor(pix.toImage().pixel(9, 9))
    assert _is_close(color, _BRAND_BLUE), f"expected brand blue near {color.name()}"


def test_grid_tile_checkbox_unchecked_is_not_filled(app):
    pix = _paint_tile_checkbox(checked=False)
    color = QColor(pix.toImage().pixel(9, 9))
    # Unchecked = white fill with a grey outline, not brand blue and not some
    # platform-accent fill either.
    assert not _is_close(color, _BRAND_BLUE)
