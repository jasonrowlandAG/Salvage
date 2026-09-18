"""A small, light-theme stylesheet for the whole app."""

STYLESHEET = """
* {
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    color: #1d1d1f;
}

QMainWindow, QWidget {
    background: #f5f5f7;
}

QLabel[role="heading"] {
    font-size: 20px;
    font-weight: 600;
    color: #111111;
    background: transparent;
}

QLabel[role="subheading"] {
    font-size: 13px;
    color: #6e6e73;
    background: transparent;
}

QLabel[role="error"] {
    color: #c0392b;
    background: transparent;
}

QLabel[role="badge"] {
    background: #f0e6c8;
    color: #7f641b;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 11px;
}

QFrame[role="card"] {
    background: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 8px;
}

QFrame[role="card"][selected="true"] {
    border: 2px solid #0a72e8;
    background: #eef5ff;
}

QFrame[role="panel"] {
    background: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 8px;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #c7c7cc;
    border-radius: 6px;
    padding: 6px 14px;
}

QPushButton:hover {
    background: #f0f0f2;
}

QPushButton:disabled {
    color: #a1a1a6;
    background: #f5f5f7;
}

QPushButton[role="primary"] {
    background: #0a72e8;
    color: #ffffff;
    border: none;
    font-weight: 600;
    padding: 8px 20px;
}

QPushButton[role="primary"]:hover {
    background: #085fc4;
}

QPushButton[role="primary"]:disabled {
    background: #a9cdf4;
    color: #eef5ff;
}

/* Text-selection colour is pinned to the brand blue below (selection-background-
   color/selection-color) - it otherwise follows the OS accent colour (confirmed
   rendering in system red on a Mac set to a non-blue accent, docs/ux-review.md
   finding 3.6), which fights every hardcoded brand colour elsewhere in the app. */
QLineEdit, QComboBox, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #c7c7cc;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #0a72e8;
    selection-color: #ffffff;
}

QListView {
    background: #ffffff;
    border: 1px solid #dcdce0;
    border-radius: 8px;
}

QListWidget {
    background: transparent;
    border: none;
}

QProgressBar {
    border: 1px solid #c7c7cc;
    border-radius: 6px;
    background: #ffffff;
    text-align: center;
    height: 18px;
}

QProgressBar::chunk {
    background-color: #0a72e8;
    border-radius: 6px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QCheckBox, QRadioButton {
    spacing: 8px;
}

/* Same accent-colour leak as the text selection above, on the checkbox/radio
   glyph itself (FileTileDelegate's custom-painted grid-tile checkbox has its own,
   separate fix in results_page.py - this covers every real QCheckBox/QRadioButton
   widget: the filter checkboxes, "Delete scan working files...", etc). Solid-fill
   rather than a checkmark glyph - simplest reliable look given ":focus" on the
   button-family widgets already broke in this Qt/PySide6 build (see below), and a
   colour-filled indicator is still unambiguous next to its own label text. */
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1.5px solid #8e8e93;
    border-radius: 3px;
    background: #ffffff;
}

QRadioButton::indicator {
    border-radius: 8px;
}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: #0a72e8;
    border: 1.5px solid #0a72e8;
}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border: 1.5px solid #0a72e8;
}

/* Selected-tab colour, same accent-leak fix (observed red on the Licences
   window's tabs). */
QTabWidget::pane {
    border: 1px solid #dcdce0;
    border-radius: 6px;
    top: -1px;
}

QTabBar::tab {
    background: #f0f0f2;
    border: 1px solid #dcdce0;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 14px;
    margin-right: 2px;
}

QTabBar::tab:selected {
    background: #0a72e8;
    color: #ffffff;
}

QTabBar::tab:!selected:hover {
    background: #e4e4e7;
}

/* QPushButton/QComboBox are deliberately excluded here: in this Qt/PySide6 build,
   any ":focus" rule on those two widget classes (outline or border, tested both)
   makes the widget's own text label disappear while focused - confirmed with an
   isolated repro, worse than having no focus indicator at all. Left as a documented
   follow-up (needs a QProxyStyle-based focus painter, not a QSS tweak) - see
   docs/ux-review.md section 6. */
QLineEdit:focus, QCheckBox:focus, QRadioButton:focus, QListView:focus, QListWidget:focus {
    border: 2px solid #0a72e8;
}
"""
