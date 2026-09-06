"""Unit tests for PreviewPanel's state machine: selection -> player created/released,
unsupported category -> icon path, cloud placeholder -> thumbcache never touched.

Runs offscreen (no real window needed); video playback exercises the real QMediaPlayer
against a tiny real .mp4 built with ffmpeg when it's available on PATH, and is skipped
otherwise rather than faked, since the whole point is to prove the real player object
gets created and released.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image

from salvage.engine import thumbcache

from PySide6.QtWidgets import QApplication  # noqa: E402

from salvage.ui.preview_panel import PreviewPanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbcache, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def panel(app):
    p = PreviewPanel()
    yield p
    p.release()
    p.deleteLater()


def _wait_until(app: QApplication, predicate, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        app.processEvents()
        time.sleep(0.01)


def _jpeg(path: Path, size=(64, 48)) -> None:
    Image.new("RGB", size, (10, 20, 30)).save(path, "JPEG")


# ---------------------------------------------------------------------------
# No selection / clearing
# ---------------------------------------------------------------------------


def test_no_selection_shows_placeholder(panel):
    panel.show_item(None, None)
    assert panel.media_stack.currentWidget() is panel._image_page
    assert panel.image_label.text() == "Select a file to preview it."
    assert panel.name_label.text() == ""
    assert panel._player is None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_image_selection_renders_and_no_player_created(app, panel, tmp_path):
    img = tmp_path / "photo.jpg"
    _jpeg(img)

    panel.show_item(img, "image", name="photo.jpg", size_text="1 KB", meta_lines=["Taken: today"])

    assert panel.media_stack.currentWidget() is panel._image_page
    assert panel.name_label.text() == "photo.jpg"
    assert panel._player is None

    _wait_until(app, lambda: panel._full_pixmap is not None and not panel._full_pixmap.isNull())
    assert panel.open_full_btn.isEnabled()


def test_image_dimensions_reported_once_background_load_completes(app, panel, tmp_path):
    img = tmp_path / "photo.jpg"
    _jpeg(img, size=(200, 100))

    panel.show_item(img, "image", name="photo.jpg", size_text="1 KB")

    _wait_until(app, lambda: "Dimensions" in panel.dims_label.text())
    assert "200" in panel.dims_label.text() and "100" in panel.dims_label.text()


def test_preview_only_image_is_labelled(panel, tmp_path):
    img = tmp_path / "photo.jpg"
    _jpeg(img)

    panel.show_item(img, "image", name="photo.jpg", size_text="1 KB", preview_only=True)

    labels = [panel.meta_layout.itemAt(i).widget().text() for i in range(panel.meta_layout.count())]
    assert any("iCloud" in text for text in labels)


# ---------------------------------------------------------------------------
# Cloud placeholders — must never touch thumbcache (would trigger a download)
# ---------------------------------------------------------------------------


def test_cloud_placeholder_never_touches_thumbcache(panel, tmp_path, monkeypatch):
    img = tmp_path / "photo.jpg"
    _jpeg(img)

    def _boom(*args, **kwargs):
        raise AssertionError("thumbcache must never be called for a cloud placeholder")

    monkeypatch.setattr(thumbcache, "get", _boom)
    monkeypatch.setattr(thumbcache, "ensure", _boom)
    monkeypatch.setattr(thumbcache, "get_preview", _boom)
    monkeypatch.setattr(thumbcache, "ensure_preview", _boom)

    panel.show_item(img, "image", name="photo.jpg", size_text="1 KB", cloud_placeholder=True)

    assert panel.image_label.text() == "Stored in iCloud — open it in Finder to download"
    assert panel._player is None
    assert not panel.open_full_btn.isEnabled()


# ---------------------------------------------------------------------------
# Unsupported / generic categories -> icon + Reveal in Finder, never a player
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", ["document", "archive", "other"])
def test_generic_category_shows_icon_path(panel, tmp_path, category):
    f = tmp_path / f"file.{category}"
    f.write_bytes(b"data")

    panel.show_item(f, category, name=f.name, size_text="4 B")

    assert panel.media_stack.currentWidget() is panel._icon_page
    assert not panel.icon_label.pixmap().isNull()
    assert panel._player is None
    assert panel.reveal_btn.isEnabled()


# ---------------------------------------------------------------------------
# Video: player created on selection, released on selection change / hide / release()
# ---------------------------------------------------------------------------


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@pytest.fixture
def sample_video(tmp_path):
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not available")
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5",
            "-t", "1", "-pix_fmt", "yuv420p", str(path),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return path


def test_video_selection_creates_player(app, panel, sample_video):
    panel.show_item(sample_video, "video", name="sample.mp4", size_text="1 KB")

    assert panel._player is not None
    assert panel.media_stack.currentWidget() is panel._video_page
    _wait_until(app, lambda: panel._duration_ms > 0)
    assert panel.seek_slider.maximum() == panel._duration_ms


def test_selecting_a_new_item_releases_previous_player(app, panel, sample_video, tmp_path):
    panel.show_item(sample_video, "video", name="sample.mp4", size_text="1 KB")
    assert panel._player is not None

    img = tmp_path / "photo.jpg"
    _jpeg(img)
    panel.show_item(img, "image", name="photo.jpg", size_text="1 KB")

    assert panel._player is None


def test_release_stops_player(app, panel, sample_video):
    panel.show_item(sample_video, "video", name="sample.mp4", size_text="1 KB")
    assert panel._player is not None

    panel.release()

    assert panel._player is None
    assert panel._audio_output is None
    assert not panel.play_btn.isEnabled()


def test_hide_releases_player(app, panel, sample_video):
    panel.show()
    panel.show_item(sample_video, "video", name="sample.mp4", size_text="1 KB")
    assert panel._player is not None

    panel.hide()

    assert panel._player is None


def test_truncated_video_reports_error_not_crash(app, panel, sample_video, tmp_path):
    truncated = tmp_path / "truncated.mp4"
    with open(sample_video, "rb") as src:
        truncated.write_bytes(src.read(2000))

    panel.show_item(truncated, "video", name="truncated.mp4", size_text="2 KB")
    assert panel._player is not None
    panel._toggle_playback()

    _wait_until(app, lambda: panel.video_message_label.text() != "", timeout_s=15.0)
    assert "can't be played" in panel.video_message_label.text()
    assert not panel.play_btn.isEnabled()
    # poster stays visible, never crashes/hangs the process
    assert panel.video_area_stack.currentWidget() is panel.poster_label
