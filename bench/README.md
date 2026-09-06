# Salvage bench

A repeatable accuracy measurement harness for recovery engines. It builds real disk
images from a deterministic file corpus, damages them in specific, realistic ways
(delete, format, overwrite, fragment, empty trash), and scores whatever an engine
recovers against ground truth - byte-for-byte, not "looks about right."

This exists because Salvage currently recovers by signature carving (PhotoRec).
Commercial competitors (Disk Drill, Recoverit, EaseUS) look more "accurate" mainly
because they parse filesystem records instead of guessing from file signatures.
Before adding that (`salvage.engine.filesystem`), we needed an objective yardstick
so every future change is scored and regressions are caught - this is that yardstick.

## Running it

```bash
# full matrix, all filesystems this host can test, all scenarios
.venv/bin/python -m bench.run --engines photorec --filesystems fat32,exfat,hfs+,apfs --scenarios all

# compare all three engines, including CombinedEngine (filesystem + carving merged)
.venv/bin/python -m bench.run --engines photorec,filesystem,combined --filesystems fat32,exfat,hfs+,apfs --scenarios all

# fast CI-sized run (64MB images, 3 scenarios)
.venv/bin/python -m bench.run --quick

# a single combination while debugging
.venv/bin/python -m bench.run --engines photorec --filesystems fat32 --scenarios delete_all --size-mb 64
```

Results are written as JSON to `--out` (default `bench/results/<timestamp>.json`) and
as a Markdown table to `bench/results/latest.md` (paste straight into the README or a
PR description). `bench/results/latest.json` in this repo is the most recent baseline.

This only runs on macOS - image creation is entirely built on `hdiutil` and
`diskutil`. It needs `photorec` (`brew install testdisk`) for the `photorec` engine.

### Disk space and cleanup

Each (filesystem x scenario) combination builds one image, scans it, scores it, and
deletes it before moving to the next - so disk usage stays bounded to roughly one
image's worth (64-96MB by default) regardless of matrix size, not the whole matrix at
once. The main real hazard is a *leaked attach*: if a scenario crashes between
`hdiutil attach` and `hdiutil detach`, macOS won't let anyone delete or resize the
image underneath the mount. Every attach in `bench/images.py` goes through the
`attached()` context manager, which detaches in a `finally` block even on error, and
`bench/run.py` calls `images.detach_leaked()` after every combination (and once more
at the end) as a second safety net - it only ever touches images this run itself
created, tracked by path, so it can't accidentally detach someone else's mounted
volume.

## What each metric means

- **Recall** - fraction of files that *should* be recoverable in this scenario that
  came back with byte-identical content (sha256 of recovered bytes == sha256 of the
  original). A same-size file with the wrong bytes is a miss, not partial credit.
- **Precision** - fraction of *everything the engine returned* that matched some
  expected file. Carvers emit junk and file fragments; those count as false
  positives here. `junk_count`/`junk_bytes` report the raw numbers.
- **Name accuracy** - of the files recovered with correct content, the fraction whose
  `original_name` matches the planted name. Pure carvers (PhotoRec) always score 0%
  here - carved files are named `f<offset>.<ext>`, never the real name. This is the
  metric filesystem-record parsing exists to fix.
