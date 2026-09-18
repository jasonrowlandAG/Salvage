"""End-to-end correctness test for the "Photos & videos on this Mac" search.

Plants a fully synthetic $HOME covering every MediaSource kind local_media.py knows
about (plain folders, iCloud Drive, an on-demand cloud-storage mount, a Photos library,
Messages attachments + chat.db, and an iPhone backup), plus a pile of decoys that must
never surface. Then drives the REAL UI offscreen (source page -> media options -> media
scan -> media results), exactly like a user would: ticking sources, adding the iPhone
backup through the "Add a folder..." picker handler, starting the scan, and asserting
against the real results model — exact file set, badges, year sidebar, search, filters,
and export.

No personal data, no real HOME touched: HOME/Path.home() are monkeypatched to a tmp_path
directory for the duration of the test, and the on-disk thumbnail cache is redirected
into tmp_path too.
"""
from __future__ import annotations

import io
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image

from bench.corpus import HEIC_FIXTURE
from salvage.engine import ios_fixtures
from salvage.engine import local_media as lm
from salvage.engine import thumbcache
from tests.test_local_media import _write_jpeg_with_exif, _write_mp4_stub

pytestmark = pytest.mark.e2e

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from salvage.ui.app import MainWindow  # noqa: E402


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _pad_to(path: Path, min_bytes: int = 20_500) -> None:
    """Pads a media file past the 20KB min-size threshold with trailing junk bytes.

    Every format used here (JPEG/HEIC/WEBP/GIF/MP4) tolerates trailing bytes after its
    real end-of-data marker — decoders stop at the marker and never look further.
    """
    size = path.stat().st_size
    if size < min_bytes:
        with open(path, "ab") as f:
            f.write(b"\x00" * (min_bytes - size))


def _jpeg(path: Path, year: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jpeg_with_exif(path, f"{year}:06:01 12:00:00", size=(20, 20))
    _pad_to(path)
    # Salt with the path so same-year images aren't byte-identical (dedup would merge them).
    with open(path, "ab") as f:
        f.write(str(path).encode())


def _mp4(path: Path, year: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_mp4_stub(path, datetime(year, 6, 1, 12, 0, 0))
    _pad_to(path)


def _real_jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    """A real, Pillow-decodable JPEG (unlike raw placeholder bytes) so the thumbnail
    service can actually generate a thumbnail for it."""
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buf, "JPEG")
    return buf.getvalue()


def _mtime_dated(path: Path, year: int) -> None:
    ts = datetime(year, 6, 1, 12, 0, 0).timestamp()
    os.utime(path, (ts, ts))


def _heic(path: Path, year: int) -> None:
    # Copied from the checked-in fixture rather than encoded: Salvage ships a decode-only
    # libheif, so there is no HEVC encoder here any more (packaging/THIRD_PARTY.md).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEIC_FIXTURE.read_bytes())
    _pad_to(path)
    _mtime_dated(path, year)


def _webp(path: Path, year: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), (40, 50, 60)).save(path, "WEBP")
    _pad_to(path)
    _mtime_dated(path, year)


def _gif(path: Path, year: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), (70, 80, 90)).save(path, "GIF")
    _pad_to(path)
    _mtime_dated(path, year)


def _process(app: QApplication, ms: float = 0) -> None:
    app.processEvents()
    if ms:
        time.sleep(ms / 1000)
        app.processEvents()


