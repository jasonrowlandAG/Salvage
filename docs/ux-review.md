# Salvage — UX & Visual Design Review

Reviewed against Disk Drill, Wondershare Recoverit, EaseUS Data Recovery and Stellar.
Driven live with `SALVAGE_FAKE=1`, real windowing (not offscreen), at the app's default
1100×720 and at 900×600. Screenshots referenced below are fresh, in `.probe/review_*.png`
(the driver scripts are `.probe/review_drive_flow.py`, `review_ios_flow.py`,
`review_media_flow.py`, `review_scale_stress.py` — run any of them with
`SALVAGE_FAKE=1 .venv/bin/python .probe/<script>.py`). Priority: **P0** blocks launch,
**P1** important, **P2** polish.

## Top 10 things to fix before launch

1. **(P0) Results grid freezes for ~20s+ after a real scan lands.** Thumbnail generation
   for the drive-recovery flow is completely unthrottled — one background task fired
   per image the instant results land, with no batching or thread cap. Measured:
   `select_all_filtered()` took **21,467 ms** with that flood in flight vs **64 ms**
   isolated. The Mac-media flow already solved this correctly (§4, finding 4.1) — the
   drive flow doesn't use it.
2. **(P0) Results-page footer buttons visibly clip at scale** — "Select all (filtered)"
   renders as `ect all (filtere` and "Recover selected" as `:over selec` once the
   verify-progress label's text gets long (`.probe/review_01_scale_drive_results_30k_top.png`).
   Real, reproducible, screenshotted at 30,000 items (§4, finding 4.2).
3. **(P0) Exporting a selected iCloud-only item can crash the export.** `export_found`
   has no per-file error handling and the whole call runs synchronously on the UI
   thread with no try/except; a single unreadable `cloud_placeholder` file raises an
   uncaught `FileNotFoundError` out of a button click and aborts the entire batch —
   see reproduction and exact evidence in §2, finding 2.4.
4. **(P0/P1) The window can't actually shrink** — every page shares one `QStackedWidget`
   (`app.py:42,56-69`), whose minimum size is the *largest* page's minimum, not the
   visible one's. Two `setFixedWidth` panels on the Results page force a ~1359px floor
   that leaks into every other page too. Confirmed empirically both in-app and in an
   isolated Qt repro. Full detail in §6, finding 6.4.
5. **(P0/P1) The system-disk warning may over-promise the one thing this app is supposed
   to be scrupulously honest about.** With FileVault off, `source_page.py:33-39` tells
   the user granting Full Disk Access will let Salvage scan the startup disk — but
   `README.md:22` documents that the kernel refuses raw reads of any *mounted* volume
   regardless of FDA on Apple Silicon. See §2, finding 2.1.
6. **(P1) The Done page is nearly empty.** "Recovery complete" — the one screen meant to
   feel like a win — renders as a thin strip of text with two stray, far-apart buttons
   in a sea of grey (`.probe/review_25_drive_done_page.png`,
   `review_11_ios_done_page.png`). Uniquely among every page, `done_page.py:24` adds a
   *leading* `addStretch()` nothing else in the app uses. §3, finding 3.1.
7. **(P1) No time estimate anywhere** — not on the mode picker, not on the scan progress
   page. A first-time user cannot tell if a scan takes 30 seconds or 3 hours. Compounded
   by the Options page being ~50% dead grey space at default window size where that
   information could easily live. §1, finding 1.2 / §3, finding 3.2.
8. **(P1/P2) Stock-icon mismatches.** USB/removable drives use a floppy-disk icon
   (`SP_DriveFDIcon`); both fake iPhones use a "network drive" globe icon
   (`SP_DriveNetIcon`); PDFs, zips and generic docs all share one blue-gradient "lined
   page" icon. None read as their real-world object. §3, finding 3.4.
