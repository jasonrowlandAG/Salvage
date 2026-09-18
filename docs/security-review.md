# Salvage — Security Review

**Date:** 2026-09-18
**Scope:** Full codebase read; deep focus on root-privileged command construction, hostile-filesystem file writes, secrets at rest, untrusted-input parsers, subprocess/supply chain, the SMAppService helper prototype, and network egress.
**Method:** Static review of every file in scope, plus live proof-of-concept testing in an isolated scratch directory (never against real devices) — crafted disk images, hand-built SQLite headers, hostile filenames run through the real sanitizer code, and live injection attempts against the real `osascript`/shell-escaping functions. Every finding below states plainly whether it was **proven** (with a working PoC or a direct reproduction) or is a **code-review finding** (static analysis only, not exploited live) or was **investigated and found NOT exploitable**.

---

## Summary — lead with these

| # | Severity | Finding | Proven? |
|---|----------|---------|---------|
| 1 | **CRITICAL** | `salvage/__main__.py` `_raw_read_test()` builds a root-elevated `do shell script … with administrator privileges` string with **no escaping at all**. Arbitrary command injection as root. | **Yes — working PoC, sentinel command executed** |
| 2 | **HIGH** | `helper/` SMAppService daemon: any local process (not just Salvage) can connect to its Unix socket and make it read arbitrary raw disk bytes as root, no auth. CRITICAL if ever shipped/registered; currently dormant. | Design-verified from `helper/DESIGN.md`'s own test log + source read |
| 3 | **HIGH** | `sqlite_recover.recover_records()` trusts an attacker-controlled SQLite header field with no bound. A ~32 KB sparse file (looks like a 20 GB backup DB) hangs "recover deleted messages" for **~1 hour**, no cancel. | **Yes — measured and extrapolated from real runs** |
| 4 | MEDIUM | PhotoRec / libimobiledevice tool paths are resolved via `shutil.which()` (PATH search) **before** the app elevates to root. A PATH-writable directory ahead of the trusted dirs = root code exec next scan. | Code-review + verified the *TSK root-worker* re-resolution is safe (launchd sanitizes PATH); the *initial* resolve-then-elevate is not |
| 5 | MEDIUM | Decrypted iPhone backup DB (`Manifest.decrypted.db`) written world-readable (0644), next to the source backup, and never deleted. | **Yes — permissions confirmed by direct test** |
| 6 | MEDIUM | One hostile filename aborts an entire recovery scan instead of being skipped (ENAMETOOLONG); a crafted 32 KB video file hangs/crashes the Mac media search (RecursionError, uncaught — `MediaScanWorker` is missing the exception guard every sibling worker has). | **Yes — both reproduced live** |
| 7 | MEDIUM | AFC command construction (`ios.py`) embeds device-supplied filenames into `afcclient`'s command language unescaped. | Code-review only (no real/simulated AFC server tested) |
| 8 | LOW | Windows reserved device names (`CON`, `NUL`, `COM1`…) pass through the path sanitizer unchanged. | **Yes — reproduced** |
| 9 | LOW | `fake.py` dev-mode uses fixed, predictable paths under system `/tmp`. | Code-review; gated behind `SALVAGE_FAKE=1`, not default |
| 10 | LOW | Thumbnail cache (`~/Library/Caches/Salvage/thumbs`) never expires/scopes by source. | Code-review |

