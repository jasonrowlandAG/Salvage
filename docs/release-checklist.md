# Salvage — Release Checklist

Everything needed to actually ship Salvage publicly, on macOS and Windows.
Items are grouped by topic, each with concrete `- [ ]` actions. Nothing here
is done just by reading it — check items off as they're genuinely
completed, not when they're merely understood.

Related docs: `docs/legal-compliance.md` (licensing analysis),
`packaging/THIRD_PARTY.md` (notices + written source offer shipped in the
app), `docs/privacy.md`, `CHANGELOG.md`.

---

## 0. Distribution mode (chosen 2026-09-22)

**Active mode: no-fee / source-first.** We are **not** joining the Apple
Developer Program for v0.1. Public install is clone + Homebrew tools +
`python -m salvage` (see README). Optional self-built `.app` is documented
as Gatekeeper-rejected without right-click → Open. Notarization (§4) and a
Windows installer (§5) remain optional later work, not launch blockers.

### No-fee launch checklist

- [ ] README install path works on a clean Mac (Homebrew + uv + clone).
- [ ] `docs/privacy.md` published; crash reporting = **none**; updates = **manual git pull**.
- [ ] Support contact published (`jay@assemblygrowth.com`).
- [ ] GitHub repo set to **public**.
- [ ] Tag `v0.1.0`, GitHub Release with release notes (source tag; no notarized DMG asset required).
- [ ] CI green on `main` (macOS + Ubuntu + Windows tests).
- [ ] `SALVAGE_FAKE=1 .venv/bin/python -m salvage` smoke pass.
- [ ] About / Licences screens work when run from source.

---

## 1. Version discipline

**Single source of truth: `pyproject.toml`'s `[project].version`.**
`packaging/salvage.spec` now reads it directly at build time (via
`tomllib`) for both `CFBundleShortVersionString` and `CFBundleVersion` —
previously these were hardcoded separately in the spec and could drift from
`pyproject.toml` silently (they had: both said `0.1.0`, but nothing enforced
that staying true). There is nothing left to keep in sync by hand on macOS.

Windows has no automated equivalent yet: `packaging/windows/installer.nsi`
defines `APP_VERSION` with a hardcoded fallback, designed to be overridden
via `makensis /DAPP_VERSION=x.y.z installer.nsi`.

- [ ] To bump the version: edit `pyproject.toml` only, then rebuild
      (`packaging/build_mac.sh`) — confirm with
      `/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' dist/Salvage.app/Contents/Info.plist`.
- [ ] Whatever CI/build step eventually drives `makensis` on Windows must
      read the version from `pyproject.toml` and pass it via `/DAPP_VERSION`
      — it is not automatic yet, unlike macOS. Wire this up before the
      first Windows release, not after.
- [ ] Tag the release in git (`vX.Y.Z`) matching `pyproject.toml` once
      shipped, and rename `CHANGELOG.md`'s `[Unreleased]` section to
      `[X.Y.Z] - YYYY-MM-DD`.

## 2. CHANGELOG

`CHANGELOG.md` exists at the repo root, derived from `git log` (32 commits
to date), in Keep a Changelog format.

- [ ] Add an entry (even one line, under `[Unreleased]`) alongside any
      user-facing change going forward — don't let it fall behind again.
- [ ] Before tagging a release, review `[Unreleased]` reads cleanly as
      release notes a user could plausibly read.

## 3. Licence & attribution surface

`docs/legal-compliance.md` (the assessment) and `packaging/THIRD_PARTY.md`
(the shipped notices + written source offer, full licence texts included)
are both complete as of this pass. What's still needed is surfacing them
*in the app* — ownership of the actual UI is with whoever's working in
`salvage/ui/`, not this pass, but here's exactly what it must show:

- [ ] **An "About Salvage" panel**, reachable from the app/Help menu,
      showing: Salvage's version (already available via the bundled
      `Info.plist`/package version), a copyright line, and Salvage's own
      MIT licence (full text or a clear link/button to it).
