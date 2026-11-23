"""Fixes from a real failed iPhone backup on 23/11/2026.

1. "Could not perform backup protocol version exchange, error code -1" is transient and
   cleared on retry, but the app showed a generic failure (it only matched "mismatch").
2. The options page defaulted to a destination with 14 GB free for a 67 GB iPhone and said
   nothing, so the backup looked like it hung and then failed with error 105.
3. The iPhone flow offered messages/notes/contacts but none of the photos in the backup.
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from salvage.engine import ios
from salvage.engine.ios import IOSBackupError, IOSDevice


# -- 1. retry ---------------------------------------------------------------------------

def _counting_tool(tmp_path, monkeypatch, outputs):
    """A stub idevicebackup2 that prints outputs[n] on its n-th run (the last repeats)."""
    for i, (text, _code) in enumerate(outputs):
        (tmp_path / f"out{i}.txt").write_text(text)
    codes = " ".join(str(c) for _t, c in outputs)
    script = tmp_path / "idevicebackup2"
    script.write_text(
        "#!/bin/sh\n"
        f'D="{tmp_path}"\nN=$(cat "$D/n" 2>/dev/null || echo 0)\necho $((N+1)) > "$D/n"\n'
        f'LAST={len(outputs) - 1}\nI=$N; [ "$I" -gt "$LAST" ] && I=$LAST\n'
        'cat "$D/out$I.txt"\nset -- ' + codes + '\nshift $I\nexit $1\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr(ios, "_tool", lambda name: str(script))
    monkeypatch.setattr(ios, "_RETRY_DELAYS_S", (0.0, 0.0))
    return lambda: int((tmp_path / "n").read_text())


_EXCHANGE = "Could not perform backup protocol version exchange, error code -1\n"
_OK = "Received 3 files from device.\nBackup Successful.\n"


@pytest.mark.skipif(sys.platform == "win32", reason="stub tool is a POSIX shell script")
def test_transient_start_failure_is_retried_and_then_succeeds(tmp_path, monkeypatch):
    runs = _counting_tool(tmp_path, monkeypatch, [(_EXCHANGE, 1), (_OK, 0)])
    seen = []
    ios.create_backup("UDID", tmp_path / "bk", on_progress=seen.append)
    assert runs() == 2
    assert any("Retrying" in (p.current_file or "") for p in seen)


@pytest.mark.skipif(sys.platform == "win32", reason="stub tool is a POSIX shell script")
def test_persistent_start_failure_gives_up_with_actionable_advice(tmp_path, monkeypatch):
    runs = _counting_tool(tmp_path, monkeypatch, [(_EXCHANGE, 1)])
    with pytest.raises(IOSBackupError) as exc:
        ios.create_backup("UDID", tmp_path / "bk")
    assert runs() == 3                      # first try + two retries, then stop
    assert "Unlock it" in str(exc.value)
    assert not isinstance(exc.value, ios._TransientBackupError)


# -- 2. space warning -------------------------------------------------------------------

@pytest.fixture
def options_page():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from salvage.engine.fake import FakeEngine
    from salvage.ui.app import MainWindow
    return MainWindow(True, FakeEngine()).ios_options_page


def _device(used):
    return IOSDevice("U", "iPhone", "iPhone15,4", "27.2.1", 256_000_000_000, False, data_used_bytes=used)


def test_warns_when_the_destination_cannot_hold_the_phones_data(options_page, monkeypatch, tmp_path):
    import salvage.ui.ios_options_page as mod
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _p: type("U", (), {"free": 14e9}))
    options_page.set_source(_device(67e9), None)
    options_page._set_destination(tmp_path)
    assert not options_page.space_note.isHidden()
    text = options_page.space_note.text()
    assert "about 67 GB" in text and "14 GB free" in text
    assert "external drive" in text


def test_no_warning_with_plenty_of_room_or_an_existing_backup(options_page, monkeypatch, tmp_path):
    import salvage.ui.ios_options_page as mod
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _p: type("U", (), {"free": 500e9}))
    options_page.set_source(_device(67e9), None)
    options_page._set_destination(tmp_path)
    assert options_page.space_note.isHidden()
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _p: type("U", (), {"free": 1e9}))
    options_page.set_source(None, tmp_path)          # reading an existing backup needs no room
    options_page._set_destination(tmp_path)
    assert options_page.space_note.isHidden()


# -- 3. photos & videos from the backup -------------------------------------------------

def test_media_tab_model_lists_filters_and_labels_items(tmp_path):
    from datetime import datetime
    from PySide6.QtCore import Qt
    from salvage.engine.local_media import FoundMedia
    from salvage.ui.ios_results_page import MediaTableModel

    def item(name, cat, note=None):
        return FoundMedia(path=tmp_path / name, name=name, ext=name.split(".")[-1], size=2048,
                          category=cat, source_key="k", taken=datetime(2025, 3, 1),
                          modified=datetime(2025, 3, 1), sha256=None, duplicate_of=None, note=note)

    m = MediaTableModel([item("IMG_1.HEIC", "image"), item("clip.MOV", "video", "From Messages")])
    assert m.rowCount() == 2
    kinds = {m.data(m.index(r, 0)): m.data(m.index(r, 3)) for r in range(2)}
    assert kinds == {"IMG_1.HEIC": "Photo", "clip.MOV": "Video"}
    m.set_filter("clip", False)
    assert m.rowCount() == 1 and m.data(m.index(0, 4)) == "From Messages"


def test_export_copies_media_into_a_photos_and_videos_folder(tmp_path):
    from datetime import datetime
    from salvage.engine.local_media import FoundMedia
    from salvage.ui import ios_facade as F

    src = tmp_path / "blob"; src.write_bytes(b"\xff\xd8\xff" + b"x" * 200)
    fm = FoundMedia(path=src, name="IMG_9.JPG", ext="jpg", size=203, category="image", source_key="k",
                    taken=datetime(2024, 6, 1), modified=datetime(2024, 6, 1), sha256=None, duplicate_of=None)
    data = F.IOSParsedData(media=[fm])
    F.export_ios_results(data, tmp_path / "out")
    copied = list((tmp_path / "out" / "Photos and videos").rglob("IMG_9.JPG"))
    assert len(copied) == 1 and copied[0].read_bytes() == src.read_bytes()


# -- 4. dates: use the iPhone's own file time, not when the backup was written -----------

def _blob(**fields):
    import plistlib
    return plistlib.dumps(
        {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": plistlib.UID(1)},
         "$objects": ["$null", fields]},
        fmt=plistlib.FMT_BINARY,
    )


def test_manifest_file_time_reads_the_iphones_own_modification_time():
    blob = _blob(Size=5, LastModified=1_700_000_000, Birth=1_600_000_000)
    assert ios._manifest_file_time(blob) == 1_700_000_000.0
    assert ios._manifest_file_time(_blob(Size=5, Birth=1_600_000_000)) == 1_600_000_000.0
    assert ios._manifest_file_time(None) is None
    assert ios._manifest_file_time(b"not a plist") is None


def test_backup_media_is_dated_from_the_manifest_and_labelled_by_origin(tmp_path):
    """Regression: every photo in the iPhone results said "Taken <today>" (the time the
    backup was written) and the export filed them all under the current year."""
    import hashlib, plistlib, sqlite3
    from salvage.engine.local_media import scan_sources, source_from_backup_dir

    bk = tmp_path / "UDID"; bk.mkdir()
    (bk / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "x"}))
    (bk / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    conn = sqlite3.connect(bk / "Manifest.db")
    conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")

    def add(domain, rel, taken_ts, payload):
        fid = hashlib.sha1((domain + "/" + rel).encode()).hexdigest()
        conn.execute("INSERT INTO Files VALUES (?,?,?,1,?)", (fid, domain, rel, _blob(Size=len(payload), LastModified=taken_ts)))
        (bk / fid[:2]).mkdir(exist_ok=True)
        (bk / fid[:2] / fid).write_bytes(payload)

    # 2021-06-01 and 2023-03-05 - years that cannot be confused with "now".
    add("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.HEIC", 1_622_548_800, b"heic-bytes" * 4000)
    add("MediaDomain", "Library/SMS/Attachments/ab/12/IMG_0002.jpg", 1_678_000_000, b"jpg-bytes!" * 4000)
    conn.commit(); conn.close()

    items = {m.name: m for m in scan_sources([source_from_backup_dir(bk)], None, None, min_size=0, hash_dupes=False)}
    assert items["IMG_0001.HEIC"].taken.year == 2021
    assert items["IMG_0002.jpg"].taken.year == 2023
    assert items["IMG_0001.HEIC"].origin == "Camera Roll"
    assert items["IMG_0002.jpg"].origin == "Messages attachment"


def test_backup_media_dates_come_from_the_phones_databases(tmp_path):
    """A restored phone stamps every file with the day it was re-downloaded. The truthful
    dates are the message date (attachments) and the Photos capture date (Camera Roll)."""
    import hashlib, plistlib, sqlite3
    from salvage.engine.local_media import scan_sources, source_from_backup_dir

    APPLE = 978_307_200
    def apple_ns(unix):  # modern Messages stores nanoseconds since 2001
        return int((unix - APPLE) * 1e9)

    bk = tmp_path / "UDID"; bk.mkdir()
    (bk / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "x"}))
    (bk / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    mconn = sqlite3.connect(bk / "Manifest.db")
    mconn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")

    def add(domain, rel, payload):
        fid = hashlib.sha1((domain + "/" + rel).encode()).hexdigest()
        # Every file is stamped "re-downloaded in Nov 2026", as on a restored phone.
        mconn.execute("INSERT INTO Files VALUES (?,?,?,1,?)",
                      (fid, domain, rel, _blob(Size=len(payload), LastModified=1_795_000_000)))
        (bk / fid[:2]).mkdir(exist_ok=True)
        (bk / fid[:2] / fid).write_bytes(payload)

    sms = tmp_path / "sms.db"; c = sqlite3.connect(sms)
    c.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);""")
    c.execute("INSERT INTO message VALUES (1, ?)", (apple_ns(1_647_000_000),))        # Mar 2022
    c.execute("INSERT INTO attachment VALUES (1, '~/Library/SMS/Attachments/ab/12/IMG_0002.jpg')")
    c.execute("INSERT INTO message_attachment_join VALUES (1, 1)")
    c.commit(); c.close()

    ph = tmp_path / "Photos.sqlite"; c = sqlite3.connect(ph)
    c.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZFILENAME TEXT, ZDATECREATED REAL)")
    c.execute("INSERT INTO ZASSET VALUES (1, 'IMG_0001.HEIC', ?)", (1_560_000_000 - APPLE,))  # Jun 2019
    c.commit(); c.close()

    add("HomeDomain", "Library/SMS/sms.db", sms.read_bytes())
    add("CameraRollDomain", "Media/PhotoData/Photos.sqlite", ph.read_bytes())
    add("CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.HEIC", b"heic-bytes" * 4000)
    add("MediaDomain", "Library/SMS/Attachments/ab/12/IMG_0002.jpg", b"jpg-bytes!" * 4000)
    add("MediaDomain", "Library/SMS/Attachments/cd/34/IMG_0003.jpg", b"other-byte" * 4000)  # no db row
    mconn.commit(); mconn.close()

    items = {m.name: m for m in scan_sources([source_from_backup_dir(bk)], None, None, min_size=0, hash_dupes=False)}
    assert items["IMG_0001.HEIC"].taken.year == 2019      # Photos capture date
    assert items["IMG_0002.jpg"].taken.year == 2022       # date of the message that carried it
    assert items["IMG_0003.jpg"].taken.year == 2026       # no database row: manifest time, never wrong-by-crash
