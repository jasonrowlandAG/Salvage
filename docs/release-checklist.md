# Release checklist

## Known limitation: TRIM/UNMAP defeats deleted-file recovery (all platforms)

On any TRIM-capable volume — which is most modern Windows and macOS machines with
an SSD — deleting a file lets the OS/filesystem issue a TRIM (Windows) or the
equivalent UNMAP notification to the underlying storage. The storage device then
discards those blocks and reads them back as zeros. This is not a bug in Salvage:
once TRIM has run, the data is physically gone, and no recovery tool (this one
included) can get it back. This is already documented for macOS/SSDs in the main
README's "Honest limits" section; it applies identically on Windows.

This was confirmed directly while building `tests/test_windows_ntfs.py` (real NTFS
recovery, exercised on a Windows CI runner via a diskpart-attached VHD): deleted
files came back as correctly-sized, correctly-located clusters of pure zeros —
FilesystemEngine's MFT/offset handling was correct, the content was simply gone.
The test fixture disables delete notifications for its own plant/delete window
(`fsutil behavior set DisableDeleteNotify 1`, restoring the original value
afterwards) specifically so there is something to recover at all; this is a
testing necessity, not something the shipped app does or should do.

**Action before public launch:** make sure user-facing copy (docs, in-app copy,
sales materials) is honest that recovery odds drop sharply, often to zero, on an
SSD where TRIM has already run for the deleted file — for both Windows and macOS.
Don't imply recovery is reliably possible on a TRIM-capable SSD after enough time
has passed for the TRIM to execute.

## NTFS deleted-file recovery: verified end to end on real Windows

Confirmed via `tests/test_windows_ntfs.py` on a Windows CI runner (diskpart-built
NTFS volume in a fixed VHD): FilesystemEngine (Quick scan) and PhotoRecEngine (Deep
scan) both correctly recover deleted files on NTFS — real names, real folders, and
byte-identical content, not just "something was found." Two real engine bugs were
found and fixed along the way (both in `salvage/engine/filesystem.py`):

- `fls`'s mode field for a deleted NTFS entry is `-/r...` (the directory entry is
  left unallocated), not the `r/r...` FAT/exFAT/HFS+ use — the old check discarded
  every deleted NTFS file outright. Fixed by deciding "regular file" from the
  meta-type (after the slash) rather than requiring both sides to agree.
- NTFS lists a deleted file's `$FILE_NAME` attribute as a separate, metadata-only
  fls row alongside its real `$DATA` row — without filtering, every NTFS file was
  "recovered" twice, once correctly and once as a tiny wrong-content duplicate.
- `icat` can hand back a whole allocated cluster rather than truncating to a file's
  recorded logical size (cluster slack); FilesystemEngine now truncates to fls's
  reported size when known.

## Windows scan progress granularity on very small/fast scans (minor, unconfirmed either way)

`test_photorec_carves_deleted_ntfs_files_with_live_progress` originally asserted
that PhotoRec's live progress (via pywinpty) advances across at least two distinct
sector readings, to distinguish genuine streaming from a silent fallback that only
reports once at exit. On the ~95 MB test VHD, PhotoRec's DEEP scan finishes on CI
hardware well within a second — too fast to reliably sample more than one reading
even when streaming is working correctly — so the test was relaxed to require only
that at least one live update arrived. This wasn't re-verified against a scan large
enough to prove multiple genuinely distinct readings arrive over time on Windows;
if progress-bar smoothness during a real (multi-second-or-longer) Deep scan ever
looks coarser on Windows than on macOS, check pywinpty's streaming behaviour there
specifically before assuming it's a UI bug.
