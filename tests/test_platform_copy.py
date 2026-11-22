"""User-facing copy and system-drive behaviour must match the OS the app is running on.

Found from a screenshot of the installed app on a real Windows 11 runner: the source page
said "This Mac" / "Photos library, Messages" on Windows, and selecting the system drive
would have disabled Continue, blocking Windows users from scanning C: at all.
"""
import pytest

from salvage.engine.models import Device
from salvage.ui import format_utils


@pytest.fixture
def platform(monkeypatch):
    def set_mac(is_mac: bool):
        monkeypatch.setattr(format_utils, "is_mac", lambda: is_mac)
        # source_page imported the name directly, so patch it there too.
        import salvage.ui.source_page as sp
        monkeypatch.setattr(sp, "is_mac", lambda: is_mac)
    return set_mac


def test_copy_names_the_right_machine(platform):
    platform(True)
    assert format_utils.this_computer() == "this Mac"
    assert "Photos library" in format_utils.media_search_blurb()
    platform(False)
    assert format_utils.this_computer() == "this PC"
    blurb = format_utils.media_search_blurb()
    assert "OneDrive" in blurb and "Messages" not in blurb and "Photos library" not in blurb


def _page_with_system_drive(qtbot_app):
    from salvage.engine.fake import FakeEngine
    from salvage.ui.app import MainWindow
    w = MainWindow(True, FakeEngine())
    page = w.source_page
    sysdev = next(r.device for r in page._rows if r.device.is_system)
    return w, page, sysdev


def test_windows_system_drive_is_a_caution_not_a_block(platform):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    platform(False)
    w, page, sysdev = _page_with_system_drive(None)
    page._update_system_disk_warning(sysdev)
    assert page.continue_btn.isEnabled(), "Windows users must be able to scan their system drive"
    text = page.system_disk_warning.text()
    assert "Windows" in text
    for macos_only in ("FileVault", "APFS", "Time Machine", "Full Disk Access"):
        assert macos_only not in text


def test_mac_system_drive_still_blocks_with_mac_wording(platform):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    platform(True)
    w, page, sysdev = _page_with_system_drive(None)
    page._update_system_disk_warning(sysdev)
    assert not page.continue_btn.isEnabled()
    assert "startup disk" in page.system_disk_warning.text()
