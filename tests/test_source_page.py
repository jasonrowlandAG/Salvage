"""Tests for the system-disk warning accuracy fix (docs/ux-review.md finding 2.1 /
Top-10 #5): the FileVault-off message used to imply that granting Full Disk Access
would let Salvage scan the startup disk. README.md's "Honest limits" section
documents that the kernel refuses raw reads of any *mounted* volume on a running
Apple Silicon Mac regardless of FileVault or Full Disk Access - no permission grant
changes that outcome, so the message must not suggest one does, and Continue must
stay disabled either way rather than only when FileVault happens to be on.

Runs offscreen.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.engine.models import Device  # noqa: E402
from salvage.ui import engine_facade  # noqa: E402
from salvage.ui.app import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def source_page(app):
    engine, _warning, _needs_dialog = engine_facade.make_engine(True)
    window = MainWindow(True, engine)
    try:
        yield window.source_page
    finally:
        window.close()


def _system_disk() -> Device:
    return Device(
        id="disk0", path="/dev/disk0", name="Macintosh HD", size_bytes=1_000_000,
        kind="disk", is_removable=False, is_system=True,
    )


@pytest.mark.parametrize("filevault_on", [True, False])
def test_continue_disabled_for_system_disk_regardless_of_filevault(source_page, monkeypatch, filevault_on):
    monkeypatch.setattr(engine_facade, "filevault_enabled", lambda fake: filevault_on)
    source_page._update_system_disk_warning(_system_disk())
    assert source_page.continue_btn.isEnabled() is False
    assert not source_page.system_disk_warning.isHidden()


@pytest.mark.parametrize("filevault_on", [True, False])
def test_warning_text_never_implies_full_disk_access_enables_scanning(source_page, monkeypatch, filevault_on):
    monkeypatch.setattr(engine_facade, "filevault_enabled", lambda fake: filevault_on)
    source_page._update_system_disk_warning(_system_disk())
    text = source_page.system_disk_warning.text()
    # The old FileVault-off copy said "...unless Full Disk Access is granted..." -
    # any wording implying FDA is a path to scanning the startup disk must be gone.
    assert "unless Full Disk Access" not in text
    assert "no matter what permissions" in text
    # Must still point somewhere useful instead.
    assert "Recently Deleted" in text
    assert "Time Machine" in text


def test_warning_hidden_and_continue_enabled_for_non_system_device(source_page, monkeypatch):
    monkeypatch.setattr(engine_facade, "filevault_enabled", lambda fake: False)
    removable = Device(
        id="disk1", path="/dev/disk1", name="SanDisk Ultra", size_bytes=1000,
        kind="partition", is_removable=True, is_system=False,
    )
    source_page._update_system_disk_warning(removable)
    assert source_page.continue_btn.isEnabled() is True
    assert source_page.system_disk_warning.isHidden()
