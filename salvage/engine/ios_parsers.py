"""Parsers for iOS backup artefacts: Messages, WhatsApp, Contacts, Notes, trashed Photos.

Each parse_* reads the live rows with plain sqlite3, then (if include_deleted)
merges in whatever sqlite_recover.recover_records can find in freed space,
filtered for plausibility and de-duplicated against the live rows.
"""

from __future__ import annotations

import csv
import gzip
import html
import re
import sqlite3
import struct
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from salvage.engine.sqlite_recover import recover_records

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_DATE_MIN = datetime(2007, 1, 1, tzinfo=timezone.utc)
_DATE_MAX = datetime(2026, 12, 31, tzinfo=timezone.utc)
# Apple timestamps switched from seconds-since-2001 to nanoseconds-since-2001
# around iOS 11. 1e11 sits safely between "seconds through ~2038" (~1.1e9) and
# "nanoseconds today" (~7e17), so it cleanly separates the two encodings.
_NS_THRESHOLD = 100_000_000_000


@dataclass
class Message:
    id: int | None
    chat: str
    sender: str
    is_from_me: bool
    text: str
    date: datetime | None
    service: str  # "iMessage" / "SMS" / "WhatsApp"
    attachments: list[str] = field(default_factory=list)
    deleted: bool = False


@dataclass
class Contact:
    name: str
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    organisation: str | None = None
    deleted: bool = False


@dataclass
class Note:
    title: str
    body: str
    folder: str | None
    modified: datetime | None
    deleted: bool = False


@dataclass
class TrashedPhoto:
    filename: str
    directory: str
    trashed_date: datetime | None
    thumbnail_path: Path | None
    kind: str  # "image" / "video"


def _apple_date(raw) -> datetime | None:
    if not raw:
        return None
    try:
        seconds = raw / 1_000_000_000 if abs(raw) > _NS_THRESHOLD else raw
        return _APPLE_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, OSError, TypeError):
        return None


