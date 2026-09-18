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
  socket protocol was intentionally minimal. **Done — see "Authorisation
  model" below.**
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

## Authorisation model (implemented 2026-09-18)

The spike's transport was a Unix socket at `/var/run/com.salvage.helper.sock`,
root:admin 0660, carrying line-delimited JSON. That authenticated nobody:
Unix-domain-socket permissions are enforced against the *connecting* process's
credentials, so on the ordinary single-admin-user Mac every app the user runs
could ask a root daemon to read arbitrary raw disk bytes — around filesystem
permissions entirely. `docs/security-review.md` §7 rates that HIGH only because
the component is dormant, and CRITICAL the day it ships. The socket is gone.

**Transport.** `NSXPCConnection` over the launchd Mach service
`com.salvage.helper` (`MachServices` in the plist). One method:

```swift
func readRawDevice(device: String, offset: UInt64, length: Int,
                   withReply reply: @escaping (Int32, Data?) -> Void)
```

`status` is 0, or a negative errno: `-EINVAL` malformed, `-EACCES` refused by
policy, `-EAGAIN` when `diskutil` could not be consulted.

**Who may connect.** Every connection is checked in
`NSXPCListenerDelegate.listener(_:shouldAcceptNewConnection:)` before the
interface is exported. The peer's code signature must satisfy a requirement
built at startup from the *helper's own* signature:

- signed with a Team ID → `identifier "com.salvage.app" and anchor apple
  generic and certificate leaf[subject.OU] = "<team>"`
- self-signed, no Team ID (the "Salvage Dev" case this spike actually uses) →
  `identifier "com.salvage.app" and certificate leaf = H"<leaf SHA-1>"`
- ad-hoc signed → **no requirement can be derived, so every connection is
  refused.** An ad-hoc signature pins nothing. `packaging/build_mac.sh` warns
  when it falls back to ad-hoc signing for exactly this reason.

Deriving the requirement at runtime rather than hardcoding it is what lets a
self-signed build work at all: the certificate differs per developer machine,
and `build_mac.sh` already signs the app and the helper with the same identity.

*Known limitation:* the peer is resolved by `connection.processIdentifier` via
`kSecGuestAttributePid`, which is in principle open to a PID-reuse race. There
is no public API that hands an XPC connection's audit token to the server
(`NSXPCConnection.auditToken` exists but is not API). This is the fix the
security review asked for and is a different order of protection from "any
admin-group process", but it is worth revisiting if Apple ships a supported
audit-token accessor.

**What may be asked for.** `device` is no longer passed to `open()` as given.
It must parse as exactly a whole-disk or partition node (`/dev/disk3s1`,
`/dev/rdisk3s1` — nothing else, no traversal, no trailing junk), must be a
device `diskutil list` currently knows about, and must be unmounted *through
the whole stack above it*:

- the device itself has no mount point and no mounted snapshot, **and**
- nothing layered on top of it is mounted — a whole disk whose partition is
  mounted, or an APFS physical store whose container has a mounted volume, is
  in use even though it reports no mount point of its own.

That second rule is what refuses `/dev/rdisk0` and `/dev/rdisk3`, both of which
this spike's own test log (§6 above) records as *successful* root reads. They
are the live startup disk seen from one level down, and reading them was only
ever going to yield the FileVault ciphertext §7 measured. Refusing them matches
the scope this document already set: unmounted volumes only.

The path finally opened is rebuilt from the validated identifier rather than
reused from the request, and `length` must be in 1…64 MiB — the spike passed it
straight to `Data(count:)`. The inventory is re-read per request, because a
device that was unmounted when the connection opened may have been mounted
since.

**Tests.** `helper/run-tests.sh` (21 tests, swift-testing — Command Line Tools
ship `Testing.framework` but no XCTest, and the script points `swift test` at
it). Covers device-path parsing against hostile inputs, the in-use rules
including the container/physical-store case above, the length bounds, and the
requirement-string builder. The peer check has both a negative test and a
positive control, so a `SecCode` lookup that silently never worked would fail
the suite rather than pass it. `build_mac.sh` runs the suite before it will
embed the daemon.