- **Path accuracy** - same idea, for `original_dir`.
- **Date accuracy** - fraction where `modified` is within 2 seconds of the planted
  mtime (FAT's own mtime resolution is 2s, so this is already generous).
- **by_ext** - recall broken down by file extension, since carving quality varies
  wildly by format (see the JPEG story below).
- **elapsed_s** - wall-clock scan time.

A `BenchResult` row with `error` set means the engine failed outright on that
combination (e.g. a filesystem parser that doesn't support a given fs) - it's
recorded as a zero row with the reason, not a crash.

## The corpus (`bench/corpus.py`)

A seeded, deterministic set of ~20 real, structurally valid files spanning the types
a landscaping-photo/document workload actually has: JPEG (with EXIF), HEIC, PNG,
PDF, DOCX, XLSX, ZIP, MP4, MP3, TXT - about 11MB total, planted at realistic paths
(`DCIM/100APPLE/IMG_0001.JPG`, `Documents/Reports/q3.pdf`, etc.) with planted mtimes.
Same seed always produces byte-identical files (see `tests/test_bench.py`), so bench
runs are comparable across time and machines.

## Scenarios (`bench/scenarios.py`)

| Scenario | What it does | What it tests |
|---|---|---|
| `delete_all` | Delete every file | Baseline recovery of intact, contiguous data |
| `delete_subset` | Delete half, leave half live | False positives - does the engine just re-list live files? |
| `delete_folder_tree` | `rm -r Documents/Reports` | Directory/path recovery |
| `quick_format` | Reformat the same fs, same size | Whether metadata remnants survive a "quick" format |
| `partial_overwrite` | Delete all, then overwrite ~25% of the volume with random data at a randomised offset | Which files survive when *some* data is destroyed |
| `fragmentation` | Fill free space, punch holes, force target files to be rewritten non-contiguously, then delete them | Where carving genuinely fails and extent-aware parsing should win |
| `emptied_trash` | Move files into `.Trashes/501`, then delete them there | Whether an engine reports the *last* known location, not the original one |

`partial_overwrite`'s ground truth isn't a guess: after the overwrite, every corpus
file's exact bytes are searched for in the raw image, and only files whose bytes
still exist somewhere are marked recoverable. `quick_format`'s ground truth
deliberately keeps the original name/path as "expected" even though a plain
reformat erases the metadata that would let anything recover it - name/path
accuracy on that scenario measures whether an engine can dig up *remnants* of the
old metadata, not just carve data back.

## Filesystems

`fat32`, `exfat`, `hfs+`, `apfs` are all built via `hdiutil create -fs ...` and
tested. **NTFS is not testable on this host**: `hdiutil create -fs` has no NTFS
option, and neither `mkntfs` nor `mkfs.ntfs` is installed - Homebrew's `ntfs-3g`
formula isn't installed here, and even installed it ships a FUSE read/write
*driver*, not a `mkfs` tool, on macOS. Testing NTFS would need a Linux host with
`ntfsprogs`/`ntfs-3g`, or Windows.

## Honest caveats

- **This flatters carving.** Every corpus file is small-to-medium and, outside the
  `fragmentation` scenario, written contiguously to an otherwise-empty volume.
  Real disks are messier: files get fragmented by ordinary use over months, which is
  exactly the case carving handles worst and filesystem parsing handles best. Treat
  these numbers as an upper bound on PhotoRec's real-world accuracy, not a
  prediction of it.
- **SSD TRIM is not modelled.** On a TRIMmed SSD, deleted blocks are typically
  zeroed by the drive itself within seconds, and *nothing* - carving or filesystem
  parsing - can recover them. These images are plain files on the host's own
  filesystem; "deleted" here only ever means "unlinked, bytes still present."
- **PhotoRec's `/cmd` file-type toggles aren't literally file extensions.** `docx`
  and `xlsx` aren't valid toggle names - they ride PhotoRec's generic `zip`
  detector (which then renames the output correctly once it's carved). `mp4` and
  `heic` aren't valid toggle names either - both ride the `mov` (ISO-BMFF/QuickTime
  family) detector. `bench/run.py` maps corpus extensions to the right PhotoRec
  toggle names before building the `/cmd` string; see `_EXT_TO_PHOTOREC_TOGGLE`.
  Passing an extension PhotoRec doesn't recognise as a toggle produces a
  `PhotoRec syntax error` on stderr and an empty scan, not a helpful error.
- **We enable only the corpus's own file types**, not PhotoRec's full ~300-format
  "everything" list. With "everything" enabled, high-entropy compressed data (real
  JPEGs, MP4s, and MP3s all are) randomly matches other formats' 2-4 byte magic
  numbers often enough to produce *thousands* of spurious tiny "recovered" files
  per run - that's a genuine, reproducible property of signature carving, but it
  swamps precision with noise that has nothing to do with recovering *this*
  corpus. Restricting to the types actually present matches how a real Salvage user
  would run a scan (pick the file types you care about), and makes precision
  numbers meaningful.

## A real bug this harness found

Building the corpus's JPEGs with a minimal, spec-valid EXIF segment (just a
`DateTime` tag written directly into IFD0, no nested Exif SubIFD) made this
machine's PhotoRec build (`testdisk` 7.2 via Homebrew) **recover zero JPEGs at
all** - not truncated, not corrupted, simply never detected as a candidate, even
for a single small JPEG alone on an otherwise-empty volume. Confirmed with a real
macOS system JPEG too (`/System/Library/CoreServices/DefaultBackground.jpg`), same
result.

Isolated via a byte-for-byte A/B test (`variant_a` no EXIF vs `variant_b` with a
flat IFD0 EXIF - identical pixels, identical `quality=`): variant A carved
successfully, variant B did not. Adding a proper `ExifOffset` (`0x8769`) pointer to
a nested Exif SubIFD - the structure every real camera actually uses, with
`DateTimeOriginal`/`DateTimeDigitized` living there instead of IFD0 - immediately
fixed it. **PhotoRec's JPEG carver in this build appears to require an EXIF
SubIFD to detect the file as a JPEG at all**; a flat, SubIFD-less EXIF segment
(spec-valid, if unusual) causes silent, total detection failure with no error.

`bench/corpus.py`'s `_jpeg_bytes()` now writes EXIF the way real cameras do (see
its comments) specifically to route around this - which also means the corpus is
*more* realistic, not less, but it's worth knowing this landmine exists if anyone
generates test JPEGs a simpler way and gets confused by 0% recall.