def _wait_until(app: QApplication, predicate, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        app.processEvents()
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# The big fixture-planting function
# ---------------------------------------------------------------------------


def _plant_home(tmp_path: Path) -> tuple[Path, list[dict], Path, Path]:
    """Builds the synthetic $HOME, returns (home, expected_records, ios_backup_dir, dataless_path).

    expected_records is a list of dicts describing every FoundMedia item the scan
    should surface: source_key, name, year, recovered, deleted, dup, cloud, preview_only.
    """
    home = tmp_path / "home"
    for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Movies"):
        (home / sub).mkdir(parents=True)
    (home / "Library" / "Mobile Documents" / "com~apple~CloudDocs").mkdir(parents=True)
    (home / "Library" / "CloudStorage" / "GoogleDrive-test").mkdir(parents=True)
    (home / "Library" / "Messages" / "Attachments").mkdir(parents=True)

    expected: list[dict] = []

    def rec(source_key, name, year, **flags) -> None:
        d = {"source_key": source_key, "name": name, "year": year, "recovered": False,
             "deleted": False, "dup": False, "cloud": False, "preview_only": False}
        d.update(flags)
        expected.append(d)

    # --- plain folders ------------------------------------------------------
    _jpeg(home / "Desktop" / "photo_desktop.jpg", 2015)
    rec("desktop", "photo_desktop.jpg", 2015)

    _jpeg(home / "Documents" / "nested" / "deep" / "photo_documents.jpg", 2016)
    rec("documents", "photo_documents.jpg", 2016)

    _jpeg(home / "Downloads" / "photo_downloads.jpg", 2017)
    rec("downloads", "photo_downloads.jpg", 2017)

    _jpeg(home / "Pictures" / "photo_pictures.jpg", 2018)
    rec("pictures", "photo_pictures.jpg", 2018)

    _mp4(home / "Movies" / "video_movies.mp4", 2019)
    rec("movies", "video_movies.mp4", 2019)

    _heic(home / "Desktop" / "photo_heic.heic", 2020)
    rec("desktop", "photo_heic.heic", 2020)

    _webp(home / "Downloads" / "photo_webp.webp", 2021)
    rec("downloads", "photo_webp.webp", 2021)

    _gif(home / "Pictures" / "photo_gif.gif", 2022)
    rec("pictures", "photo_gif.gif", 2022)

    _jpeg(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "photo_icloud.jpg", 2023)
    rec("icloud_drive", "photo_icloud.jpg", 2023)

    _jpeg(home / "Library" / "CloudStorage" / "GoogleDrive-test" / "photo_gdrive.jpg", 2024)
    rec("cloudstorage_GoogleDrive-test", "photo_gdrive.jpg", 2024)

    # dataless (iCloud placeholder) file — never hashed, never thumbnailed.
    dataless_path = home / "Desktop" / "photo_dataless.jpg"
    _jpeg(dataless_path, 2025)  # EXIF year is irrelevant: dataless skips EXIF entirely
    _mtime_dated(dataless_path, 2025)
    rec("desktop", "photo_dataless.jpg", 2025, cloud=True)

    # exact-duplicate pair across two sources.
    twin_bytes = _real_jpeg_bytes((123, 45, 67)) + b"\x00" * 25000
    (home / "Desktop" / "twin.jpg").write_bytes(twin_bytes)
    (home / "Downloads" / "twin.jpg").write_bytes(twin_bytes)
    _mtime_dated(home / "Desktop" / "twin.jpg", 2026)
    _mtime_dated(home / "Downloads" / "twin.jpg", 2026)
    rec("desktop", "twin.jpg", 2026)
    rec("downloads", "twin.jpg", 2026, dup=True)

    # --- decoys that must never appear --------------------------------------
    (home / "Desktop" / "notes.txt").write_bytes(b"not media")
    (home / "Desktop" / "document.pdf").write_bytes(b"%PDF-1.4 not real")
    (home / "Desktop" / ".hidden").mkdir()
    (home / "Desktop" / ".hidden" / "secret.jpg").write_bytes(b"x" * 30_000)
    (home / "Desktop" / "node_modules" / "pkg").mkdir(parents=True)
    (home / "Desktop" / "node_modules" / "pkg" / "icon.png").write_bytes(b"y" * 30_000)
    (home / "Desktop" / "Foo.app" / "Contents").mkdir(parents=True)
    (home / "Desktop" / "Foo.app" / "Contents" / "icon.png").write_bytes(b"z" * 30_000)
    small_icon = home / "Desktop" / "tiny_icon.jpg"
    _jpeg(small_icon, 2015)
    # undo the padding _jpeg applied — this one must stay under the threshold.
    small_icon.write_bytes(small_icon.read_bytes()[:5_000])
    loop_link = home / "Desktop" / "loop_link"
    loop_link.symlink_to(home / "Desktop")

    # --- Photos library -------------------------------------------------------
    lib = home / "Pictures" / "Photos Library.photoslibrary"
    lib_key = f"photoslibrary_{lib.name}"
    (lib / "database").mkdir(parents=True)
    (lib / "originals" / "A").mkdir(parents=True)
    (lib / "originals" / "C").mkdir(parents=True)
    (lib / "resources" / "derivatives" / "B").mkdir(parents=True)

    uuid_original = "AAAAAAAA-0000-0000-0000-000000000001"
    uuid_preview = "BBBBBBBB-0000-0000-0000-000000000002"
    uuid_trashed = "CCCCCCCC-0000-0000-0000-000000000003"

    (lib / "originals" / "A" / f"{uuid_original}.jpg").write_bytes(_real_jpeg_bytes((10, 20, 30)))
    derivative_bytes = _real_jpeg_bytes((200, 210, 220))
    (lib / "resources" / "derivatives" / "B" / f"{uuid_preview}_1_105_c.jpeg").write_bytes(derivative_bytes)
    (lib / "originals" / "C" / f"{uuid_trashed}.jpg").write_bytes(_real_jpeg_bytes((250, 5, 5)))

    conn = sqlite3.connect(lib / "database" / "Photos.sqlite")
    conn.execute(
        "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZFILENAME TEXT, "
        "ZDIRECTORY TEXT, ZDATECREATED REAL, ZKIND INTEGER, ZTRASHEDSTATE INTEGER, ZFAVORITE INTEGER)"
    )

    def core_data_ts(year: int) -> float:
        return datetime(year, 6, 1, 12, 0, 0).timestamp() - lm._CORE_DATA_EPOCH_OFFSET

    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZDIRECTORY, ZDATECREATED, ZKIND, ZTRASHEDSTATE, ZFAVORITE) "
        "VALUES (?, 'IMG_ORIGINAL.jpg', '', ?, 0, 0, 0)",
        (uuid_original, core_data_ts(2026)),
    )
    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZDIRECTORY, ZDATECREATED, ZKIND, ZTRASHEDSTATE, ZFAVORITE) "
        "VALUES (?, 'IMG_PREVIEW.jpg', '', ?, 0, 0, 0)",
        (uuid_preview, core_data_ts(2026)),
    )
    conn.execute(
        "INSERT INTO ZASSET (ZUUID, ZFILENAME, ZDIRECTORY, ZDATECREATED, ZKIND, ZTRASHEDSTATE, ZFAVORITE) "
        "VALUES (?, 'IMG_TRASHED.jpg', '', ?, 0, 1, 0)",
        (uuid_trashed, core_data_ts(2012)),
    )
    conn.commit()
    conn.close()

    rec(lib_key, "IMG_ORIGINAL.jpg", 2026)
    rec(lib_key, "IMG_PREVIEW.jpg", 2026, preview_only=True)
    rec(lib_key, "IMG_TRASHED.jpg", 2012, deleted=True)

    # --- Messages attachments + chat.db ----------------------------------------
    attach_dir = home / "Library" / "Messages" / "Attachments"
    existing_attachment = attach_dir / "ab" / "12" / "IMG_EXIST.jpg"
    _jpeg(existing_attachment, 2015)
    rec("messages", "IMG_EXIST.jpg", 2015)

    missing_attachment_path = attach_dir / "cd" / "34" / "IMG_MISSING1.jpg"  # never written to disk
    missing2_attachment_path = attach_dir / "cd" / "35" / "IMG_MISSING2.jpg"  # missing, no backup match

    chat_db = home / "Library" / "Messages" / "chat.db"
    cconn = sqlite3.connect(chat_db)
    cconn.execute(
        "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, mime_type TEXT)"
    )
    cconn.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, date INTEGER)")
    cconn.execute(
        "CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER)"
    )
    cconn.execute("INSERT INTO message (ROWID, date) VALUES (1, 700000000000000000)")
    cconn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type) VALUES (1, ?, 'image/jpeg')",
        (str(existing_attachment),),
    )
    cconn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type) VALUES (2, ?, 'image/jpeg')",
        (str(missing_attachment_path),),
    )
    cconn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type) VALUES (3, ?, 'image/jpeg')",
        (str(missing2_attachment_path),),
    )
    for aid in (1, 2, 3):
        cconn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, ?)", (aid,))
    cconn.commit()
    cconn.close()

    # --- iPhone backup: recovers IMG_MISSING1.jpg, decoy IMG_MISSING2.jpg stays gone ---
    backups_root = tmp_path / "external_backups"
    backups_root.mkdir()
    udid = "00001111-000222223333AAAA"
    backup_dir = ios_fixtures.build_synthetic_backup(backups_root, udid)
    ios_backup_key = f"ios_backup_{udid}"

    sms_jpeg_path = tmp_path / "_scratch_sms.jpg"
    _jpeg(sms_jpeg_path, 2020)
    camera_jpeg_path = tmp_path / "_scratch_camera.jpg"
    _jpeg(camera_jpeg_path, 2021)

    bconn = sqlite3.connect(backup_dir / "Manifest.db")
    ios_fixtures._add_manifest_file(
        bconn, backup_dir, "MediaDomain", "Library/SMS/Attachments/aa/bb/IMG_MISSING1.jpg",
        sms_jpeg_path.read_bytes(),
    )
    ios_fixtures._add_manifest_file(
        bconn, backup_dir, "CameraRollDomain", "Media/DCIM/100APPLE/IMG_CAMERAROLL1.jpg",
        camera_jpeg_path.read_bytes(),
    )
    bconn.commit()
    bconn.close()

    rec(ios_backup_key, "IMG_MISSING1.jpg", 2020, recovered=True)
    rec(ios_backup_key, "IMG_CAMERAROLL1.jpg", 2021)

    return home, expected, backup_dir, dataless_path


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

