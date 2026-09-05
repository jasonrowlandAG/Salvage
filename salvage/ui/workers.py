"""QThread workers wrapping the blocking engine calls."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from salvage.engine.models import RecoveredFile, ScanMode, ScanProgress, ScanResult
from salvage.ui import engine_facade


class ScanWorker(QThread):
    progress = Signal(object)   # ScanProgress
    finished_scan = Signal(object)  # ScanResult

    def __init__(self, engine, source: str | Path, workdir: Path, mode: ScanMode,
                 extensions: list[str] | None, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._source = source
        self._workdir = workdir
        self._mode = mode
        self._extensions = extensions
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            result = self._engine.scan(
                self._source,
                self._workdir,
                mode=self._mode,
                extensions=self._extensions,
                on_progress=lambda p: self.progress.emit(p),
                cancel=self.cancel_event,
            )
        except Exception as exc:
            result = ScanResult(success=False, error=str(exc))
        self.finished_scan.emit(result)


class RecoverWorker(QThread):
    progress = Signal(int, int)
    finished_recover = Signal(object)  # list[Path]
    failed = Signal(str)

    def __init__(self, files: list[RecoveredFile], destination: Path, parent=None) -> None:
        super().__init__(parent)
        self._files = files
        self._destination = destination

    def run(self) -> None:
        try:
            written = engine_facade.recover_files(
                self._files,
                self._destination,
                organise_by_category=True,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_recover.emit(written)
