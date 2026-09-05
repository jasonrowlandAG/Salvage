"""Main window: owns the engine, session state, and page navigation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow, QMessageBox, QProgressDialog, QStackedWidget

from salvage.engine.models import Device, RecoveredFile, ScanMode, ScanResult
from salvage.ui.done_page import DonePage
from salvage.ui.options_page import OptionsPage
from salvage.ui.results_page import ResultsPage
from salvage.ui.scan_page import ScanPage
from salvage.ui.session import ScanSession
from salvage.ui.source_page import SourcePage
from salvage.ui.workers import RecoverWorker


class MainWindow(QMainWindow):
    def __init__(self, fake: bool, engine) -> None:
        super().__init__()
        self.fake = fake
        self.engine = engine
        self.session = ScanSession()
        self._progress_dialog: QProgressDialog | None = None
        self._recover_worker: RecoverWorker | None = None

        self.setWindowTitle("Salvage")
        self.resize(1100, 720)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.source_page = SourcePage(self)
        self.options_page = OptionsPage(self)
        self.scan_page = ScanPage(self)
        self.results_page = ResultsPage(self)
        self.done_page = DonePage(self)
        for page in (
            self.source_page,
            self.options_page,
            self.scan_page,
            self.results_page,
            self.done_page,
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
        super().closeEvent(event)
