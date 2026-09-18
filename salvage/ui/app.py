"""Main window: owns the engine, session state, and page navigation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QMainWindow, QMessageBox, QProgressDialog, QStackedWidget

from salvage.engine.ios import IOSDevice
from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanResult
from salvage.ui import ios_facade, media_facade
from salvage.ui.done_page import DonePage
from salvage.ui.ios_backup_page import IOSBackupPage
from salvage.ui.ios_options_page import IOSOptionsPage
from salvage.ui.ios_results_page import IOSResultsPage
from salvage.ui.media_options_page import MediaOptionsPage
from salvage.ui.media_results_page import MediaResultsPage
from salvage.ui.media_scan_page import MediaScanPage
from salvage.ui.options_page import OptionsPage
from salvage.ui.results_page import ResultsPage
from salvage.ui.scan_page import ScanPage
from salvage.ui.session import ScanSession
from salvage.ui.source_page import SourcePage
from salvage.ui.workers import IOSParseWorker, MediaExportWorker, RecoverWorker


class _CurrentPageStackedWidget(QStackedWidget):
    """A QStackedWidget whose size hints come from the *currently shown* page only.

    Plain QStackedWidget reports the maximum minimumSizeHint() across every page it
    has ever held (so switching pages never resizes the window out from under the
    user) - with 11 very different wizard pages sharing one stack, that pinned the
    window's minimum width to whichever page happens to need the most room (the
    Results page, via its two setFixedWidth side panels) on every OTHER page too,
    so the window couldn't shrink below ~1200-1360px even on a page with nothing
    wide in it (docs/ux-review.md finding 6.4). Recompute from just the visible
    widget instead, and tell the layout system to re-query it on every page change.
    """

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.minimumSizeHint() if current is not None else super().minimumSizeHint()

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()


class MainWindow(QMainWindow):
    def __init__(self, fake: bool, engine) -> None:
        super().__init__()
        self.fake = fake
        self.engine = engine
        self.session = ScanSession()
        self._progress_dialog: QProgressDialog | None = None
        self._recover_worker: RecoverWorker | None = None
        self._media_export_worker: MediaExportWorker | None = None
        self._media_export_items: list = []
        self._ios_parse_worker: IOSParseWorker | None = None

        self.setWindowTitle("Salvage")
        self.resize(1100, 720)

        self.stack = _CurrentPageStackedWidget()
        self.stack.currentChanged.connect(lambda _: self.stack.updateGeometry())
        self.setCentralWidget(self.stack)

        self.source_page = SourcePage(self)
        self.options_page = OptionsPage(self)
        self.scan_page = ScanPage(self)
        self.results_page = ResultsPage(self)
        self.done_page = DonePage(self)
        self.ios_options_page = IOSOptionsPage(self)
        self.ios_backup_page = IOSBackupPage(self)
        self.ios_results_page = IOSResultsPage(self)
        self.media_options_page = MediaOptionsPage(self)
        self.media_scan_page = MediaScanPage(self)
        self.media_results_page = MediaResultsPage(self)
        for page in (
            self.source_page,
            self.options_page,
            self.scan_page,
            self.results_page,
            self.done_page,
            self.ios_options_page,
            self.ios_backup_page,
            self.ios_results_page,
            self.media_options_page,
            self.media_scan_page,
            self.media_results_page,
        ):
            self.stack.addWidget(page)

        self.source_page.refresh()
        self.stack.setCurrentWidget(self.source_page)

    def go_to_source(self) -> None:
        self.stack.setCurrentWidget(self.source_page)

    def go_to_options(self, device: Device) -> None:
        self.session.device = device
        self.options_page.set_device(device)
        self.stack.setCurrentWidget(self.options_page)

    def go_to_scan(self, mode: ScanMode, extensions: list[str] | None, destination: Path) -> None:
        self.session.mode = mode
        self.session.extensions = extensions
        self.session.destination = destination
        self.session.timestamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        self.session.workdir = self.session.session_dir / "_scan"
        self.session.workdir.mkdir(parents=True, exist_ok=True)
        self.stack.setCurrentWidget(self.scan_page)
        self.scan_page.start_scan()

    def go_to_results(self, scan_result: ScanResult) -> None:
        self.results_page.set_result(scan_result)
        self.stack.setCurrentWidget(self.results_page)

    def start_recovery(self, files: list[RecoveredFile]) -> None:
        if not files:
            return
        recover_dir = self.session.recover_dir
        assert recover_dir is not None

        self._progress_dialog = QProgressDialog("Recovering files…", "", 0, len(files), self)
        self._progress_dialog.setWindowTitle("Recovering")
        self._progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setCancelButton(None)
        self._progress_dialog.setValue(0)
        self._progress_dialog.show()

        self._recover_worker = RecoverWorker(files, recover_dir)
        self._recover_worker.progress.connect(lambda done, _total: self._progress_dialog.setValue(done))
        self._recover_worker.finished_recover.connect(self._on_recover_finished)
        self._recover_worker.failed.connect(self._on_recover_failed)
        self._recover_worker.start()

    def _on_recover_finished(self, written: list[Path]) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        self.session.recovered_paths = written
        self.done_page.set_result(written, self.session.recover_dir)
        self.stack.setCurrentWidget(self.done_page)

    def _on_recover_failed(self, message: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        QMessageBox.critical(self, "Recovery failed", message)

    def go_to_ios_options(self, device: IOSDevice | None, existing_backup_dir: Path | None) -> None:
        self.session.ios_device = device
        self.session.ios_existing_backup_dir = existing_backup_dir
        self.ios_options_page.set_source(device, existing_backup_dir)
        self.stack.setCurrentWidget(self.ios_options_page)

    def go_to_ios_start(
        self,
        device: IOSDevice | None,
        existing_backup_dir: Path | None,
        categories: set[str],
        destination: Path,
        password: str | None = None,
    ) -> None:
        self.session.ios_device = device
        self.session.ios_existing_backup_dir = existing_backup_dir
        self.session.ios_categories = categories
        self.session.ios_password = password
        self.session.destination = destination
        self.session.timestamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        self.session.session_dir.mkdir(parents=True, exist_ok=True)

        if device is not None:
            self.stack.setCurrentWidget(self.ios_backup_page)
            self.ios_backup_page.start_backup()
        else:
            self.session.ios_backup_dir = existing_backup_dir
            self.go_to_ios_parse()

    def go_to_ios_parse(self) -> None:
        session = self.session
        backup_dir = session.ios_backup_dir
        assert backup_dir is not None
        workdir = session.session_dir / "_ios_extracted"
        workdir.mkdir(parents=True, exist_ok=True)

        self._progress_dialog = QProgressDialog("Extracting and parsing iPhone data…", "", 0, 0, self)
        self._progress_dialog.setWindowTitle("Parsing")
        self._progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setCancelButton(None)
        self._progress_dialog.show()

        self._ios_parse_worker = IOSParseWorker(
            backup_dir, session.ios_categories, workdir, password=session.ios_password
        )
        self._ios_parse_worker.finished_parse.connect(self._on_ios_parse_finished)
        self._ios_parse_worker.failed.connect(self._on_ios_parse_failed)
        self._ios_parse_worker.password_error.connect(self._on_ios_parse_password_error)
        self._ios_parse_worker.start()

    def _on_ios_parse_finished(self, data) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        self.session.ios_parsed = data
        self.ios_results_page.set_data(data)
        self.stack.setCurrentWidget(self.ios_results_page)

    def _on_ios_parse_failed(self, message: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        QMessageBox.critical(self, "Couldn't read backup", message)
        self.go_to_source()

    def _on_ios_parse_password_error(self, message: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        # Only a live-device backup reaches here with a bad password (an existing backup
        # folder is checked on the options page before we ever start extracting) — send
        # the user back there to retry rather than dead-ending on an error dialog.
        self.ios_options_page.set_source(self.session.ios_device, self.session.ios_existing_backup_dir)
        self.ios_options_page.show_password_error(message)
        self.stack.setCurrentWidget(self.ios_options_page)

    def export_ios_results(self, data) -> None:
        recover_dir = self.session.ios_recover_dir
        assert recover_dir is not None
        written = ios_facade.export_ios_results(data, recover_dir)
        self.session.recovered_paths = written
        self.done_page.set_result(written, recover_dir)
        self.stack.setCurrentWidget(self.done_page)

    def go_to_media_options(self) -> None:
        self.media_options_page.refresh()
        self.stack.setCurrentWidget(self.media_options_page)

    def go_to_media_scan(self, sources, min_size: int, hash_dupes: bool, destination: Path) -> None:
        self.session.media_sources = sources
        self.session.media_min_size = min_size
        self.session.media_hash_dupes = hash_dupes
        self.session.destination = destination
        self.session.timestamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        self.stack.setCurrentWidget(self.media_scan_page)
        self.media_scan_page.start_scan(sources, min_size, hash_dupes)

    def go_to_media_results(self, found) -> None:
        self.session.media_found = found
        self.media_results_page.set_items(found, self.session.media_sources)
        self.stack.setCurrentWidget(self.media_results_page)

    def export_media_results(self, items) -> None:
        export_dir = self.session.media_export_dir
        assert export_dir is not None
        self._media_export_items = items

        self._progress_dialog = QProgressDialog("Exporting files…", "", 0, len(items), self)
        self._progress_dialog.setWindowTitle("Exporting")
        self._progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setCancelButton(None)
        self._progress_dialog.setValue(0)
        self._progress_dialog.show()

        self._media_export_worker = MediaExportWorker(items, export_dir, self)
        self._media_export_worker.progress.connect(lambda done, _total: self._progress_dialog.setValue(done))
        self._media_export_worker.finished_export.connect(self._on_media_export_finished)
        self._media_export_worker.start()

    def _on_media_export_finished(self, written: list[Path], failed: int) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        export_dir = self.session.media_export_dir
        assert export_dir is not None
        items = self._media_export_items
        self.session.recovered_paths = written
        preview_only_count = sum(1 for it in items if it.preview_only and it.duplicate_of is None)
        notes = []
        if preview_only_count:
            plural = "s" if preview_only_count != 1 else ""
            notes.append(
                f"{preview_only_count} file{plural} were iCloud-only in Photos — a local preview "
                "was copied instead of the full-resolution original."
            )
        if failed:
            plural = "s" if failed != 1 else ""
            was_were = "were" if failed != 1 else "was"
            notes.append(f"{failed} file{plural} could not be copied and {was_were} skipped.")
        note = "\n".join(notes) if notes else None
        self.done_page.set_result(written, export_dir, note=note)
        self.stack.setCurrentWidget(self.done_page)

    def reset_and_go_to_source(self) -> None:
        self.session.reset()
        self.source_page.refresh()
        self.stack.setCurrentWidget(self.source_page)

    def closeEvent(self, event) -> None:
        # A ScanWorker/RecoverWorker QThread keeps running (and, for a scan, keeps the
        # photorec subprocess alive) even after the window closes unless we stop it here.
        worker = self.scan_page.worker
        if worker is not None and worker.isRunning():
            worker.cancel_event.set()
            worker.wait(5000)
        recover_worker = self._recover_worker
        if recover_worker is not None and recover_worker.isRunning():
            recover_worker.wait(5000)
        media_export_worker = self._media_export_worker
        if media_export_worker is not None and media_export_worker.isRunning():
            media_export_worker.wait(5000)
        ios_backup_worker = self.ios_backup_page.worker
        if ios_backup_worker is not None and ios_backup_worker.isRunning():
            ios_backup_worker.cancel_event.set()
            ios_backup_worker.wait(5000)
        ios_parse_worker = self._ios_parse_worker
        if ios_parse_worker is not None and ios_parse_worker.isRunning():
            ios_parse_worker.wait(5000)
        media_worker = self.media_scan_page.worker
        if media_worker is not None and media_worker.isRunning():
            media_worker.cancel_event.set()
            media_worker.wait(5000)
        self.media_results_page.thumb_service.cancel()
        self.media_results_page.thumb_service.wait(3000)
        self.results_page.thumb_service.cancel()
        self.results_page.thumb_service.wait(3000)
        super().closeEvent(event)
