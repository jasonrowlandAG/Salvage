"""Main window: owns the engine, session state, and page navigation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow, QMessageBox, QProgressDialog, QStackedWidget

from salvage.engine.ios import IOSDevice
from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanResult
from salvage.ui import ios_facade
from salvage.ui.done_page import DonePage
from salvage.ui.ios_backup_page import IOSBackupPage
from salvage.ui.ios_options_page import IOSOptionsPage
from salvage.ui.ios_results_page import IOSResultsPage
from salvage.ui.options_page import OptionsPage
from salvage.ui.results_page import ResultsPage
from salvage.ui.scan_page import ScanPage
from salvage.ui.session import ScanSession
from salvage.ui.source_page import SourcePage
from salvage.ui.workers import IOSParseWorker, RecoverWorker


class MainWindow(QMainWindow):
    def __init__(self, fake: bool, engine) -> None:
        super().__init__()
        self.fake = fake
        self.engine = engine
        self.session = ScanSession()
        self._progress_dialog: QProgressDialog | None = None
        self._recover_worker: RecoverWorker | None = None
        self._ios_parse_worker: IOSParseWorker | None = None

        self.setWindowTitle("Salvage")
        self.resize(1100, 720)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.source_page = SourcePage(self)
        self.options_page = OptionsPage(self)
        self.scan_page = ScanPage(self)
        self.results_page = ResultsPage(self)
        self.done_page = DonePage(self)
        self.ios_options_page = IOSOptionsPage(self)
        self.ios_backup_page = IOSBackupPage(self)
        self.ios_results_page = IOSResultsPage(self)
        for page in (
            self.source_page,
            self.options_page,
            self.scan_page,
            self.results_page,
            self.done_page,
            self.ios_options_page,
            self.ios_backup_page,
            self.ios_results_page,
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
    ) -> None:
        self.session.ios_device = device
        self.session.ios_existing_backup_dir = existing_backup_dir
        self.session.ios_categories = categories
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

        self._ios_parse_worker = IOSParseWorker(backup_dir, session.ios_categories, workdir)
        self._ios_parse_worker.finished_parse.connect(self._on_ios_parse_finished)
        self._ios_parse_worker.failed.connect(self._on_ios_parse_failed)
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

    def export_ios_results(self, data) -> None:
        recover_dir = self.session.ios_recover_dir
        assert recover_dir is not None
        written = ios_facade.export_ios_results(data, recover_dir)
        self.session.recovered_paths = written
        self.done_page.set_result(written, recover_dir)
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
        ios_backup_worker = self.ios_backup_page.worker
        if ios_backup_worker is not None and ios_backup_worker.isRunning():
            ios_backup_worker.cancel_event.set()
            ios_backup_worker.wait(5000)
        ios_parse_worker = self._ios_parse_worker
        if ios_parse_worker is not None and ios_parse_worker.isRunning():
            ios_parse_worker.wait(5000)
        super().closeEvent(event)
