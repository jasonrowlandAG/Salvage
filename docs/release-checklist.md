# Salvage — Release Checklist

Everything needed to actually ship Salvage publicly, on macOS and Windows.
Items are grouped by topic, each with concrete `- [ ]` actions. Nothing here
is done just by reading it — check items off as they're genuinely
completed, not when they're merely understood.

Related docs: `docs/legal-compliance.md` (licensing analysis),
`packaging/THIRD_PARTY.md` (notices + written source offer shipped in the
app), `CHANGELOG.md`.

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

**Current state:** no Windows build exists yet (separate work, in
progress in another worktree). `packaging/windows/installer.nsi` is
written and documented but **never compiled or run** — this is a macOS
machine and cannot run `makensis` or exercise the resulting installer.

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

## 6. First-run experience

- [ ] **macOS:** the app needs Full Disk Access (System Settings → Privacy
      & Security) for the "Photos & videos on this Mac" finder, and
      triggers the standard administrator prompt once per scan of a real
      device (not an image file) for raw disk access. First run should
      explain *why* before the OS prompt appears, not just let the OS
      dialog surprise the user — confirm the current UI does this (README
      already documents the behaviour; verify the in-app messaging matches
      before shipping).
- [ ] **Windows:** confirm whatever privilege model the Windows engine
      port lands on (likely: an admin prompt for raw device access,
      mirroring macOS) is explained the same way before the OS prompt
      fires. Out of scope for this pass — flag for whoever finishes the
      Windows engine work.
- [ ] Both platforms: confirm `packaging/THIRD_PARTY.md`'s note that
      PhotoRec needs raw disk access, and that no bundled component
      (Sleuth Kit tools especially, see §8 below) silently no-ops without
      explanation when missing.

## 7. Crash / error reporting policy

No crash or error reporting exists today — nothing in the dependency list
(`pip list`) includes a crash-reporting SDK (no Sentry, no Crashlytics
equivalent, nothing). Given the privacy stance below, that's arguably
correct as a default, but it should be a decision, not an oversight.

- [ ] Decide, explicitly: ship with no automated crash reporting (keeps
      the "no network calls" claim in §8 literally true, strongest
      privacy story, but means you rely entirely on user-reported bugs),
      or add strictly opt-in reporting (never on by default, given this
      app reads recovered personal photos/messages/files — an opt-out or
      silent crash reporter would be a serious privacy regression for a
      *recovery* tool specifically, which by definition handles sensitive
      recovered data).
- [ ] If adding opt-in reporting later, it must not send recovered file
      contents, paths that reveal personal information, or device
      identifiers beyond what's needed to reproduce a crash — write that
      constraint down before choosing a vendor, not after integrating one.
- [ ] Until then: document for users how to find and send you a crash log
      manually (macOS: Console.app / `~/Library/Logs/DiagnosticReports`).

## 8. Privacy statement

**Verified this pass: Salvage makes no network calls.** Checked by:
- `pip list` inside `.venv` — no `requests`, `urllib3`, `httpx`, `aiohttp`,
  or any HTTP/analytics/telemetry package installed at all.
- `grep -rniE` across `salvage/` for `socket`, `QtNetwork`,
  `QNetworkAccessManager`, `urllib`, `http.client`, and similar — zero
  matches in actual code. The only `http(s)://` strings anywhere in
  `salvage/` are (a) a help-dialog message pointing a user to *manually*
  download TestDisk if it's missing, and (b) hardcoded URL strings inside
  test fixture data for a Safari-history *parsing* test — neither is a
  live network call.
