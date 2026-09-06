"""Synthetic iOS-artefact builders shared by tests and fake (SALVAGE_FAKE=1) mode.

Provides the trimmed-but-real table schemas the parsers in ios_parsers.py
understand, small helpers to build the blob formats they decode
(attributedBody streamtyped strings, Notes' gzipped protobuf), and
build_synthetic_backup(), which assembles a full BackupReader-readable
backup directory (Manifest.db + Info.plist + Manifest.plist + payload
files) so the fake iPhone flow exercises the exact same extraction and
parsing code path as a real device backup.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import plistlib
import sqlite3
import struct
from pathlib import Path

from Crypto.Cipher import AES

MESSAGE_SCHEMA = """
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    text TEXT,
    attributedBody BLOB,
    handle_id INTEGER DEFAULT 0,
    is_from_me INTEGER DEFAULT 0,
    date INTEGER,
    service TEXT
);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT NOT NULL);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    chat_identifier TEXT,
    display_name TEXT
);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, filename TEXT);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
CREATE TABLE chat_recoverable_message_join (
    chat_id INTEGER, message_id INTEGER, delete_date INTEGER, ck_sync_state INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
"""

ADDRESSBOOK_SCHEMA = """
CREATE TABLE ABPerson (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    First TEXT, Last TEXT, Organization TEXT
);
CREATE TABLE ABMultiValue (
    UID INTEGER PRIMARY KEY, record_id INTEGER, property INTEGER, value TEXT
);
"""

NOTESTORE_SCHEMA = """
CREATE TABLE ZICNOTEDATA (
    Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB
);
CREATE TABLE ZICCLOUDSYNCINGOBJECT (
    Z_PK INTEGER PRIMARY KEY,
    ZTITLE1 VARCHAR, ZTITLE2 VARCHAR, ZFOLDER INTEGER, ZFOLDERTYPE INTEGER,
    ZMARKEDFORDELETION INTEGER, ZMODIFICATIONDATE TIMESTAMP
);
"""

WHATSAPP_SCHEMA = """
CREATE TABLE ZWAMESSAGE (
    Z_PK INTEGER PRIMARY KEY,
    ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZFROMJID VARCHAR, ZTOJID VARCHAR,
    ZMESSAGEDATE TIMESTAMP, ZTEXT VARCHAR
);
CREATE TABLE ZWACHATSESSION (
    Z_PK INTEGER PRIMARY KEY, ZCONTACTJID VARCHAR, ZPARTNERNAME VARCHAR
);
CREATE TABLE ZWAMEDIAITEM (
    Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH VARCHAR
);
"""

WHATSAPP_CONTACTS_SCHEMA = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (
    Z_PK INTEGER PRIMARY KEY, ZWHATSAPPID VARCHAR, ZFULLNAME VARCHAR
);
"""

PHOTOS_SCHEMA = """
CREATE TABLE ZASSET (
    Z_PK INTEGER PRIMARY KEY,
    ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZTRASHEDDATE TIMESTAMP,
    ZKIND INTEGER, ZTRASHEDSTATE INTEGER
);
"""

# Both of these only exist in real encrypted backups — included here regardless so the
# fake encrypted device has something to show once BackupReader decrypts them.
CALL_HISTORY_SCHEMA = """
CREATE TABLE ZCALLRECORD (
    Z_PK INTEGER PRIMARY KEY AUTOINCREMENT,
    ZADDRESS VARCHAR, ZDATE TIMESTAMP, ZDURATION REAL,
    ZORIGINATED INTEGER, ZANSWERED INTEGER, ZSERVICE_PROVIDER VARCHAR
);
"""

SAFARI_HISTORY_SCHEMA = """
CREATE TABLE history_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, visit_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE history_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, history_item INTEGER NOT NULL, visit_time REAL NOT NULL, title TEXT
);
"""


def make_db(path: Path, schema: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA secure_delete=OFF")
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return path


def fake_attributed_body(text: str) -> bytes:
    payload = text.encode("utf-8")
    assert len(payload) < 0x80
    return b"\x04\x0bstreamtyped...NSString\x01\x95\x84\x01+" + bytes([len(payload)]) + payload


def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_field(field_no: int, wire_type: int, payload: bytes) -> bytes:
    tag = _pb_varint((field_no << 3) | wire_type)
    if wire_type == 2:
        return tag + _pb_varint(len(payload)) + payload
    raise ValueError("only length-delimited fields needed for this fixture")


def fake_note_blob(text: str) -> bytes:
    note_msg = _pb_field(2, 2, text.encode("utf-8"))
    document_msg = _pb_field(3, 2, note_msg)
    root = _pb_field(2, 2, document_msg)
    return gzip.compress(root)


def _file_id(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()


def _add_manifest_file(
    conn: sqlite3.Connection, backup_dir: Path, domain: str, relative_path: str, content: bytes
) -> None:
    file_id = _file_id(domain + "/" + relative_path)
    conn.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, 1, NULL)",
        (file_id, domain, relative_path),
    )
    shard = backup_dir / file_id[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / file_id).write_bytes(content)


def build_synthetic_backup(dest_root: Path, udid: str) -> Path:
    """Builds a small but fully BackupReader-readable backup under dest_root/udid.

    Returns the backup directory path (dest_root / udid), matching
    ios.create_backup's return contract.
    """
    dest_root = Path(dest_root)
    backup_dir = dest_root / udid
    backup_dir.mkdir(parents=True, exist_ok=True)
    scratch = backup_dir / "_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    (backup_dir / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "Device Name": "Jay's iPhone (Fake)",
                "Product Type": "iPhone13,2",
                "Product Version": "17.4",
                "Last Backup Date": "2026-09-06T00:00:00Z",
            }
        )
    )
    (backup_dir / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))

    manifest_db = backup_dir / "Manifest.db"
    conn = sqlite3.connect(manifest_db)
    conn.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, "
        "flags INTEGER, file BLOB)"
    )

    # --- sms.db -----------------------------------------------------------
    sms_db = make_db(scratch / "sms.db", MESSAGE_SCHEMA)
    sconn = sqlite3.connect(sms_db)
    sconn.execute("INSERT INTO handle (ROWID, id, service) VALUES (1, '+61400000001', 'iMessage')")
    sconn.execute("INSERT INTO handle (ROWID, id, service) VALUES (2, '+61400000002', 'iMessage')")
    sconn.execute(
        "INSERT INTO chat (ROWID, guid, chat_identifier, display_name) VALUES (1, 'chat-1', '+61400000001', NULL)"
    )
    sconn.execute("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (1, 1)")
    for i in range(6):
        sconn.execute(
            "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) VALUES (?,?,?,?,?,?)",
            (f"guid-{i}", f"Fake message number {i}", 1, i % 2, 700000000000000000 + i * 1_000_000_000, "iMessage"),
        )
        sconn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (i + 1,))
    # mark the last message as staged in "Recently Deleted" (iOS 16+).
    sconn.execute(
        "INSERT INTO chat_recoverable_message_join (chat_id, message_id, delete_date) VALUES (1, 6, 800000000000000000)"
    )
    sconn.commit()
    sconn.close()
    _add_manifest_file(conn, backup_dir, "HomeDomain", "Library/SMS/sms.db", sms_db.read_bytes())

    # --- AddressBook.sqlitedb ----------------------------------------------
    ab_db = make_db(scratch / "AddressBook.sqlitedb", ADDRESSBOOK_SCHEMA)
    aconn = sqlite3.connect(ab_db)
    names = [("Ada", "Lovelace"), ("Grace", "Hopper"), ("Alan", "Turing")]
    for i, (first, last) in enumerate(names, start=1):
        aconn.execute("INSERT INTO ABPerson (ROWID, First, Last, Organization) VALUES (?, ?, ?, NULL)", (i, first, last))
        aconn.execute(
            "INSERT INTO ABMultiValue (record_id, property, value) VALUES (?, 3, ?)", (i, f"+6140000000{i}")
        )
        aconn.execute(
            "INSERT INTO ABMultiValue (record_id, property, value) VALUES (?, 4, ?)",
            (i, f"{first.lower()}@example.com"),
        )
    aconn.commit()
    aconn.close()
    _add_manifest_file(
        conn, backup_dir, "HomeDomain", "Library/AddressBook/AddressBook.sqlitedb", ab_db.read_bytes()
    )

    # --- NoteStore.sqlite ----------------------------------------------------
    notes_db = make_db(scratch / "NoteStore.sqlite", NOTESTORE_SCHEMA)
    nconn = sqlite3.connect(notes_db)
    nconn.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZFOLDERTYPE) VALUES (1, 'Notes', 0)")
    nconn.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZFOLDERTYPE) VALUES (2, 'Recently Deleted', 1)"
    )
    note_rows = [
        (10, "Shopping list", 1, "Milk\nEggs\nBread"),
        (11, "Meeting notes", 1, "Discuss Q3 roadmap"),
        (12, "Old idea", 2, "An idea I dropped"),
    ]
    for pk, title, folder, body in note_rows:
        nconn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION, ZMODIFICATIONDATE) "
            "VALUES (?, ?, ?, 0, 700000000.0)",
            (pk, title, folder),
        )
        nconn.execute("INSERT INTO ZICNOTEDATA (ZNOTE, ZDATA) VALUES (?, ?)", (pk, fake_note_blob(body)))
    nconn.commit()
    nconn.close()
    _add_manifest_file(
        conn, backup_dir, "AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite", notes_db.read_bytes()
    )

    # --- WhatsApp ------------------------------------------------------------
    chatstorage_db = make_db(scratch / "ChatStorage.sqlite", WHATSAPP_SCHEMA)
    wconn = sqlite3.connect(chatstorage_db)
    wconn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME) VALUES (1, '61400000009@s.whatsapp.net', 'Sam')"
    )
    for i in range(3):
        wconn.execute(
            "INSERT INTO ZWAMESSAGE (ZCHATSESSION, ZISFROMME, ZFROMJID, ZTOJID, ZMESSAGEDATE, ZTEXT) "
            "VALUES (1, ?, '61400000009@s.whatsapp.net', NULL, ?, ?)",
            (i % 2, 700000000 + i * 1000, f"Fake WhatsApp message {i}"),
        )
    wconn.commit()
    wconn.close()
    _add_manifest_file(
        conn,
        backup_dir,
        "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
        "ChatStorage.sqlite",
        chatstorage_db.read_bytes(),
    )

    contacts_v2_db = make_db(scratch / "ContactsV2.sqlite", WHATSAPP_CONTACTS_SCHEMA)
    cconn = sqlite3.connect(contacts_v2_db)
    cconn.execute(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (ZWHATSAPPID, ZFULLNAME) VALUES ('61400000009@s.whatsapp.net', 'Sam')"
    )
    cconn.commit()
    cconn.close()
    _add_manifest_file(
        conn,
        backup_dir,
        "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
        "ContactsV2.sqlite",
        contacts_v2_db.read_bytes(),
    )

    # --- CallHistory.storedata (encrypted backups only) ------------------------
    calls_db = make_db(scratch / "CallHistory.storedata", CALL_HISTORY_SCHEMA)
    ccconn = sqlite3.connect(calls_db)
    call_rows = [
        ("+61400000005", 700000100.0, 42.0, 1, 1, "com.apple.CoreTelephony"),
        ("+61400000006", 700003600.0, 0.0, 0, 0, "com.apple.CoreTelephony"),
        ("+61400000005", 700007200.0, 305.0, 1, 1, "FaceTime"),
    ]
    for address, date, duration, originated, answered, service in call_rows:
        ccconn.execute(
            "INSERT INTO ZCALLRECORD (ZADDRESS, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, "
            "ZSERVICE_PROVIDER) VALUES (?, ?, ?, ?, ?, ?)",
            (address, date, duration, originated, answered, service),
        )
    ccconn.commit()
    ccconn.close()
    _add_manifest_file(
        conn, backup_dir, "HomeDomain", "Library/CallHistoryDB/CallHistory.storedata", calls_db.read_bytes()
    )

    # --- Safari History.db (encrypted backups only) -----------------------------
    safari_db = make_db(scratch / "History.db", SAFARI_HISTORY_SCHEMA)
    safconn = sqlite3.connect(safari_db)
    safari_rows = [
        ("https://example.com/", "Example Domain", 700000200.0, 3),
        ("https://www.assemblygrowth.com/", "Assembly Growth", 700003800.0, 7),
    ]
    for url, title, visit_time, visit_count in safari_rows:
        safconn.execute("INSERT INTO history_items (url, visit_count) VALUES (?, ?)", (url, visit_count))
        item_id = safconn.execute("SELECT id FROM history_items WHERE url = ?", (url,)).fetchone()[0]
        safconn.execute(
            "INSERT INTO history_visits (history_item, visit_time, title) VALUES (?, ?, ?)",
            (item_id, visit_time, title),
        )
    safconn.commit()
    safconn.close()
    _add_manifest_file(conn, backup_dir, "HomeDomain", "Library/Safari/History.db", safari_db.read_bytes())

    # --- Photos.sqlite (trashed photos) ---------------------------------------
    photos_db = make_db(scratch / "Photos.sqlite", PHOTOS_SCHEMA)
    pconn = sqlite3.connect(photos_db)
    trashed = [
        ("IMG_0001.HEIC", "202601", 0),
        ("IMG_0002.HEIC", "202601", 0),
        ("IMG_0003.MOV", "202602", 1),
    ]
    for filename, directory, kind in trashed:
        pconn.execute(
            "INSERT INTO ZASSET (ZFILENAME, ZDIRECTORY, ZTRASHEDDATE, ZKIND, ZTRASHEDSTATE) "
            "VALUES (?, ?, 700000000.0, ?, 1)",
            (filename, directory, kind),
        )
    pconn.commit()
    pconn.close()
    _add_manifest_file(conn, backup_dir, "CameraRollDomain", "Media/PhotoData/Photos.sqlite", photos_db.read_bytes())

    # A real thumbnail for the first trashed photo so the results grid has
    # something to display.
    try:
        from PIL import Image
        import io

        buf = io.BytesIO()
        Image.new("RGB", (160, 160), (120, 170, 220)).save(buf, "JPEG")
        thumb_bytes = buf.getvalue()
    except ImportError:
        thumb_bytes = b""
    if thumb_bytes:
        _add_manifest_file(
            conn,
            backup_dir,
            "CameraRollDomain",
            "Media/PhotoData/Thumbnails/V2/202601/IMG_0001.HEIC/5005.JPG",
            thumb_bytes,
        )

    conn.commit()
    conn.close()

    for f in scratch.iterdir():
        f.unlink()
    scratch.rmdir()

    return backup_dir