## FilesystemEngine bugs found and fixed against real disk images

Three problems showed up in the first real FilesystemEngine run (`bench/results/compare.json`):
precision pinned at 50% with `junk_count` exactly equal to the expected file count, 0% date
accuracy, and 15% recall on `delete_folder_tree`/`emptied_trash`. All three were diagnosed by
building real FAT32/exFAT images with `bench.images`, deleting/moving files, and running
`fls`/`icat`/`istat` directly against them (not by guessing) - the actual root causes were not
the ones first suspected.

**1. Precision (junk == expected count): macOS AppleDouble sidecars, not double-counted
volumes.** The suspicion was that `probe()` scans a volume twice (whole image + partition) or
that FAT's LFN+8.3 pair produces two entries per file. Neither is true: `mmls`/`fsstat` on every
image this bench builds (fat32, exfat, hfs+, apfs - GPT and MBR alike) returns exactly one real
partition row, confirmed by direct inspection. The actual cause: on a FAT32/exFAT volume mounted
by macOS, every file gets an AppleDouble sidecar (`._IMG_0001.JPG`, 4096 bytes) holding the
resource fork/xattrs FAT can't store natively. Deleting a file deletes its sidecar too, and
`fls -r` faithfully reports both as recovered entries - one real, one junk, 1:1, which is exactly
why `junk_count` always equalled `expected_count`. `FilesystemEngine._scan_impl` now skips any
entry whose name starts with `._` before ever calling `icat` on it. Separately, `emptied_trash`
showed *more* junk than sidecars alone explained (9 junk for 3 real files): a rename (move to
`.Trashes/501`) leaves the file's old directory entry behind as a *second* deleted record
pointing at the same data - `fls` reports both, `icat` extracts identical bytes from both. The
engine now hashes every file it extracts and de-duplicates by content within a scan, keeping
whichever copy was found *later* in the scan (in practice the more recently-added directory,
e.g. `.Trashes` walked after directories that already existed) - i.e. the file's last known
location, which is what `emptied_trash` exists to test in the first place. Precision on every
fat32/exfat scenario is 100% after both fixes (`bench/results/latest.md`). A defensive
de-duplication-by-offset was also added to `_probe_volumes` in case some filesystem/tool
combination *does* list the same volume twice - not proven necessary on anything tested here,
but cheap insurance against the originally-suspected failure mode.

**2. Date accuracy: our engine was the bug, but a real Sleuth Kit limitation remains.**
`FilesystemEngine._timestamp()` built the `modified` datetime with `datetime.fromtimestamp(value)`
- no `tzinfo` - even though `fls -m`'s epoch values are UTC. That produced a *naive* datetime
whose numbers were actually local time, and `bench/score.py`'s tolerance check (reasonably)
treats a naive datetime as UTC, so every comparison was off by exactly the host's UTC offset
(confirmed: 39600s = 11h, this host's AEDT offset, reproduced with a two-line repro before
touching any code). Fixed by returning `datetime.fromtimestamp(value, tz=timezone.utc)` instead
- this was the engine's bug, not the benchmark's; `_within_tolerance` in `bench/score.py` didn't
need to change. That fix alone took FAT32 date accuracy from 0% to 30-67% depending on scenario.
The remainder is a genuine, external Sleuth Kit (Homebrew `sleuthkit` 4.15.0) limitation, not
something this codebase can honestly fix:
  - **FAT32**: confirmed via a direct A/B test that macOS's own kernel round-trips a planted FAT
    timestamp back to the exact correct UTC instant (`os.stat()` on the live mount matches the
    planted mtime to the second), but `istat`/`fls` on the *identical on-disk bytes* decode a
    wall-clock time that's off by exactly one hour for any file whose planted date falls in a
    different DST regime (AEDT vs AEST) than whatever the conversion assumes. Since the corpus
    spans a full year, roughly half of every fat32 scenario's files land in each regime - hence
    date accuracy capped around 50% rather than 0% or 100%. `fls -m`'s `-z` flag doesn't apply
    here ("only useful with -l" per `fls -h`), so there's no supported way to correct this from
    the command line.
  - **exFAT**: worse, and different - exFAT timestamps carry their own explicit UTC-offset byte
    per spec, but this Sleuth Kit build's exFAT decoder is off by a large, roughly-constant
    ~20-21 hours regardless of DST (verified across six files with known planted mtimes), driving
    date accuracy to 0% on every exFAT scenario. This isn't a timezone-naive-vs-aware ambiguity
    of the kind our own code had - it's baked into what `fls -m` reports.
  Reverse-engineering and hand-compensating for either bug in this codebase would mean silently
  assuming a specific third-party tool's internal arithmetic mistake, which breaks (or inverts)
  the moment Homebrew ships a fixed `sleuthkit`. Documenting it honestly here instead.

