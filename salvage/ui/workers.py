"""QThread workers wrapping the blocking engine calls."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from salvage.engine.ios import BackupPasswordError
from salvage.engine.models import RecoveredFile, ScanMode, ScanProgress, ScanResult
from salvage.ui import engine_facade, ios_facade


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


class IOSBackupWorker(QThread):
    progress = Signal(object)   # BackupProgress
    finished_backup = Signal(object)  # Path
    failed = Signal(str)

    def __init__(self, fake: bool, udid: str, backup_root: Path, parent=None) -> None:
        super().__init__(parent)
        self._fake = fake
        self._udid = udid
        self._backup_root = backup_root
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            backup_dir = ios_facade.create_ios_backup(
                self._fake,
                self._udid,
                self._backup_root,
                on_progress=lambda p: self.progress.emit(p),
                cancel=self.cancel_event,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_backup.emit(backup_dir)


class MediaScanWorker(QThread):
    progress = Signal(object)   # local_media.ScanStats
    finished_scan = Signal(object)  # list[FoundMedia]

    def __init__(self, fake: bool, sources, min_size: int, hash_dupes: bool, parent=None) -> None:
        super().__init__(parent)
        self._fake = fake
        self._sources = sources
        self._min_size = min_size
        self._hash_dupes = hash_dupes
        self.cancel_event = threading.Event()

    def run(self) -> None:
        from salvage.ui import media_facade

        found = media_facade.scan(
            self._fake,
            self._sources,
            self._min_size,
            self._hash_dupes,
            on_progress=lambda s: self.progress.emit(s),
            cancel=self.cancel_event,
        )
        self.finished_scan.emit(found)


class IOSParseWorker(QThread):
    finished_parse = Signal(object)  # ios_facade.IOSParsedData
    failed = Signal(str)
    password_error = Signal(str)  # wrong/missing backup password — distinct from other failures

    def __init__(
        self,
        backup_dir: Path,
        categories: set[str],
        workdir: Path,
        password: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._backup_dir = backup_dir
        self._categories = categories
        self._workdir = workdir
        self._password = password

    def run(self) -> None:
        try:
            data = ios_facade.extract_and_parse_ios(
                self._backup_dir, self._categories, self._workdir, password=self._password
            )
        except BackupPasswordError as exc:
            self.password_error.emit(str(exc))
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_parse.emit(data)