9. **(P1)** No `QMenuBar`, `QAction`, `QKeySequence`, or About box anywhere in the
   codebase (grep-confirmed, zero matches) — no Quit/Close, no standard shortcuts, no
   app icon, no version string surfaced in the UI. §7, finding 7.1.
10. **(P2)** The app's hardcoded blue brand colour is fought by a few natively-drawn
    controls that pick up the *user's* macOS accent colour instead (grid-tile
    checkboxes, text-field selection highlight both render in system red on this test
    Mac). §3, finding 3.6.

Fixed in this pass (see bottom of doc): badge/pill contrast ratios, Done-page dead
space, results-footer button clipping, preview-panel icon-page centring, and a visible
keyboard-focus indicator for text fields, checkboxes, radio buttons and lists (buttons
turned out to need a different, bigger fix — see finding 6.1b). Everything else above
is a documented recommendation, not yet applied — several (④, structural QStackedWidget sizing; ⑨,
menu bar) are deliberately out of scope for a "small, safe" pass.

---

## 1. First-run comprehension

**1.1 (P1) Two visually-identical rows for the same disk.** The source list shows
"Macintosh HD · System" (the whole disk) immediately above "Macintosh HD · System ·
APFS · /" (its partition) — same name, same badge, distinguished only by a 28px indent
and a trailing filesystem string (`source_page.py:122-170`,
`.probe/review_01_drive_source_initial.png`). Nothing in the UI explains "disk vs.
partition"; the only guidance ("Recovering from a partition… is usually best") sits
below the whole list, disconnected from which rows are partitions.

**1.2 (P1) Quick/Deep/Thorough are explained, but not costed.** The three-line
descriptions on the Options page (`options_page.py:63-71`) are genuinely good —
each states outcome and trade-off in one sentence. What's missing across the entire
app is *any* indication of how long a mode will take (not even "roughly X minutes per
GB"), and Thorough's description ("filesystem records first, then every sector") is
shown unconditionally even when Sleuth Kit isn't installed and that half silently
won't run (`options_page.py:69-71` vs. the fallback comment in
`engine_facade.py:178-180`). A first-time user picks blind on both time and, in that
one case, on what will actually happen.

**1.3 (P2) "Contacts (3 · 0 recovered)"** (`ios_results_page.py:397-401`) reads like
recovery failed for all three, when it actually means "0 of these 3 were
deleted-and-restored — all 3 are just present." Confirmed confusing on first read;
low-confidence on ideal replacement wording so left as a recommendation.

**Positive:** the removable USB partition is auto-selected on load
(`source_page.py:310-311`), saving a click for the single most common case (recover a
dropped SD card / USB stick) — a nice, understated touch competitors don't bother with.

## 2. Trust and honesty

**2.1 (P0/P1) System-disk warning may over-promise.** See Top 10 #5. Exact text at
`source_page.py:33-39`:
> "...macOS doesn't allow raw scanning of it unless Full Disk Access is granted to
> Salvage... Salvage can scan external drives, USB sticks, SD cards and disk images."

`README.md:22` ("Honest limits"):
> "...the kernel refuses raw reads of any *mounted* volume ('device is not readable')...
> No tool gets past that on a running Apple Silicon Mac."

If the README is right (it reads as the more carefully-verified of the two, citing a
specific kernel error), the in-app copy sets up a real user to burn 20 minutes granting
FDA in System Settings for a scan that will still find nothing — the exact
over-promising this app is positioned against. Screenshot:
`.probe/review_03_drive_source_system_disk_warning.png`. **Fix:** either update the
banner to match the README's finding, or state the FDA branch is unverified/best-effort.

**2.2 (Positive, high confidence) Corruption reasons are real, not generic.** Selecting
a verified file shows specific, correct technical reasons — "Integrity: Corrupt —
missing moov atom" for a truncated MP4, and a genuinely broken video preview shows
"This video can't be played — the file may be damaged" rather than hanging or crashing
(`.probe/review_22_drive_preview_video_broken_message.png`). This is exactly the kind
of specific, earned honesty the brand is built on and is worth calling out to Jay as
strong, launch-ready material — screenshot it for marketing.

