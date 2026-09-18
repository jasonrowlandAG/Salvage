from __future__ import annotations

import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest

from salvage.engine.ios_fixtures import (
    ADDRESSBOOK_SCHEMA as _ADDRESSBOOK_SCHEMA,
    CALL_HISTORY_SCHEMA as _CALL_HISTORY_SCHEMA,
    MESSAGE_SCHEMA as _MESSAGE_SCHEMA,
    NOTESTORE_SCHEMA as _NOTESTORE_SCHEMA,
    SAFARI_HISTORY_SCHEMA as _SAFARI_HISTORY_SCHEMA,
    WHATSAPP_SCHEMA as _WHATSAPP_SCHEMA,
    fake_attributed_body as _fake_attributed_body,
    fake_note_blob as _fake_note_blob,
)
from salvage.engine.ios_parsers import (
    Contact,
    Message,
    Note,
    export_contacts_vcf,
    export_csv,
    export_messages_html,
    export_notes_txt,
    parse_call_history,
    parse_contacts,
    parse_messages,
    parse_notes,
    parse_safari_history,
    parse_whatsapp,
)
from salvage.engine.sqlite_recover import recover_records

# Schemas are trimmed to the columns the parsers use, but with real column
# names/types/PRIMARY KEY layout so the on-disk cell format matches genuine
# iOS databases. Shared with salvage.engine.fake.FakeIOSBackup so the fake
# iPhone flow exercises the same parser code paths as these tests.


