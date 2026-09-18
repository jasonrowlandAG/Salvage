"""Unit tests for the QThread workers in salvage/ui/workers.py.

Runs offscreen (no real window needed) -- see tests/test_preview_panel.py for
the same pattern. Workers are exercised by calling run() directly (not
start()) so each test is synchronous and doesn't need a Qt event loop.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.ui import media_facade  # noqa: E402
from salvage.ui.workers import MediaScanWorker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_media_scan_worker_survives_unexpected_exception(app, monkeypatch):
    # Every sibling worker in workers.py (ScanWorker, RecoverWorker,
    # IOSBackupWorker, IOSParseWorker) wraps its run() body in a broad
    # try/except so an unexpected exception surfaces as a signal instead of
    # silently killing the QThread. MediaScanWorker was missing that guard --
    # a single hostile file (e.g. the deeply-nested-moov RecursionError in
    # local_media.py) would previously propagate straight out of run() with
    # no signal ever emitted, leaving a progress dialog waiting forever.
    def _boom(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(media_facade, "scan", _boom)

    worker = MediaScanWorker(fake=False, sources=[], min_size=0, hash_dupes=False)

    failures: list[str] = []
    completions: list[object] = []
    worker.failed.connect(failures.append)
    worker.finished_scan.connect(completions.append)

    worker.run()  # synchronous - no real thread, no event loop needed

    assert failures, "failed signal was never emitted for an unexpected exception"
    assert "maximum recursion depth exceeded" in failures[0]
    # finished_scan must still fire so a caller only waiting on it (today's
    # media scan page) isn't left with a dialog that never closes.
    assert completions == [[]]


def test_media_scan_worker_emits_results_on_success(app, monkeypatch):
    monkeypatch.setattr(media_facade, "scan", lambda *a, **k: ["fake-result"])

    worker = MediaScanWorker(fake=False, sources=[], min_size=0, hash_dupes=False)

    failures: list[str] = []
    completions: list[object] = []
    worker.failed.connect(failures.append)
    worker.finished_scan.connect(completions.append)

    worker.run()

    assert failures == []
    assert completions == [["fake-result"]]