**2.3 (Positive) The destination-on-source-drive guard is immediate, clear, and
impossible to miss** — red text appears inline the instant a bad folder is chosen,
explains *why* ("can overwrite the files you're trying to get back"), and the Start
button is disabled until fixed (`options_page.py:194-199`,
`.probe/review_04_drive_options_dest_on_source_blocked.png`). This satisfies the
brief's item 5 question directly: yes, it's explained before it blocks.

**2.4 (P0) Exporting an iCloud-placeholder item can crash the whole export.**
`export_found` (`salvage/engine/local_media.py:856-874`) loops every checked,
non-duplicate item and calls `shutil.copy2(fm.path, dest_path)` with no per-item
try/except and no special case for `cloud_placeholder`. `MainWindow.export_media_results`
(`app.py:228-242`) calls this **synchronously on the UI thread** with no surrounding
try/except either (contrast the drive-flow's `RecoverWorker`, which runs on a QThread
and is wrapped in try/except with a `failed` signal → `QMessageBox.critical`,
`app.py:96-126`). Reproduced by selecting a synthetic `cloud_placeholder=True` item
(the fake generator never produces real ones) and exporting:
```
FileNotFoundError: [Errno 2] No such file or directory:
'.../Mobile Documents/com~apple~CloudDocs/IMG_9999.HEIC'
```
Nothing in `MediaListModel`/`MediaResultsPage` prevents a cloud-placeholder item from
being checked and included in "Select all (filtered)" — this is a real production code
path, exercisable any time a user has Full Disk Access granted and iCloud Drive
selected as a source with un-downloaded files in it (exactly what the "iCloud" badge
is advertising it found). **Fix:** wrap the copy loop with per-file try/except and
either skip `cloud_placeholder` items with a note, or wrap the whole call from
`export_media_results` in a try/except mirroring `RecoverWorker`'s pattern and move it
off the UI thread.

## 3. Visual craft

**3.1 (P1, fixed) Done page is nearly empty** — see Top 10 #6. Before/after in
"Fixes applied" below.

**3.2 (P1) Options page is ~50% dead grey space** at the default window size — three
short panels (mode / types / destination) top-anchored, then a large blank gap down to
Back/Start (`.probe/review_08_drive_options_ready_thorough.png`). This follows the
same `addStretch()`-before-buttons convention used consistently elsewhere in the app
(`scan_page.py`, `ios_options_page.py`, etc.), so it's not an inconsistency bug like
Done's — but it's the single best piece of unused real estate in the whole wizard to
put the missing time estimate (1.2) or a short "what happens next" summary, and
currently nothing lives there.

**3.3 (P1, fixed) Preview panel's generic-file page floats awkwardly.**
`_build_icon_page` (`preview_panel.py:421-434`) gives `icon_label` all the stretch
(`v.addWidget(icon_label, 1)`) then appends the message and "Reveal in Finder" button
with no stretch after them — so for a PDF/zip/doc, the icon+message+button cluster
sits centred in the *upper* half of the panel with a second, separate dead gap between
the button and the file's metadata block below
(`.probe/review_19_drive_preview_document.png`). Fixed by adding a trailing stretch so
the cluster centres as a group (see "Fixes applied").

