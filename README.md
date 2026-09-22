# Salvage

Free, open-source file recovery for **macOS**. Windows and Linux engines are
exercised in CI; the supported install path today is **run from source** (no
Apple Developer Program, no notarized DMG).

A friendly desktop front end over proven open-source engines — the part paid
tools like Recoverit, Disk Drill and EaseUS actually charge for.

- **Drives, USB sticks, SD cards, disk images** — three scan modes:
  - **Quick** reads the filesystem's own records of deleted files ([The Sleuth Kit](https://www.sleuthkit.org)), so results keep their **original names, folders and timestamps**.
  - **Deep** carves every sector by signature ([PhotoRec](https://www.cgsecurity.org/wiki/PhotoRec), 480+ formats) — finds files after a format, but names are lost.
  - **Thorough** (default) runs both and merges by content hash: carving's recall with real filenames where they survive.
- **Every result is verified, not guessed** — structural checks (PNG chunk CRCs, ZIP member CRCs, ISO-BMFF atom walks, PDF trailers) label each file **Intact / Partial / Corrupt** with the reason, and carved copies of files that still exist are flagged "Already on disk" and hidden by default.
- **Preview before you recover** — full-size photo preview with zoom, and real video playback with seek and audio (truncated carved video fails gracefully instead of hanging).
- **iPhone / iPad** — pulls a local backup over USB via [libimobiledevice](https://libimobiledevice.org), then reads Messages, WhatsApp, Contacts, Notes and Recently Deleted photos out of it, including items sitting in iOS's own Recently Deleted holding areas. Works on existing Finder/iTunes backups too.
- **Photos & videos on this Mac** — for the Mac's own drive, where raw scanning is impossible (see below). Searches every place media hides — Photos library and its Recently Deleted, Messages attachments, iCloud Drive, cloud-sync folders, iPhone backups — de-duplicates by content, and flags Messages media that has vanished from the Mac but survives in an iPhone backup.
- No paywall, no "pay to unlock the files we found".

## Install (macOS, recommended)

Needs [Homebrew](https://brew.sh), [uv](https://github.com/astral-sh/uv), and Python 3.11+.

```bash
brew install testdisk sleuthkit libimobiledevice
git clone https://github.com/jasonrowlandAG/Salvage.git
cd Salvage
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m salvage
```

- Scanning a real device (not an image file) triggers the macOS administrator
  prompt once per scan — PhotoRec and Quick scan need raw disk access.
- **Photos & videos on this Mac** needs Full Disk Access
  (System Settings → Privacy & Security → Full Disk Access → add Terminal, or
  your Python binary, depending how you launch it).
- Never recover onto the same drive you are scanning; Salvage refuses to.

Try the whole UI without touching hardware:

```bash
SALVAGE_FAKE=1 .venv/bin/python -m salvage
```

Updates: `git pull` and reinstall if dependencies changed
(`uv pip install --python .venv/bin/python -e ".[dev]"`).

## Privacy

Salvage never sends anything over the network. All scanning, recovery, and
preview happen on your device. There is no telemetry, no analytics, no account,
and no cloud component — verify it yourself: the source is on GitHub.

More detail: [`docs/privacy.md`](docs/privacy.md). Support / GPL source
requests: **jay@assemblygrowth.com**.

## Honest limits

- A recovery tool can only get back what's still on the media. Files whose sectors were overwritten are gone — including on SSDs after **TRIM/UNMAP** has run (macOS and Windows).
- Carved files come back with generated names (`f0002059.jpg`), not the originals — that's how signature recovery works.
- iOS zeroes deleted database rows (`secure_delete`), so truly deleted messages/contacts/notes cannot be carved off a modern iPhone. What is recoverable: anything in Recently Deleted (Messages, Notes, Photos), and anything in older backups.
- iCloud-optimised photo libraries keep only thumbnails on the phone; full-size Recently Deleted photos are restored from iCloud Photos, not from the device.
- Call history and Health data are only present in **encrypted** backups. Salvage reads encrypted backups when you enter the backup password (used locally only).
- **The Mac's own startup disk can't be carved.** The kernel refuses raw reads of any *mounted* volume, and the APFS container underneath is often FileVault ciphertext. SSD TRIM would zero deleted blocks anyway. Use the "Photos & videos on this Mac" finder instead.
- Never recover files onto the drive you're recovering from. Salvage refuses to.

## Optional: build a `.app` yourself

For a double-clickable bundle on **your** Mac (not a public download):

```bash
packaging/build_mac.sh   # → dist/Salvage.app
```

The app is signed with a local self-signed "Salvage Dev" identity so Full Disk
Access grants survive rebuilds. It is **not** Apple-notarized. On another Mac,
Gatekeeper will reject it until the user right-clicks → **Open**. We are not
shipping a notarized DMG (that requires the paid Apple Developer Program).

## Tests

```bash
.venv/bin/python -m pytest
```

CI runs the suite on macOS, Windows, and Ubuntu on every push.

## Measuring accuracy

Recovery quality is measured, not asserted. `bench/` builds real FAT32/exFAT/HFS+/APFS
images, applies seven loss scenarios (delete, deleted folder tree, quick format, partial
overwrite, fragmentation, emptied trash), and scores recall, precision, name/path/date
accuracy and integrity-verdict correctness per engine.

```bash
.venv/bin/python -m bench.run --engines photorec,filesystem,combined \
    --filesystems fat32,exfat --scenarios all --out bench/results/run.json
```

Latest results live in `bench/results/latest.md`. Highlights: Thorough reaches carving-level
recall (90% on a deleted folder tree, up from 15% for filesystem records alone) at 100%
precision, with 100% name and path accuracy on exFAT, and a **0% false-Intact rate** — the
verifier has never labelled a byte-damaged file as intact on this corpus.

Known weak spots, documented rather than hidden: JPEG/HEIC decoders tolerate bit-flips
inside image data, so mid-stream corruption can still read as Intact; MP4 verification walks
the box tree but does not scan inside multi-gigabyte payloads; 7z/RAR return Unknown. FAT and
exFAT timestamps are decoded off by an hour (DST) and ~20h respectively by this Sleuth Kit
build. NTFS is wired and covered in Windows CI; TRIM still defeats recovery on real SSDs.

## Layout

| Path | What |
|---|---|
| `salvage/engine/photorec.py` | PhotoRec wrapper: pty-streamed progress, cancel, log-based success check |
| `salvage/engine/filesystem.py` | Sleuth Kit engine: deleted files with original names, folders, dates |
| `salvage/engine/combined.py` | Thorough mode: filesystem records + carving, merged by content hash |
| `salvage/engine/integrity.py` | Structural verification producing Intact/Partial/Corrupt verdicts |
| `bench/` | Accuracy benchmark: synthetic images, loss scenarios, scoring |
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

Salvage is MIT. It may bundle PhotoRec/TestDisk (GPLv2) and libimobiledevice (LGPL)
when you build an `.app` — see `packaging/THIRD_PARTY.md`. From source, those tools
come from Homebrew and stay separate programs.
