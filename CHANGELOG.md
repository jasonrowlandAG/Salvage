# Changelog

All notable changes to Salvage are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project
hasn't cut a tagged release yet, so everything to date is under
"Unreleased." When a version is actually tagged and shipped, rename its
section to `## [x.y.z] - YYYY-MM-DD` (DD/MM/YYYY in any user-facing text,
per Assembly Growth convention; ISO 8601 here to match the Changelog spec)
and start a fresh "Unreleased" section above it.

The single source of truth for the current version is `pyproject.toml`'s
`[project].version` — `packaging/salvage.spec` reads it directly at build
time, so bumping that one field is the entire version-bump process on
macOS (see `docs/release-checklist.md`).

## [Unreleased]

### Changed — distribution (2026-09-22)
- Chose the **no-fee / source-first** public path: install via Homebrew tools
  + `python -m salvage`; no Apple Developer Program / notarized DMG for v0.1.
- Documented privacy stance (no telemetry, no crash reporting, manual updates)
  in `docs/privacy.md` and the README; support contact `jay@assemblygrowth.com`.

### Added — packaging, licensing and release infrastructure (2026-09-18)
- `packaging/make_dmg.sh`: builds a compressed, signed, read-only
  `Salvage-<version>.dmg` with an `/Applications` shortcut and a generated
  background/icon layout.
- `packaging/notarize.sh`: the Apple notarization flow (`notarytool submit`
  + `stapler staple`), fully documented but untested — this machine only has
  a self-signed code-signing identity today.
- `packaging/windows/installer.nsi`: a minimal NSIS installer (Program
  Files install, Start Menu + optional desktop shortcuts, registered
  uninstaller, admin elevation) — untested, cannot be built or run on macOS.
- `docs/legal-compliance.md`: full licensing/compliance assessment for
  every bundled third-party component, with a free-vs-paid distribution
  verdict and citations to primary license sources.
- `docs/release-checklist.md`: the pre-launch checklist covering version
  discipline, licensing surfaces, code signing/notarization on both
  platforms, first-run experience, privacy, and smoke testing.
- `packaging/THIRD_PARTY.md`: completed with full license texts, precise
  per-component obligations, and a written offer of source for the
  GPL-licensed components.
- `CHANGELOG.md` (this file).

### Changed — packaging, licensing and release infrastructure (2026-09-18)
- `packaging/salvage.spec` now reads the app version from `pyproject.toml`
  at build time instead of hardcoding its own copy (the two had been able
  to drift independently); also bundles `LICENSE` and
  `packaging/THIRD_PARTY.md` as app resources for an in-app Licences
  screen, and removes a duplicate-key bug that silently dropped the more
  accurate `NSHumanReadableCopyright` string.

### Added — product features
- PhotoRec-backed carving engine (Deep scan) with pty-streamed progress,
  cancellation, and log-based success detection.
- The Sleuth Kit-backed filesystem engine (Quick scan) reading a volume's
  own deleted-file records, preserving original names/folders/timestamps.
- Thorough scan mode: runs both engines and merges results by content
  hash, combining carving's recall with real filenames where they survive.
- Structural integrity verification (PNG/ZIP/ISO-BMFF/PDF, etc.) labelling
  every recovered file Intact/Partial/Corrupt, and flagging carved copies
  that duplicate a file still present on disk.
- Cross-platform physical/removable device listing (macOS/Windows/Linux).
- One-time privileged-scan flow for raw device access (macOS/Linux
  `osascript`/`pkexec`; helper script builder shared by both).
- Full iPhone/iPad recovery flow: device detection, `idevicebackup2`
  backup with progress, encrypted-backup support, and Manifest.db-driven
  parsing of Messages, WhatsApp, Contacts, Notes, Recently Deleted photos,
  call history and Safari history.
- "Photos & videos on this Mac" local media finder: Photos library
  (including Recently Deleted), Messages attachments, iCloud Drive,
  cloud-sync folders, and iPhone backups, de-duplicated by content hash,
  with a persistent background thumbnail cache.
- SMAppService root-helper prototype (`helper/`, opt-in build) — proven
  to inherit Full Disk Access for raw reads of unmounted disks, though not
  wired into scanning since the kernel still refuses raw reads of any
  *mounted* volume regardless of privilege.
- `bench/`: a recovery-accuracy benchmark building real FAT32/exFAT/HFS+/APFS
  images, applying seven loss scenarios, and scoring recall, precision,
  name/path/date accuracy and integrity-verdict correctness per engine.
- Stable local code-signing identity ("Salvage Dev") so Full Disk Access
  grants survive rebuilds, and a PyInstaller-based `.app` bundle with
  PhotoRec and the libimobiledevice CLI tools bundled and relinked.
- End-to-end UI test driving the real Qt wizard offscreen over a synthetic
  home directory covering every media source kind.

### Changed — product features
- Drive list hides EFI/recovery/system-plumbing volumes and labels APFS
  containers; on-demand cloud mounts are left unticked by default in the
  media finder, and encrypted backups are skipped there (handled instead
  by the dedicated iOS backup flow, which supports them).

### Fixed — product features
- Drive rows not responding to clicks on the source page.
- iCloud placeholder files being read (and thus materialized) during media
  scans.
- PhotoRec file-type filter toggles returning no results.
- Duplicate results and non-UTC timestamps in Thorough mode.
- Icon path in the PyInstaller spec.
- Several end-to-end UI seams: missing-binary dialog, cancel state, and
  closing the app mid-scan.
