# Salvage

Free, open-source file recovery for macOS (Windows/Linux support is scaffolded but untested). A friendly desktop front end over proven open-source engines — the part paid tools like Recoverit, Disk Drill and EaseUS actually charge for.

- **Drives, USB sticks, SD cards, disk images** — deleted-file recovery via [PhotoRec](https://www.cgsecurity.org/wiki/PhotoRec) (signature carving, 480+ formats). Quick scan (free space only) or Deep scan (every sector). Thumbnails, previews, filter by type, recover the ones you want.
- **iPhone / iPad** — pulls a local backup over USB via [libimobiledevice](https://libimobiledevice.org), then reads Messages, WhatsApp, Contacts, Notes and Recently Deleted photos out of it, including items sitting in iOS's own Recently Deleted holding areas. Works on existing Finder/iTunes backups too.
- **Photos & videos on this Mac** — for the Mac's own drive, where raw scanning is impossible (see below). Searches every place media hides — Photos library and its Recently Deleted, Messages attachments, iCloud Drive, cloud-sync folders, iPhone backups — de-duplicates by content, and flags Messages media that has vanished from the Mac but survives in an iPhone backup.
- No paywall, no "pay to unlock the files we found".

## Honest limits

- A recovery tool can only get back what's still on the media. Files whose sectors were overwritten are gone.
- Carved files come back with generated names (`f0002059.jpg`), not the originals — that's how signature recovery works.
- iOS zeroes deleted database rows (`secure_delete`), so truly deleted messages/contacts/notes cannot be carved off a modern iPhone. What is recoverable: anything in Recently Deleted (Messages, Notes, Photos), and anything in older backups.
- iCloud-optimised photo libraries keep only thumbnails on the phone; full-size Recently Deleted photos are restored from iCloud Photos, not from the device.
- Call history is only present in **encrypted** backups. Encrypted backups aren't supported yet.
- **The Mac's own startup disk can't be carved.** Tested exhaustively: a root helper registered with `SMAppService` (prototype in `helper/`) does inherit the app's Full Disk Access and can read unmounted disks raw, but the kernel refuses raw reads of any *mounted* volume ("device is not readable"), and the APFS container underneath is FileVault ciphertext. No tool gets past that on a running Apple Silicon Mac; SSD TRIM would zero deleted blocks anyway. Use the "Photos & videos on this Mac" finder instead — it needs Full Disk Access (System Settings → Privacy & Security) to read the Photos library and Messages.
- Builds are signed with a local "Salvage Dev" identity (`packaging/build_mac.sh`) so a Full Disk Access grant survives rebuilds; ad-hoc signatures change every build and silently invalidate it.
- Never recover files onto the drive you're recovering from. Salvage refuses to.

## Run from source

```bash
brew install testdisk libimobiledevice   # PhotoRec + iPhone tools
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m salvage
```

Scanning a real device (not an image file) triggers the standard macOS administrator prompt once per scan; PhotoRec needs raw disk access.

Try the whole flow without touching any real drive or phone:

```bash
SALVAGE_FAKE=1 .venv/bin/python -m salvage
```

## Build the .app

```bash
packaging/build_mac.sh   # → dist/Salvage.app (PhotoRec and iPhone tools bundled)
```

The bundle is signed with a local self-signed identity (created on first build via `security import`; falls back to ad-hoc), not notarised: first launch needs right-click → Open.

## Tests

```bash
.venv/bin/python -m pytest
```

## Layout

| Path | What |
|---|---|
| `salvage/engine/photorec.py` | PhotoRec wrapper: pty-streamed progress, cancel, log-based success check |
| `salvage/engine/privileged.py` | One-time admin prompt for raw device scans (launchd-backed on macOS) |
| `salvage/engine/local_media.py` | Media finder for the Mac: Photos library, Messages, cloud folders, iOS backups |
| `helper/` | SMAppService root-helper prototype (opt-in build; see `helper/DESIGN.md`) |
| `salvage/engine/devices.py` | Disk/partition listing for macOS, Windows, Linux |
| `salvage/engine/results.py` | Collect recovered files, copy to destination by category |
| `salvage/engine/ios.py` | iPhone detection, backup, Manifest.db reader, DCIM access |
| `salvage/engine/ios_parsers.py` | Messages, WhatsApp, Contacts, Notes, trashed photos + exports |
| `salvage/engine/sqlite_recover.py` | Free-page / unallocated-space row carving for SQLite |
| `salvage/ui/` | PySide6 wizard |
| `packaging/` | PyInstaller spec, build script, third-party licences |

## Licences

Salvage is MIT. It bundles PhotoRec/TestDisk (GPLv2) and libimobiledevice (LGPL) unmodified — see `packaging/THIRD_PARTY.md`.
