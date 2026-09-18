"""Tests for the media-export error handling fix (docs/ux-review.md finding 2.4):
a single unreadable item (e.g. an un-downloaded iCloud placeholder) must not abort
the whole export, and export must not block the UI thread.

media_facade.export() is plain Python (no Qt), tested directly. MediaExportWorker
is a QThread; QThread.start()/wait() work without a running Qt event loop or a
display, so these run offscreen with no QT_QPA_PLATFORM juggling needed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from salvage.engine.local_media import FoundMedia
from salvage.ui import media_facade


def _item(tmp_path: Path, name: str, exists: bool, *, cloud_placeholder: bool = False) -> FoundMedia:
    path = tmp_path / "src" / name
    if exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content")
    return FoundMedia(
        path=path,
        name=name,
        ext="jpg",
        size=7,
        category="image",
        source_key="t",
        taken=datetime(2020, 1, 1),
        modified=datetime(2020, 1, 1),
        sha256=None,
        duplicate_of=None,
        cloud_placeholder=cloud_placeholder,
    )


def test_export_skips_unreadable_item_and_continues(tmp_path):
    good = _item(tmp_path, "good.jpg", exists=True)
    missing = _item(tmp_path, "missing.jpg", exists=False, cloud_placeholder=True)  # never downloaded

    dest = tmp_path / "export"
    written, failed = media_facade.export([good, missing], dest)

    assert failed == 1
    assert len(written) == 1
    assert written[0].name == "good.jpg"
    assert written[0].read_bytes() == b"content"


def test_export_all_unreadable_reports_all_failed_without_raising(tmp_path):
    missing_a = _item(tmp_path, "a.jpg", exists=False, cloud_placeholder=True)
    missing_b = _item(tmp_path, "b.jpg", exists=False, cloud_placeholder=True)

    written, failed = media_facade.export([missing_a, missing_b], tmp_path / "export")

    assert written == []
    assert failed == 2


def test_export_progress_callback_fires_per_item(tmp_path):
    items = [_item(tmp_path, f"f{i}.jpg", exists=True) for i in range(3)]
    seen: list[tuple[int, int]] = []

    written, failed = media_facade.export(
        items, tmp_path / "export", on_progress=lambda done, total: seen.append((done, total))
    )

    assert failed == 0
    assert len(written) == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]


# ---------------------------------------------------------------------------
# MediaExportWorker (QThread) - runs export off the UI thread
# ---------------------------------------------------------------------------


def test_media_export_worker_runs_off_thread_and_reports_failures(tmp_path):
    from PySide6.QtCore import Qt
    from salvage.ui.workers import MediaExportWorker

    good = _item(tmp_path, "good.jpg", exists=True)
    missing = _item(tmp_path, "missing.jpg", exists=False, cloud_placeholder=True)
    dest = tmp_path / "export"

    result: dict = {}

    def on_finished(written, failed):
        result["written"] = written
        result["failed"] = failed

    worker = MediaExportWorker([good, missing], dest)
    # DirectConnection: the queued (default) delivery needs the *calling* thread's
    # event loop pumping, which this plain QThread test never starts - invoke the
    # slot straight from the worker thread instead, same as the assertion below
    # only cares that run() itself did the right thing off the main thread.
    worker.finished_export.connect(on_finished, Qt.ConnectionType.DirectConnection)
    worker.start()
    assert worker.wait(10_000), "MediaExportWorker did not finish in time"

    assert result["failed"] == 1
    assert len(result["written"]) == 1
    assert result["written"][0].name == "good.jpg"