def _make_db(tmp_path: Path, name: str, schema: str) -> Path:
    db_path = tmp_path / name
    conn = _connect(db_path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    """secure_delete is a per-connection pragma, not a database-file setting, so it
    has to be set again on every fresh connection -- not just the one _make_db() used
    to create the schema. Without it, a DELETE's freed bytes may be immediately
    zeroed rather than left recoverable, and whether that's SQLite's default with no
    pragma set at all depends on how the linked libsqlite3 was built: off by default
    on macOS's bundled SQLite, on by default on Ubuntu's system package -- which is
    exactly why these recovery tests passed on macOS CI but failed on Ubuntu CI."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA secure_delete=OFF")
    return conn


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_parse_messages_live_text_chat_and_attachment(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, '+15551234567', 'iMessage')")
    conn.execute(
        "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, 'chat-1', '+15551234567', NULL)"
    )
    conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) "
        "VALUES ('m1', 'hello there', 1, 0, 700000000000000000, 'iMessage')"
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
    conn.execute("INSERT INTO attachment (guid, filename) VALUES ('a1', '/var/mobile/photo.jpg')")
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    msgs = parse_messages(db, include_deleted=False)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.text == "hello there"
    assert m.chat == "+15551234567"
    assert m.sender == "+15551234567"
    assert m.is_from_me is False
    assert m.attachments == ["photo.jpg"]
    assert m.date is not None
    assert m.date.year == 2023  # 700000000000000000 ns after 2001-01-01
    assert m.deleted is False


def test_parse_messages_decodes_attributed_body_when_text_is_null(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, '+15551234567', 'iMessage')")
    blob = _fake_attributed_body("decoded from attributedBody")
    conn.execute(
        "INSERT INTO message (guid, text, attributedBody, handle_id, is_from_me, date, service) "
        "VALUES ('m1', NULL, ?, 1, 1, 700000000000000000, 'iMessage')",
        (blob,),
    )
    conn.commit()
    conn.close()

    msgs = parse_messages(db, include_deleted=False)
    assert msgs[0].text == "decoded from attributedBody"
    assert msgs[0].sender == "Me"


def test_parse_messages_group_chat_uses_display_name_or_joined_handles(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, 'alice@example.com', 'iMessage')")
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (2, 'bob@example.com', 'iMessage')")
    conn.execute(
        "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, 'chat-1', 'grp1', 'Trip Planning')"
    )
    conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
    conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 2)")
    conn.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) "
        "VALUES ('m1', 'hi group', 1, 0, 700000000000000000, 'iMessage')"
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    msgs = parse_messages(db, include_deleted=False)
    assert msgs[0].chat == "Trip Planning"


def _insert_messages(conn, n: int) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) VALUES (?,?,?,?,?,?)",
            (f"guid-{i}", f"hello world message number {i}", 1, i % 2, 700000000000000000 + i * 1_000_000_000, "iMessage"),
        )


def test_recover_records_finds_deleted_message_reclaimed_into_gap(tmp_path):
    # SQLite reclaims a deleted cell straight into the page's unallocated gap
    # (no freeblock stamp, so nothing is lost) when it was the most recently
    # written cell on the page — i.e. the highest ROWID(s). That's the
    # reliable recovery path this module targets.
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    _insert_messages(conn, 60)
    conn.commit()
    conn.execute("DELETE FROM message WHERE ROWID = (SELECT MAX(ROWID) FROM message)")
    conn.commit()
    conn.close()

    recovered = recover_records(db, "message", min_match=0.7)
    assert len(recovered) >= 1


def test_recover_records_bounds_hostile_page_count_in_sparse_file(tmp_path):
    # A hostile/corrupt SQLite header can declare a page_count far beyond
    # what real data the file holds. Before the fix, recover_records() would
    # iterate `range(1, page_count + 1)` unconditionally - a sparse file made
    # *apparently* consistent with a huge page_count (a few KB actually on
    # disk, tens of GB of logical/apparent size, exactly as someone else's
    # corrupt or crafted iPhone backup could look) drove an effectively
    # uncancellable, multi-second-to-multi-minute scan with no work cap.
    # It must now bound its own work and return promptly regardless.
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = sqlite3.connect(db)
    _insert_messages(conn, 5)
    conn.commit()
    conn.close()

    hostile_page_count = 12_000_000  # ~46 GB logical size if trusted outright

    with open(db, "r+b") as f:
        header = bytearray(f.read(100))
        page_size = struct.unpack(">H", bytes(header[16:18]))[0]
        if page_size == 1:
            page_size = 65536
        header[28:32] = struct.pack(">I", hostile_page_count)
        f.seek(0)
        f.write(header)
        # Sparse-extend so the file's *apparent* size matches the hostile
        # claim (this writes ~1 byte to disk, not tens of GB).
        f.seek(page_size * hostile_page_count - 1)
        f.write(b"\x00")

    start = time.monotonic()
    recovered = recover_records(db, "message", min_match=0.7)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"recover_records() took {elapsed:.1f}s against a hostile page_count"
    assert isinstance(recovered, list)


def test_recover_records_honours_cancel_event(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = sqlite3.connect(db)
    _insert_messages(conn, 60)
    conn.commit()
    conn.execute("DELETE FROM message WHERE ROWID = (SELECT MAX(ROWID) FROM message)")
    conn.commit()
    conn.close()

    cancel = threading.Event()
    cancel.set()  # already cancelled before the scan starts

    recovered = recover_records(db, "message", min_match=0.7, cancel=cancel)
    assert recovered == []


def test_parse_messages_marks_recently_deleted_from_recoverable_join(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, '+15551234567', 'iMessage')")
    conn.execute(
        "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, 'chat-1', '+15551234567', NULL)"
    )
    conn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) "
        "VALUES ('m1', 'still here but recently deleted', 1, 0, 700000000000000000, 'iMessage')"
    )
    conn.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) "
        "VALUES ('m2', 'never touched', 1, 0, 700000000000000000, 'iMessage')"
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
    # message ROWID 1 is staged for deletion (iOS 16+ Recently Deleted).
    conn.execute(
        "INSERT INTO chat_recoverable_message_join (chat_id, message_id, delete_date) VALUES (1, 1, 800000000000000000)"
    )
    conn.commit()
    conn.close()

    msgs = parse_messages(db, include_deleted=False)
    assert len(msgs) == 2
    by_text = {m.text: m for m in msgs}
    assert by_text["still here but recently deleted"].deleted is True
    assert by_text["never touched"].deleted is False


def test_parse_messages_merges_recovered_deleted_rows(tmp_path):
    db = _make_db(tmp_path, "sms.db", _MESSAGE_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, '+15551234567', 'iMessage')")
    _insert_messages(conn, 60)
    conn.commit()
    last_rowid = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
    last_text = conn.execute("SELECT text FROM message WHERE ROWID=?", (last_rowid,)).fetchone()[0]
    conn.execute("DELETE FROM message WHERE ROWID=?", (last_rowid,))
    conn.commit()
    conn.close()

    msgs = parse_messages(db, include_deleted=True)
    deleted = [m for m in msgs if m.deleted]
    assert any(m.text == last_text for m in deleted)
    for m in deleted:
        assert m.date is not None
        assert m.is_from_me in (True, False)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def test_parse_contacts_live(tmp_path):
    db = _make_db(tmp_path, "AddressBook.sqlitedb", _ADDRESSBOOK_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO ABPerson (ROWID, First, Last, Organization) VALUES (1, 'Ada', 'Lovelace', NULL)")
    conn.execute("INSERT INTO ABMultiValue (record_id, property, value) VALUES (1, 3, '+15551234567')")
    conn.execute("INSERT INTO ABMultiValue (record_id, property, value) VALUES (1, 4, 'ada@example.com')")
    conn.commit()
    conn.close()

    contacts = parse_contacts(db, include_deleted=False)
    assert len(contacts) == 1
    assert contacts[0].name == "Ada Lovelace"
    assert contacts[0].phones == ["+15551234567"]
    assert contacts[0].emails == ["ada@example.com"]
    assert contacts[0].deleted is False


def test_parse_contacts_recovers_deleted_person(tmp_path):
    db = _make_db(tmp_path, "AddressBook.sqlitedb", _ADDRESSBOOK_SCHEMA)
    conn = _connect(db)
    for i in range(60):
        conn.execute(
            "INSERT INTO ABPerson (First, Last, Organization) VALUES (?, ?, NULL)",
            (f"First{i}", f"Last{i}"),
        )
    conn.commit()
    last_rowid = conn.execute("SELECT MAX(ROWID) FROM ABPerson").fetchone()[0]
    conn.execute("DELETE FROM ABPerson WHERE ROWID=?", (last_rowid,))
    conn.commit()
    conn.close()

    contacts = parse_contacts(db, include_deleted=True)
    deleted = [c for c in contacts if c.deleted]
    assert any(f"First{last_rowid - 1}" in c.name for c in deleted)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


def test_parse_notes_decodes_body_and_folder(tmp_path):
    db = _make_db(tmp_path, "NoteStore.sqlite", _NOTESTORE_SCHEMA)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZFOLDERTYPE) VALUES (1, 'Notes', 0)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZFOLDERTYPE) VALUES (2, 'Recently Deleted', 1)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION, ZMODIFICATIONDATE) "
        "VALUES (10, 'Shopping list', 1, 0, 700000000.0)"
    )
    conn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION, ZMODIFICATIONDATE) "
        "VALUES (11, 'Old idea', 2, 0, 700000000.0)"
    )
    conn.execute(
        "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (10, ?)", (_fake_note_blob("Milk\nEggs\nBread"),)
    )
    conn.execute(
        "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (11, ?)", (_fake_note_blob("An idea I dropped"),)
    )
    conn.commit()
    conn.close()

    notes = parse_notes(db, include_deleted=False)
    assert len(notes) == 2
    by_title = {n.title: n for n in notes}
    assert by_title["Shopping list"].body == "Milk\nEggs\nBread"
    assert by_title["Shopping list"].folder == "Notes"
    assert by_title["Shopping list"].deleted is False
    assert by_title["Old idea"].folder == "Recently Deleted"
    assert by_title["Old idea"].deleted is True  # in the Recently Deleted folder


def _insert_note_rows(conn, n: int) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)",
            (i + 1, _fake_note_blob(f"note body number {i} with enough padding text to fill a row")),
        )


def test_parse_notes_recovers_deleted_note_body(tmp_path):
    db = _make_db(tmp_path, "NoteStore.sqlite", _NOTESTORE_SCHEMA)
    conn = _connect(db)
    _insert_note_rows(conn, 60)
    conn.commit()
    last_pk = conn.execute("SELECT MAX(Z_PK) FROM ZICNOTEDATA").fetchone()[0]
    conn.execute("DELETE FROM ZICNOTEDATA WHERE Z_PK=?", (last_pk,))
    conn.commit()
    conn.close()

    notes = parse_notes(db, include_deleted=True)
    deleted = [n for n in notes if n.deleted]
    assert any("note body number" in n.body for n in deleted)


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


def test_parse_whatsapp_live(tmp_path):
    db = _make_db(tmp_path, "ChatStorage.sqlite", _WHATSAPP_SCHEMA)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME) VALUES (1, '61400000000@s.whatsapp.net', 'Sam')"
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (ZCHATSESSION, ZISFROMME, ZFROMJID, ZTOJID, ZMESSAGEDATE, ZTEXT) "
        "VALUES (1, 0, '61400000000@s.whatsapp.net', NULL, 700000000, 'hey there')"
    )
    conn.commit()
    conn.close()

    msgs = parse_whatsapp(db, None, include_deleted=False)
    assert len(msgs) == 1
    assert msgs[0].text == "hey there"
    assert msgs[0].service == "WhatsApp"
    assert msgs[0].chat == "Sam"
    assert msgs[0].is_from_me is False


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def test_export_messages_html_groups_by_chat_and_marks_deleted(tmp_path):
    msgs = [
        Message(id=1, chat="Alice", sender="Alice", is_from_me=False, text="hi", date=None, service="iMessage"),
        Message(id=None, chat="Alice", sender="Alice", is_from_me=False, text="recovered msg", date=None, service="iMessage", deleted=True),
    ]
    dest = tmp_path / "out.html"
    export_messages_html(msgs, dest)
    content = dest.read_text(encoding="utf-8")
    assert "Alice" in content
    assert "hi" in content
    assert "recovered msg" in content
    assert "deleted" in content


def test_export_csv_writes_dataclass_rows(tmp_path):
    contacts = [Contact(name="Ada Lovelace", phones=["123"], emails=[], organisation=None, deleted=False)]
    dest = tmp_path / "contacts.csv"
    export_csv(contacts, dest)
    content = dest.read_text(encoding="utf-8")
    assert "Ada Lovelace" in content
    assert "name" in content.splitlines()[0]


def test_export_contacts_vcf(tmp_path):
    contacts = [Contact(name="Ada Lovelace", phones=["+1555"], emails=["ada@example.com"], organisation="Acme")]
    dest = tmp_path / "contacts.vcf"
    export_contacts_vcf(contacts, dest)
    content = dest.read_text(encoding="utf-8")
    assert "BEGIN:VCARD" in content
    assert "FN:Ada Lovelace" in content
    assert "TEL:+1555" in content
    assert "EMAIL:ada@example.com" in content
    assert "ORG:Acme" in content


def test_export_notes_txt_writes_one_file_per_note(tmp_path):
    notes = [
        Note(title="Shopping", body="Milk", folder="Notes", modified=None, deleted=False),
        Note(title="Shopping", body="Eggs", folder="Notes", modified=None, deleted=True),
    ]
    dest_dir = tmp_path / "notes"
    export_notes_txt(notes, dest_dir)
    files = sorted(dest_dir.glob("*.txt"))
    assert len(files) == 2
    contents = [f.read_text(encoding="utf-8") for f in files]
    assert any("Milk" in c for c in contents)
    assert any("Eggs" in c and "recovered" in c for c in contents)


# ---------------------------------------------------------------------------
# Call history (encrypted backups only)
# ---------------------------------------------------------------------------


def test_parse_call_history(tmp_path):
    db = _make_db(tmp_path, "CallHistory.storedata", _CALL_HISTORY_SCHEMA)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO ZCALLRECORD (ZADDRESS, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, "
        "ZSERVICE_PROVIDER) VALUES (?, ?, ?, ?, ?, ?)",
        ("+61400000001", 700000000.0, 90.0, 1, 1, "com.apple.CoreTelephony"),
    )
    conn.execute(
        "INSERT INTO ZCALLRECORD (ZADDRESS, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, "
        "ZSERVICE_PROVIDER) VALUES (?, ?, ?, ?, ?, ?)",
        ("+61400000002", 700003600.0, 0.0, 0, 0, "FaceTime"),
    )
    conn.commit()
    conn.close()

    calls = parse_call_history(db)
    assert len(calls) == 2
    assert calls[0].address == "+61400000001"
    assert calls[0].duration_s == 90
    assert calls[0].outgoing is True
    assert calls[0].answered is True
    assert calls[0].service == "com.apple.CoreTelephony"
    assert calls[1].outgoing is False
    assert calls[1].answered is False
    assert calls[0].date is not None


def test_parse_call_history_csv_export(tmp_path):
    db = _make_db(tmp_path, "CallHistory.storedata", _CALL_HISTORY_SCHEMA)
    conn = _connect(db)
    conn.execute(
        "INSERT INTO ZCALLRECORD (ZADDRESS, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, "
        "ZSERVICE_PROVIDER) VALUES (?, ?, ?, ?, ?, ?)",
        ("+61400000001", 700000000.0, 90.0, 1, 1, "com.apple.CoreTelephony"),
    )
    conn.commit()
    conn.close()

    calls = parse_call_history(db)
    dest = tmp_path / "calls.csv"
    export_csv(calls, dest)
    content = dest.read_text(encoding="utf-8")
    assert "address" in content.splitlines()[0]
    assert "+61400000001" in content


# ---------------------------------------------------------------------------
# Safari history (encrypted backups only)
# ---------------------------------------------------------------------------


def test_parse_safari_history(tmp_path):
    db = _make_db(tmp_path, "History.db", _SAFARI_HISTORY_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO history_items (id, url, visit_count) VALUES (1, ?, 5)", ("https://example.com/",))
    conn.execute(
        "INSERT INTO history_visits (history_item, visit_time, title) VALUES (1, ?, ?)",
        (700000000.0, "Example Domain"),
    )
    conn.execute(
        "INSERT INTO history_visits (history_item, visit_time, title) VALUES (1, ?, ?)",
        (700003600.0, "Example Domain (again)"),
    )
    conn.commit()
    conn.close()

    visits = parse_safari_history(db)
    assert len(visits) == 2
    assert visits[0].url == "https://example.com/"
    assert visits[0].visit_count == 5
    assert {v.title for v in visits} == {"Example Domain", "Example Domain (again)"}
    assert all(v.date is not None for v in visits)


def test_parse_safari_history_csv_export(tmp_path):
    db = _make_db(tmp_path, "History.db", _SAFARI_HISTORY_SCHEMA)
    conn = _connect(db)
    conn.execute("INSERT INTO history_items (id, url, visit_count) VALUES (1, ?, 2)", ("https://example.com/",))
    conn.execute(
        "INSERT INTO history_visits (history_item, visit_time, title) VALUES (1, ?, ?)",
        (700000000.0, "Example Domain"),
    )
    conn.commit()
    conn.close()

    visits = parse_safari_history(db)
    dest = tmp_path / "safari_history.csv"
    export_csv(visits, dest)
    content = dest.read_text(encoding="utf-8")
    assert "url" in content.splitlines()[0]
    assert "https://example.com/" in content