_EXPECTED_TICKED_KEY_PREFIXES = (
    "pictures", "desktop", "documents", "downloads", "movies",
    "icloud_drive", "messages", "cloudstorage_GoogleDrive-test", "photoslibrary_",
)


def test_media_search_finds_everything_and_only_that(tmp_path, monkeypatch):
    home, expected, backup_dir, dataless_path = _plant_home(tmp_path)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(thumbcache, "CACHE_DIR", tmp_path / "thumb_cache")

    # Simulate an iCloud placeholder: the real SF_DATALESS stat flag can't be set from
    # userland, so make _is_dataless report True for this one planted file's inode.
    dataless_stat = dataless_path.stat()
    real_is_dataless = lm._is_dataless

    def fake_is_dataless(st):
        if st.st_dev == dataless_stat.st_dev and st.st_ino == dataless_stat.st_ino:
            return True
        return real_is_dataless(st)

    monkeypatch.setattr(lm, "_is_dataless", fake_is_dataless)

    dest_dir = tmp_path / "export_dest"
    dest_dir.mkdir()

    app = QApplication.instance() or QApplication([])
    window = MainWindow(False, None)
    window.show()
    _process(app)

    # --- source page -> media options -----------------------------------------
    window.source_page.media_search_card.clicked.emit()
    _process(app)
    op = window.media_options_page
    assert window.stack.currentWidget() is op

    rows_by_key = {row.source.key: row for row in op._rows}
    # Google Drive must start unticked (default_on=False) even though it's accessible.
    gdrive_row = rows_by_key["cloudstorage_GoogleDrive-test"]
    assert gdrive_row.source.default_on is False
    assert gdrive_row.check.isChecked() is False

    # Tick every source we actually planted; leave everything else (real /Volumes
    # entries on this dev Mac, WhatsApp, etc.) unticked so the scan never touches
    # real data and stays fast.
    for key, row in rows_by_key.items():
        want = key.startswith(_EXPECTED_TICKED_KEY_PREFIXES)
        row.check.setChecked(want)

    # Add the iPhone backup through the real picker handler, bypassing the native dialog.
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(backup_dir)))
    op._add_folder()
    ios_backup_key = f"ios_backup_{backup_dir.name}"
    assert ios_backup_key in {row.source.key for row in op._rows}

    op._set_destination(dest_dir)
    assert op.start_btn.isEnabled()

    op._start()
    _process(app)
    assert window.stack.currentWidget() is window.media_scan_page

    _wait_until(app, lambda: window.stack.currentWidget() is window.media_results_page)

    rp = window.media_results_page
    model = rp.model
    assert model is not None

    # --- exact file set ---------------------------------------------------------
    expected_pairs = {(e["source_key"], e["name"]) for e in expected}
    actual_pairs = {(f.source_key, f.name) for f in model._all}
    assert actual_pairs == expected_pairs

    by_pair = {(e["source_key"], e["name"]): e for e in expected}
    actual_by_pair = {(f.source_key, f.name): f for f in model._all}

    # --- badges / flags ----------------------------------------------------------
    for pair, exp in by_pair.items():
        fm = actual_by_pair[pair]
        assert fm.recovered == exp["recovered"], pair
        assert fm.in_recently_deleted == exp["deleted"], pair
        assert (fm.duplicate_of is not None) == exp["dup"], pair
        assert fm.cloud_placeholder == exp["cloud"], pair
        assert fm.preview_only == exp["preview_only"], pair

    dataless_fm = actual_by_pair[("desktop", "photo_dataless.jpg")]
    assert dataless_fm.sha256 is None  # dataless items are never hashed

    # --- year sidebar ------------------------------------------------------------
    expected_year_counts: dict[str, int] = {}
    for e in expected:
        y = str(e["year"])
        expected_year_counts[y] = expected_year_counts.get(y, 0) + 1
    assert model.year_counts() == expected_year_counts

    # --- search box ---------------------------------------------------------------
    rp.search_edit.setText("photo_gdrive")
    _process(app)
    assert model.rowCount() == 1
    assert model._item(0).name == "photo_gdrive.jpg"
    rp.search_edit.setText("")
    _process(app)

    # --- "Only recovered from backups" filter -------------------------------------
    expected_recovered = sum(1 for e in expected if e["recovered"])
    rp.only_recovered_check.setChecked(True)
    _process(app)
    assert model.rowCount() == expected_recovered
    rp.only_recovered_check.setChecked(False)
    _process(app)

    # --- "Hide duplicates" toggle ---------------------------------------------------
    expected_dupes = sum(1 for e in expected if e["dup"])
    assert rp.hide_dupes_check.isChecked() is True  # default
    count_hidden = model.rowCount()
    rp.hide_dupes_check.setChecked(False)
    _process(app)
    count_shown = model.rowCount()
    assert count_shown - count_hidden == expected_dupes
    rp.hide_dupes_check.setChecked(True)
    _process(app)

    # --- export "select all (filtered)" for year 2026 -------------------------------
    # Year 2026 holds exactly: the Photos-library original, its preview-only sibling,
    # and the Desktop half of the duplicate pair (the Downloads half is hidden by the
    # default "Hide duplicates" filter) — a clean mix that also proves a preview-only
    # Photos asset exports its local derivative instead of failing.
    year_item = None
    for row in range(rp.year_list.count()):
        item = rp.year_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == "2026":
            year_item = item
            break
    assert year_item is not None
    rp.year_list.setCurrentRow(rp.year_list.row(year_item))
    _process(app)
    assert model.rowCount() == 3

    rp._select_all_filtered()
    selected = model.checked_files()
    assert len(selected) == 3
    rp._export_clicked()
    # Export now runs on a background QThread (MediaExportWorker) instead of
    # synchronously on the UI thread - wait for it to actually finish rather than
    # assuming it's done after one short sleep.
    _wait_until(app, lambda: window.stack.currentWidget() is window.done_page)

    export_dir = dest_dir / f"Salvage {window.session.timestamp}" / "Photos and videos" / "2026"
    exported = sorted(p.name for p in export_dir.iterdir())
    assert exported == sorted(f.name for f in selected)

    preview_export = export_dir / "IMG_PREVIEW.jpg"
    assert preview_export.read_bytes() == derivative_bytes_for_check(home)
    original_export = export_dir / "IMG_ORIGINAL.jpg"
    assert original_export.read_bytes() == (home / "Pictures" / "Photos Library.photoslibrary"
                                             / "originals" / "A"
                                             / "AAAAAAAA-0000-0000-0000-000000000001.jpg").read_bytes()

    # --- thumbnails ---------------------------------------------------------------
    rp.thumb_service.wait(15_000)
    _process(app)
    for e in expected:
        if e["cloud"]:
            continue
        fm = actual_by_pair[(e["source_key"], e["name"])]
        if fm.category != "image":
            continue
        assert thumbcache.get(fm.path) is not None, (e, fm.path)
    assert thumbcache.get(dataless_fm.path) is None

    rp.thumb_service.cancel()
    rp.thumb_service.wait(3000)
    window.close()


def derivative_bytes_for_check(home: Path) -> bytes:
    return (
        (home / "Pictures" / "Photos Library.photoslibrary" / "resources" / "derivatives" / "B"
         / "BBBBBBBB-0000-0000-0000-000000000002_1_105_c.jpeg").read_bytes()
    )


# ---------------------------------------------------------------------------
# min_size filter: engine-level check (the options page just forwards this value;
# re-running the whole UI flow a second time would double the runtime for no benefit).
# ---------------------------------------------------------------------------


def test_min_size_filter_excludes_and_includes_small_icon(tmp_path, monkeypatch):
    home, _expected, _backup_dir, _dataless_path = _plant_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    source = lm.MediaSource(key="desktop", label="Desktop", path=home / "Desktop", kind="folder", accessible=True)

    with_filter = lm.scan_sources([source], min_size=20_000, hash_dupes=False)
    assert "tiny_icon.jpg" not in {f.name for f in with_filter}

    without_filter = lm.scan_sources([source], min_size=0, hash_dupes=False)
    assert "tiny_icon.jpg" in {f.name for f in without_filter}