**3.4 (P1/P2) Icon quality** — see Top 10 #8. `SP_DriveFDIcon` (a floppy disk) for
every removable drive, `SP_DriveNetIcon` (a network globe) for both iPhone rows,
`SP_FileDialogDetailedView` for PDFs/txt/generic docs, `SP_DirIcon` for zips. None are
wrong per se (they're valid Qt stock icons) but none look purpose-built next to Disk
Drill's or CleanMyMac's device-accurate iconography
(`.probe/review_01_drive_source_initial.png`).

**3.5 (P2, fixed) Badge and pill contrast.** Computed WCAG contrast ratios (white text
on badge fill, from `results_page.py:39-48`):

| Badge | Colour | Ratio | AA (4.5:1) |
|---|---|---|---|
| Partial | `#b8860b` | 3.25:1 | **Fail** |
| Intact | `#1d8a3d` | 4.42:1 | **Fail (borderline)** |
| "System"/"Backups encrypted" pill text `#8a6d1d` on `#f0e6c8` | — | 3.93:1 | **Fail** |
| Recovered `#0a72e8`, Deleted/Corrupt `#c0392b`, Dup `#8a6d1d`, iCloud/On-disk `#6e6e73` | — | 4.58–5.44:1 | Pass |

Fixed — see "Fixes applied" for the new hex values (chosen to be minimally different
by eye while clearing 4.5:1).

**3.6 (P2) OS accent colour leaks into a brand-blue UI.** Native-drawn controls follow
this Mac's system accent colour (set to red) — the grid-tile checkboxes
(`FileTileDelegate.paint`, drawn via `QApplication.style().drawControl(CE_CheckBox...)`,
`results_page.py:246-251`) and a QLineEdit's text-selection highlight both render red,
while every hardcoded brand colour in `style.py` (buttons, selected-card border,
progress bar, badges) is blue. On a different user's Mac the two colour systems will
disagree even more visibly. Screenshots:
`.probe/review_13_drive_results_verified_badges.png` (red checkboxes),
`.probe/review_05_drive_options_quick_scan_unavailable_simulated.png` (red text
selection). Not fixed here — would need either full-Fusion-style enforcement or
custom-drawn checkboxes, both bigger than "small, safe."

**3.7 (P2) iOS table views use plain text for status, grid views use coloured badges.**
The Messages/Contacts/Notes tables' "Status" column (`ios_results_page.py:65-66`,
`118-119`, `165-166`) is unstyled black text ("Recently Deleted" / "Recovered"), while
the exact same concept elsewhere in the app is always a coloured pill. Two visual
languages for one idea.

**3.8 (P2) Recently-Deleted-photos grid has no fallback icon.** `PhotoGridModel.data`
(`ios_results_page.py:284-297`) returns whatever `QPixmap` it loads from
`thumbnail_path`, with no fallback when that's missing/null — items without a usable
thumbnail render with no icon or box at all, just floating text
(`.probe/review_09_ios_results_photos_tab.png`), unlike every other grid in the app
which falls back to a category icon.

## 4. Information density and scale

Tested with 30,000 synthetic items directly loaded into both results pages
(`.probe/review_scale_stress.py`) — not 30,000 distinct files on disk (a handful of
real sample files are reused by path across all 30,000 entries), so these are UI/model
rendering numbers, not disk-IO numbers.

**4.1 (P0) Drive-flow thumbnailing is unthrottled — see Top 10 #1.**
`ThumbnailLoader.request` (`thumbnails.py:52-53`) does
`self._pool.start(_ThumbnailTask(...))` on `QThreadPool.globalInstance()` with **no
batching, no thread cap** — `ResultsPage.set_result` (`results_page.py:405-406`) fires
one of these per image result immediately. The Mac-media flow's
`BackgroundThumbnailService` (`thumb_service.py`) was clearly built to avoid exactly
this — 3 max threads, batches of 6, explicit viewport-prioritisation — and its own
docstring says so ("so the grid stays smooth with tens of thousands of items"). The
drive flow just doesn't use it. Measured on identical 30,000-item models:
`select_all_filtered()` = **21,467 ms** (drive, flood in flight) vs **88 ms** (media,
throttled service) vs **64 ms** (drive, isolated with no flood — proving the model/view
code itself is fast and the flood is the entire cause).

