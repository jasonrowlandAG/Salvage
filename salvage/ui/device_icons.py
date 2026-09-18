"""Purpose-built device icons for the source picker.

Replaces Qt's stock icons, which read as the wrong real-world object for two of
these on every platform tested: SP_DriveFDIcon (a floppy disk / generic drive
glyph, depending on style) for USB drives, and SP_DriveNetIcon (a network globe)
for iPhones (docs/ux-review.md finding 3.4 / Top-10 #8). Drawn flat in the app's
own brand blue (style.py's #0a72e8, same as packaging/assets/icon_1024.png's
palette) instead of pulling in new bitmap assets, so they always match the
in-app blue regardless of platform/OS icon theme.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

from salvage.engine.models import Device

_BRAND_BLUE = QColor("#0a72e8")
_SIZE = 28

# Heuristic only - Device has no dedicated "is SD card" field. Matches common
# card names/product lines ("SD Card", "SDXC", "microSD", "Card Reader", ...).
_SD_CARD_HINTS = ("sd card", "sdxc", "sdhc", "microsd", "sd-card", " sd ", "card reader")


def _new_pixmap() -> QPixmap:
    pix = QPixmap(_SIZE, _SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    return pix


def _painter(pix: QPixmap) -> QPainter:
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_BRAND_BLUE)
    return p


def _internal_disk_pixmap() -> QPixmap:
    """A squat drive silhouette with an activity light - reads as "fixed disk"."""
    pix = _new_pixmap()
    p = _painter(pix)
    body = QRectF(3, 7, 22, 15)
    p.drawRoundedRect(body, 3, 3)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(QRectF(6, 18, 11, 2), 1, 1)
    p.drawEllipse(QRectF(20, 18, 2.4, 2.4))
    p.end()
    return pix


def _external_drive_pixmap() -> QPixmap:
    """A drive body plus a small connector nub, distinguishing it from the
    internal-disk glyph at a glance - reads as "something plugged in"."""
    pix = _new_pixmap()
    p = _painter(pix)
    body = QRectF(5, 9, 18, 14)
    p.drawRoundedRect(body, 3, 3)
    p.drawRect(QRectF(10, 4, 8, 6))  # connector, overlapping the top edge
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(QRectF(8, 19, 9, 2), 1, 1)
    p.end()
    return pix


def _sd_card_pixmap() -> QPixmap:
    """Classic SD card silhouette: a cut top-right corner and contact bars."""
    pix = _new_pixmap()
    p = _painter(pix)
    path = QPainterPath()
    path.moveTo(7, 3)
    path.lineTo(17, 3)
    path.lineTo(22, 8)
    path.lineTo(22, 24)
    path.lineTo(7, 24)
    path.closeSubpath()
    p.drawPath(path)
    p.setBrush(QColor("#ffffff"))
    for i in range(3):
        p.drawRect(QRectF(9 + i * 3.5, 5, 2.2, 6))
    p.end()
    return pix


def _disk_image_pixmap() -> QPixmap:
    """The internal-disk silhouette, translucent with a dashed outline, signalling
    "virtual / mounted from a file" rather than real hardware."""
    pix = _new_pixmap()
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QColor(_BRAND_BLUE)
    fill.setAlpha(90)
    p.setBrush(fill)
    dash_pen = p.pen()
    dash_pen.setColor(_BRAND_BLUE)
    dash_pen.setWidthF(1.6)
    dash_pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(dash_pen)
    p.drawRoundedRect(QRectF(4, 8, 20, 14), 3, 3)
    p.end()
    return pix


def _phone_pixmap() -> QPixmap:
    """A phone silhouette (rounded body + home-indicator bar) - unambiguously a
    handset, unlike the network-globe icon it replaces."""
    pix = _new_pixmap()
    p = _painter(pix)
    body = QRectF(8, 2, 12, 24)
    p.drawRoundedRect(body, 3, 3)
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(QRectF(11, 22.5, 6, 1.6), 0.8, 0.8)
    p.end()
    return pix


def _is_sd_card(name: str) -> bool:
    lowered = f" {name.lower()} "
    return any(hint in lowered for hint in _SD_CARD_HINTS)


def device_icon(device: Device) -> QIcon:
    """Icon for a drive/partition/disk-image row on the Source page."""
    if device.kind == "image":
        return QIcon(_disk_image_pixmap())
    if device.is_removable:
        if _is_sd_card(device.name):
            return QIcon(_sd_card_pixmap())
        return QIcon(_external_drive_pixmap())
    return QIcon(_internal_disk_pixmap())


def phone_icon() -> QIcon:
    """Icon for an iPhone/iPad row on the Source page."""
    return QIcon(_phone_pixmap())
