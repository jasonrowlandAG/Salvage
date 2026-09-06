"""Background, low-priority generation of thumbnails for a whole result set.

Runs on a small dedicated QThreadPool (a handful of threads) so it never competes hard
with the UI thread or the on-demand preview loader. Walks visible items first — the
results page calls `prioritize()` with the current viewport range whenever the user
scrolls — then the rest of the set, calling `thumbcache.generate_batch()` a few files at a
time and persisting to disk as it goes, so a later scan of the same files is instant.
Never touches `cloud_placeholder` items — generating a thumbnail would read the file,
which for a cloud placeholder means triggering an iCloud download.
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from salvage.engine import thumbcache

_BATCH_SIZE = 6
_MAX_THREADS = 3


class _Worker(QRunnable):
    def __init__(self, service: "BackgroundThumbnailService", generation: int) -> None:
        super().__init__()
        self._service = service
        self._generation = generation
        self.setAutoDelete(True)

    def run(self) -> None:
        service = self._service
        while True:
            if service._cancel.is_set():
                return
            batch = service._take_batch(_BATCH_SIZE, self._generation)
            if not batch:
                return
            try:
                results = thumbcache.generate_batch(batch)
            except Exception:
                results = {p: None for p in batch}
            if service._cancel.is_set():
                return
            for path in batch:
                service._mark_done(path, results.get(path), self._generation)


class BackgroundThumbnailService(QObject):
    """One instance per results page; call `start()` once the full item list is known."""

    progress = Signal(int, int)         # done, total
    thumb_ready = Signal(str, object)   # path str, cache Path or None
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(_MAX_THREADS)
        self._lock = threading.Lock()
        self._queue: deque[Path] = deque()
        self._queued_set: set[Path] = set()
        self._cancel = threading.Event()
        self._generation = 0
        self._total = 0
        self._done = 0

    def start(self, paths: list[Path]) -> None:
        """(Re)starts the service over a fresh set of paths, discarding any prior run."""
        self.cancel()
        unique: list[Path] = []
        seen: set[Path] = set()
        for p in paths:
            p = Path(p)
            if p not in seen:
                seen.add(p)
                unique.append(p)
        with self._lock:
            self._cancel = threading.Event()
            self._generation += 1
            generation = self._generation
            self._queue = deque(unique)
            self._queued_set = set(unique)
            self._total = len(unique)
            self._done = 0
        if not unique:
            self.finished.emit()
            return
        for _ in range(self._pool.maxThreadCount()):
            self._pool.start(_Worker(self, generation))

    def prioritize(self, visible_paths: list[Path]) -> None:
        """Moves the given paths to the front of the queue so they generate next."""
        with self._lock:
            wanted = [p for p in (Path(v) for v in visible_paths) if p in self._queued_set]
            if not wanted:
                return
            wanted_set = set(wanted)
            rest = (p for p in self._queue if p not in wanted_set)
            self._queue = deque(wanted)
            self._queue.extend(rest)

    def cancel(self) -> None:
        """Stops delivering results and drops the remaining queue. Safe to call repeatedly."""
        self._cancel.set()
        with self._lock:
            self._queue.clear()
            self._queued_set.clear()

    def wait(self, timeout_ms: int = 3000) -> None:
        self._pool.waitForDone(timeout_ms)

    def _take_batch(self, n: int, generation: int) -> list[Path]:
        with self._lock:
            if generation != self._generation:
                return []
            batch = []
            for _ in range(n):
                if not self._queue:
                    break
                batch.append(self._queue.popleft())
            for p in batch:
                self._queued_set.discard(p)
            return batch

    def _mark_done(self, path: Path, cache_path: Path | None, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._done += 1
            done, total = self._done, self._total
        try:
            self.thumb_ready.emit(str(path), cache_path)
        except RuntimeError:
            pass  # service torn down while a worker was finishing
        self.progress.emit(done, total)
        if done >= total:
            self.finished.emit()