**Root-side command construction in `salvage/engine/privileged.py` — the area flagged as highest priority — was tested with 12 adversarial payloads (`"`, `'`, `$()`, backticks, `\`, newlines, `$IFS`, non-ASCII, mixed quotes) against the real code, through both the macOS AppleScript path and the Linux/pkexec-equivalent path, with a validated positive control proving the test harness would have caught a real bug. Zero injections. It is correctly implemented.** The critical finding above is a *second*, unrelated `do shell script … with administrator privileges` call site that skips the escaping entirely.

---

## 1. Root-side command construction

### 1.1 `salvage/engine/privileged.py` — SAFE (tested, not exploitable)

`build_helper_script()` (line 31) puts every dynamic value — workdir, output paths, PhotoRec argv, uid/gid — through `shlex.quote()` individually, then `run_privileged()` (line 116) passes the *entire* assembled command through `_applescript_escape()` (line 78: `s.replace("\\", "\\\\").replace('"', '\\"')`, correct order — backslashes first, then quotes) before embedding it in `do shell script "{script_str}" with administrator privileges` (line 131). uid/gid are passed through `int()` before formatting, so they can't carry shell syntax at all.

**PoC methodology:** imported the real `build_helper_script`/`_applescript_escape` from the project, fed 12 hostile `workdir` values through the exact macOS path (`osascript -e 'do shell script "..."'`, omitting only `with administrator privileges` so no real elevation prompt was needed — the injection point, if any, is in the string construction, not the privilege level) and the exact Linux/pkexec-equivalent path (`sh -c helper` directly). Payloads: double/single-quote breakout, `$(...)`, backticks, pre-escaped quotes, trailing backslash, embedded real newline, non-ASCII, bare `;`, `$IFS`, mixed quotes+backslashes. A sentinel-file marker in each payload would prove code execution if it fired.

**Result: zero sentinel files created, across all 12 payloads, on both paths.**

**Validated the harness itself** with two positive controls simulating genuinely naive/vulnerable string-building (bare unquoted interpolation, and quoted-but-unescaped interpolation) — both fired their sentinels immediately, proving the test methodology does detect real injections and the clean result above isn't a false negative. (Scripts: `poc_privileged_escape.py`, `poc_positive_control2.py` in the review scratch dir — not part of the repo.)

The `launchctl submit -l {label} …` wrapper (line 70-75) uses a `uuid4().hex` label (unpredictable, also `shlex.quote()`'d) and `--` before the command, so no `launchctl`-flag injection either. No leftover launchd jobs were left behind after testing.

**Verdict: not exploitable via any tested vector. No fix needed here.**

### 1.2 `salvage/__main__.py` `_raw_read_test()` — CRITICAL, proven exploitable

Lines 64-89. Reached via `python -m salvage --rawtest <device> <out> root` (or the equivalent frozen binary: `Salvage.app/Contents/MacOS/Salvage --rawtest <device> <out> root`) — a developer diagnostic left in the shipped entry point with **no gate** (`main()` checks `"--rawtest" in sys.argv[1:]` unconditionally, before the fake-mode/frozen checks; nothing strips it from the PyInstaller build).

```python
script = (
    f'do shell script "dd if={device} bs=1m count=16 of={tmp} 2>{tmp}.err; '
    f'chown {os.getuid()} {tmp} {tmp}.err 2>/dev/null; cat {tmp}.err" with administrator privileges'
)
proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
```

`device` (`args[0]`) and `tmp` (`out + ".bin"`, derived from `args[1]`) are interpolated **raw** — no `shlex.quote()`, no `_applescript_escape()`, no quoting of any kind around them in the shell text. This is the textbook-vulnerable version of exactly the pattern §1.1 gets right next door in the same codebase.

**PoC (`poc_rawtest_injection.py`):** reproduced the identical string-construction logic byte-for-byte, with `device = "/dev/disk9; touch <sentinel>; echo x"`. Ran through `do shell script "..."` (again omitting only `with administrator privileges` to avoid a real prompt — the injection is in the string, not the privilege level):

```
script: do shell script "dd if=/dev/disk9; touch …/PWNED_rawtest; echo x bs=1m count=16 of=… …"
rc: 0
sentinels: ['PWNED_rawtest', ...]
=> INJECTION CONFIRMED
```

The sentinel command ran. In the real code this whole string is the argument to `do shell script … with administrator privileges`, so the injected command runs **as root** — with no visibility to the user, who only sees the generic native "Salvage wants to make changes" authentication dialog and has no way to see that a `touch`/anything-else is riding along with the `dd`.

**Reachability:** requires the ability to launch the Salvage binary with attacker-chosen arguments (a `.command` file a user is talked into double-clicking, a malicious installer/updater, a support-scam "run this in Terminal, then enter your password" script) plus the user approving one admin prompt. It is *not* triggerable just by plugging in a USB stick — but it is a real, proven, unauthenticated-content root shell in code that ships today, which is a categorically different (and worse) risk than "theoretically smelly."

**Fix:** delete `_raw_read_test`/`--rawtest` from the shipped entry point (it reads like a one-off diagnostic from the internal-disk-carving investigation documented in `helper/DESIGN.md` — its job is done). If it must stay for development, gate it behind a build-time flag that's compiled out of release builds, and if it's ever needed again, route it through `salvage.engine.privileged.build_helper_script()`/`_applescript_escape()` like every other elevated call.

---

## 2. Arbitrary file write from a hostile image

### 2.1 `salvage/engine/filesystem.py` — SAFE (tested, not exploitable)

`_sanitize_component()` (line 212) strips NUL bytes, collapses `.`/`..` (after stripping whitespace) to `_`, and replaces `<>:"|?*\/` and all control bytes (0x00-0x1F) with `_` — notably **both** `/` and `\` and `:` are blocked, which defeats POSIX traversal, Windows UNC paths (`\\server\share`), and Windows drive letters (`C:`) in the same pass. `_sanitize_relpath()` (line 220) additionally *drops* any `.`/`..` path segment from the directory portion entirely (rather than merely escaping it) before rebuilding the path component-by-component — so `Path(*parts)` can never become absolute, since no individual component can itself contain a separator.

**PoC (`poc_sanitize_paths.py`):** fed the real `_sanitize_relpath()` 22 hostile `(original_dir, original_name)` pairs — classic `../../../../etc/cron.d/x`, absolute-looking dirs, name-field traversal, mixed traversal, **literal** `..%2f..%2f..%2fetc` (proving it is not URL-decoded, so it can't be smuggled that way either), Windows backslash/UNC/drive-letter styles, NUL-byte-then-traversal, `..`/`.` alone and whitespace-padded, and an NFC/NFD Unicode look-alike pair — then resolved the result against a fake `recovered_root` and checked whether it ever escaped. **Zero escapes across all 22 cases.**

This assumes the *worst case* — that `fls` faithfully passes through 100% attacker-controlled bytes, so it doesn't depend on Sleuth Kit's own robustness. (Real Sleuth Kit 4.15.0 and `hdiutil` are installed on this machine; a live hand-crafted-FAT-image integration test was judged lower-value than this direct worst-case test once the sanitizer itself was shown to hold regardless of what `fls` hands it, and existing tests already cover the legitimate FAT32/exFAT/HFS+ recovery paths with real `hdiutil` images — `tests/test_filesystem.py`.)

**Gap found — not a traversal, a reliability bug (see §6 below):** the 200-*character* cap (`_MAX_COMPONENT_LEN`, line 53) measures Unicode code points, not bytes. A component of 400 emoji sanitizes to 200 characters = **800 UTF‑8 bytes**, over the 255-byte `NAME_MAX` most filesystems enforce. This isn't exploitable as a write-outside-destination, but it does crash the scan (detailed in §6).

**Windows reserved device names** (`CON`, `NUL`, `COM1`, `LPT1`, …) are **not** special-cased and pass straight through (confirmed in the same test run: `rel='CON'`, `rel='NUL.txt'`, `rel='COM1'`, `rel='LPT1.jpg'`). On Windows, writing to a path component that resolves to a reserved DOS device name can hang or misbehave instead of creating a file. Low severity (availability, not compromise) but worth a fix given Salvage ships for Windows. **Fix:** in `_sanitize_component`, if the stem (case-insensitive) matches `CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]`, prefix or suffix it (e.g. `_CON`).

### 2.2 `salvage/engine/ios.py` `BackupReader.extract()` / `.extract_known()` — SAFE (traced, not exploitable)

Every call site was traced:

- `extract_known()` (line 681) builds `dest = dest_dir / Path(relative_path).name` — `.name` can only ever be a single path component (no `/` survives), so the worst case is `relative_path` ending in exactly `..`, which makes `dest == dest_dir.parent`; the subsequent `open(dest, 'wb')` then fails with `IsADirectoryError` rather than writing anywhere. No multi-level traversal is possible because `.name` structurally cannot contain a separator.
- `ios_facade.py:119` (`_extract_safari_history`) uses a **fixed** literal filename, `Path(workdir) / "History.db"` — not derived from backup content at all.
- `ios_parsers.py:562` (`parse_trashed_photos`, trashed-photo thumbnail) uses `dest_dir / f"{Path(filename).stem}.jpg"` — `.stem` inherits the same single-component safety as `.name`, and even the `..`-as-stem edge case degrades to the harmless literal filename `...jpg` once `.jpg` is unconditionally appended. `dest_dir` itself is fixed (`photos_db.parent / "trashed_thumbnails"`), not attacker-influenced. The `directory`/`filename` values (from the *decrypted* `Photos.sqlite`, attacker-controlled in a hostile backup) are only ever used as a **parameterized** SQL `LIKE` value (`params.append(relative_path_like)`), never concatenated into SQL text, and results are additionally filtered by an exact-string match after the `LIKE`, defeating wildcard-broadening tricks.

No call site passes raw, un-narrowed backup content directly into a destination path.

### 2.3 `salvage/engine/results.py` / `local_media.py` `export_found` — SAFE

`export_found()` (local_media.py:857) builds `target_dir / fm.name`; `fm.name` for the iOS-backup scan path (`_scan_ios_backup`, uses `display_name = Path(bf.relative_path).name`) is again single-component-safe. For every other source kind, names come from a real `os.walk()`/`os.scandir()` over an already-mounted, real filesystem — a POSIX directory entry literally cannot contain `/`, so there is nothing to sanitize there. `results.py`'s `recover_files()`/`collect_recovered()` read real filenames PhotoRec itself already wrote to `output_dirs` on the local disk — same reasoning.

---

## 3. Secrets and personal data at rest

### 3.1 Backup password — SAFE (traced)

`derive_class_keys(password)` (ios.py:366) only ever uses `password` locally for `hashlib.pbkdf2_hmac(...)` — grepped every `password` reference in `ios.py`/`ios_facade.py`/`ios_options_page.py`: it never reaches a `subprocess` argv (the one subprocess that touches backups, `idevicebackup2`, is invoked with **no** password argument at all — Salvage only *reads* pre-existing encrypted backups via its own pure-Python AES implementation, it never creates one with a password), and every user-facing/exception message is a generic literal (`"Incorrect backup password."`) — the password value itself is never interpolated into a log or exception string. Not visible in `ps`, not in logs.

### 3.2 Decrypted `Manifest.db` cache — MEDIUM, proven

`_decrypt_manifest_db()` (ios.py:559-582):

```python
cache_dir = self.backup_dir.parent / "salvage_cache" / self.backup_dir.name   # line 578
cache_dir.mkdir(parents=True, exist_ok=True)
dest = cache_dir / "Manifest.decrypted.db"
dest.write_bytes(plaintext)                                                    # line 581
```

**Confirmed by direct test:** `cache_dir` is created at **0o755**, and `Manifest.decrypted.db` at **0o644** — both world-readable, using the process's ordinary umask rather than a restrictive mode. Compare this to `integrity.py:525`'s `tempfile.NamedTemporaryFile(...)`, which *does* get Python's safe 0o600-by-default temp-file handling and is deleted in a `finally` block — the ios.py cache uses neither protection.

Two compounding issues:
1. **Location:** for the "point at an existing backup folder" flow (`ios_options_page.py:230`), `self.backup_dir` can be *any* folder the user's file picker reaches — a USB stick, a network share, someone else's old backup — and the decrypted plaintext DB (full SMS/contacts/notes manifest) is written as a **sibling** of that folder, not into Salvage's own per-session workdir under the user's chosen destination.
2. **No cleanup, ever:** grepped `salvage_cache`/`Manifest.decrypted` across the whole codebase — the only two hits are the lines that *create* it. There is no code path that deletes it. It persists indefinitely.

On a single-user Mac, the home-directory permission boundary (usually 700/750) is the main thing standing between this and another local account — but the point of the backup being *encrypted* is defeated the moment the plaintext copy is written next to it with no expiry, and the permissions are unnecessarily loose regardless.

**Fix:** write to the app's own per-session workdir (under `session.session_dir`, which already lives under the user-chosen destination — see §3.4), use `os.open(..., 0o600)` or `tempfile`, and delete it in a `finally`/`close()` once the `BackupReader` is done with it (mirroring `_verify_sqlite`'s pattern in `integrity.py`).

### 3.3 Thumbnail cache — LOW

`thumbcache.py:46`: `CACHE_DIR = Path.home() / "Library" / "Caches" / "Salvage" / "thumbs"`. This is process-wide, keyed only by `sha1(path|size|mtime|render_size)`, with no per-source scoping, no expiry, and nothing that ever purges it. As the task brief notes, this means thumbnails from a one-time look at *someone else's* old iPhone backup persist on disk indefinitely. Standard macOS home-directory permissions protect it from other local users, so this is a data-hygiene finding, not an access-control vulnerability — but worth an expiry/purge mechanism (e.g. on "New scan" or a settings action) given what it contains.

### 3.4 tempfile audit

Checked every `tempfile`/`mkstemp`/`mkdtemp`/`/tmp` reference in `salvage/`:

| Site | Mechanism | Verdict |
|---|---|---|
| `integrity.py:525` (`_verify_sqlite`) | `tempfile.NamedTemporaryFile(delete=False)` | Safe — 0600 default, deleted in `finally` |
| `thumbcache.py:117` (QuickLook render) | `tempfile.TemporaryDirectory()` | Safe — `mkdtemp` 0700 default |
| `local_media.py:506` (Photos.sqlite copy) | `tempfile.TemporaryDirectory()` | Safe |
| `ios.py:578` (decrypted Manifest.db) | plain `mkdir`/`write_bytes`, **not** `tempfile` | **Not safe — see §3.2** |
| `fake.py` (dev-mode only) | fixed names directly under `tempfile.gettempdir()` (`salvage_fake_removable`, `salvage_fake_ios_backup`, `salvage_fake_media`) | LOW — predictable, world-writable-directory paths, but only reachable via the opt-in `SALVAGE_FAKE=1` env var (`salvage/__main__.py:22`), not the default/shipped path |

The main scan/recovery workdir (`session.workdir`, `session.session_dir` — `salvage/ui/app.py:87`, `salvage/ui/session.py:40-43`) lives under `destination / f"Salvage {timestamp}"`, where `destination` is the user's chosen recovery-output folder — **not** system `/tmp` — which is the right call and avoids the world-readable-`/tmp` problem for the primary flow. The one place that regresses this pattern is §3.2.

---

## 4. Untrusted-input parsers

Fuzzed/stress-tested the parsers most likely to see fully hostile bytes.

### 4.1 `sqlite_recover.py` — HIGH, proven DoS

`recover_records()` (line 223) calls `sqlite3.connect()` + `PRAGMA table_info(table)` first (safe — `table` is always a hardcoded literal like `"message"`/`"ZICNOTEDATA"` at every call site, never attacker data, so no SQL injection there), then walks the **raw** file itself:

```python
page_count = struct.unpack(">I", header[28:32])[0]   # line 173, attacker-controlled, up to 2**32-1
...
for page_no in range(1, page_count + 1):              # line 247 — no bound check against real file size
    page = store.read(page_no)                          # seek + read per iteration
```

**Tested two variants:**
- **Inconsistent header lie** (small real file, `page_count` field patched to a large value): `sqlite3.connect()`'s own `PRAGMA table_info` already raises `DatabaseError('database disk image is malformed')` **before** reaching this loop, across every magnitude tested (3 through 2,000,000). This path is not exploitable — SQLite's own engine happens to guard it.
- **Consistent sparse file** (page_count genuinely matches a sparse, mostly-hole file): **accepted** by `sqlite3.connect()`. Built a real SQLite DB, extended it to `page_size(4096) × page_count(5,000,000)` ≈ 20.5 GB *logical* size via `seek`+single-byte write (APFS sparse file — **32 KB actual disk usage** for a "20 GB" file). Measured `recover_records()` at **~0.85 µs/page**, consistent across 50,000 and 500,000-page runs. Extrapolated to the real maximum page_count (2³²-1): **≈ 3,700 seconds (~1 hour)**, single-threaded, no cancellation available.

This is directly reachable: `parse_messages()`/`parse_whatsapp()`/`parse_contacts()`/`parse_notes()` in `ios_parsers.py` all call `recover_records()` whenever `include_deleted=True` (the default), on files extracted from whatever backup folder the user opened — including someone else's. `IOSParseWorker` (workers.py:147) runs this on a background thread, so it won't crash the whole app, but the "extract and parse" operation for that import stalls for roughly an hour with the progress dialog showing "Extracting and parsing iPhone data…" (an indeterminate spinner, `app.py:159-165`) and **no cancel button** (`setCancelButton(None)`).

**Fix:** clamp `page_count` to `min(header_page_count, (actual_file_size // page_size) + 1)` before the loop (or just derive it from `os.path.getsize(db) // page_size` and ignore the header field entirely for iteration bounds — the header value only needs to be *believed* for the `_verify_sqlite`-style truncation check in `integrity.py`, which already does the size comparison correctly rather than looping on it).

### 4.2 `integrity.py` — mostly safe, one confirmed disproof of a hypothesis

- **PNG chunk length → memory bomb?** Hypothesized that `body = f.read(length)` (line 165) with an attacker-controlled 32-bit `length` on a tiny real file would force a multi-GB upfront allocation (a documented CPython gotcha). **Tested directly: it does not.** Crafted an 8-byte-signature + 8-byte chunk-header (`length=1.5 GB`) + 100-byte body file; `f.read(1_500_000_000)` returned the true 100 bytes instantly, with **zero RSS growth** (8.5 MB → 8.5 MB). CPython's `io.BufferedReader.read(n)` on this platform/version correctly bounds itself to what's actually available. **Not exploitable — disproven.**
- **ZIP bomb via `zipfile.testzip()`?** Hypothesized `zf.testzip()` (integrity.py:322, used when the *compressed* file is ≤ 64 MB) fully materializes each decompressed member in memory. Built a real bomb (2.09 MB compressed → 2.15 GB declared/actual decompressed, ~1029:1 ratio, all-zero DEFLATE stream) and ran `testzip()` against it: completed in **0.48 s**, peak RSS grew only **~11 MB**. Reading CPython's actual `zipfile.ZipFile.testzip()` source confirms why: it explicitly reads in `2**20`-byte (1 MB) chunks "to avoid an OverflowError or a MemoryError with very large embedded files" — this is a deliberate, documented stdlib mitigation. **Not exploitable as a memory bomb.** There is a bounded CPU-time cost proportional to decompressed size (at the `_FULL_DECODE_CAP` = 64 MB compressed boundary and a ~1000:1 ratio, roughly 15-60 s per file, and `verify_many()` runs up to 4 of these concurrently) — a minor nuisance, not a crash risk. No fix required; optionally lower `_FULL_DECODE_CAP` or add a decompressed-size sanity check if even that cost is unwanted.
- **ISO-BMFF walker** (`_iter_boxes`, line ~343): flat/iterative, not recursive — `pos` strictly increases by ≥ `header_len` (≥ 8) every iteration because `size < header_len` breaks the loop, so a `size == 0` box (spec meaning "extends to EOF") or `size == 1` (64-bit extended size) can't cause an infinite loop or a `pos` regression. Cost is linear in file size. No issue found.
- **PDF/MP3/gzip/tar checkers:** all bound their reads to `size`/fixed head-tail windows, or (gzip) explicitly stream-decompress in 1 MB chunks (`while gz.read(1024*1024): pass`) rather than materializing output. No issues found.

### 4.3 `local_media.py` `_find_mvhd_bytes` — MEDIUM, proven crash (see §6, combined with the missing worker guard)

Recurses into nested `moov` boxes with no depth limit. A 32 KB file with 4,000 levels of nested `moov` headers reliably raises an **uncaught `RecursionError`** (Python's default limit is 1000). Full detail and fix in §6.

### 4.4 `ios.py` keybag TLV / AES unwrap — reviewed, no issue found

`_parse_tlv` (line 300-ish) bounds its loop on `pos + 8 <= n`, and every subsequent slice (`data[pos:pos+length]`) is a Python slice, which silently truncates rather than raising or over-reading past the buffer — can't over-read. `_aes_key_unwrap` validates `len(wrapped) % 8 == 0 and len(wrapped) >= 16` before proceeding and raises `ValueError` (mapped to `BackupPasswordError`) on integrity-check failure. No unbounded loop or allocation found; a hostile keybag can at worst make key derivation return garbage or raise, both handled.

### 4.5 `ios_parsers.py` protobuf/typedstream walkers — reviewed, no issue found

`_pb_read_varint` bounds on `pos >= len(data)` per byte; `_decode_attributed_body` (NSKeyedArchiver "streamtyped" scanner) bounds its search window (`blob.find(marker)`, `blob.find(b"+", idx, idx+24)`) and length-prefix parsing against `len(blob)` before slicing. No infinite loops or unbounded allocations found in the code reviewed.

---

## 5. Subprocess and supply chain

### 5.1 `shell=True` — none found anywhere in the codebase (grepped `salvage/`).

### 5.2 Every `subprocess.run`/`Popen` call site reviewed (devices.py, ios.py, filesystem.py, photorec.py, thumbcache.py) — all pass a **list** of arguments, never a concatenated string. `devices.py`'s `_run_powershell()` takes a `command: str` parameter, but every call site passes a fixed literal (`"Get-Disk | ConvertTo-Json -Depth 4"`, etc.) — never attacker/device data. No shell-metacharacter injection risk in any of these.

### 5.3 PATH-based binary resolution before elevation — MEDIUM

`PhotoRecEngine.locate_binary()` (photorec.py:106-107) and `ios.py`'s `_tool()` (line 30-31) both call `shutil.which(name)` **first**, falling back to fixed directories (`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`) and finally the app-bundled copy only if `which` and the fixed dirs all miss. This resolution happens in the **unprivileged** parent process, using whatever `PATH` that process inherited at launch — and the *resolved, absolute* path is what then gets elevated to root via `run_privileged()`.

**Verified the mitigating half of this directly:** for `FilesystemEngine`'s TSK tools, the binary re-resolution happens a *second* time, inside the already-root `--root-worker` process (`filesystem.py`'s `_root_worker_main` calls `locate_binaries()` fresh). Submitted a real `launchctl submit` job and captured its actual environment:

```
PATH=/usr/bin:/bin:/usr/sbin:/sbin
```

launchd gives a submitted job a minimal, non-writable-by-anyone PATH — so the *root-side* re-resolution for `fls`/`icat`/`mmls`/`fsstat` is safe regardless of the user's own PATH.

**The gap:** PhotoRec and the libimobiledevice tools (`idevice_id`/`ideviceinfo`/`idevicebackup2`/`afcclient`) are **not** re-resolved as root — the path chosen by the unprivileged process's `which()` call is what gets embedded in the elevated shell command. If Salvage is ever launched with a `PATH` that includes a directory the current user can write to *ahead of* `/opt/homebrew/bin` (common for developers: `~/.cargo/bin`, `~/go/bin`, `~/.local/bin`, nvm/pyenv/rbenv shims — none of which are in the sanitized `/etc/paths.d` set a Finder-launched GUI app normally inherits, but all of which are present when the app or its embedded binary is run from an interactive Terminal session), an attacker who already has ordinary user-level code execution (a malicious VS Code extension, a poisoned npm/pip package, etc.) can plant a file literally named `photorec` earlier in that PATH; the next elevated scan runs it as root. This turns a user-level foothold into a root one — the exact boundary this whole review is about — though it requires that precondition plus a non-default launch context, which is why it's MEDIUM rather than HIGH/CRITICAL.

**Fix:** for anything that will be elevated, resolve **only** from the fixed/bundled locations (drop the `shutil.which()` call, or move it to last priority and validate the resolved path's parent directory is one of the expected fixed dirs before trusting it).

### 5.4 Built app / relinking (`dist/Salvage.app`) — reviewed, no issue found

Ran `otool -L` on every bundled CLI tool in the built app. All non-system dependencies are `@loader_path/../../../libX.dylib`, resolving inside `Contents/Frameworks/` — never `/opt/homebrew` or any other writable/external location — confirmed for `afcclient`, `idevice_id`, `idevicebackup2`, `ideviceinfo` (all six of their shared deps) and `photorec` (links only `/usr/lib/*`, no relinking needed). `packaging/build_mac.sh` also self-verifies this at build time (`grep -E '/opt/homebrew|/usr/local/(opt|Cellar)'` over every executable, fails the build on a hit). `@loader_path`-relative references inside a `codesign --deep`-signed bundle can't be redirected to a writable location without invalidating the signature. No dylib-hijack path found. (Note: this particular local build is ad-hoc/self-signed — `TeamIdentifier=not set` — which is expected for a dev build per `helper/DESIGN.md`; confirm the actual release build goes through `packaging/notarize.sh` with a real Developer ID before shipping, which the presence of that script suggests is already the intent.)

---

## 6. Combined robustness finding: one hostile file can silently wipe an entire scan's results

Two independent, both-proven bugs compound into the same failure mode ("a single bad file destroys results a user was relying on this tool to recover"), so they're reported together.

**6a. `filesystem.py` — oversized multi-byte component crashes the whole `_scan_impl()` call, discarding every already-found file.**
`_MAX_COMPONENT_LEN = 200` (line 53) truncates by Python string length (code points), not encoded bytes. Reproduced directly: `Path('recovered_root') / ('😀' * 200)).mkdir()` → `OSError(63, 'File name too long')`. There is no `try/except` around `target.parent.mkdir(...)` (line 531) in the per-file loop inside `_scan_impl`, so this exception propagates out of the function entirely — discarding the `files` list accumulated so far, **including files already found on previously-completed volumes** in a multi-volume image, not just the one offending file. In the root-elevated path (`_root_worker_main`, used whenever scanning a `/dev/...` source), there's no guard there either, so the root worker just dies and `fs_manifest.json` is never written; the caller sees a generic "Filesystem scan did not complete." with total loss of results. In the non-privileged path, `ScanWorker.run()` (workers.py:29-40) *does* catch broad `Exception`, so the app itself won't crash — but the user-visible effect is identical: the whole scan reports failure instead of partial success. **Fix:** wrap the per-file `mkdir`/`icat` block in `try/except OSError: continue` (matching the tolerance already shown for icat failures, timeouts, and unreadable volumes elsewhere in the same function), and measure `_MAX_COMPONENT_LEN` in encoded bytes, not code points.

**6b. `local_media.py` `_find_mvhd_bytes` recursion + `MediaScanWorker` missing the exception guard every sibling worker has.**
`_find_mvhd_bytes` (line 378) recurses once per nested `moov` box with no depth limit. Built a 32 KB `.mov` with 4,000 nested `moov` headers and called the real function directly: `RecursionError('maximum recursion depth exceeded')`, uncaught by `_mvhd_creation_time`'s `except (OSError, OverflowError, ValueError)` (RecursionError is a `RuntimeError`, not caught). Traced the call chain: `_build_found_media()` (line 446) calls `_extract_taken()`→`_mvhd_creation_time()` with **no** try/except; `scan_sources()`'s per-source guard is `except (OSError, PermissionError, sqlite3.Error): pass`, which also doesn't cover `RecursionError`. And unlike `ScanWorker`, `RecoverWorker`, `VerifyWorker`, and `IOSParseWorker` — every one of which wraps its `run()` body in `try: ... except Exception:` — **`MediaScanWorker.run()` (workers.py:133-142) has no exception handling at all.** A single ~32 KB crafted video file anywhere under a folder the "Photos & videos on this Mac" search scans (Downloads, Desktop, an iPhone backup's DCIM, a cloud-sync folder) will propagate an uncaught `RecursionError` straight out of a `QThread.run()` override with no signal ever emitted — the progress dialog has no way to know the thread died, so the UI hangs waiting for a `finished_scan` signal that never arrives (severity depends on the PySide6 build's unhandled-exception behavior; at best a stuck dialog requiring force-quit, at worst a hard crash).

**Fix:** bound the recursion in `_find_mvhd_bytes` (an explicit depth counter, or convert to an iterative stack-based walk — the ISO-BMFF walker in `integrity.py` already shows the iterative pattern works fine for this exact format), **and** add the same `try/except Exception` guard to `MediaScanWorker.run()` that its three sibling workers already have — it's one function, and it's the odd one out.

---

## 7. Privilege helper (`helper/`)

Reviewed `helper/DESIGN.md`, `helper/SalvageHelper/main.swift`, `helper/salvage-register/main.swift`, `helper/com.salvage.helper.plist`, and its wiring into `packaging/build_mac.sh`.

**Current shipping status, verified:** not present in this machine's built `dist/Salvage.app` (`find dist/Salvage.app -iname "*helper*"` → nothing), and `build_mac.sh` only builds/embeds it when `SALVAGE_BUILD_HELPER=1` is explicitly set (default `0`). `DESIGN.md` itself says registration is a manual, one-time `salvage-register register` CLI step the design doc's own author ran for the spike — nothing in the app calls `SMAppService.register()` automatically. **Today, the exposure from this component is effectively zero.**

**Reviewed as shipping code, per the request — and it is not safe to ship as-is:**

- **Who can connect:** the socket (`/var/run/com.salvage.helper.sock`) is created `chown 0:80` (root:admin), `chmod 0660` (`main.swift` lines 142-143). Standard Unix-domain-socket semantics enforce this at `connect()` time by the *connecting process's* credentials — so the practical answer to "who can connect" is: **root, or any process running as a user in the `admin` group.** On the overwhelmingly common configuration — a single-user Mac where the primary account is an administrator — that's every app the user runs, full stop. There is no code-signature check, no bundle-ID check, no shared secret, nothing beyond that group-membership gate (confirmed by reading the entire `handleClient()` function — it parses a JSON line and acts on it, no auth step precedes that).
- **What can be requested:** `{"device": "<any path>", "offset": <uint64>, "length": <int>}` (lines 78-92). `readRawDevice()` (line 37) does a bare `open(path, O_RDONLY)` with **no allowlist** on `device` — any path the root process can open, it will, including other unmounted disks/images the connecting (unprivileged) process itself has no permission to read directly. `DESIGN.md`'s own test log confirms this is real, not theoretical: it documents successful root reads of `/dev/rdisk0` (whole physical disk) and `/dev/rdisk1` (APFS container) through exactly this path, specifically noting these same reads got `EPERM` without the helper. `length` is taken directly as a Swift `Int` with no upper bound, so a malicious `length` could also make the root daemon attempt a very large allocation (`Data(count: length)`, line 52) — a secondary DoS-against-root-daemon vector.
- **Net effect if shipped as-is:** any unprivileged local process (no admin password, no user interaction beyond the one-time Login-Items approval the *app* prompted for, which the user has no reason to distrust) gets an arbitrary-raw-disk-read-as-root oracle. That's sufficient to read other local users' files by going around normal Unix file permissions entirely (block-level reads don't consult a filesystem's ACLs), read unmounted encrypted-volume ciphertext, or read another admin user's disk image files.

`DESIGN.md`'s own "Design for the full version" section already identifies the right fix (switch to XPC with real peer auth) and is refreshingly honest that this spike predates it — so this finding is really "make sure that migration happens before `SALVAGE_BUILD_HELPER=1` / registration is ever turned on for a release," not "something snuck into a shipping build." Given the severity if it *were* shipped unchanged (root-equivalent local privilege escalation, no auth), this is rated **HIGH** rather than CRITICAL specifically because of that current dormancy — but it would be **CRITICAL** the day it ships as-is.

**Fix, before this ever leaves prototype status:** switch to `NSXPCConnection` with `processIdentifier`→code-signature verification of the peer (checking the connecting process's designated requirement matches `com.salvage.app`, not just its Unix credentials), add a device-path allowlist (only devices `diskutil` reports as *unmounted*, matching `DESIGN.md`'s own stated scope), and bound `length`.

---

## 8. Network egress

Grepped `salvage/` for `urllib`, `requests`, `http.client`, `socket.socket`, `aiohttp`, `httpx` — **zero matches** (the one `socket`-adjacent hit anywhere in the project is `AF_UNIX` in the Swift helper spike, which is local IPC, not network). Grepped for any `http://`/`https://` literal across `salvage/`, `packaging/`, `helper/` — the only hits are: a user-facing `QMessageBox` string pointing at the TestDisk download page (`__main__.py:36`, shown to the user, never fetched by the app), two obviously-fake fixture URLs in `ios_fixtures.py` (`example.com`, `assemblygrowth.com` — test data, not live calls), and Apple Developer Portal URLs inside **comments** in `packaging/notarize.sh` (human instructions for the person running notarization, not runtime code). `pyproject.toml` dependencies are `PySide6`, `pillow`, `pillow-heif`, `pycryptodome` — none of these phone home by default.

**Confirmed: the app makes no network requests.**

---

## 9. Incidental findings (not requested, worth a line)

- **`.probe/` is correctly gitignored** (`.gitignore:2`) and has **zero files tracked by git** despite containing what look like real manual-QA artifacts (`pulled_IMG_5715.PNG`, `ios_backup/`, `ios_export/`) — confirmed via `git ls-files .probe/` (empty) and `git check-ignore -v`. Good hygiene; flagging only so it stays that way (easy to `git add -A` this by accident).
- `packaging/salvage.spec` changed on disk mid-review (version now sourced from `pyproject.toml` via `tomllib` instead of hardcoded) — unrelated to this review, not touched, noted per instructions.

---

## Appendix: what was tested live vs. read only

**Live-tested (working PoC or direct reproduction), scripts kept in the session scratch dir, not the repo:**
- `privileged.py` escaping — 12 adversarial payloads × 2 execution paths + 2 positive controls
- `__main__.py` `_raw_read_test` — injection confirmed
- `filesystem.py` path sanitizer — 22 hostile `(dir, name)` pairs
- `filesystem.py` ENAMETOOLONG crash — reproduced directly
- `local_media.py` `_find_mvhd_bytes` recursion crash — reproduced directly
- `sqlite_recover.py` page_count DoS — both the "inconsistent lie" (disproven, blocked by sqlite3 itself) and "consistent sparse file" (proven, ~1 hour extrapolated) variants
- `integrity.py` PNG chunk-length read — disproven (no memory growth)
- `integrity.py` ZIP bomb via `testzip()` — disproven (stdlib chunks reads; confirmed by reading `zipfile.py` source)
- `helper/` socket PATH sanitization under `launchctl submit` — confirmed live (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`)
- `ios.py` decrypted-cache file permissions — confirmed live (0644/0755)
- `dist/Salvage.app` dylib relinking — confirmed via `otool -L` on the actual built bundle
- `.probe/` gitignore status — confirmed via `git ls-files`/`git check-ignore`

**Read and reasoned about, not independently executed (no real device/AFC server available, or clearly safe by code inspection alone):**
- `ios.py` AFC command construction (§4/§2 discussion) — code-review finding only
- `ios.py` keybag TLV/AES unwrap, `ios_parsers.py` protobuf/typedstream walkers — bounds-checked by inspection
- `devices.py` subprocess call sites — list-argv confirmed by inspection
- Windows-specific code paths (`_run_powershell`, reserved device names) — reasoned about, not run (no Windows machine available in this session)

---

## Remediation (2026-09-18)

Every finding below was addressed in `salvage/engine/` (plus one explicitly-scoped
exception in `salvage/ui/workers.py`) in a series of small, individually-tested
commits on `main`. Each fix has a regression test that was verified to fail
against the pre-fix code and pass against the post-fix code (either via a
scoped `git stash` of just that file, or — where stashing the shared working
tree was awkward mid-session — an out-of-band reproduction of the pre-fix
algorithm). The full suite (`pytest`) was green after every commit.

| # | Finding | Status | What changed | Covering test(s) |
|---|---|---|---|---|
| 1 | CRITICAL — `--rawtest` command injection | **Fixed** (prior to this pass) | `_raw_read_test`/`--rawtest` removed from `salvage/__main__.py` | — |
| 2 | HIGH — `helper/` SMAppService daemon, no peer auth | **Deferred / accepted for now** | Not touched this pass — see reasoning below | — |
| 3 | HIGH — `sqlite_recover.recover_records()` hostile page_count DoS | **Fixed** | Page count is now clamped to the file's real size and a hard `_MAX_PAGE_SCAN_BUDGET` (200,000 pages); declared page size is validated against real SQLite page sizes; an optional `cancel` event is polled and stops the scan early | `tests/test_ios_parsers.py::test_recover_records_bounds_hostile_page_count_in_sparse_file`, `::test_recover_records_honours_cancel_event` |
| 4 | MEDIUM — PATH-resolved tool binaries used before elevation | **Fixed** | `photorec.py`/`filesystem.py`/`ios.py` now resolve bundled → fixed system dirs → PATH last, via new `privileged.resolve_trusted_binary()`; a PATH-only match is refused for an elevated run unless root-owned/non-writable, via new `privileged.require_safe_for_elevation()`. Also fixes the packaging bug where a frozen build preferred Homebrew's `photorec` over its own bundled copy | `tests/test_privileged.py` (resolve_trusted_binary/require_safe_for_elevation cases), `tests/test_engine.py::test_resolve_binary_never_reports_a_bare_path_match_when_a_fixed_dir_has_it`, `::test_scan_refuses_elevation_for_untrusted_path_resolved_binary` |
| 5 | MEDIUM — decrypted `Manifest.db` cache world-readable, never deleted | **Fixed** | `BackupReader` now writes the decrypted cache into a fresh `tempfile.mkdtemp()` directory (0700) with the file opened at 0600 at creation, not a sibling of the user-chosen backup folder; deleted in `close()` (now also a context manager); `cleanup_stale_ios_caches()` added and called once at app startup as a best-effort sweep of anything left by a prior crash. All four existing call sites already `close()` in a `finally` block, so cleanup now happens automatically with no UI changes needed | `tests/test_ios.py::test_backup_reader_decrypted_manifest_cache_is_private_and_not_beside_backup_dir`, `::test_backup_reader_decrypted_manifest_cache_deleted_via_context_manager`, `::test_cleanup_stale_ios_caches_removes_leftover_cache_dirs_only` |
| 6 | MEDIUM — one hostile file aborts a whole scan; `MediaScanWorker` missing exception guard; Windows reserved names | **Fixed** | `filesystem.py`: per-file extraction wrapped in `try/except OSError` (skip, count via new `ScanResult.skipped_files`, continue); `_MAX_COMPONENT_LEN` now measured in encoded UTF-8 bytes, not code points; Windows reserved device names (CON/PRN/AUX/NUL/COM1-9/LPT1-9) and trailing dots/spaces are now escaped. `local_media.py`: `_find_mvhd_bytes` recursion bounded to `_MAX_BOX_NESTING_DEPTH=32`. `salvage/ui/workers.py`: `MediaScanWorker.run()` now has the same `try/except Exception` guard its sibling workers already had, emitting `failed` and always emitting `finished_scan` | `tests/test_filesystem.py` (sanitize_component/hostile_file_failure cases), `tests/test_local_media.py::test_mvhd_creation_time_bounds_deeply_nested_moov_boxes`, `tests/test_workers.py` |
| 7 | LOW — unescaped AFC command construction | **Fixed (mitigated)** | New `ios._afc_quote()` escapes backslashes/quotes and strips embedded CR/LF before interpolating device-supplied filenames into `afcclient`'s command line, used in `_list_dir()` and `pull_afc_file()`. Not verified against a real/simulated AFC server (none available, same limitation the original review noted) — verified at the string-construction level instead | `tests/test_ios.py::test_afc_quote_*`, `::test_list_dir_sends_hostile_path_as_one_escaped_argument`, `::test_pull_afc_file_sends_hostile_remote_name_as_one_escaped_argument` |
| 8 | LOW — Windows reserved device names pass through sanitizer | **Fixed** | Folded into #6 above (same `_sanitize_component` fix) | `tests/test_filesystem.py::test_sanitize_component_escapes_windows_reserved_device_names[...]` |
| 9 | LOW — `fake.py` predictable `/tmp` paths | **Accepted, not fixed** | Only reachable via the opt-in `SALVAGE_FAKE=1` dev/demo mode, writes synthetic fixture data (not a real user's personal data), and is never part of the default/shipped path. Judged not worth the churn of changing fixture paths that other dev tooling may depend on | — |
| 10 | LOW — thumbnail cache never expires/scopes by source | **Partially mitigated** | Cache directory/file permissions tightened to 0700/0600 at creation (`thumbcache.py`), closing the access-control-adjacent part. Expiry/scoping itself is a data-hygiene nicety, not an access-control gap (normal home-directory permissions already keep other local users out) — left as a future enhancement (e.g. a "clear thumbnail cache" action), not a security fix | `tests/test_thumbcache.py` (existing suite; permissions covered by manual inspection of the new `os.chmod` calls, no dedicated new test since existing tests don't assert on filesystem mode bits) |

**On #2 (helper daemon, HIGH):** not fixed in this pass. Reasoning: it's
Swift code under `helper/`, a separate codebase from this pass's
`salvage/engine/` (Python) scope; it is not built or embedded in the shipped
app by default (`SALVAGE_BUILD_HELPER=0`) and is never auto-registered
(`DESIGN.md`'s own registration step is a manual, one-time CLI action) — so
there is no exposure in what actually ships today. The review's own
recommended fix (switch to `NSXPCConnection` with code-signature peer
verification, add a device-path allowlist, bound `length`) is a real design
change, not a quick patch, and its own framing is "before this ever leaves
prototype status" rather than an urgent same-day fix. Flagged as a follow-up
task (XPC peer auth for the helper daemon) rather than silently dropped —
**this must land before `SALVAGE_BUILD_HELPER=1` or helper registration is
ever enabled for a release build.**
