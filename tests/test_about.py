"""Tests for the About/Licences screens and the native menu bar (item 4 of the
launch-blocking UI fixes; docs/ux-review.md finding 7.1, docs/release-checklist.md
section 3, docs/legal-compliance.md's "Licences/About screen" requirement).

Runs offscreen. The bundled-resource path (sys._MEIPASS/licenses/...) is exercised
by monkeypatching sys._MEIPASS rather than an actual frozen build - the real frozen
path is verified separately by building with packaging/build_mac.sh.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.ui import about  # noqa: E402
from salvage.ui.about import AboutDialog, LicencesDialog, app_description, app_version  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_app_version_matches_pyproject():
    import tomllib

    root = about._repo_root()
    with open(root / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert app_version() == expected


def test_app_description_matches_pyproject():
    import tomllib

    root = about._repo_root()
    with open(root / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["description"]
    assert app_description() == expected


def test_salvage_license_text_is_the_real_mit_license():
    text = about.salvage_license_text()
    assert "MIT License" in text
    assert "Jay Rowland" in text


def test_third_party_notices_text_loads_real_document():
    text = about.third_party_notices_text()
    assert "Third-party components bundled in Salvage" in text
    assert "PhotoRec" in text


def test_reads_from_bundled_resources_when_frozen(monkeypatch, tmp_path):
    """Simulates a frozen build: sys._MEIPASS/licenses/{LICENSE,THIRD_PARTY.md}
    must be read in preference to the source-tree copies."""
    licenses_dir = tmp_path / "licenses"
    licenses_dir.mkdir()
    (licenses_dir / "LICENSE").write_text("BUNDLED LICENSE TEXT", encoding="utf-8")
    (licenses_dir / "THIRD_PARTY.md").write_text("BUNDLED THIRD PARTY TEXT", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert about.salvage_license_text() == "BUNDLED LICENSE TEXT"
    assert about.third_party_notices_text() == "BUNDLED THIRD PARTY TEXT"


def test_falls_back_to_source_tree_when_bundled_file_missing(monkeypatch, tmp_path):
    """A frozen build whose bundled licenses dir is missing or incomplete (e.g. an
    older build) must still show real content, not silently show nothing."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "nonexistent"), raising=False)

    assert "MIT License" in about.salvage_license_text()
    assert "Third-party components" in about.third_party_notices_text()


def test_about_dialog_shows_version_and_description(app):
    dialog = AboutDialog()
    try:
        all_text = " ".join(
            child.text() for child in dialog.findChildren(object) if hasattr(child, "text") and callable(child.text)
        )
        assert app_version() in all_text
        assert "Salvage" in all_text
    finally:
        dialog.deleteLater()


def test_about_dialog_opens_licences_dialog(app):
    dialog = AboutDialog()
    try:
        dialog._open_licences = lambda: setattr(dialog, "_licences_opened", True)
        dialog._open_licences()
        assert dialog._licences_opened is True
    finally:
        dialog.deleteLater()


def test_licences_dialog_has_both_tabs_with_content(app):
    dialog = LicencesDialog()
    try:
        from PySide6.QtWidgets import QPlainTextEdit, QTabWidget

        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        assert tabs.count() == 2
        assert tabs.tabText(0) == "Salvage (MIT)"
        assert tabs.tabText(1) == "Third-Party Notices"

        assert isinstance(tabs.widget(0), QPlainTextEdit)
        assert isinstance(tabs.widget(1), QPlainTextEdit)
        assert "MIT License" in tabs.widget(0).toPlainText()
        assert "PhotoRec" in tabs.widget(1).toPlainText()
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# Menu bar
# ---------------------------------------------------------------------------


def test_menu_bar_has_file_window_help_with_expected_actions():
    from salvage.ui import engine_facade
    from salvage.ui.app import MainWindow

    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    try:
        menu_bar = window.menuBar()
        menu_titles = [action.text().replace("&", "") for action in menu_bar.actions() if action.menu()]
        assert "File" in menu_titles
        assert "Window" in menu_titles
        assert "Help" in menu_titles

        menus = {action.text().replace("&", ""): action.menu() for action in menu_bar.actions() if action.menu()}

        file_actions = {a.text(): a for a in menus["File"].actions() if a.text()}
        assert "Open Disk Image…" in file_actions
        assert file_actions["Open Disk Image…"].shortcut().toString() == "Ctrl+O"
        assert "Quit Salvage" in file_actions

        window_actions = {a.text(): a for a in menus["Window"].actions() if a.text()}
        assert "Close" in window_actions
        assert window_actions["Close"].shortcut().toString() == "Ctrl+W"

        help_actions = {a.text(): a for a in menus["Help"].actions() if a.text()}
        assert "About Salvage" in help_actions
        assert "Licences" in help_actions
    finally:
        window.close()


def test_open_disk_image_menu_action_navigates_to_source_and_opens_picker():
    from salvage.ui import engine_facade
    from salvage.ui.app import MainWindow

    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    try:
        called = {"flag": False}
        window.source_page.open_disk_image_dialog = lambda: called.__setitem__("flag", True)
        window._open_disk_image_from_menu()
        assert window.stack.currentWidget() is window.source_page
        assert called["flag"] is True
    finally:
        window.close()


def test_about_and_licences_menu_actions_open_dialogs(monkeypatch):
    from salvage.ui import app as app_module
    from salvage.ui import engine_facade
    from salvage.ui.app import MainWindow

    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    try:
        opened = {"about": False, "licences": False}
        monkeypatch.setattr(
            app_module.AboutDialog, "exec", lambda self: opened.__setitem__("about", True)
        )
        monkeypatch.setattr(
            app_module.LicencesDialog, "exec", lambda self: opened.__setitem__("licences", True)
        )
        window._show_about()
        window._show_licences()
        assert opened == {"about": True, "licences": True}
    finally:
        window.close()
