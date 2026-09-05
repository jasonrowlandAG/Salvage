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
    color: #8a6d1d;
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

QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #c7c7cc;
    border-radius: 6px;
    padding: 5px 8px;
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
"""
