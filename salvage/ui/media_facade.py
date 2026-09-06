"""Resolves the "media on this Mac" search calls for the UI, with fake-mode fallbacks.

Mirrors engine_facade.py's fake/real routing pattern but for local_media.py: source
discovery and scanning route through FakeMediaFinder under SALVAGE_FAKE=1; export always
uses the real export_found, since fake-mode items are backed by real sample files on disk
(same pattern as ios_facade's fake iOS backup, which is genuinely BackupReader-readable).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from salvage.engine.local_media import FoundMedia, MediaSource, ScanStats, export_found


def default_sources(fake: bool) -> list[MediaSource]:
    if fake:
        from salvage.engine.fake import fake_media_sources
        return fake_media_sources()
    from salvage.engine.local_media import default_sources as _default_sources
    return _default_sources()


def scan(
    fake: bool,
    sources: list[MediaSource],
    min_size: int,
    hash_dupes: bool,
    on_progress: Callable[[ScanStats], None] | None = None,
    cancel: threading.Event | None = None,
) -> list[FoundMedia]:
    if fake:
        from salvage.engine.fake import FakeMediaFinder
        return FakeMediaFinder().scan(
            sources, on_progress=on_progress, cancel=cancel, min_size=min_size, hash_dupes=hash_dupes
        )
    from salvage.engine.local_media import scan_sources
    return scan_sources(sources, on_progress=on_progress, cancel=cancel, min_size=min_size, hash_dupes=hash_dupes)


def export(
    items: list[FoundMedia],
    destination: Path,
    on_progress: Callable[[int, int], None] | None = None,
    include_duplicates: bool = False,
) -> list[Path]:
    return export_found(items, destination, on_progress=on_progress, include_duplicates=include_duplicates)
