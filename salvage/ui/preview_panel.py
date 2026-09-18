"""Shared preview panel used by both results pages (drive recovery + Mac media finder).

Three states, switched on the selected item's category:

* **image** — the panel's own area, scaled to fit and re-scaled on resize, backed by
  `thumbcache`'s persistent cache (Pillow/pi_heif, falling back to QuickLook for RAW
  and anything else Pillow can't open). A small "Open full size" button (or a
  double-click) opens a separate, resizable window with fit/100% toggle, mouse-wheel
  zoom, and drag-to-pan.
* **video / audio** — real playback via `QMediaPlayer` + `QAudioOutput` (+ `QVideoWidget`
  for video), with a poster frame from `thumbcache` shown until the user presses Play,
  a seek slider, and a mute/volume control. The player is created only once an item is
  actually selected, and is torn down again on every selection change (`show_item`) and
  whenever the panel is hidden (`hideEvent`) or explicitly released (`release()`), so no
  decoder is ever left running in the background. A broken/truncated file surfaces as a
  plain "can't be played" message next to the still-visible poster, never a crash or a
  hang — `errorOccurred` is the only place playback failure is handled.
* **everything else** (documents, archives, unknown) — a generic category icon plus a
  "Reveal in Finder" button.

`cloud_placeholder` items are never read — `thumbcache.get()/ensure()` are simply never
called for them, matching the rule already enforced by `thumbcache.ensure_for()`.
`preview_only` items (a Photos-library asset whose real original is iCloud-only; `path`
is a local derivative) are previewed like any other image, with a note saying so.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRunnable, QThreadPool, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from salvage.engine import thumbcache
from salvage.ui.format_utils import human_elapsed

_ICON_BY_CATEGORY = {
    "document": QStyle.StandardPixmap.SP_FileDialogDetailedView,
    "archive": QStyle.StandardPixmap.SP_DirIcon,
    "other": QStyle.StandardPixmap.SP_FileIcon,
}

_PREVIEW_ONLY_NOTE = "Local preview — the original is stored in iCloud"

# Some truncated/carved videos never fire QMediaPlayer.errorOccurred at all — the FFmpeg
# backend just sits in MediaStatus.BufferingMedia forever with no error and no progress
# (observed on a video-only stream truncated mid-frame). That doesn't hang the UI thread,
# but it does leave the user staring at a player that looks "stuck", which is exactly
# what "must never hang" is guarding against — so we bound it with a watchdog, matching
# thumbcache's own 8s cap for hanging QuickLook calls on the same class of bad files.
_STALL_TIMEOUT_MS = 8000


# ---------------------------------------------------------------------------
# Background loaders (never block the UI thread on file IO/decoding)
# ---------------------------------------------------------------------------


class _ImagePreviewSignals(QObject):
    ready = Signal(str, object, object)  # path str, cache Path|None, dims (w, h)|None


class _ImagePreviewTask(QRunnable):
    """Generates (or fetches) the large preview render for one image, plus its true
    pixel dimensions read straight from the source file (not the downsampled render)."""

    def __init__(self, path: Path, signals: _ImagePreviewSignals) -> None:
        super().__init__()
        self._path = path
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        dims = None
        try:
            from PIL import Image

            with Image.open(self._path) as im:
                dims = im.size
        except Exception:
            dims = None
        try:
            cache_path = thumbcache.ensure_preview(self._path)
        except Exception:
            cache_path = None
        try:
            self._signals.ready.emit(str(self._path), cache_path, dims)
        except RuntimeError:
            pass  # panel torn down while this task was finishing


class _PosterSignals(QObject):
    ready = Signal(str, object)  # path str, cache Path|None


class _PosterTask(QRunnable):
    """Fetches (or generates) the small poster frame for a video/audio file."""

    def __init__(self, path: Path, signals: _PosterSignals) -> None:
        super().__init__()
        self._path = path
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            cache_path = thumbcache.ensure(self._path)
        except Exception:
            cache_path = None
        try:
            self._signals.ready.emit(str(self._path), cache_path)
        except RuntimeError:
            pass  # panel torn down while this task was finishing


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------


class _ScalingImageLabel(QLabel):
    """A QLabel that keeps a source pixmap and re-scales it (aspect preserved) to fill
    whatever size it's given, including on resize — this is what makes the preview
    "render at the panel's full available size" instead of a small fixed box."""

    doubleClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: QPixmap | None = None

    def set_source(self, pixmap: QPixmap | None) -> None:
        self._source = pixmap
        self._rescale()

    def source(self) -> QPixmap | None:
        return self._source

    def _rescale(self) -> None:
        if self._source is None or self._source.isNull():
            return
        scaled = self._source.scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        super().setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._rescale()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class _PannableScrollArea(QScrollArea):
    """A QScrollArea whose viewport can be dragged to pan (left-click + drag)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragging = False
        self._drag_origin = QPoint()
        self._scroll_origin = QPoint()
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def _pos(self, event) -> QPoint:
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_origin = self._pos(event)
            self._scroll_origin = QPoint(self.horizontalScrollBar().value(), self.verticalScrollBar().value())
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            delta = self._pos(event) - self._drag_origin
            self.horizontalScrollBar().setValue(self._scroll_origin.x() - delta.x())
            self.verticalScrollBar().setValue(self._scroll_origin.y() - delta.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)