**4.2 (P0) Footer buttons clip at scale — see Top 10 #2.** At 30,000 items the
`verify_progress_label` text ("Checking recovered files… 0 / 30,000") is long enough
that the `footer_row` (`results_page.py:348-366`) squeezes "Select all (filtered)" and
"Recover selected" below their own text width — QPushButton's default size policy
allows shrinking below its sizeHint. Screenshot:
`.probe/review_01_scale_drive_results_30k_top.png`. The equivalent Mac-media footer
(shorter "Thumbnails 0 / 30,000" text) does not clip at the same count
(`.probe/review_05_scale_media_results_30k_top.png`). Fixed — see below.

**4.3 (Positive) Filtering and selection logic itself scales fine.** Category-filter
change: 72ms; search-filter: 45ms; select-all on 30,000 media items: 88ms; scrolling
(12 discrete jumps across the full 30k-item range): avg 7.3ms/jump, max 10.3ms — no
scrolling jank once the thumbnail flood isn't competing for CPU. The `QListView` +
`QAbstractListModel` architecture is sound; 4.1 is a throttling bug, not a design
flaw.

**4.4 (P2) Footer "N selected" doesn't say "of what."** Filtering to Videos (3 items,
all unchecked) while 23 items remain checked elsewhere still shows "23 selected · 23.4
KB" with no indication that's a *global*, not filtered, count
(`.probe/review_22_drive_preview_video_broken_message.png`). Not wrong, just easy to
misread as "23 selected in what I'm looking at."

## 5. Flow friction

**5.1 Click count, common case (drive flow):** Continue (1, source auto-selected) →
Browse + choose folder (2) → Start scan (3) → Recover selected (4, default selection
already applied post-verification) → Open folder/Start new scan. **Four clicks**
from launch to "files recovered," which is genuinely lean and compares well to
competitors that gate behind more choices. The auto-selected removable partition (1.1
positive) and the sensible default selection (Intact+Partial, excluding Corrupt and
duplicates already on disk — `results_page.py:199-210`) both do real work reducing this
count.

**5.2 (P2) Destination doesn't remember the last folder within a session.**
`OptionsPage.set_device` (`options_page.py:149-159`) always resets `_destination` to
`None` then re-defaults to Desktop, discarding whatever folder the user explicitly
picked for a previous scan in the same run — relevant for the "second scan" case
exercised in `.probe/review_29_small_done.png`'s flow. Minor, but a competitor
convenience explicitly called out as missing in the brief (item 8).

**5.3 (Positive) The dest-on-source block (2.3) prevents the single most damaging
mistake before it can happen**, not after — no wasted scan/recovery cycle.

## 6. Accessibility basics

**6.1 (P1, partially fixed) No `:focus` state defined anywhere in `style.py`.** Every
interactive selector (`QPushButton`, `QLineEdit`, `QComboBox`, `QCheckBox`,
`QRadioButton`) has a default and `:hover`/`:disabled` state but no `:focus` —
keyboard-only navigation had no guaranteed visible indicator of where focus is. Added
a visible focus border for `QLineEdit`, `QCheckBox`, `QRadioButton`, `QListView` and
`QListWidget` (see "Fixes applied") — confirmed working both in isolated repros and in
the real app (`.probe/review_after_06_06_done_page_top_anchored.png` shows the
"Delete scan working files…" checkbox with a clear blue focus box).

**6.1b (P1, new finding — not fixed) `QPushButton` and `QComboBox` cannot be given a
QSS `:focus` style in this Qt/PySide6 build without their own text label disappearing
while focused.** Discovered while implementing 6.1: adding *any* `:focus` rule to
`QPushButton` — `outline` or `border`, minimal or fully-respecified — makes the
button's own text vanish the instant it receives keyboard focus (confirmed with an
isolated repro comparing no-stylesheet native rendering, which is fine, against the
same button with a one-line `QPushButton:focus{border:2px solid #0a72e8;}` added,
which blanks the label; `QComboBox:focus` reproduces identically). Checkboxes, radio
buttons, line edits and list widgets are unaffected — only the "button-family"
widgets. Net effect: buttons remain **exactly as before this review** — Tab-focusing
"Continue"/"Start scan"/"Recover selected" etc. still has **no visible indicator at
all**, and this is the single biggest remaining keyboard-accessibility gap in the app.
Fixing it properly needs a `QProxyStyle` that paints a custom focus rect for
`PE_FrameFocusRect`/`CE_PushButton`, which is beyond a "small, safe" QSS change —
flagged for a follow-up, not attempted here.

