"""Tests for the purpose-built device icons (docs/ux-review.md finding 3.4 / Top-10
#8): a floppy-disk-ish stock icon represented USB drives and a network-globe stock
icon represented iPhones. device_icon()/phone_icon() replace both, plus give disk
images and SD cards their own look instead of falling through to the internal-disk
icon.

Runs offscreen - icon rendering needs a QApplication but no real window.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.engine.models import Device  # noqa: E402
from salvage.ui.device_icons import device_icon, phone_icon  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _device(**overrides) -> Device:
    base = dict(id="1", path="/dev/disk0", name="Test Disk", size_bytes=1000, kind="disk", is_removable=False)
    base.update(overrides)
    return Device(**base)


def test_internal_disk_icon_is_not_null(app):
    d = _device(is_removable=False)
    assert not device_icon(d).isNull()


def test_external_drive_icon_differs_from_internal(app):
    internal = device_icon(_device(name="Macintosh HD", is_removable=False))
    external = device_icon(_device(name="SanDisk Ultra", kind="partition", is_removable=True))
    internal_bytes = internal.pixmap(28, 28).toImage().bits().tobytes()
    external_bytes = external.pixmap(28, 28).toImage().bits().tobytes()
    assert internal_bytes != external_bytes


def test_sd_card_gets_its_own_icon(app):
    external = device_icon(_device(name="SanDisk Ultra", kind="partition", is_removable=True))
    sd_card = device_icon(_device(name="SD Card Reader", kind="partition", is_removable=True))
    assert external.pixmap(28, 28).toImage().bits().tobytes() != sd_card.pixmap(28, 28).toImage().bits().tobytes()


@pytest.mark.parametrize(
    "name",
    ["SD Card", "microSD", "SDXC Reader", "Built-in SD Card Reader"],
)
def test_sd_card_name_variants_detected(app, name):
    plain = device_icon(_device(name="Generic USB Drive", kind="partition", is_removable=True))
    sd = device_icon(_device(name=name, kind="partition", is_removable=True))
    assert plain.pixmap(28, 28).toImage().bits().tobytes() != sd.pixmap(28, 28).toImage().bits().tobytes()


def test_disk_image_gets_its_own_icon_even_if_not_marked_removable(app):
    """A disk image Device defaults is_removable=False (engine_facade.device_from_image
    never sets it) - kind == "image" must still route to the disk-image icon, not the
    internal-disk one, or a mounted .dmg would look like the startup disk."""
    internal = device_icon(_device(name="Macintosh HD", kind="disk", is_removable=False))
    image = device_icon(_device(name="backup.dmg", kind="image", is_removable=False))
    assert internal.pixmap(28, 28).toImage().bits().tobytes() != image.pixmap(28, 28).toImage().bits().tobytes()


def test_phone_icon_is_not_null_and_differs_from_disk_icons(app):
    phone = phone_icon()
    assert not phone.isNull()
    internal = device_icon(_device(is_removable=False))
    assert phone.pixmap(28, 28).toImage().bits().tobytes() != internal.pixmap(28, 28).toImage().bits().tobytes()
