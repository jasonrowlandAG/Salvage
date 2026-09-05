"""Wizard step 3: run the scan and show live progress."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from salvage.engine.models import ScanProgress, ScanResult
from salvage.ui.format_utils import human_elapsed
from salvage.ui.workers import ScanWorker


class ScanPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.worker: ScanWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("Scanning")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.device_label = QLabel("")
        self.device_label.setProperty("role", "subheading")
        outer.addWidget(self.device_label)

        outer.addSpacing(12)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        outer.addWidget(self.progress_bar)

        stats_row = QHBoxLayout()
        self.sector_label = QLabel("Sector — of —")
        self.files_label = QLabel("Files found: 0")
        self.elapsed_label = QLabel("Elapsed: 00:00")
        for lbl in (self.sector_label, self.files_label, self.elapsed_label):
            stats_row.addWidget(lbl)
        stats_row.addStretch()
        outer.addLayout(stats_row)

        self.phase_label = QLabel("")
        self.phase_label.setProperty("role", "subheading")
        outer.addWidget(self.phase_label)

        outer.addStretch()

        self.error_frame = QFrame()
        self.error_frame.setProperty("role", "panel")
        error_layout = QVBoxLayout(self.error_frame)
        self.error_label = QLabel("")
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        error_layout.addWidget(self.error_label)
        error_back_row = QHBoxLayout()
        error_back_row.addStretch()
        error_back_btn = QPushButton("Back")
        error_back_btn.clicked.connect(
            lambda: self.controller.go_to_options(self.controller.session.device)
        )
        error_back_row.addWidget(error_back_btn)
        error_layout.addLayout(error_back_row)
        self.error_frame.hide()
        outer.addWidget(self.error_frame)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        bottom_row.addWidget(self.cancel_btn)
        outer.addLayout(bottom_row)

    def start_scan(self) -> None:
        session = self.controller.session
        self.error_frame.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.sector_label.setText("Sector — of —")
        self.files_label.setText("Files found: 0")
        self.elapsed_label.setText("Elapsed: 00:00")
        self.phase_label.setText("Starting…")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.device_label.setText(f"Source: {session.device.name}")

        self.worker = ScanWorker(
            self.controller.engine,
            session.device.path,
            session.workdir,
            session.mode,
            session.extensions,
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_scan.connect(self._on_finished)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel_event.set()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")

    def _on_progress(self, progress: ScanProgress) -> None:
        if progress.total_sectors > 0:
            self.progress_bar.setRange(0, 100)
            pct = int((progress.fraction or 0) * 100)
            self.progress_bar.setValue(pct)
            self.sector_label.setText(f"Sector {progress.sector:,} of {progress.total_sectors:,}")
        else:
            self.progress_bar.setRange(0, 0)
            self.sector_label.setText("Reading partition table…")
        self.files_label.setText(f"Files found: {progress.files_found}")
        self.elapsed_label.setText(f"Elapsed: {human_elapsed(progress.elapsed_s)}")
        self.phase_label.setText(progress.phase)

    def _on_finished(self, result: ScanResult) -> None:
        self.cancel_btn.setEnabled(False)
        if result.cancelled:
            self.error_label.setText("Scan cancelled.")
            self.error_frame.show()
            return
        if not result.success:
            self.error_label.setText(
                (result.error or "The scan failed.")
                + "\n\nIf this mentions permissions, try running Salvage with administrator "
                'rights (sudo on macOS/Linux, or "Run as administrator" on Windows).'
            )
            self.error_frame.show()
            return
        self.controller.session.scan_result = result
        self.controller.go_to_results(result)
