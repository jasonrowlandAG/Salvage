"""This-Mac media flow, step 2: run the search and show live progress."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from salvage.ui.format_utils import human_size
from salvage.ui.workers import MediaScanWorker


class MediaScanPage(QWidget):
    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.worker: MediaScanWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(16)

        heading = QLabel("Searching this Mac")
        heading.setProperty("role", "heading")
        outer.addWidget(heading)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        outer.addWidget(self.progress_bar)

        stats_row = QHBoxLayout()
        self.seen_label = QLabel("Files seen: 0")
        self.found_label = QLabel("Media found: 0")
        self.size_label = QLabel("0 B")
        for lbl in (self.seen_label, self.found_label, self.size_label):
            stats_row.addWidget(lbl)
        stats_row.addStretch()
        outer.addLayout(stats_row)

        self.current_label = QLabel("")
        self.current_label.setProperty("role", "subheading")
        self.current_label.setWordWrap(True)
        outer.addWidget(self.current_label)

        outer.addStretch()

        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        bottom_row.addWidget(self.cancel_btn)
        outer.addLayout(bottom_row)

    def start_scan(self, sources, min_size: int, hash_dupes: bool) -> None:
        self.progress_bar.setRange(0, max(len(sources), 1))
        self.progress_bar.setValue(0)
        self.seen_label.setText("Files seen: 0")
        self.found_label.setText("Media found: 0")
        self.size_label.setText("0 B")
        self.current_label.setText("Starting…")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")

        self.worker = MediaScanWorker(self.controller.fake, sources, min_size, hash_dupes)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_scan.connect(self._on_finished)
        self.worker.start()

    def _cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel_event.set()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")

    def _on_progress(self, stats) -> None:
        self.progress_bar.setRange(0, max(stats.sources_total, 1))
        self.progress_bar.setValue(stats.sources_done)
        self.seen_label.setText(f"Files seen: {stats.files_seen:,}")
        self.found_label.setText(f"Media found: {stats.media_found:,}")
        self.size_label.setText(human_size(stats.bytes))
        self.current_label.setText(stats.current)

    def _on_finished(self, found) -> None:
        self.cancel_btn.setEnabled(False)
        self.controller.go_to_media_results(found)