def _col_index_map(db: Path, table: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        info = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    finally:
        conn.close()
    return {row[1]: row[0] for row in info}


def _get(values: tuple, cols: dict[str, int], name: str):
    idx = cols.get(name)
    if idx is None or idx >= len(values):
        return None
    return values[idx]


# ---------------------------------------------------------------------------
# Messages (SMS/iMessage)
# ---------------------------------------------------------------------------

# Messages stores rich text as an NSKeyedArchiver "streamtyped" blob when the
# `text` column is NULL (iOS 16+). We don't unpack the full archive format —
# just find the NSString payload: "NSString\x01" (or "NSMutableString\x01" for
# the rare all-object-replacement-character case) is followed by a couple of
# class-reference control bytes, a '+' marker, then a length-prefixed run of
# UTF-8 bytes. Length is one byte if <0x80, or 0x81 + little-endian uint16, or
# 0x82 + little-endian uint32 for longer strings.
_ATTR_BODY_MARKERS = (b"NSString\x01", b"NSMutableString\x01")


def _decode_attributed_body(blob: bytes) -> str:
    idx = -1
    for marker in _ATTR_BODY_MARKERS:
        idx = blob.find(marker)
        if idx != -1:
            break
    if idx == -1:
        return ""
    plus = blob.find(b"+", idx, idx + 24)
    if plus == -1:
        return ""
    pos = plus + 1
    if pos >= len(blob):
        return ""
    length_byte = blob[pos]
    try:
        if length_byte == 0x81:
            length = struct.unpack("<H", blob[pos + 1 : pos + 3])[0]
            pos += 3
        elif length_byte == 0x82:
            length = struct.unpack("<I", blob[pos + 1 : pos + 5])[0]
            pos += 5
        else:
            length = length_byte
            pos += 1
    except struct.error:
        return ""
    raw = blob[pos : pos + length]
    text = raw.decode("utf-8", errors="replace")
    return text.replace("￼", "").strip()


def parse_messages(sms_db: Path, include_deleted: bool = True) -> list[Message]:
    sms_db = Path(sms_db)
    conn = sqlite3.connect(f"file:{sms_db}?mode=ro", uri=True)

    handles = {rowid: hid for rowid, hid in conn.execute("SELECT ROWID, id FROM handle")}

    chat_info: dict[int, tuple[str | None, str | None]] = {}
    for rowid, display_name, chat_identifier in conn.execute(
        "SELECT ROWID, display_name, chat_identifier FROM chat"
    ):
        chat_info[rowid] = (display_name, chat_identifier)

    chat_handles: dict[int, list[str]] = {}
    for chat_id, handle_id in conn.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
        chat_handles.setdefault(chat_id, []).append(handles.get(handle_id, "?"))

    msg_chat: dict[int, int] = {}
    for chat_id, message_id in conn.execute("SELECT chat_id, message_id FROM chat_message_join"):
        msg_chat[message_id] = chat_id

    attachments: dict[int, list[str]] = {}
    for message_id, filename in conn.execute(
        "SELECT j.message_id, a.filename FROM message_attachment_join j "
        "JOIN attachment a ON a.ROWID = j.attachment_id"
    ):
        if filename:
            attachments.setdefault(message_id, []).append(Path(filename).name)

    def chat_label(message_id: int, handle_id: int) -> str:
        chat_id = msg_chat.get(message_id)
        if chat_id in chat_info:
            display_name, chat_identifier = chat_info[chat_id]
            if display_name:
                return display_name
            names = chat_handles.get(chat_id, [])
            if len(names) > 1:
                return ", ".join(names)
            if chat_identifier:
                return chat_identifier
        return handles.get(handle_id, "Unknown")

    messages: list[Message] = []
    query = "SELECT ROWID, text, attributedBody, handle_id, is_from_me, date, service FROM message"
    for rowid, text, body, handle_id, is_from_me, date, service in conn.execute(query):
        if not text and body:
            text = _decode_attributed_body(body)
        sender = "Me" if is_from_me else handles.get(handle_id, "Unknown")
        messages.append(
            Message(
                id=rowid,
                chat=chat_label(rowid, handle_id),
                sender=sender,
                is_from_me=bool(is_from_me),
                text=text or "",
                date=_apple_date(date),
                service=service or "SMS",
                attachments=attachments.get(rowid, []),
                deleted=False,
            )
        )
    conn.close()

    if include_deleted:
        cols = _col_index_map(sms_db, "message")
        existing = {(m.date, m.text, m.sender) for m in messages}
        for values in recover_records(sms_db, "message"):
            candidate = _message_from_recovered(values, cols, handles)
            if candidate is None:
                continue
            key = (candidate.date, candidate.text, candidate.sender)
            if key in existing:
                continue
            existing.add(key)
            messages.append(candidate)

    return messages


def _message_from_recovered(
    values: tuple, cols: dict[str, int], handles: dict[int, str]
) -> Message | None:
    date = _apple_date(_get(values, cols, "date"))
    if date is None or not (_DATE_MIN <= date <= _DATE_MAX):
        return None
    is_from_me = _get(values, cols, "is_from_me")
    if is_from_me not in (0, 1):
        return None
    text = _get(values, cols, "text")
    if not isinstance(text, str) or not text:
        body = _get(values, cols, "attributedBody")
        text = _decode_attributed_body(bytes(body)) if isinstance(body, (bytes, bytearray)) else ""
    if not text:
        return None
    handle_id = _get(values, cols, "handle_id")
    sender = "Me" if is_from_me else handles.get(handle_id, "Unknown")
    service = _get(values, cols, "service")
    if not isinstance(service, str) or not service:
        service = "SMS"
    return Message(
        id=None,
        chat=sender,
        sender=sender,
        is_from_me=bool(is_from_me),
        text=text,
        date=date,
        service=service,
        attachments=[],
        deleted=True,
    )


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


def parse_whatsapp(
    chatstorage: Path, contacts_v2: Path | None, include_deleted: bool = True
) -> list[Message]:
    chatstorage = Path(chatstorage)
    conn = sqlite3.connect(f"file:{chatstorage}?mode=ro", uri=True)

    names: dict[str, str] = {}
    if contacts_v2 is not None and Path(contacts_v2).exists():
        cconn = sqlite3.connect(f"file:{contacts_v2}?mode=ro", uri=True)
        for jid, full_name in cconn.execute(
            "SELECT ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT WHERE ZWHATSAPPID IS NOT NULL"
        ):
            if full_name:
                names[jid] = full_name
        cconn.close()

    chat_names: dict[int, str] = {}
    for pk, jid, partner_name in conn.execute(
        "SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME FROM ZWACHATSESSION"
    ):
        chat_names[pk] = names.get(jid, partner_name) or jid or "Unknown"

    media: dict[int, str] = {}
    for message_pk, path in conn.execute(
        "SELECT ZMESSAGE, ZMEDIALOCALPATH FROM ZWAMEDIAITEM WHERE ZMEDIALOCALPATH IS NOT NULL"
    ):
        media[message_pk] = Path(path).name

    messages: list[Message] = []
    query = (
        "SELECT Z_PK, ZCHATSESSION, ZISFROMME, ZFROMJID, ZTOJID, ZMESSAGEDATE, ZTEXT "
        "FROM ZWAMESSAGE"
    )
    for pk, chat_pk, is_from_me, from_jid, to_jid, msg_date, text in conn.execute(query):
        chat = chat_names.get(chat_pk, "Unknown")
        sender_jid = to_jid if is_from_me else from_jid
        sender = "Me" if is_from_me else names.get(sender_jid, sender_jid or "Unknown")
        messages.append(
            Message(
                id=pk,
                chat=chat,
                sender=sender,
                is_from_me=bool(is_from_me),
                text=text or "",
                date=_apple_date(msg_date),
                service="WhatsApp",
                attachments=[media[pk]] if pk in media else [],
                deleted=False,
            )
        )
    conn.close()

    if include_deleted:
        cols = _col_index_map(chatstorage, "ZWAMESSAGE")
        existing = {(m.date, m.text, m.sender) for m in messages}
        for values in recover_records(chatstorage, "ZWAMESSAGE"):
            date = _apple_date(_get(values, cols, "ZMESSAGEDATE"))
            if date is None or not (_DATE_MIN <= date <= _DATE_MAX):
                continue
            is_from_me = _get(values, cols, "ZISFROMME")
            if is_from_me not in (0, 1):
                continue
            text = _get(values, cols, "ZTEXT")
            if not isinstance(text, str) or not text:
                continue
            from_jid = _get(values, cols, "ZFROMJID")
            sender = "Me" if is_from_me else names.get(from_jid, from_jid or "Unknown")
            key = (date, text, sender)
            if key in existing:
                continue
            existing.add(key)
            messages.append(
                Message(
                    id=None,
                    chat=sender,
                    sender=sender,
                    is_from_me=bool(is_from_me),
                    text=text,
                    date=date,
                    service="WhatsApp",
                    attachments=[],
                    deleted=True,
                )
            )

    return messages


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

_AB_PHONE_PROPERTY = 3
_AB_EMAIL_PROPERTY = 4


def parse_contacts(addressbook: Path, include_deleted: bool = True) -> list[Contact]:
    addressbook = Path(addressbook)
    conn = sqlite3.connect(f"file:{addressbook}?mode=ro", uri=True)

    phones: dict[int, list[str]] = {}
    emails: dict[int, list[str]] = {}
    for record_id, prop, value in conn.execute(
        "SELECT record_id, property, value FROM ABMultiValue WHERE property IN (?, ?)",
        (_AB_PHONE_PROPERTY, _AB_EMAIL_PROPERTY),
    ):
        if not value:
            continue
        (phones if prop == _AB_PHONE_PROPERTY else emails).setdefault(record_id, []).append(value)

    contacts: list[Contact] = []
    for rowid, first, last, org in conn.execute(
        "SELECT ROWID, First, Last, Organization FROM ABPerson"
    ):
        name = " ".join(p for p in (first, last) if p) or org or "Unknown"
        contacts.append(
            Contact(
                name=name,
                phones=phones.get(rowid, []),
                emails=emails.get(rowid, []),
                organisation=org,
                deleted=False,
            )
        )
    conn.close()

    if include_deleted:
        cols = _col_index_map(addressbook, "ABPerson")
        existing_names = {c.name for c in contacts}
        for values in recover_records(addressbook, "ABPerson"):
            first = _get(values, cols, "First")
            last = _get(values, cols, "Last")
            org = _get(values, cols, "Organization")
            org = org if isinstance(org, str) else None
            name_parts = [p for p in (first, last) if isinstance(p, str) and p]
            name = " ".join(name_parts) or org
            if not name or name in existing_names:
                continue
            existing_names.add(name)
            contacts.append(Contact(name=name, phones=[], emails=[], organisation=org, deleted=True))

    return contacts


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

# The note body lives in a gzip-compressed protobuf: root message, field 2
# ("document", length-delimited) -> field 3 ("note", length-delimited) ->
# field 2 (the plain-text body, length-delimited). No protobuf dependency
# needed: a minimal varint/wire-type walker is enough to pull that one string.


def _pb_read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            return result, pos
        shift += 7


def _pb_get_length_delimited_field(data: bytes, field_no: int) -> bytes | None:
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _pb_read_varint(data, pos)
        wire_type = tag & 0x7
        fn = tag >> 3
        if wire_type == 0:
            _, pos = _pb_read_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 2:
            length, pos = _pb_read_varint(data, pos)
            value = data[pos : pos + length]
            if fn == field_no:
                return value
            pos += length
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    return None


def _extract_note_text(gzipped: bytes) -> str:
    try:
        raw = gzip.decompress(gzipped)
        document = _pb_get_length_delimited_field(raw, 2)
        if document is None:
            return ""
        note = _pb_get_length_delimited_field(document, 3)
        if note is None:
            return ""
        text = _pb_get_length_delimited_field(note, 2)
        if text is None:
            return ""
        return text.decode("utf-8", errors="replace")
    except (OSError, ValueError, IndexError):
        return ""


def parse_notes(notestore: Path, include_deleted: bool = True) -> list[Note]:
    notestore = Path(notestore)
    conn = sqlite3.connect(f"file:{notestore}?mode=ro", uri=True)

    folders: dict[int, tuple[str, bool]] = {}
    for pk, title, folder_type in conn.execute(
        "SELECT Z_PK, ZTITLE2, ZFOLDERTYPE FROM ZICCLOUDSYNCINGOBJECT WHERE ZTITLE2 IS NOT NULL"
    ):
        folders[pk] = (title, folder_type == 1)

    notes: list[Note] = []
    query = (
        "SELECT d.ZDATA, o.ZTITLE1, o.ZFOLDER, o.ZMARKEDFORDELETION, o.ZMODIFICATIONDATE "
        "FROM ZICNOTEDATA d JOIN ZICCLOUDSYNCINGOBJECT o ON o.Z_PK = d.ZNOTE"
    )
    for zdata, title, folder_pk, marked_deleted, mod_date in conn.execute(query):
        body = _extract_note_text(zdata) if zdata else ""
        folder_name, folder_deleted = folders.get(folder_pk, (None, False))
        notes.append(
            Note(
                title=title or "",
                body=body,
                folder=folder_name,
                modified=_apple_date(mod_date),
                deleted=bool(marked_deleted) or folder_deleted,
            )
        )
    conn.close()

    if include_deleted:
        cols = _col_index_map(notestore, "ZICNOTEDATA")
        existing_bodies = {n.body for n in notes if n.body}
        for values in recover_records(notestore, "ZICNOTEDATA"):
            zdata = _get(values, cols, "ZDATA")
            if not isinstance(zdata, (bytes, bytearray)):
                continue
            body = _extract_note_text(bytes(zdata))
            if not body or body in existing_bodies:
                continue
            existing_bodies.add(body)
            notes.append(Note(title="", body=body, folder=None, modified=None, deleted=True))

    return notes


# ---------------------------------------------------------------------------
# Trashed photos
# ---------------------------------------------------------------------------


def parse_trashed_photos(photos_db: Path, reader) -> list[TrashedPhoto]:
    photos_db = Path(photos_db)
    conn = sqlite3.connect(f"file:{photos_db}?mode=ro", uri=True)

    dest_dir = photos_db.parent / "trashed_thumbnails"
    results: list[TrashedPhoto] = []
    for filename, directory, trashed_date, kind in conn.execute(
        "SELECT ZFILENAME, ZDIRECTORY, ZTRASHEDDATE, ZKIND FROM ZASSET WHERE ZTRASHEDSTATE = 1"
    ):
        thumbnail_path = None
        if filename and directory:
            relative = f"Media/PhotoData/Thumbnails/V2/{directory}/{filename}/5005.JPG"
            matches = reader.find(domain="CameraRollDomain", relative_path_like=relative)
            exact = [m for m in matches if m.relative_path == relative]
            if exact:
                dest = dest_dir / f"{Path(filename).stem}.jpg"
                thumbnail_path = reader.extract(exact[0], dest)
        results.append(
            TrashedPhoto(
                filename=filename or "",
                directory=directory or "",
                trashed_date=_apple_date(trashed_date),
                thumbnail_path=thumbnail_path,
                kind="video" if kind == 1 else "image",
            )
        )
    conn.close()
    return results


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def export_messages_html(msgs: Iterable[Message], dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    by_chat: dict[str, list[Message]] = {}
    for m in msgs:
        by_chat.setdefault(m.chat, []).append(m)

    parts = [
        "<html><head><meta charset='utf-8'><title>Messages</title>",
        "<style>",
        "body{font-family:-apple-system,sans-serif;background:#f5f5f5;margin:0;padding:16px}",
        "h2{background:#fff;padding:8px 12px;border-radius:8px}",
        ".msg{max-width:70%;margin:4px 0;padding:8px 12px;border-radius:14px;clear:both}",
        ".me{background:#0b93f6;color:#fff;float:right}",
        ".them{background:#e5e5ea;color:#000;float:left}",
        ".meta{font-size:11px;opacity:.7;display:block}",
        ".deleted{outline:2px dashed #e00;outline-offset:2px}",
        ".deleted .meta::after{content:' (recovered — deleted)';color:#e00;font-weight:bold}",
        ".chat{overflow:auto;padding-bottom:8px}",
        "</style></head><body>",
    ]
    for chat, chat_msgs in by_chat.items():
        parts.append(f"<h2>{html.escape(chat)}</h2><div class='chat'>")
        for m in sorted(chat_msgs, key=lambda x: x.date or datetime.min.replace(tzinfo=timezone.utc)):
            css = "me" if m.is_from_me else "them"
            deleted_css = " deleted" if m.deleted else ""
            when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "unknown date"
            body = html.escape(m.text) or "<i>(no text)</i>"
            attach = ""
            if m.attachments:
                attach = "<br>" + ", ".join(html.escape(a) for a in m.attachments)
            parts.append(
                f"<div class='msg {css}{deleted_css}'>"
                f"<span class='meta'>{html.escape(m.sender)} · {when} · {html.escape(m.service)}</span>"
                f"{body}{attach}</div>"
            )
        parts.append("</div>")
    parts.append("</body></html>")
    dest.write_text("\n".join(parts), encoding="utf-8")
    return dest


def export_csv(rows: list, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return dest
        if is_dataclass(rows[0]):
            fieldnames = [f.name for f in fields(rows[0])]
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            for row in rows:
                writer.writerow([getattr(row, name) for name in fieldnames])
        else:
            fieldnames = list(rows[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return dest


def export_contacts_vcf(contacts: Iterable[Contact], dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for c in contacts:
        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        lines.append(f"FN:{c.name}")
        if c.organisation:
            lines.append(f"ORG:{c.organisation}")
        for phone in c.phones:
            lines.append(f"TEL:{phone}")
        for email in c.emails:
            lines.append(f"EMAIL:{email}")
        if c.deleted:
            lines.append("NOTE:Recovered — deleted contact")
        lines.append("END:VCARD")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 _-]+")


def export_notes_txt(notes: Iterable[Note], dest_dir: Path) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    for i, n in enumerate(notes):
        base = _SAFE_FILENAME_RE.sub("", n.title or "Untitled").strip() or "Untitled"
        name = base
        suffix = 1
        while name in used_names:
            suffix += 1
            name = f"{base} ({suffix})"
        used_names.add(name)
        prefix = f"{i:05d}_"
        path = dest_dir / f"{prefix}{name}.txt"
        header = f"{n.title}\n"
        if n.folder:
            header += f"Folder: {n.folder}\n"
        if n.modified:
            header += f"Modified: {n.modified.isoformat()}\n"
        if n.deleted:
            header += "(recovered — deleted)\n"
        path.write_text(header + "\n" + n.body, encoding="utf-8")
    return dest_dir
