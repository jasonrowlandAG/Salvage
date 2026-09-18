"""Resolves the iOS engine calls for the UI, with fake-mode fallbacks.

Mirrors engine_facade.py's fake/real routing pattern but for the iPhone
flow: device discovery, backup, and extraction+parsing of the selected
categories into an IOSParsedData bundle, plus writing that bundle out to
disk on export.
"""

from __future__ import annotations

import plistlib
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from salvage.engine.ios import BackupProgress, BackupReader, IOSDevice
from salvage.engine.ios_parsers import (
    Call,
    Contact,
    Message,
    Note,
    TrashedPhoto,
    Visit,
    export_contacts_vcf,
    export_csv,
    export_messages_html,
    export_notes_txt,
    parse_call_history,
    parse_contacts,
    parse_messages,
    parse_notes,
    parse_safari_history,
    parse_trashed_photos,
    parse_whatsapp,
)

CATEGORY_LABELS = {
    "messages": "Messages (iMessage/SMS)",
    "whatsapp": "WhatsApp",
    "contacts": "Contacts",
    "notes": "Notes",
    "photos": "Recently Deleted photos",
    "call_history": "Call history",
    "safari_history": "Safari history",
}

# Domain/path for Safari's per-profile history on iOS 17+ — the classic
# "HomeDomain","Library/Safari/History.db" location (BackupReader.KNOWN
# "safari_history") stops existing once Safari moves to per-profile storage.
_SAFARI_PROFILE_DOMAIN = "AppDomain-com.apple.mobilesafari"
_SAFARI_PROFILE_PATTERN = "Library/Safari/Profiles/%/History.db"


@dataclass
class IOSParsedData:
    messages: list[Message] = field(default_factory=list)
    whatsapp: list[Message] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    trashed_photos: list[TrashedPhoto] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    safari_history: list[Visit] = field(default_factory=list)


def ios_recovery_available() -> tuple[bool, str | None]:
    """Whether live iPhone/iPad recovery (device discovery + backup over USB) can
    work on this platform at all. Doesn't affect reading an *existing* backup
    folder (BackupReader is pure Python and works fine on any OS) -- only the
    idevice_id/idevicebackup2 device-facing flow.

    libimobiledevice is realistically macOS-only today: there's no official
    Windows build, and it needs Apple's own device-pairing service to talk to a
    phone at all, so a half-working Windows port would just fail confusingly
    per-device rather than explaining itself. Report that cleanly instead of
    showing a "Connect one by USB" empty state that will never find anything.
    """
    if sys.platform != "darwin":
        return False, "iPhone recovery is macOS-only for now."
    return True, None


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


def is_encrypted_backup_folder(path: Path) -> bool:
    plist_path = Path(path) / "Manifest.plist"
    if not plist_path.exists():
        return False
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    return bool(data.get("IsEncrypted", False))


def _extract_safari_history(reader: BackupReader, workdir: Path) -> Path | None:
    """Classic pre-iOS-17 path first, then the newest/largest per-profile History.db
    (iOS 17+ splits Safari history per profile — usually one real profile plus
    several near-empty ones for extensions/private browsing/CarPlay).
    """
    classic = reader.extract_known("safari_history", workdir)
    if classic is not None:
        return classic

    profiles = [
        bf
        for bf in reader.find(domain=_SAFARI_PROFILE_DOMAIN, relative_path_like=_SAFARI_PROFILE_PATTERN)
        if bf.relative_path.endswith("/History.db")
    ]
    if not profiles:
        return None
    largest = max(profiles, key=lambda bf: bf.size)
    dest = Path(workdir) / "History.db"
    return reader.extract(largest, dest)


def extract_and_parse_ios(
    backup_dir: Path,
    categories: set[str],
    workdir: Path,
    password: str | None = None,
) -> IOSParsedData:
    reader = BackupReader(backup_dir, password=password)
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
        if "call_history" in categories:
            calls_path = reader.extract_known("call_history", workdir)
            if calls_path is not None:
                data.calls = parse_call_history(calls_path)
        if "safari_history" in categories:
            safari_path = _extract_safari_history(reader, workdir)
            if safari_path is not None:
                data.safari_history = parse_safari_history(safari_path)
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
    if data.calls:
        written.append(export_csv(data.calls, dest_dir / "call_history.csv"))
    if data.safari_history:
        written.append(export_csv(data.safari_history, dest_dir / "safari_history.csv"))
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
