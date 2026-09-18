"""About Salvage / Licences windows.

Satisfies the "visible attribution" legal requirement in docs/legal-compliance.md
and the exact spec in docs/release-checklist.md section 3: version + copyright +
Salvage's own MIT licence in an About panel, and full third-party licence texts +
the GPL written source offer in a separate Licences screen.

`packaging/salvage.spec` already bundles LICENSE and packaging/THIRD_PARTY.md
verbatim as app resources for exactly this (under sys._MEIPASS/licenses/ when
frozen). Read them from there at runtime, falling back to the source tree when
running unfrozen (`python -m salvage`) where sys._MEIPASS doesn't exist.
"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

# pyproject.toml is the single source of truth for both, same convention
# packaging/salvage.spec already uses (it reads version via tomllib at build time).
_FALLBACK_VERSION = "0.1.0"
_FALLBACK_DESCRIPTION = "Free, open-source file recovery desktop app built on PhotoRec"
_COPYRIGHT = "Copyright © 2026 Jay Rowland"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def app_version() -> str:
    """Installed package version when available (works from source in this repo's
    own venv, and in a frozen build since PyInstaller's hook records it too), else
    parses pyproject.toml directly so this never silently shows a stale number."""
    try:
        return metadata.version("salvage")
    except metadata.PackageNotFoundError:
        pass
    try:
        import tomllib

        with open(_repo_root() / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return _FALLBACK_VERSION


def app_description() -> str:
    try:
        return metadata.metadata("salvage")["Summary"] or _FALLBACK_DESCRIPTION
    except metadata.PackageNotFoundError:
        pass
    try:
        import tomllib

        with open(_repo_root() / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["description"]
    except Exception:
        return _FALLBACK_DESCRIPTION


def _bundled_licenses_dir() -> Path | None:
    """Where packaging/salvage.spec puts LICENSE and THIRD_PARTY.md when frozen."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) / "licenses" if meipass else None


def _read_text_with_fallback(bundled_name: str, source_path: Path) -> str:
    bundled_dir = _bundled_licenses_dir()
    if bundled_dir is not None:
        candidate = bundled_dir / bundled_name
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            pass  # fall through to the source-tree copy below
    try:
        return source_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not load {bundled_name}: {exc}"


def salvage_license_text() -> str:
    return _read_text_with_fallback("LICENSE", _repo_root() / "LICENSE")


def third_party_notices_text() -> str:
    return _read_text_with_fallback("THIRD_PARTY.md", _repo_root() / "packaging" / "THIRD_PARTY.md")


class LicencesDialog(QDialog):
    """Full licence texts, per docs/release-checklist.md section 3: Salvage's own
    MIT licence and the third-party notices (with the GPL written source offer),
    each scrollable and rendered as plain monospace text - "no need for anything
    fancier" per that spec."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Licences")
        self.resize(720, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)

        salvage_tab = QPlainTextEdit(self)
        salvage_tab.setReadOnly(True)
        salvage_tab.setPlainText(salvage_license_text())
        salvage_tab.setStyleSheet("font-family: Menlo, Consolas, monospace;")
        tabs.addTab(salvage_tab, "Salvage (MIT)")

        third_party_tab = QPlainTextEdit(self)
        third_party_tab.setReadOnly(True)
        third_party_tab.setPlainText(third_party_notices_text())
        third_party_tab.setStyleSheet("font-family: Menlo, Consolas, monospace;")
        tabs.addTab(third_party_tab, "Third-Party Notices")

        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    """About Salvage: name, version, one-line description, copyright, and a way
    into the full licence texts - docs/release-checklist.md section 3."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Salvage")
        self.setFixedWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(6)

        name_label = QLabel("Salvage")
        name_label.setProperty("role", "heading")
        layout.addWidget(name_label)

        version_label = QLabel(f"Version {app_version()}")
        version_label.setProperty("role", "subheading")
        layout.addWidget(version_label)

        layout.addSpacing(8)

        description_label = QLabel(app_description())
        description_label.setWordWrap(True)
        layout.addWidget(description_label)

        layout.addSpacing(8)

        copyright_label = QLabel(_COPYRIGHT + ". MIT licensed.")
        copyright_label.setProperty("role", "subheading")
        copyright_label.setWordWrap(True)
        layout.addWidget(copyright_label)

        layout.addSpacing(12)

        button_row = QHBoxLayout()
        licences_btn = QPushButton("View Licences…", self)
        licences_btn.clicked.connect(self._open_licences)
        button_row.addWidget(licences_btn)
        button_row.addStretch()
        close_btn = QPushButton("Close", self)
        close_btn.setProperty("role", "primary")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    def _open_licences(self) -> None:
        LicencesDialog(self).exec()