# ---------------------------------------------------------------------------
# Encrypted variant — builds the same backup, then encrypts it in place using
# our own reverse of ios.py's decrypt path (BackupKeyBag TLV, RFC 3394 key
# wrap, AES-256-CBC/zero-IV). Exercises the same format BackupReader parses,
# without needing a real encrypted device backup.
# ---------------------------------------------------------------------------

_FIXTURE_PROTECTION_CLASS = 4


def _tlv(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(value)) + value


def _aes_key_wrap(kek: bytes, key: bytes) -> bytes:
    """RFC 3394 AES key wrap (the encrypt-direction inverse of ios._aes_key_unwrap)."""
    n = len(key) // 8
    r = [key[8 * i : 8 * i + 8] for i in range(n)]
    a = b"\xa6\xa6\xa6\xa6\xa6\xa6\xa6\xa6"
    cipher = AES.new(kek, AES.MODE_ECB)
    for j in range(6):
        for i in range(1, n + 1):
            block = cipher.encrypt(a + r[i - 1])
            t = n * j + i
            a = (int.from_bytes(block[:8], "big") ^ t).to_bytes(8, "big")
            r[i - 1] = block[8:]
    return a + b"".join(r)


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size) or block_size
    return data + bytes([pad_len]) * pad_len


def _aes_cbc_encrypt_zero_iv(key: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).encrypt(plaintext)


