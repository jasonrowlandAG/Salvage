# SalvageHelper — SMAppService spike: results and design

## Verdict

**Registration works. Root access works. FDA inheritance works. Internal-disk
carving still doesn't, for a different reason: the kernel blocks raw reads of
a mounted volume, and the raw *container* underneath is FileVault ciphertext.**

## What was tested (2026-09-06, this Mac)

1. Built `helper/SalvageHelper` (daemon) and `helper/salvage-register` (CLI
   wrapping `SMAppService`) with `swiftc` directly — Command Line Tools only,
   no Xcode project needed.
2. Placed `SalvageHelper` + `salvage-register` in `Contents/MacOS/`, the
   plist in `Contents/Library/LaunchDaemons/com.salvage.helper.plist`
   (`BundleProgram` + `AssociatedBundleIdentifiers: [com.salvage.app]`).
   Signed both individually, then re-signed the whole app `--deep`, all with
   the existing self-signed "Salvage Dev" identity (`TeamIdentifier=not set`
   confirmed on every binary afterward).
3. Ran `salvage-register` from inside the bundle
   (`Salvage.app/Contents/MacOS/salvage-register`). `Bundle.main` correctly
   resolved to the `.app` — **a plain CLI placed in `Contents/MacOS/` can
   drive `SMAppService` for the enclosing bundle.** No PyObjC path was
   needed.
4. `register()` returned `SMAppServiceErrorDomain code=1 "Operation not
   permitted"` but flipped status to `.requiresApproval`. `sfltool dumpbtm`
   confirmed this is the normal first-run flow, not a rejection: a `daemon`
   entry appeared under `com.salvage.app`, disposition
   `disallowed/pending authorization` — same mechanism Disk Drill uses (also
   present in the same BTM database on this machine).
   - No system notification banner was observed for this run. It was
     launched by directly exec'ing the CLI rather than via `open`/Finder;
     the real integration should trigger registration from the running app
     (already opened via Launch Services) rather than a standalone CLI, or
     the notification may not reliably fire.
5. **The Login Items approval happened during the session** (no sudo, no
   GUI automation used or needed on my end — this is the one step that is
   inherently the user's). After approval, `sfltool dumpbtm` showed the
   daemon disposition flip to `enabled/allowed`, and `launchctl print
   system/com.salvage.helper` showed it move from `state = uninitialized`
   to `state = running`, `program identifier =
   Contents/MacOS/SalvageHelper`, running as **uid 0**. No separate Full
   Disk Access row was needed in System Settings → Privacy & Security — the
   daemon's `AssociatedBundleIdentifiers` link to `com.salvage.app` was
   sufficient.
6. Raw reads through the daemon's Unix socket (`/var/run/com.salvage.helper.sock`,
   root:admin 660, exactly as specced):
   - `/dev/rdisk0` (whole physical disk, unmounted container) — **success**, 1 MB read.
   - `/dev/rdisk1` (APFS ISC container, unmounted) — **success**, 1 MB read.
   - `/dev/rdisk3` (synthesized whole APFS container, unmounted as a device
     even though its child volumes are mounted) — **success**.
   - `/dev/rdisk3s1` (`Macintosh HD - Data`, the actual mounted, unlocked,
     FileVault volume) — **failed, errno 13 (EACCES)**.
   This is the key finding: disk0/disk1 previously failed with **EPERM**
   under the `osascript`/`launchctl submit` root approaches (established
   fact #2) and now **succeed** under the SMAppService daemon — direct proof
   the daemon inherits the app's FDA via TCC. But `rdisk3s1` fails for an
   unrelated reason: `log show` caught `kernel[0] (IOStorageFamily)
   disk3s1: device is not readable.` at the exact timestamp — a
   **kernel-level block on raw reads to an actively-mounted volume**,
   independent of TCC/FDA/privilege. This cannot be fixed by more
   entitlements; it would need the volume unmounted (impossible for the
   live startup disk) or a different access path.
7. Value check, substituting the raw container `/dev/rdisk3` for the
   blocked `/dev/rdisk3s1` (256 MB sampled from the middle,
   offset 247,058,178,048): 4.51% of 4 KB blocks all-zero (not
   TRIM-dominated); 16 JPEG-magic (`FFD8FF`) hits, 0 PDF/ZIP/PNG. Expected
   hit count for a 3-byte pattern in 256 MB of **uniformly random data** is
   ≈16 — matching what we saw — while 4-byte/8-byte signatures (PDF/PNG)
   correctly landed at ≈0. First 4 KB block used all 256 byte values
   roughly evenly. **This reads as FileVault ciphertext, not recoverable
   file data.** Carving the raw container would not find real files without
   also reimplementing FileVault+APFS decryption — a much bigger project
   than PhotoRec.

## Answers to the original questions

- Can a self-signed, no-Team-ID `SMAppService` daemon register? **Yes**,
  same approval flow as a Developer ID app.
- Does it run as root? **Yes.**
- Does it inherit the app's FDA? **Yes**, proven on unmounted raw disks.
- Does the daemon need its own separate FDA entry? **No** — not observed;
  `AssociatedBundleIdentifiers` was sufficient.
- Can it read the internal startup disk's data volume raw? **No** — blocked
  at the kernel/IOStorageFamily level while mounted, and the readable
  parent container is encrypted ciphertext. This is a strictly harder wall
  than TCC and matches why commercial tools don't block-carve a live
  internal boot volume.

## Design for the full version (if pursued)

- Keep the daemon (root, `SMAppService`, same identity) — it's the only
  proven way to get any privileged disk access on this Mac without a
  Developer ID/notarization.
- Switch the wire protocol from a bare Unix socket to XPC
  (`NSXPCConnection` + a `MachServices` entry in the plist) for proper
  request auth and structured errors instead of raw bytes — the spike's
  socket protocol was intentionally minimal.
- Do NOT attempt to carve the live internal Data volume — it is blocked at
  the kernel level and, even if it weren't, the underlying bytes are
  encrypted. Scope internal-disk recovery to: (a) already-unmounted
  volumes/disks (external drives not currently mounted, a second internal
  partition that's unmounted), and (b) invoking PhotoRec against those,
  proxied through the daemon so PhotoRec itself can stay unprivileged.
  Explicitly do not offer "scan my startup disk" as a feature backed by raw
  carving; it cannot work as designed.
- Registration should be triggered from the *running, Launch-Services
  launched* Salvage.app (not a bare CLI exec) so the OS's own "background
  item added" notification fires reliably, then poll
  `SMAppService.status`/`sfltool dumpbtm` rather than blocking the UI
  thread.
