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
