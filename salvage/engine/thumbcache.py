"""Persistent on-disk thumbnail cache.

Thumbnails are generated once and written to
``~/Library/Caches/Salvage/thumbs/<sha1(path|size|mtime)>.jpg`` so a later scan (or a later
run of the app) can show them instantly instead of re-decoding every image/video from
scratch. Never call `ensure()` on a `FoundMedia.cloud_placeholder` item — the file isn't
actually on disk yet and reading it would trigger an iCloud download.

Generation order per file:
1. Pillow (+ pi_heif for HEIC/HEIF, and whatever RAW plugins are installed) for images.
2. macOS QuickLook (`qlmanage -t`) for videos and anything Pillow can't open — this is the
   only practical way to get a frame out of most consumer video codecs without a full
   ffmpeg dependency, and QuickLook already knows how to preview them.
3. Nothing — `ensure()` returns None and the caller falls back to a generic category icon.

QuickLook is invoked per file with a short timeout rather than one call for several files at
once: real Messages video attachments occasionally make `qlmanage` hang for a long time
instead of failing (observed on roughly 1 in 5 real .MOV files during testing — apparently
an unsupported codec) and a multi-file invocation processes its inputs sequentially, so one
bad file would stall every other file queued behind it in the same call.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    # pi-heif, not pillow-heif: same upstream project and the same decode API, but its
    # wheel bundles only libheif + libde265 (decode) and no x265 HEVC *encoder*, which
    # keeps the whole bundle off GPLv2. Salvage never writes HEIC, so the encoder was
    # dead weight. See packaging/THIRD_PARTY.md.
    import pi_heif

    pi_heif.register_heif_opener()
except ImportError:  # pragma: no cover - exercised only when the dep is missing
    pass

THUMB_SIZE = 256
# Larger render used by the preview panel's main view and its "open full size" zoom
# window — same cache dir and generation logic as THUMB_SIZE, just a bigger target, so
# it's naturally a different cache entry (the cache key already includes size).
PREVIEW_SIZE = 2048
JPEG_QUALITY = 80

CACHE_DIR = Path.home() / "Library" / "Caches" / "Salvage" / "thumbs"

# Extensions QuickLook is worth trying for (Pillow can't open these).
_QUICKLOOK_EXTS = {
    "mov", "mp4", "m4v", "avi", "mkv", "3gp", "3g2",
    "heic", "heif",  # tried only if Pillow/pi_heif failed to open them
}


def _cache_key(path: Path, file_size: int, mtime: float, render_size: int) -> str:
    # file_size + mtime invalidate the entry when the file's own content changes;
    # render_size distinguishes the small grid thumbnail from the larger preview render
    # of the same file, so the two never collide on one cache file.
    raw = f"{path}|{file_size}|{mtime}|{render_size}"
    return hashlib.sha1(raw.encode("utf-8", errors="surrogateescape")).hexdigest()


def _cache_path_for(path: Path, file_size: int, mtime: float, render_size: int = THUMB_SIZE) -> Path:
    return CACHE_DIR / f"{_cache_key(path, file_size, mtime, render_size)}.jpg"


def get(path: Path) -> Path | None:
    """Returns the cached thumbnail for `path` if one already exists on disk, else None.

    Does not generate anything — safe to call on cloud placeholders.
    """
    return _get_sized(path, THUMB_SIZE)


def get_preview(path: Path) -> Path | None:
    """Like `get()` but for the larger `PREVIEW_SIZE` render."""
    return _get_sized(path, PREVIEW_SIZE)


def _get_sized(path: Path, render_size: int) -> Path | None:
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    cache_path = _cache_path_for(path, st.st_size, st.st_mtime, render_size)
    return cache_path if cache_path.exists() else None


def _generate_with_pillow(path: Path, dest: Path, max_size: int = THUMB_SIZE) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_size, max_size))
            # This cache holds thumbnails of whatever was scanned, including someone
            # else's private photos (an old iPhone backup, a found drive, ...) - keep
            # both the directory and each file locked to this user, not left at
            # whatever the process umask happens to allow.
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(dest.parent, 0o700)  # tighten a dir that pre-dates this fix, too
            tmp = dest.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=JPEG_QUALITY)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        return True
    except Exception:
        return False


_QUICKLOOK_TIMEOUT = 8  # seconds. Real-world videos occasionally make qlmanage hang rather
# than fail fast (observed on ~1 in 5 Messages video attachments in testing, likely a codec
# quicklookd can't decode) — this bounds the damage to one slow file rather than one hung
# worker thread for minutes.


def _generate_with_quicklook(path: Path, dest: Path, size: int = THUMB_SIZE) -> bool:
    if shutil.which("qlmanage") is None:
        return False
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            subprocess.run(
                ["qlmanage", "-t", "-s", str(size), "-o", tmp_dir, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_QUICKLOOK_TIMEOUT, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        produced = list(Path(tmp_dir).glob(f"{path.name}.png"))
        if not produced:
            produced = [p for p in Path(tmp_dir).iterdir() if p.is_file()]
        if not produced:
            return False
        return _generate_with_pillow(produced[0], dest, max_size=size)


def generate_batch(paths: list[Path]) -> dict[Path, Path | None]:
    """Generates thumbnails for several files, trying Pillow first (fast, never hangs).

    Anything Pillow can't open falls back to QuickLook, one file at a time rather than one
    `qlmanage` call for the whole remainder: qlmanage occasionally hangs on a real-world
    video (an unsupported codec, apparently — observed on roughly 1 in 5 Messages video
    attachments) rather than failing fast, and it processes a multi-file invocation's inputs
    sequentially, so a single hung file would stall every other file queued behind it in the
    same call until the whole invocation times out. Bounding each file to its own short
    timeout keeps one bad file from costing the batch more than a few seconds.
    Returns {path: cache_path_or_None}.
    """
    results: dict[Path, Path | None] = {}
    remaining: list[Path] = []
    for path in paths:
        path = Path(path)
        try:
            st = path.stat()
        except OSError:
            results[path] = None
            continue
        dest = _cache_path_for(path, st.st_size, st.st_mtime)
        if dest.exists():
            results[path] = dest
            continue
        if _generate_with_pillow(path, dest):
            results[path] = dest
        else:
            remaining.append(path)

    for path in remaining:
        try:
            st = path.stat()
        except OSError:
            results[path] = None
            continue
        dest = _cache_path_for(path, st.st_size, st.st_mtime)
        results[path] = dest if _generate_with_quicklook(path, dest) else None

    return results


def ensure_for(media) -> Path | None:
    """Like `ensure()`, but takes a FoundMedia-shaped object and refuses cloud placeholders.

    A `cloud_placeholder` item's bytes aren't actually on this Mac yet — opening it (even
    just to read a few header bytes for a thumbnail) would trigger an iCloud download.
    """
    if getattr(media, "cloud_placeholder", False):
        return None
    return ensure(media.path)


def ensure_preview_for(media) -> Path | None:
    """Like `ensure_for()`, but generates the larger `PREVIEW_SIZE` render."""
    if getattr(media, "cloud_placeholder", False):
        return None
    return ensure_preview(media.path)


def ensure(path: Path) -> Path | None:
    """Returns the cached thumbnail for `path`, generating it if missing.

    Never call on a cloud_placeholder FoundMedia — it isn't actually on disk yet.
    Returns None (caller should show a category icon) if nothing could be generated.
    """
    return _ensure_sized(path, THUMB_SIZE)


def ensure_preview(path: Path) -> Path | None:
    """Like `ensure()`, but generates the larger `PREVIEW_SIZE` render used by the
    preview panel's main view and its "open full size" zoom window.

    Never call on a cloud_placeholder FoundMedia — it isn't actually on disk yet.
    """
    return _ensure_sized(path, PREVIEW_SIZE)


def _ensure_sized(path: Path, render_size: int) -> Path | None:
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    dest = _cache_path_for(path, st.st_size, st.st_mtime, render_size)
    if dest.exists():
        return dest

    ext = path.suffix.lstrip(".").lower()
    if ext not in _QUICKLOOK_EXTS:
        if _generate_with_pillow(path, dest, max_size=render_size):
            return dest
    else:
        # Still worth a Pillow attempt for HEIC/HEIF when pi_heif is present.
        if ext in ("heic", "heif") and _generate_with_pillow(path, dest, max_size=render_size):
            return dest

    if _generate_with_quicklook(path, dest, size=render_size):
        return dest

    return None
