"""Resolves the iOS engine calls for the UI, with fake-mode fallbacks.

Mirrors engine_facade.py's fake/real routing pattern but for the iPhone
flow: device discovery, backup, and extraction+parsing of the selected
categories into an IOSParsedData bundle, plus writing that bundle out to
disk on export.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from salvage.engine.ios import BackupProgress, BackupReader, IOSDevice
from salvage.engine.ios_parsers import (
    Contact,
    Message,
    Note,
    TrashedPhoto,
    export_contacts_vcf,
    export_csv,
    export_messages_html,
    export_notes_txt,
    parse_contacts,
    parse_messages,
    parse_notes,
    parse_trashed_photos,
    parse_whatsapp,
)

CATEGORY_LABELS = {
    "messages": "Messages (iMessage/SMS)",
    "whatsapp": "WhatsApp",
    "contacts": "Contacts",
    "notes": "Notes",
    "photos": "Recently Deleted photos",
}


@dataclass
class IOSParsedData:
    messages: list[Message] = field(default_factory=list)
    whatsapp: list[Message] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    trashed_photos: list[TrashedPhoto] = field(default_factory=list)


def list_ios_devices(fake: bool) -> list[IOSDevice]:
    if fake:
        from salvage.engine.fake import fake_ios_devices
        return fake_ios_devices()
    from salvage.engine.ios import list_ios_devices as _list
    return _list()


def create_ios_backup(
    fake: bool,
    udid: str,
    backup_root: Path,
    on_progress: Callable[[BackupProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    if fake:
        from salvage.engine.fake import FakeIOSBackup
        return FakeIOSBackup().run(udid, backup_root, on_progress, cancel)
    from salvage.engine.ios import create_backup as _create_backup
    return _create_backup(udid, backup_root, on_progress=on_progress, cancel=cancel)


def is_valid_backup_folder(path: Path) -> bool:
    return (Path(path) / "Manifest.db").exists()


def extract_and_parse_ios(backup_dir: Path, categories: set[str], workdir: Path) -> IOSParsedData:
    reader = BackupReader(backup_dir)
    data = IOSParsedData()
    try:
        if "messages" in categories:
            sms_path = reader.extract_known("sms", workdir)
            if sms_path is not None:
                data.messages = parse_messages(sms_path)
        if "whatsapp" in categories:
            chat_path = reader.extract_known("whatsapp", workdir)
            contacts_path = reader.extract_known("whatsapp_contacts", workdir)
            if chat_path is not None:
                data.whatsapp = parse_whatsapp(chat_path, contacts_path)
        if "contacts" in categories:
            ab_path = reader.extract_known("contacts", workdir)
            if ab_path is not None:
                data.contacts = parse_contacts(ab_path)
        if "notes" in categories:
            notes_path = reader.extract_known("notes", workdir)
            if notes_path is not None:
                data.notes = parse_notes(notes_path)
        if "photos" in categories:
            photos_path = reader.extract_known("photos_db", workdir)
            if photos_path is not None:
                data.trashed_photos = parse_trashed_photos(photos_path, reader)
    finally:
        reader.close()
    return data


def export_ios_results(data: IOSParsedData, dest_dir: Path) -> list[Path]:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if data.messages:
        written.append(export_messages_html(data.messages, dest_dir / "messages.html"))
        written.append(export_csv(data.messages, dest_dir / "messages.csv"))
    if data.whatsapp:
        written.append(export_messages_html(data.whatsapp, dest_dir / "whatsapp.html"))
    if data.contacts:
        written.append(export_contacts_vcf(data.contacts, dest_dir / "contacts.vcf"))
        written.append(export_csv(data.contacts, dest_dir / "contacts.csv"))
    if data.notes:
        written.append(export_notes_txt(data.notes, dest_dir / "notes"))
    if data.trashed_photos:
        photos_dir = dest_dir / "recently_deleted_photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        for p in data.trashed_photos:
            if p.thumbnail_path is not None and Path(p.thumbnail_path).exists():
                target = photos_dir / Path(p.thumbnail_path).name
                shutil.copy2(p.thumbnail_path, target)
                written.append(target)
        written.append(photos_dir)

    return written