- [ ] **A "Licences" (or "Third-Party Notices") screen**, reachable from
      that About panel or directly from the Help menu, showing:
      - The full list of bundled components with name/version/licence
        (mirrors `packaging/THIRD_PARTY.md`'s "At a glance" table).
      - Full licence text per component, scrollable — a plain monospace
        text view is enough, no need for anything fancier.
      - The written offer of source for the GPL-licensed components, with
        the contact email as selectable/visible text (not baked into an
        image) — a source offer nobody can actually read or act on doesn't
        satisfy the intent of GPLv2 §3(b) even where it technically exists
        in a repo somewhere.
- [ ] **Implementation shortcut already in place:** `packaging/salvage.spec`
      now bundles `LICENSE` and `packaging/THIRD_PARTY.md` verbatim into the
      app as resources, at `licenses/LICENSE` and `licenses/THIRD_PARTY.md`
      under `sys._MEIPASS` when frozen (confirmed present at both
      `Contents/Frameworks/licenses/` and `Contents/Resources/licenses/` in
      a built `dist/Salvage.app` — either path works). **Render these files
      directly** rather than re-typing their content into UI code — keeps
      one source of truth; when `packaging/THIRD_PARTY.md` is updated, the
      in-app screen updates automatically with zero UI code changes. When
      running from source (not frozen), read them from the repo root/
      `packaging/` directly instead of `sys._MEIPASS`.
- [ ] Confirm this screen exists and reads correctly in a **built** app
      (not just when run from source) — `sys._MEIPASS` only exists when
      frozen, so this specific path needs testing against `dist/Salvage.app`,
      not just `.venv/bin/python -m salvage`.
- [ ] Windows equivalent once the Windows build exists: same panel, same
      bundled files, reachable from wherever Windows apps conventionally
      put it (Help menu / a Windows-native About dialog).

## 4. macOS code signing & notarization

**Current state:** signed only with a local self-signed "Salvage Dev"
identity (`security find-identity -p codesigning` shows exactly one
identity, untrusted outside this Mac — `CSSMERR_TP_NOT_TRUSTED`). This is
enough to keep Full Disk Access grants stable across rebuilds on this
machine, but Gatekeeper rejects it on any Mac, confirmed this pass:

```
$ spctl --assess --type open --context context:primary-signature -vv dist/Salvage-0.1.0.dmg
dist/Salvage-0.1.0.dmg: rejected
origin=Salvage Dev
```

That "rejected" is the exact message a downloader would see (surfaced to
them as "Apple could not verify... is free of malware" with a right-click →
Open workaround) until notarized.

- [ ] `packaging/make_dmg.sh` exists, builds, and was verified this pass:
      mounts cleanly (`hdiutil attach`, checksum-verified by hdiutil
      itself), the app inside launches (ran for several seconds under
      `SALVAGE_FAKE=1`, exited cleanly on request, only benign Qt/FFmpeg
      log lines), and produces `dist/Salvage-0.1.0.dmg` at **58 MB**
      (compressed from the 150 MB `.app`).
- [ ] Enrol in the Apple Developer Program (paid, ~US$99/yr) —
      prerequisite for everything below.
- [ ] Create a "Developer ID Application" certificate, install it in this
      Mac's keychain, confirm with `security find-identity -p codesigning`.
- [ ] Rebuild **both** `dist/Salvage.app` and the DMG with
      `SALVAGE_SIGN_IDENTITY="Developer ID Application: ..."` set — the
      "Salvage Dev" identity cannot be notarized, Apple rejects self-signed
      submissions outright.
- [ ] Set up `xcrun notarytool store-credentials` once (needs an
      app-specific password from appleid.apple.com — see the prerequisites
      block at the top of `packaging/notarize.sh` for the exact steps).
- [ ] Run `packaging/notarize.sh dist/Salvage-<version>.dmg` — **written
      this pass but never executed**; this machine doesn't have the
      credentials it needs yet. Confirm `spctl --assess` then reports
      "accepted" / "source=Notarized Developer ID", not "rejected".
- [ ] **Re-check the LGPL "relinking" compliance argument once Hardened
      Runtime is enabled** (notarization requires it; it isn't on today).
      Hardened Runtime's Library Validation could block a user from
      substituting a modified copy of a bundled LGPL library, which is
      currently what makes PySide6/Qt's, libimobiledevice's, and OpenSSL's
      dynamic-linking-based compliance argument hold. See
      `docs/legal-compliance.md`'s "Can you sell Salvage?" section — the
      fallback (the precise source pointers already in
      `packaging/THIRD_PARTY.md`) still works, just confirm it's actually
      being relied on consciously rather than assumed away.

## 5. Windows code signing & SmartScreen

**Current state:** a Windows build now exists and is exercised on every
push: `packaging/salvage_windows.spec` (PyInstaller) plus
`packaging/build_windows.ps1` build it, and `.github/workflows/ci.yml` runs
the full test suite — including real NTFS recovery against a diskpart-built
VHD (`tests/test_windows_ntfs.py`) — on a `windows-latest` GitHub Actions
runner. See "Windows: findings from real testing" below for what that
testing found. What's still outstanding is the *installer*:
`packaging/windows/installer.nsi` is written and documented but **never
compiled, signed, or run** — nothing in CI drives `makensis` yet, and this
project has no Windows machine outside CI to exercise the resulting
installer interactively.

- [ ] Build and test `packaging/windows/installer.nsi` on an actual
      Windows machine or CI runner: install, launch, verify shortcuts
      (Start Menu + optional desktop), uninstall cleanly, confirm the
      registry entries under "Apps & features" look right.
- [ ] Obtain a code-signing certificate — an EV (Extended Validation) cert
      gets you Microsoft SmartScreen reputation immediately; an OV
      (Organization Validation) cert is cheaper but SmartScreen warnings
      ("Windows protected your PC") persist until enough people have
      downloaded and run it to build reputation organically. Budget for
      that friction if going the OV route for launch.
- [ ] Sign `Salvage-Setup-<version>.exe` with `signtool sign /fd sha256
      /a /t <timestamp-server> Salvage-Setup-<version>.exe` (or the
      cert-specific equivalent) once a certificate exists.
- [ ] Confirm the signed installer's publisher name shows correctly in the
      UAC elevation prompt (`RequestExecutionLevel admin` in the .nsi
      triggers this) — an unsigned or freshly-signed-with-no-reputation
      exe shows "Unknown publisher," which reads as a red flag to users.
      Note this is a *separate* UAC prompt from the one `salvage/engine/
      privileged.py` triggers per-scan for raw device access (see §6) —
      don't conflate the two when testing.

## 6. Windows: findings from real testing

Confirmed via `tests/test_windows_ntfs.py` on a Windows CI runner (a
diskpart-built NTFS volume in a fixed VHD): `FilesystemEngine` (Quick scan)
and `PhotoRecEngine` (Deep scan) both correctly recover deleted files on
NTFS — real names, real folders, and byte-identical content, not just
"something was found." Two real engine bugs were found and fixed along the
way (both in `salvage/engine/filesystem.py`):

- `fls`'s mode field for a deleted NTFS entry is `-/r...` (the directory
  entry is left unallocated), not the `r/r...` FAT/exFAT/HFS+ use — the old
  check discarded every deleted NTFS file outright. Fixed by deciding
  "regular file" from the meta-type (after the slash) rather than requiring
  both sides to agree.
- NTFS lists a deleted file's `$FILE_NAME` attribute as a separate,
  metadata-only `fls` row alongside its real `$DATA` row — without
  filtering, every NTFS file was "recovered" twice, once correctly and once
  as a tiny wrong-content duplicate.
- `icat` can hand back a whole allocated cluster rather than truncating to
  a file's recorded logical size (cluster slack); `FilesystemEngine` now
  truncates to `fls`'s reported size when known.

**Known limitation, all platforms: TRIM/UNMAP defeats deleted-file
recovery.** On any TRIM-capable volume — which is most modern Windows and
macOS machines with an SSD — deleting a file lets the OS/filesystem issue a
TRIM (Windows) or the equivalent UNMAP notification to the underlying
storage. The storage device then discards those blocks and reads them back
as zeros. This is not a bug in Salvage: once TRIM has run, the data is
physically gone, and no recovery tool (this one included) can get it back.
This is already documented for macOS/SSDs in the main README's "Honest
limits" section; it applies identically on Windows. This was confirmed
directly while building `tests/test_windows_ntfs.py`: deleted files came
back as correctly-sized, correctly-located clusters of pure zeros —
`FilesystemEngine`'s MFT/offset handling was correct, the content was
simply gone. The test fixture disables delete notifications for its own
plant/delete window (`fsutil behavior set DisableDeleteNotify 1`, restoring
the original value afterwards) specifically so there is something to
recover at all; this is a testing necessity, not something the shipped app
does or should do.

- [ ] **Action before public launch:** make sure user-facing copy (docs,
      in-app copy, sales materials) is honest that recovery odds drop
      sharply, often to zero, on an SSD where TRIM has already run for the
      deleted file — for both Windows and macOS. Don't imply recovery is
      reliably possible on a TRIM-capable SSD after enough time has passed
      for the TRIM to execute.

**Minor, unconfirmed either way — Windows scan progress granularity on
very small/fast scans:** `test_photorec_carves_deleted_ntfs_files_with_live_progress`
originally asserted that PhotoRec's live progress (via pywinpty) advances
across at least two distinct sector readings, to distinguish genuine
streaming from a silent fallback that only reports once at exit. On the
~95 MB test VHD, PhotoRec's DEEP scan finishes on CI hardware well within a
second — too fast to reliably sample more than one reading even when
streaming is working correctly — so the test was relaxed to require only
that at least one live update arrived. This wasn't re-verified against a
scan large enough to prove multiple genuinely distinct readings arrive over
time on Windows; if progress-bar smoothness during a real
(multi-second-or-longer) Deep scan ever looks coarser on Windows than on
macOS, check pywinpty's streaming behaviour there specifically before
assuming it's a UI bug.

## 7. First-run experience

- [ ] **macOS:** the app needs Full Disk Access (System Settings → Privacy
      & Security) for the "Photos & videos on this Mac" finder, and
      triggers the standard administrator prompt once per scan of a real
      device (not an image file) for raw disk access. First run should
      explain *why* before the OS prompt appears, not just let the OS
      dialog surprise the user — confirm the current UI does this (README
      already documents the behaviour; verify the in-app messaging matches
      before shipping).
- [ ] **Windows:** `salvage/engine/privileged.py` now mirrors the macOS
      design — a UAC prompt (`ShellExecuteW` "runas") once per scan of a
      raw device path, skipped entirely when the process already holds an
      elevated token (e.g. GitHub's `windows-latest` runners). Confirm the
      in-app "why is this prompt appearing" messaging fires before the UAC
      dialog the same way it does before macOS's administrator prompt —
      not yet verified against a real (non-CI) Windows desktop.
- [ ] Both platforms: confirm `packaging/THIRD_PARTY.md`'s note that
      PhotoRec needs raw disk access, and that no bundled component
      (Sleuth Kit tools especially, see §10 below) silently no-ops without
      explanation when missing.

## 8. Crash / error reporting policy

**Decided 2026-09-22 (no-fee path):** ship with **no** automated crash
reporting. Users report bugs manually. How to find crash logs is documented
in `docs/privacy.md` (macOS: Console.app / DiagnosticReports).

- [x] Decision recorded: no crash SDK.
- [x] Manual crash-log instructions in `docs/privacy.md`.
- [ ] If adding opt-in reporting later, it must not send recovered file
      contents, paths that reveal personal information, or device
      identifiers beyond what's needed to reproduce a crash — and
      `docs/privacy.md` must update in the same release.

## 9. Privacy statement

**Verified: Salvage makes no network calls** (no HTTP client deps in
`salvage/`; see earlier grep pass). Public statement lives in the README
and `docs/privacy.md`.

- [x] Public-facing privacy statement written.
- [ ] Re-run the privacy grep as part of every tagged release smoke test.
- [ ] If crash reporting (§8) is ever added, update the privacy docs in
      the same release.

## 10. Support and update channels

**Decided 2026-09-22:**

- Support / GPL source offer: **jay@assemblygrowth.com** (same as
  `packaging/THIRD_PARTY.md`).
- Updates: **manual** (`git pull` / new GitHub tag) — no in-app update check.
- Releases: **GitHub Releases** on `jasonrowlandAG/Salvage`.

- [x] Support contact published in README.
- [x] Update mechanism decided (manual).
- [x] Release host decided (GitHub Releases).

## 11. Pre-release smoke test (all platforms)

- [ ] `.venv/bin/python -m pytest` — all tests pass (confirm the count and
      that nothing regressed; `.github/workflows/ci.yml` runs this on
      macOS, Windows and Ubuntu on every push, so a local pass on one
      platform isn't the whole story).
- [ ] `SALVAGE_FAKE=1 .venv/bin/python -m salvage` — full flow works
      without touching real hardware.
- [ ] `packaging/build_mac.sh` — builds cleanly, "no Homebrew references
      remain" check passes.
- [ ] `packaging/build_windows.ps1` — builds cleanly via
      `packaging/salvage_windows.spec`; confirm `--print-engine` reports
      binaries from inside the bundle, not a system install.
- [ ] `packaging/make_dmg.sh` — builds; mount, launch, and `spctl` checks
      per §4 above.
- [ ] Real-hardware pass on macOS: scan an actual USB stick or SD card,
      confirm the admin prompt and Full Disk Access flows both trigger
      and explain themselves; if an iPhone is available, run the backup
      flow end to end.
- [ ] Real-hardware pass on Windows (outside CI): scan an actual USB stick
      or SD card, confirm the UAC prompt triggers and explains itself per
      §7 — CI only ever exercises the already-elevated path (see §6),
      never the interactive UAC prompt a real user sees.
- [ ] `.venv/bin/python -m bench.run` sanity pass — confirm the accuracy
      benchmark still runs and `bench/results/latest.md` numbers haven't
      silently regressed.
- [ ] Re-run the privacy grep from §9.
- [ ] Confirm the Licences/About screen from §3 renders correctly in the
      **built** app.
- [ ] Windows installer, once §5 lands: install via the signed
      `Salvage-Setup-<version>.exe`, launch, run a scan against a real
      removable drive, uninstall, confirm no leftover files/registry
      entries.

---

## Outstanding items (no-fee path, as of 2026-09-22)

Launch blockers for source-first v0.1:

1. **Make the GitHub repo public** and tag `v0.1.0` (operational; see §0).
2. **Optional later — not required for no-fee launch:**
   - Apple Developer Program + notarized DMG (§4).
   - Windows NSIS installer signed and tested (§5).
   - Bundling Sleuth Kit inside a `.app` (source path uses `brew install sleuthkit`).
   - Removing x265 from pillow-heif before a *paid* / notarized binary launch
     (`docs/legal-compliance.md`).
