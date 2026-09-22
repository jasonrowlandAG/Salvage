# Salvage — Privacy

**Decision (2026-09-22):** Salvage ships with **no automated crash reporting,
telemetry, analytics, or accounts**. Updates are manual (re-clone / pull a new
tag). That keeps the "no network calls" claim literally true.

## What Salvage does on your machine

- Scans disks, images, and iPhone backups you choose.
- Writes recovered files only to the destination folder you pick.
- Keeps temporary working files under that destination (or a private temp
  directory for decrypted iOS manifests), then cleans up when a session ends.
- May write a local thumbnail cache under `~/Library/Caches/Salvage/` on macOS
  so previews stay fast; you can delete that folder anytime.

## What Salvage does *not* do

- It does not open network connections.
- It does not upload recovered files, paths, device identifiers, or backup
  passwords.
- It does not phone home for updates or usage stats.

Verify it yourself: the source is public, and the dependency list has no HTTP
client libraries. A short privacy statement also appears in the README.

## If something crashes

On macOS, open Console.app or look under `~/Library/Logs/DiagnosticReports`
for a Salvage-related report, then email it to the support address in the
README if you want help. Do not attach recovered personal files unless you
intentionally choose to.
