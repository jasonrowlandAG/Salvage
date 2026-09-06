"""iPhone wizard step 3: run idevicebackup2 and show live progress.

idevicebackup2 resets its percent/byte counters per internal batch rather
than for the whole backup, so this page shows an indeterminate bar plus a
running "Receiving files… N GB" total rather than a misleading global
percent (see BackupProgress in salvage/engine/ios.py).
"""

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

from salvage.engine.ios import BackupProgress
from salvage.ui.format_utils import human_elapsed, human_size
from salvage.ui.workers import IOSBackupWorker


class IOSBackupPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.worker: IOSBackupWorker | None = None
        self._max_bytes_done = 0
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("Backing up iPhone")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.device_label = QLabel("")
        self.device_label.setProperty("role", "subheading")
        outer.addWidget(self.device_label)

        self.passcode_banner = QFrame()
        self.passcode_banner.setProperty("role", "panel")
        banner_layout = QVBoxLayout(self.passcode_banner)
        banner_label = QLabel("Enter your passcode on the iPhone if prompted.")
        banner_label.setWordWrap(True)
        banner_layout.addWidget(banner_label)
        self.passcode_banner.hide()
        outer.addWidget(self.passcode_banner)

        outer.addSpacing(12)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        outer.addWidget(self.progress_bar)

        self.status_label = QLabel("Receiving files…")
        outer.addWidget(self.status_label)

        self.elapsed_label = QLabel("Elapsed: 00:00")
        self.elapsed_label.setProperty("role", "subheading")
        outer.addWidget(self.elapsed_label)

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
        error_back_btn.clicked.connect(self.controller.go_to_source)
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

    def start_backup(self) -> None:
        session = self.controller.session
        device = session.ios_device
        assert device is not None

        self.error_frame.hide()
        self.passcode_banner.hide()
        self._max_bytes_done = 0
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Starting…")
        self.elapsed_label.setText("Elapsed: 00:00")
        self.device_label.setText(f"Source: {device.name}")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")

        backup_root = session.session_dir / "_ios_backup"
        backup_root.mkdir(parents=True, exist_ok=True)

        self.worker = IOSBackupWorker(self.controller.fake, device.udid, backup_root)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_backup.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel_event.set()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")

    def _on_progress(self, progress: BackupProgress) -> None:
        if progress.current_file == "Waiting for passcode":
            self.passcode_banner.show()
        else:
            self.passcode_banner.hide()
        if progress.bytes_done is not None:
            self._max_bytes_done = max(self._max_bytes_done, progress.bytes_done)
            self.status_label.setText(f"Receiving files… {human_size(self._max_bytes_done)}")
        else:
            self.status_label.setText("Receiving files…")
        self.elapsed_label.setText(f"Elapsed: {human_elapsed(progress.elapsed_s)}")

    def _on_finished(self, backup_dir) -> None:
        self.cancel_btn.setEnabled(False)
        self.controller.session.ios_backup_dir = backup_dir
        self.controller.go_to_ios_parse()

    def _on_failed(self, message: str) -> None:
        self.cancel_btn.setEnabled(False)
        self.passcode_banner.hide()
        self.error_label.setText(message)
        self.error_frame.show()