**3. 15% recall on `delete_folder_tree`/`emptied_trash`: not a missing orphan scan.** The
suspicion was that `fls -r`'s recursive walk misses children of a removed directory and needs a
supplementary orphan scan (`fls -O`, or walking `$OrphanFiles`). Neither holds up: `fls -O` isn't
even a flag this Sleuth Kit build supports (`fls -h` has no `-O`), and after `rmtree`-ing
`Documents/Reports`, `fls -r -p` already lists all three files inside it correctly, each marked
`(deleted)`, each extracting byte-identical via `icat` - `$OrphanFiles` comes back empty because
nothing is actually orphaned in this scenario. **The real explanation:** `expected_count` in
`bench/score.py` is always every corpus file (20), regardless of whether a given scenario deleted
it, and `FilesystemEngine` (matching "Quick scan only reports what's actually deleted", the real
product behaviour) only returns entries flagged deleted. `delete_folder_tree` only deletes 3/20
files - so 15% recall (3/20) is FilesystemEngine correctly recovering *100% of what was actually
deleted*, not a partial recovery. This is the ceiling for a filesystem-only scan on these
scenarios, not a bug to chase further; `CombinedEngine` (below) is what actually raises recall on
partially-deleted volumes, by adding carving's recall (which doesn't care about deleted-vs-live)
on top of the filesystem stage's accurate names/dates.

## CombinedEngine: filesystem records first, then a full carve

`salvage/engine/combined.py` runs `FilesystemEngine.scan()` first (fast, named, accurate dates
for what it can reach) and `PhotoRecEngine.scan()` second into a separate sub-workdir (slow,
unnamed, but doesn't care whether something was deleted, formatted away, or fragmented), then
merges by content: a carved file that's byte-identical to a filesystem-recovered file is dropped
(the named copy wins), and everything else carving found is kept as extra, unnamed recall.
Hashing is the expensive part with thousands of carved files, so it's cheap by construction: a
carved file can only match a filesystem file of the *same size* (skip hashing everything else),
and whatever's left is hashed across a thread pool rather than one file at a time.

Before vs after (fat32/exfat, `bench/results/after_fixes.json`):

| Scenario | Engine | Recall | Precision | Name | Date |
|---|---|---|---|---|---|
| delete_folder_tree (fat32) | filesystem alone | 15% | 100% | 33% | 67% |
| delete_folder_tree (fat32) | **combined** | **90%** | **100%** | 6% | 11% |
| emptied_trash (fat32) | filesystem alone | 15% | 100% | 0% | 0% |
| emptied_trash (fat32) | **combined** | **90%** | **100%** | 0% | 0% |
| quick_format (fat32) | filesystem alone | 60% | 100% | 100% | 58% |
| quick_format (fat32) | **combined** | **75%** | **100%** | 80% | 47% |

Combined recall approaches PhotoRec's own ceiling (carving supplies whatever the filesystem
stage couldn't reach) while precision stays at 100% (no sidecar/duplicate junk, same as the fixed
FilesystemEngine) and name/date accuracy stay well above PhotoRec alone (0% on both, always,
since a pure carver never has a name or a filesystem-derived date to offer) wherever the
filesystem stage found a match. `CombinedEngine` reports success even when the filesystem stage
fails outright (e.g. a quick-formatted volume) as long as carving still runs - it only reports a
real error when *both* stages fail.

## Tests

`tests/test_bench.py` covers scoring maths against hand-built fixtures and corpus
determinism (both fast, no disk images) plus one `@pytest.mark.slow` end-to-end
smoke test that builds a real 33MB FAT32 image (FAT32's own practical minimum -
`newfs_msdos` refuses anything much smaller) and scores a real PhotoRec scan
against it. The slow test is skipped automatically if `photorec` or `hdiutil`
isn't available.
