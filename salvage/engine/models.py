from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

Category = Literal["image", "document", "video", "audio", "archive", "other"]

CATEGORY_BY_EXT: dict[str, Category] = {
    **{e: "image" for e in ("jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "heic", "heif", "webp", "cr2", "nef", "arw", "dng", "raf", "orf", "psd")},
    **{e: "document" for e in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt", "csv", "pages", "numbers", "key", "epub")},
    **{e: "video" for e in ("mp4", "mov", "m4v", "avi", "mkv", "wmv", "mpg", "mpeg", "3gp", "mts", "m2ts", "webm")},
    **{e: "audio" for e in ("mp3", "wav", "aac", "m4a", "flac", "ogg", "wma", "aif", "aiff")},
    **{e: "archive" for e in ("zip", "rar", "7z", "gz", "tar", "bz2", "xz", "dmg", "iso")},
}


def category_for(ext: str) -> Category:
    return CATEGORY_BY_EXT.get(ext.lower().lstrip("."), "other")


class ScanMode(Enum):
    QUICK = "freespace"   # carve only unallocated space (fast; needs a recognised filesystem)
    DEEP = "wholespace"   # carve every sector of the source


@dataclass(frozen=True)
class Device:
    id: str                       # stable identifier, e.g. "disk2s1", "\\\\.\\PhysicalDrive1", or image path
    path: str                     # what to hand to PhotoRec: raw device node or image file path
    name: str                     # human label, e.g. "SanDisk Ultra (TESTVOL)"
    size_bytes: int
    kind: Literal["disk", "partition", "image"]
    is_removable: bool = False
    is_system: bool = False       # holds the running OS — recovering *to* it is fine, scanning it needs care
    filesystem: str | None = None
    mount_point: str | None = None
    parent_id: str | None = None  # partition -> its disk


@dataclass(frozen=True)
class RecoveredFile:
    path: Path                    # where PhotoRec wrote it (inside the scan workdir)
    name: str
    ext: str                      # lowercase, no dot
    size: int
    category: Category
    offset: int | None = None     # byte offset on source, parsed from f<offset>.<ext> when possible


@dataclass
class ScanProgress:
    sector: int = 0
    total_sectors: int = 0
    files_found: int = 0
    elapsed_s: float = 0.0
    phase: str = ""               # free text from PhotoRec, e.g. "Pass 1 - Reading sector"

    @property
    def fraction(self) -> float | None:
        if self.total_sectors <= 0:
            return None
        return min(1.0, self.sector / self.total_sectors)


@dataclass
class ScanResult:
    success: bool
    files: list[RecoveredFile] = field(default_factory=list)
    output_dirs: list[Path] = field(default_factory=list)
    log_text: str = ""
    error: str | None = None
    cancelled: bool = False