**6.2 Colour is never the only signal.** Every badge pairs colour with a text label
("Intact", "Corrupt", "Dup", …) and every integrity/status line in the preview panel is
spelled out in words, not just tinted — this part is done right.

**6.3 (P2) Unchecked checkbox glyph is low-contrast against light thumbnails.** The
tile checkbox (`FileTileDelegate.paint`, `results_page.py:245-251`) is a thin native
outline with no filled background; against pale swatches (e.g. `IMG_0071.jpg`'s pink
tile, `.probe/review_04_media_results_full_grid_with_badges.png`) it nearly
disappears, making it easy to miss that a tile is selectable at all.

**6.4 (P0/P1, documented not fixed) Window cannot shrink below ~1200-1360px logical
width, on *any* page**, including the plain Source page. Root cause, confirmed by an
isolated repro:

```
requested 900x600, current widget = small page (no fixed-width children)
actual window size: 1424 600
stack minimumSizeHint: QSize(1424, 40)   # driven by a HIDDEN sibling page
```

`MainWindow` puts all 11 pages into one `QStackedWidget` (`app.py:42`, `56-69`).
`QStackedWidget`'s minimum size is the **maximum across every page it has ever held**,
not just the one currently shown, because switching pages must not resize the window
out from under the user. Two panels are hard-pinned with `setFixedWidth`:
`results_page.py:313` (`category_list`, 180px) and `:373` (`preview_panel`, 340px);
`media_results_page.py:247` (`sidebar_widget`, 220px) and `:293` (`preview_panel`,
360px). Measured in the real app: `window.resize(900, 600)` on the **Source** page
(which has no fixed-width children of its own) still produced a **1200px**-wide
window; on the **Results** page it produced **~1359px**. Neither page can be narrower
than the widest page in the stack. This is a structural fix (either give the
Results/Media-results side panels a smaller floor with a real minimum instead of a
fixed width, or stop sharing one `QStackedWidget` across every page) — flagged, not
attempted, per the "don't restructure pages" instruction for this pass.

**6.5 Window resizing otherwise behaves correctly** — text reflows, `QScrollArea`s
keep working, nothing overlaps at 900×600 on the pages that *can* reach it
(`.probe/review_26_small_source.png`, `review_27_small_options.png`).

## 7. Platform fit

**7.1 (P1) Zero native chrome.** `grep -rn "QMenuBar\|QAction\|QKeySequence\|menuBar"
salvage/` returns nothing. No Quit (Cmd-Q relies entirely on the OS default empty-menu
fallback), no Close (Cmd-W), no About box, no `setWindowIcon`/`setApplicationName`/
`setApplicationVersion` anywhere (`__main__.py`, `app.py` — confirmed by grep). The
only place a version exists is `pyproject.toml`'s `version = "0.1.0"`, never surfaced
in the running app. This is the single biggest "doesn't feel like a finished Mac app"
gap next to competitors, and for a Windows build there's currently nothing
platform-specific to adapt *from* — no menu, no icon, no accelerators exist on either
platform today.

**7.2 (P2)** The stylesheet applies globally via `app.setStyleSheet(STYLESHEET)`
(`__main__.py:26`) with no light/dark-mode awareness — `style.py` hardcodes a light
palette (`#f5f5f7` window background, `#1d1d1f` text) with no `prefers-color-scheme`
or `QPalette.ColorScheme` handling, so the app will look identical (and slightly
out of place) regardless of the user's macOS appearance setting.

## 8. Missing product features

Confirmed absent by reading `session.py`, `app.py`, and every page — not inferred:

