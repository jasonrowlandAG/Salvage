"""Background thumbnail generation for image-category recovered files."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


class _Signals(QObject):
    ready = Signal(str, QImage)


class _ThumbnailTask(QRunnable):
    def __init__(self, path: Path, size: int, signals: _Signals) -> None:
        super().__init__()
        self._path = path
        self._size = size
        self._signals = signals

    def run(self) -> None:
        try:
            with Image.open(self._path) as im:
                im = im.convert("RGB")
                im.thumbnail((self._size, self._size))
                data = im.tobytes("raw", "RGB")
                qimg = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888).copy()
        except Exception:
            return
        self._signals.ready.emit(str(self._path), qimg)


class ThumbnailLoader(QObject):
    """Loads thumbnails on a shared thread pool and reports back on the GUI thread."""

    ready = Signal(str, QPixmap)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.ready.connect(self._on_ready)

    def request(self, path: Path, size: int = 128) -> None:
        self._pool.start(_ThumbnailTask(path, size, self._signals))

    def _on_ready(self, path_str: str, image: QImage) -> None:
        self.ready.emit(path_str, QPixmap.fromImage(image))