- `QtNetwork.framework` is present in the bundle (PyInstaller's standard
  PySide6/Qt dependency set includes it whether or not it's used), but
  nothing in `salvage/` imports or calls into it.

This is a genuine, verifiable selling point for a file-recovery tool
specifically — users trust it with recovered personal photos, messages,
and documents, and "your recovered files never leave this Mac" is both true
today and easy to keep true.

- [ ] Write a short, public-facing privacy statement using the verification
      above, e.g.: *"Salvage never sends anything over the network. All
      scanning, recovery, and preview happens entirely on your device.
      There is no telemetry, no analytics, no account, and no cloud
      component — verify it yourself: the source is on GitHub."*
- [ ] Re-run the same grep check as part of the pre-release smoke test
      (§10) for every future release — this claim needs to stay true, not
      just be true once.
- [ ] If crash reporting (§7) is ever added, the privacy statement must be
      updated in the same release, not after.

## 9. Support and update channels

No infrastructure exists for either yet.

- [ ] Decide and publish a support contact — `packaging/THIRD_PARTY.md`'s
      written source offer currently points to `jay@assemblygrowth.com`;
      confirm that's the intended long-term address or set up a dedicated
      one (e.g. `support@` or `salvage@` at whatever domain Salvage ships
      under) before launch, since GPL source requests will go there too.
- [ ] Decide an update mechanism: manual re-download (simplest, matches
      "no network calls" exactly, but means users must notice a new
      version themselves), versus an in-app update check (any check-in
      call, even a passive one, contradicts the "no network calls" privacy
      claim above unless it's explicitly opt-in and disclosed — decide
      deliberately, don't add one silently later).
- [ ] Pick where releases live (GitHub Releases is the obvious default
      given `packaging/THIRD_PARTY.md`'s source links already point to
      GitHub for the bundled components) and document it somewhere a user
      would actually find it (README, and/or the About panel from §3).

## 10. Pre-release smoke test (both platforms)

- [ ] `.venv/bin/python -m pytest` — all tests pass (191 at last count;
      confirm the count and that nothing regressed).
- [ ] `SALVAGE_FAKE=1 .venv/bin/python -m salvage` — full flow works
      without touching real hardware.
- [ ] `packaging/build_mac.sh` — builds cleanly, "no Homebrew references
      remain" check passes.
- [ ] `packaging/make_dmg.sh` — builds; mount, launch, and `spctl` checks
      per §4 above.
- [ ] Real-hardware pass on macOS: scan an actual USB stick or SD card,
      confirm the admin prompt and Full Disk Access flows both trigger
      and explain themselves; if an iPhone is available, run the backup
      flow end to end.
- [ ] `.venv/bin/python -m bench.run` sanity pass — confirm the accuracy
      benchmark still runs and `bench/results/latest.md` numbers haven't
      silently regressed.
- [ ] Re-run the privacy grep from §8.
- [ ] Confirm the Licences/About screen from §3 renders correctly in the
      **built** app.
- [ ] Windows, once that work lands: install via the signed
      `Salvage-Setup-<version>.exe`, launch, run a scan against a real
      removable drive, uninstall, confirm no leftover files/registry
      entries.

---

## Outstanding launch blockers (as of this pass)

Ranked by severity, not by section order above:

1. **The Sleuth Kit tools (`fls`/`icat`/`fsstat`/`mmls`) aren't in the
   installer**, despite `salvage/engine/filesystem.py` requiring them for
   Quick scan and for the *default* Thorough mode. `packaging/build_mac.sh`'s
   `TOOLS` array only copies `photorec` and the four libimobiledevice
   tools. On this dev machine it's masked because
   `FilesystemEngine.locate_binaries()` falls back to Homebrew's
   `/opt/homebrew/bin/*` — on any other Mac, Quick/Thorough scan will raise
   `FileNotFoundError` immediately. Not fixed in this pass: closing it
   properly also pulls in `libewf` and `afflib` (licences now vetted in
   `packaging/THIRD_PARTY.md`, but afflib's non-standard advertising clause
   needs a decision) and is a product call, not a docs/installer change.
   See `docs/legal-compliance.md`'s "practical, lowest-friction path"
   section for both options.
2. **x265, bundled inside the pillow-heif wheel, is GPL-2.0** and — because
   it's dynamically loaded *into the same process* as Salvage's own code
   (unlike PhotoRec, which is a clean subprocess) — creates a genuinely
   contested "does this make the combined work GPL" question. Recommended
   fix: stop shipping x265 (Salvage only decodes HEIC for preview; a
   decode-only libheif build removes the whole question). Not fixed in
   this pass — it's an engineering task (rebuild/re-vendor libheif), not a
   doc change. See `docs/legal-compliance.md` for the full analysis; get a
   real lawyer's opinion before a *paid* launch if x265 is still bundled.
3. **Not notarized.** `packaging/notarize.sh` is written but has never
   run — needs a paid Apple Developer account, a Developer ID certificate,
   and stored notarytool credentials, none of which exist on this machine
   today. Until it runs, every downloader sees Gatekeeper's rejection
   (confirmed message captured in §4) and needs right-click → Open.
4. **Windows installer untested** — never built or run, because this is a
   macOS-only environment. Needs a real Windows pass per §5 before it
   ships, plus a code-signing certificate (SmartScreen will otherwise warn
   on every first run).
5. **No Licences/About UI yet** — §3 specifies exactly what's needed and
   the data files are already bundled and ready to read; the screen itself
   hasn't been built (owned by UI work, not this pass).
6. **No support/update channel decided** — §9. Low effort, not yet done.
7. **No crash-reporting decision recorded** — §7. Defaulting to "none" is
   defensible and keeps the privacy story simple, but write that decision
   down rather than leaving it implicit.