- **Session save/resume of a long scan** — `ScanSession` (`session.py`) is a plain
  in-memory dataclass with no persistence; closing the app mid-scan loses everything
  (the worker is cancelled in `closeEvent`, `app.py:249-271`).
- **Scan history** — `reset_and_go_to_source` (`app.py:244-247`) calls
  `self.session.reset()`, which wipes every field with no log written anywhere first.
- **Pause/resume** — only `Cancel` exists on every progress page (`scan_page.py`,
  `media_scan_page.py`, `ios_backup_page.py`); no pause.
- **Estimated time remaining** — `ScanProgress` (`salvage/engine/models.py`) has
  `elapsed_s` and a `fraction` property but no ETA field or computation anywhere.
- **Sort options** in the results grid — `FileListModel`/`MediaListModel` support
  filtering only; `MediaListModel` fixed-sorts by date descending
  (`media_results_page.py:49`) with no user control, `FileListModel` has no ordering
  control at all.
- **About/Help/version display** — see 7.1.
- **Preferences/Settings** — no such page or menu item exists anywhere.
- **Drag-and-drop of a disk image onto the window** — no `dragEnterEvent`/`dropEvent`
  anywhere in `source_page.py` or `app.py`; a disk image can only be added via the
  `Scan a disk image…` file-picker button.
- **"Recover to…" remembering the last folder** — see 5.2.

---

## Fixes applied in this pass

All changes are in `salvage/ui/style.py`, `salvage/ui/done_page.py`,
`salvage/ui/preview_panel.py` and `salvage/ui/results_page.py`; nothing in engine
code, no page restructuring, no flow-logic changes. `.venv/bin/python -m pytest` still
passes 191/192 (1 pre-existing skip) after every change below.

1. **Badge/pill contrast** (`style.py`, `results_page.py`, `media_results_page.py`
   inherit the same `_BADGE_STYLE` dict) — darkened `Intact` (`#1d8a3d` → `#1d883c`,
   4.42→4.53:1), `Partial` (`#b8860b` → `#996f09`, 3.25→4.53:1), and the `role="badge"`
   pill text (`#8a6d1d` → `#7f641b`, 3.93→4.51:1) to clear WCAG AA 4.5:1. All three
   read as the same colour by eye.
2. **Visible keyboard-focus indicator** — added a `:focus` rule to `style.py` for
   `QLineEdit`, `QCheckBox`, `QRadioButton`, `QListView` and `QListWidget` (a 2px
   brand-blue border). **`QPushButton` and `QComboBox` are deliberately excluded** —
   see finding 6.1b: giving either of those a `:focus` style in this Qt/PySide6 build
   makes the widget's own text disappear while focused, verified with an isolated
   repro before it went anywhere near the real app. Buttons are unchanged and still
   have no visible focus state; that needs a custom `QProxyStyle`, out of scope here.
3. **Done page dead space** (3.1 / Top 10 #6) — removed the leading `addStretch()` in
   `done_page.py` so it matches every other page's top-anchored convention.
4. **Preview panel generic-file page centring** (3.3) — added a trailing `addStretch()`
   after the "Reveal in Finder" button in `preview_panel.py`'s `_build_icon_page`.
5. **Results-footer button clipping at scale** (4.2 / Top 10 #2, `results_page.py`) —
   `verify_progress_label` now has an `Ignored` horizontal size policy so it yields
   space first, and the three footer buttons (`select_all_btn`, `select_none_btn`,
   `recover_btn`) got a `Minimum` horizontal size policy via a small
   `_protect_button_width()` helper, so the layout can no longer compress their text
   below its own `sizeHint()`. Verified at 30,000 items — see
   `.probe/review_after_01_01_results_footer_30k_not_clipped.png` vs. the clipped
   `.probe/review_01_scale_drive_results_30k_top.png`.

No copy was changed — the "Contacts (3 · 0 recovered)"-style wording (finding 1.3) is
left as a recommendation; no confident replacement was identified.

Before/after screenshots: `.probe/review_after_*.png`.
