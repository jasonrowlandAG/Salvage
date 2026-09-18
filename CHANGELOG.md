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

### Removed — the GPLv2 x265 encoder is no longer bundled (2026-09-18)
- Replaced the `pillow-heif` dependency with `pi-heif`, upstream's own
  decode-only wheel of the same project. pillow-heif's prebuilt wheel
  bundled `libx265` — an HEVC **encoder** under GPLv2 — which made
  pillow-heif document its whole compiled wheel as GPL-2.0 and, because it
  loaded in-process via Python's C-extension mechanism, raised a genuinely
  contested question about whether the combined, running Salvage.app was a
  GPL-covered work (`docs/legal-compliance.md`). Salvage only ever *decodes*
  HEIC — for photo thumbnails and previews — so the encoder was dead weight.
  `pi-heif` bundles only libheif and libde265, both LGPLv3, which is the
  same regime Salvage already satisfies for PySide6/Qt. The bundle now
  contains no in-process GPL library at all; PhotoRec (GPLv2, a clean
  subprocess) is the only GPL component left.
  Verified on a real build: no `x265` file anywhere in `dist/Salvage.app`,
  no `x265` reference in any bundled Mach-O, only `_pi_heif…so`/`libheif`/
  `libde265` loaded by the running app, and HEIC preview still renders (grid
  thumbnail plus full preview, correct dimensions read via the decoder).
  Note for anyone tempted by the cheaper fix: simply deleting
  `libx265.216.dylib` from a pillow-heif build does **not** work — `libheif`
  hard-links it (`@loader_path/libx265.216.dylib`) and the extension then
  fails to load, silently disabling HEIC decoding.

### Changed — HEIC test fixtures no longer encoded at test time (2026-09-18)
- `bench/fixtures/sample.heic`: a real HEIC checked into the repo. Because
  the project no longer ships an HEVC encoder, `bench/corpus.py`,
  `tests/test_integrity.py`, `tests/test_media_e2e.py` and
  `tests/test_thumbcache.py` read this fixture instead of encoding one.
  These are the exact bytes the old encoder produced for `DEFAULT_SEED`, and
  `_heic_bytes` still draws the same amount from the seeded RNG, so
  `build_corpus(DEFAULT_SEED)` remains byte-identical across all 20 files
  and `bench/results/latest.json` stays a valid baseline (verified by
  diffing the corpus hashes before and after). The corpus HEIC is now
  fixed rather than seed-varying; every other corpus file still varies.

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
  GPL-licensed components (as of the x265 removal above, that offer covers
  PhotoRec/TestDisk alone).
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
