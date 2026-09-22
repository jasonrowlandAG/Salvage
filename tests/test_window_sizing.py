"""Window minimum-size regression test (docs/ux-review.md finding 6.4): every page
used to share one QStackedWidget whose minimum size was the *largest* page's, not
the visible one's, so the window couldn't shrink below ~1200-1360px on ANY page,
including simple ones like Source. _CurrentPageStackedWidget (salvage/ui/app.py)
fixes this by deriving the stack's size hints from only the current widget.

Runs offscreen - no real window needed, just layout geometry.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# macOS is the v0.1 launch target; Ubuntu CI validates the same Qt offscreen
# sizing checks on Linux font metrics. Windows still reports wider minimum
# widths on a few wizard pages (e.g. ios_options_page) and is tracked
# separately until the Windows installer ships.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="minimum window width is validated on macOS/Linux CI pre-Windows launch",
)

from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.ui import engine_facade  # noqa: E402
from salvage.ui.app import MainWindow  # noqa: E402
from salvage.ui.style import STYLESHEET  # noqa: E402

# The primary target from docs/ux-review.md item 3: every page must be usable here.
_TARGET = (1000, 640)
# A stricter stretch check - most pages should reach this too; Results/MediaResults
# (the two most content-dense pages, each a 3-pane grid+filters+preview layout) are
# allowed a slightly higher natural floor rather than clipping their action buttons
# to force-fit it (see the same finding's own footer-button-clipping history).
_STRETCH = (900, 600)
_ALLOWED_ABOVE_STRETCH = {"results_page", "media_results_page"}
# A few px of slack on the stretch check only, for sub-pixel font-metric rounding
# once the real stylesheet's padding/spacing is applied (matches production -
# __main__.py always calls app.setStyleSheet(STYLESHEET)). The 1000x640 primary
# target above gets none - that one's the actual requirement.
_STRETCH_TOLERANCE = 6


def _app() -> QApplication:
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(STYLESHEET)
    return application


def _all_pages(window: MainWindow) -> list[tuple[str, object]]:
    return [
        ("source_page", window.source_page),
        ("options_page", window.options_page),
        ("scan_page", window.scan_page),
        ("results_page", window.results_page),
        ("done_page", window.done_page),
        ("ios_options_page", window.ios_options_page),
        ("ios_backup_page", window.ios_backup_page),
        ("ios_results_page", window.ios_results_page),
        ("media_options_page", window.media_options_page),
        ("media_scan_page", window.media_scan_page),
        ("media_results_page", window.media_results_page),
    ]


def test_every_page_reaches_1000x640():
    app = _app()
    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    window.show()
    try:
        for name, page in _all_pages(window):
            window.stack.setCurrentWidget(page)
            app.processEvents()
            hint = window.stack.minimumSizeHint()
            assert hint.width() <= _TARGET[0], f"{name}: stack minimum width {hint.width()} > {_TARGET[0]}"
            assert hint.height() <= _TARGET[1], f"{name}: stack minimum height {hint.height()} > {_TARGET[1]}"
    finally:
        window.close()


def test_most_pages_reach_900x600():
    app = _app()
    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    window.show()
    try:
        for name, page in _all_pages(window):
            window.stack.setCurrentWidget(page)
            app.processEvents()
            hint = window.stack.minimumSizeHint()
            if name in _ALLOWED_ABOVE_STRETCH:
                # Still must be well under the old ~1200-1360px floor, and under the
                # 1000x640 primary target checked above - just not necessarily 900.
                assert hint.width() <= _TARGET[0], (
                    f"{name}: stack minimum width {hint.width()} regressed toward the old floor"
                )
                continue
            assert hint.width() <= _STRETCH[0] + _STRETCH_TOLERANCE, (
                f"{name}: stack minimum width {hint.width()} > {_STRETCH[0]}"
            )
            assert hint.height() <= _STRETCH[1] + _STRETCH_TOLERANCE, (
                f"{name}: stack minimum height {hint.height()} > {_STRETCH[1]}"
            )
    finally:
        window.close()


def test_stack_minimum_follows_current_page_not_the_widest_page_ever_shown():
    """The actual bug from finding 6.4: visiting a wide page (Results) must not
    permanently inflate the minimum size reported while looking at a narrow one
    (Source) afterwards."""
    app = _app()
    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    window.show()
    try:
        window.stack.setCurrentWidget(window.source_page)
        app.processEvents()
        source_only_hint = window.stack.minimumSizeHint()

        window.stack.setCurrentWidget(window.results_page)
        app.processEvents()
        results_hint = window.stack.minimumSizeHint()
        assert results_hint.width() > source_only_hint.width()

        window.stack.setCurrentWidget(window.source_page)
        app.processEvents()
        source_after_hint = window.stack.minimumSizeHint()
        assert source_after_hint.width() == source_only_hint.width()
        assert source_after_hint.width() < results_hint.width()
    finally:
        window.close()


def test_window_resizes_to_1000x640_on_every_page():
    """End-to-end version of the above: actually resize the real window, not just
    read the size hint, on each page."""
    app = _app()
    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    window.show()
    try:
        for name, page in _all_pages(window):
            window.stack.setCurrentWidget(page)
            app.processEvents()
            window.resize(*_TARGET)
            app.processEvents()
            size = window.size()
            assert size.width() <= _TARGET[0] + 2, f"{name}: window width {size.width()}"
            assert size.height() <= _TARGET[1] + 2, f"{name}: window height {size.height()}"
    finally:
        window.resize(1100, 720)
        window.close()