def build_synthetic_encrypted_backup(dest_root: Path, udid: str, password: str) -> Path:
    """Builds the same fixture as build_synthetic_backup, then encrypts every
    payload file plus Manifest.db, and writes a BackupKeyBag/ManifestKey that
    `password` unlocks — a real, self-consistent encrypted backup that
    BackupReader can decrypt exactly like a real device's.
    """
    backup_dir = build_synthetic_backup(dest_root, udid)
    _encrypt_backup_in_place(backup_dir, password)
    return backup_dir


def _encrypt_backup_in_place(backup_dir: Path, password: str) -> None:
    dpsl = hashlib.sha256(b"salvage-fixture-dpsl").digest()[:16]
    salt = hashlib.sha256(b"salvage-fixture-salt").digest()[:20]
    dpic = 1000
    iterations = 1000
    intermediate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), dpsl, dpic, dklen=32)
    passcode_key = hashlib.pbkdf2_hmac("sha1", intermediate, salt, iterations, dklen=32)

    class_key = os.urandom(32)
    wpky = _aes_key_wrap(passcode_key, class_key)
    keybag = b"".join(
        _tlv(tag, value)
        for tag, value in [
            ("TYPE", struct.pack(">I", 0)),
            ("VERS", struct.pack(">I", 2)),
            ("UUID", os.urandom(16)),
            ("HMCK", os.urandom(20)),
            ("WRAP", struct.pack(">I", 3)),
            ("SALT", salt),
            ("ITER", struct.pack(">I", iterations)),
            ("DPWT", struct.pack(">I", 0)),
            ("DPIC", struct.pack(">I", dpic)),
            ("DPSL", dpsl),
            ("UUID", os.urandom(16)),
            ("CLAS", struct.pack(">I", _FIXTURE_PROTECTION_CLASS)),
            ("WRAP", struct.pack(">I", 2)),
            ("KTYP", struct.pack(">I", 0)),
            ("WPKY", wpky),
        ]
    )

    manifest_db_path = backup_dir / "Manifest.db"
    conn = sqlite3.connect(manifest_db_path)
    rows = conn.execute("SELECT fileID, file FROM Files WHERE flags = 1").fetchall()
    for file_id, file_blob in rows:
        path_on_disk = backup_dir / file_id[:2] / file_id
        plaintext = path_on_disk.read_bytes()
        file_key = os.urandom(32)
        enc_key_blob = struct.pack("<I", _FIXTURE_PROTECTION_CLASS) + _aes_key_wrap(class_key, file_key)
        path_on_disk.write_bytes(_aes_cbc_encrypt_zero_iv(file_key, _pkcs7_pad(plaintext)))

        try:
            parsed = plistlib.loads(file_blob) if file_blob else None
        except Exception:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("$objects"), list):
            parsed = {
                "$archiver": "NSKeyedArchiver",
                "$version": 100000,
                "$objects": ["$null", {"Size": len(plaintext)}, {"$classname": "MBFile"}],
                "$top": {"root": plistlib.UID(1)},
            }
        for obj in parsed["$objects"]:
            if isinstance(obj, dict) and "Size" in obj:
                obj["EncryptionKey"] = enc_key_blob
                obj["ProtectionClass"] = _FIXTURE_PROTECTION_CLASS
                break
        conn.execute(
            "UPDATE Files SET file = ? WHERE fileID = ?",
            (plistlib.dumps(parsed, fmt=plistlib.FMT_BINARY), file_id),
        )
    conn.commit()
    conn.close()

    manifest_file_key = os.urandom(32)
    manifest_key_blob = struct.pack("<I", _FIXTURE_PROTECTION_CLASS) + _aes_key_wrap(
        class_key, manifest_file_key
    )
    plaintext_manifest = manifest_db_path.read_bytes()
    manifest_db_path.write_bytes(
        _aes_cbc_encrypt_zero_iv(manifest_file_key, _pkcs7_pad(plaintext_manifest))
    )

    (backup_dir / "Manifest.plist").write_bytes(
        plistlib.dumps(
            {"IsEncrypted": True, "BackupKeyBag": keybag, "ManifestKey": manifest_key_blob}
        )
    )
