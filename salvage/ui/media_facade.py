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
) -> tuple[list[Path], int]:
    """Copies each item one at a time so a single unreadable file (e.g. an iCloud
    placeholder whose download never completed) can't abort the whole export -
    export_found() itself has no per-file error handling, and previously ran as one
    batch call with nothing catching the FileNotFoundError it can raise mid-loop
    (docs/ux-review.md finding 2.4). Returns (written_paths, failed_count).
    """
    written: list[Path] = []
    failed = 0
    total = len(items)
    for i, item in enumerate(items, start=1):
        try:
            written.extend(export_found([item], destination, include_duplicates=include_duplicates))
        except Exception:
            failed += 1
        if on_progress is not None:
            on_progress(i, total)
    return written, failed
