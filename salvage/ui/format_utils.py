"""Small formatting helpers shared across pages."""

from __future__ import annotations

import sys


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_elapsed(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def human_remaining(seconds: float | None) -> str:
    """A deliberately coarse "time left". Scan throughput swings widely (sparse
    regions fly, dense ones crawl), so a to-the-second figure would be false
    precision that users would hold us to. Round to a band instead."""
    if seconds is None:
        return "Estimating time left..."
    if seconds < 60:
        return "About a minute left"
    minutes = seconds / 60
    if minutes < 10:
        return f"About {int(round(minutes))} minutes left"
    if minutes < 60:
        return f"About {int(round(minutes / 5) * 5)} minutes left"
    hours = minutes / 60
    if hours < 2:
        return "Over an hour left"
    return f"About {int(round(hours))} hours left"


def is_mac() -> bool:
    return sys.platform == "darwin"


def this_computer() -> str:
    """"this Mac" / "this PC" - user-facing copy must name the machine it is running on."""
    return "this Mac" if is_mac() else "this PC"


def media_search_blurb() -> str:
    if is_mac():
        return "Search the Photos library, Messages, iCloud Drive, cloud folders and iPhone backups"
    return "Search Pictures, Videos, Documents, Downloads, OneDrive and cloud folders"