class ImageViewerWindow(QDialog):
    """A resizable "open full size" window: fit/actual-size toggle, wheel zoom, drag pan.

    Zoom is clamped to at most 100% (1:1 pixels of whatever render we have — for RAW/
    other QuickLook-only formats that render is `thumbcache.PREVIEW_SIZE`, not
    necessarily the original file's full resolution).
    """

    def __init__(self, pixmap: QPixmap, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(900, 700)
        self._source = pixmap
        self._zoom = 1.0

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        fit_btn = QPushButton("Fit")
        fit_btn.clicked.connect(self._show_fit)
        toolbar.addWidget(fit_btn)
        actual_btn = QPushButton("100%")
        actual_btn.clicked.connect(self._show_actual)
        toolbar.addWidget(actual_btn)
        self._zoom_label = QLabel("")
        self._zoom_label.setProperty("role", "subheading")
        toolbar.addWidget(self._zoom_label)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self._scroll = _PannableScrollArea()
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label = QLabel()
        self._scroll.setWidget(self._image_label)
        layout.addWidget(self._scroll, 1)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Swaps in a higher-quality render without disturbing the current zoom/pan."""
        self._source = pixmap
        self._apply_zoom()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._show_fit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom = min(max(self._zoom * factor, 0.05), 1.0)
        self._apply_zoom()
        event.accept()

    def _show_fit(self) -> None:
        avail = self._scroll.viewport().size()
        sw, sh = self._source.width(), self._source.height()
        scale = min(avail.width() / sw, avail.height() / sh, 1.0) if sw and sh else 1.0
        self._zoom = max(scale, 0.05)
        self._apply_zoom()

    def _show_actual(self) -> None:
        self._zoom = 1.0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        w = max(1, int(self._source.width() * self._zoom))
        h = max(1, int(self._source.height() * self._zoom))
        scaled = self._source.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._image_label.setPixmap(scaled)
        self._image_label.resize(scaled.size())
        self._zoom_label.setText(f"{round(self._zoom * 100)}%")


# ---------------------------------------------------------------------------
# The panel itself
# ---------------------------------------------------------------------------


class PreviewPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "panel")

        self._path: Path | None = None
        self._category: str | None = None
        self._full_pixmap: QPixmap | None = None
        self._viewer_window: ImageViewerWindow | None = None

        self._pool = QThreadPool.globalInstance()
        self._image_signals = _ImagePreviewSignals()
        self._image_signals.ready.connect(self._on_image_preview_ready)
        self._poster_signals = _PosterSignals()
        self._poster_signals.ready.connect(self._on_poster_ready)

        self._player: QMediaPlayer | None = None
        self._audio_output: QAudioOutput | None = None
        self._is_video = False
        self._seeking = False
        self._duration_ms = 0
        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.timeout.connect(self._on_stall_timeout)

        self._build_ui()
        self.show_item(None, None)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.media_stack = QStackedWidget()
        self.media_stack.setMinimumHeight(220)
        layout.addWidget(self.media_stack, 1)

        self._image_page = self._build_image_page()
        self.media_stack.addWidget(self._image_page)
        self._video_page = self._build_video_page()
        self.media_stack.addWidget(self._video_page)
        self._icon_page = self._build_icon_page()
        self.media_stack.addWidget(self._icon_page)

        self.name_label = QLabel("")
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.name_label)

        self.size_label = QLabel("")
        self.size_label.setProperty("role", "subheading")
        layout.addWidget(self.size_label)

        self.meta_widget = QWidget()
        self.meta_layout = QVBoxLayout(self.meta_widget)
        self.meta_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.meta_widget)

        self.dims_label = QLabel("")
        self.dims_label.setProperty("role", "subheading")
        layout.addWidget(self.dims_label)

        self.note_label = QLabel("")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

    def _build_image_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        self.image_label = _ScalingImageLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setProperty("role", "subheading")
        self.image_label.setWordWrap(True)
        self.image_label.doubleClicked.connect(self._open_full_size)
        v.addWidget(self.image_label, 1)
        self.open_full_btn = QPushButton("Open full size")
        self.open_full_btn.setEnabled(False)
        self.open_full_btn.clicked.connect(self._open_full_size)
        v.addWidget(self.open_full_btn, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_video_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)

        self.video_area_stack = QStackedWidget()
        self.poster_label = _ScalingImageLabel()
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setProperty("role", "subheading")
        self.poster_label.setWordWrap(True)
        self.poster_label.doubleClicked.connect(self._toggle_playback)
        self.video_area_stack.addWidget(self.poster_label)
        self.video_widget = QVideoWidget()
        self.video_area_stack.addWidget(self.video_widget)
        v.addWidget(self.video_area_stack, 1)

        self.video_message_label = QLabel("")
        self.video_message_label.setProperty("role", "error")
        self.video_message_label.setWordWrap(True)
        v.addWidget(self.video_message_label)

        controls = QHBoxLayout()
        self.play_btn = QToolButton()
        self.play_btn.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._toggle_playback)
        controls.addWidget(self.play_btn)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        controls.addWidget(self.seek_slider, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setProperty("role", "subheading")
        controls.addWidget(self.time_label)
        v.addLayout(controls)

        volume_row = QHBoxLayout()
        self.mute_btn = QToolButton()
        self.mute_btn.setCheckable(True)
        self.mute_btn.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        volume_row.addWidget(self.mute_btn)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        volume_row.addWidget(self.volume_slider, 1)
        v.addLayout(volume_row)

        return page

    def _build_icon_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.icon_label, 1)
        self.icon_message = QLabel("No preview available")
        self.icon_message.setProperty("role", "subheading")
        self.icon_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.icon_message)
        self.reveal_btn = QPushButton("Reveal in Finder")
        self.reveal_btn.clicked.connect(self._reveal_in_finder)
        v.addWidget(self.reveal_btn, 0, Qt.AlignmentFlag.AlignCenter)
        v.addStretch(1)
        return page

    # -- public API ----------------------------------------------------------

    def show_item(
        self,
        path: Path | None,
        category: str | None,
        *,
        name: str = "",
        size_text: str = "",
        meta_lines: list[str] | None = None,
        note: str = "",
        cloud_placeholder: bool = False,
        preview_only: bool = False,
    ) -> None:
        """Selects a new item to preview (or `None` to clear). Always releases any
        previous player first, so a video/audio decoder never keeps running past its
        own selection — see the module docstring."""
        self._release_player()

        if path is None:
            self._path = None
            self._category = None
            self._full_pixmap = None
            self.media_stack.setCurrentWidget(self._image_page)
            self.image_label.set_source(None)
            self.image_label.setText("Select a file to preview it.")
            self.open_full_btn.setEnabled(False)
            self._set_metadata("", "", [], "")
            return

        self._path = Path(path)
        self._category = category
        self._full_pixmap = None

        lines = list(meta_lines or [])
        if preview_only:
            lines.insert(0, _PREVIEW_ONLY_NOTE)
        self._set_metadata(name, size_text, lines, note)

        if cloud_placeholder:
            self.media_stack.setCurrentWidget(self._image_page)
            self.image_label.set_source(None)
            self.image_label.setText("Stored in iCloud — open it in Finder to download")
            self.open_full_btn.setEnabled(False)
            return

        if category == "image":
            self._show_image(self._path)
        elif category in ("video", "audio"):
            self._show_media(self._path, category)
        else:
            self._show_generic(category)

    def release(self) -> None:
        """Stops and tears down any active player. Call when navigating away from the
        page hosting this panel (in addition to the automatic `hideEvent` release)."""
        self._release_player()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(event)
        self._release_player()

    # -- metadata --------------------------------------------------------------

    def _set_metadata(self, name: str, size_text: str, meta_lines: list[str], note: str) -> None:
        self.name_label.setText(name)
        self.size_label.setText(size_text)
        while self.meta_layout.count():
            item = self.meta_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for line in meta_lines:
            lbl = QLabel(line)
            lbl.setProperty("role", "subheading")
            lbl.setWordWrap(True)
            self.meta_layout.addWidget(lbl)
        self.dims_label.setText("")
        self.note_label.setText(note or "")

    # -- images ------------------------------------------------------------

    def _show_image(self, path: Path) -> None:
        self.media_stack.setCurrentWidget(self._image_page)
        small = thumbcache.get(path)
        if small is not None:
            pixmap = QPixmap(str(small))
            if not pixmap.isNull():
                self.image_label.setText("")
                self.image_label.set_source(pixmap)
                self._full_pixmap = pixmap
                self.open_full_btn.setEnabled(True)
        else:
            self.image_label.set_source(None)
            self.image_label.setText("Loading preview…")
            self.open_full_btn.setEnabled(False)
        self._pool.start(_ImagePreviewTask(path, self._image_signals))

    def _on_image_preview_ready(self, path_str: str, cache_path, dims) -> None:
        if self._path is None or path_str != str(self._path) or self._category != "image":
            return  # selection moved on while this was loading
        if cache_path is None:
            if self._full_pixmap is None:
                self.image_label.setText("No preview available")
            return
        pixmap = QPixmap(str(cache_path))
        if pixmap.isNull():
            return
        self.image_label.setText("")
        self.image_label.set_source(pixmap)
        self._full_pixmap = pixmap
        self.open_full_btn.setEnabled(True)
        if dims:
            self.dims_label.setText(f"Dimensions: {dims[0]} × {dims[1]}")
        if self._viewer_window is not None and self._viewer_window.isVisible():
            self._viewer_window.set_pixmap(pixmap)

    def _open_full_size(self) -> None:
        if self._full_pixmap is None or self._full_pixmap.isNull() or self._path is None:
            return
        self._viewer_window = ImageViewerWindow(self._full_pixmap, self._path.name, self)
        self._viewer_window.show()

    # -- video / audio -------------------------------------------------------

    def _show_media(self, path: Path, category: str) -> None:
        self._is_video = category == "video"
        self.media_stack.setCurrentWidget(self._video_page)
        self.video_area_stack.setCurrentWidget(self.poster_label)
        self.video_message_label.setText("")

        poster = thumbcache.get(path)
        if poster is not None:
            pixmap = QPixmap(str(poster))
            if not pixmap.isNull():
                self.poster_label.setText("")
                self.poster_label.set_source(pixmap)
            else:
                self.poster_label.setText("")
                self.poster_label.set_source(None)
        else:
            self.poster_label.set_source(None)
            self.poster_label.setText("Loading preview…")
        self._pool.start(_PosterTask(path, self._poster_signals))

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(self.volume_slider.value() / 100)
        self._audio_output.setMuted(self.mute_btn.isChecked())
        self._player.setAudioOutput(self._audio_output)
        if self._is_video:
            self._player.setVideoOutput(self.video_widget)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self.play_btn.setEnabled(True)
        self._stall_timer.start(_STALL_TIMEOUT_MS)

    def _on_poster_ready(self, path_str: str, cache_path) -> None:
        if self._path is None or path_str != str(self._path) or self._category not in ("video", "audio"):
            return
        if cache_path is None:
            if self.poster_label.source() is None:
                self.poster_label.setText("No preview available")
            return
        pixmap = QPixmap(str(cache_path))
        if pixmap.isNull():
            return
        self.poster_label.setText("")
        self.poster_label.set_source(pixmap)

    def _toggle_playback(self) -> None:
        if self._player is None or not self.play_btn.isEnabled():
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            if self._is_video:
                self.video_area_stack.setCurrentWidget(self.video_widget)
            self._player.play()

    def _on_duration_changed(self, duration_ms: int) -> None:
        if self.sender() is not self._player:
            return
        self._duration_ms = duration_ms
        self.seek_slider.setRange(0, max(0, duration_ms))
        if duration_ms > 0:
            self.dims_label.setText(f"Duration: {human_elapsed(duration_ms / 1000)}")
            self._stall_timer.stop()  # got real metadata — this file is playable
        self._update_time_label(self._player.position() if self._player else 0)

    def _on_position_changed(self, position_ms: int) -> None:
        if self.sender() is not self._player:
            return
        if not self._seeking:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(position_ms)
            self.seek_slider.blockSignals(False)
        self._update_time_label(position_ms)

    def _update_time_label(self, position_ms: int) -> None:
        self.time_label.setText(f"{human_elapsed(position_ms / 1000)} / {human_elapsed(self._duration_ms / 1000)}")

    def _on_playback_state_changed(self, state) -> None:
        if self.sender() is not self._player:
            return
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_btn.setIcon(QApplication.style().standardIcon(icon))

    def _on_player_error(self, error, error_string: str) -> None:
        if self.sender() is not self._player:
            return
        if error == QMediaPlayer.Error.NoError:
            return
        self._show_playback_error()

    def _on_stall_timeout(self) -> None:
        # Fires only if nothing (neither real duration metadata nor errorOccurred) showed
        # up within _STALL_TIMEOUT_MS — see the constant's docstring for why this exists.
        if self._player is None or self._duration_ms > 0:
            return
        self._show_playback_error()

    def _show_playback_error(self) -> None:
        self._stall_timer.stop()
        if self._player is not None:
            self._player.stop()
        self.video_area_stack.setCurrentWidget(self.poster_label)
        self.video_message_label.setText("This video can't be played — the file may be damaged")
        self.play_btn.setEnabled(False)
        self.seek_slider.setEnabled(False)

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_moved(self, value: int) -> None:
        self._update_time_label(value)

    def _on_seek_released(self) -> None:
        self._seeking = False
        if self._player is not None:
            self._player.setPosition(self.seek_slider.value())

    def _on_volume_changed(self, value: int) -> None:
        if self._audio_output is not None:
            self._audio_output.setVolume(value / 100)

    def _on_mute_toggled(self, checked: bool) -> None:
        if self._audio_output is not None:
            self._audio_output.setMuted(checked)
        icon = QStyle.StandardPixmap.SP_MediaVolumeMuted if checked else QStyle.StandardPixmap.SP_MediaVolume
        self.mute_btn.setIcon(QApplication.style().standardIcon(icon))

    def _release_player(self) -> None:
        self._stall_timer.stop()
        if self._player is not None:
            player, self._player = self._player, None
            try:
                player.stop()
                player.setVideoOutput(None)
                player.setAudioOutput(None)
            except RuntimeError:
                pass  # underlying C++ object already gone
            player.deleteLater()
        if self._audio_output is not None:
            audio, self._audio_output = self._audio_output, None
            audio.deleteLater()
        self._duration_ms = 0
        self._seeking = False
        self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setValue(0)
        self.seek_slider.blockSignals(False)
        self.seek_slider.setEnabled(True)
        self.time_label.setText("0:00 / 0:00")
        self.play_btn.setEnabled(False)
        self.play_btn.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.video_message_label.setText("")

    # -- generic (documents/archives/other) -----------------------------------

    def _show_generic(self, category: str | None) -> None:
        self.media_stack.setCurrentWidget(self._icon_page)
        icon = QApplication.style().standardIcon(_ICON_BY_CATEGORY.get(category, QStyle.StandardPixmap.SP_FileIcon))
        self.icon_label.setPixmap(icon.pixmap(96, 96))

    def _reveal_in_finder(self) -> None:
        if self._path is None:
            return
        try:
            subprocess.run(["open", "-R", str(self._path)], check=False)
        except OSError:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path.parent)))
