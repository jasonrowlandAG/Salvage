from __future__ import annotations

import glob
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import pty  # POSIX only
except ImportError:
    pty = None  # type: ignore[assignment]

from salvage.engine.models import ScanMode, ScanProgress, ScanResult
from salvage.engine.results import collect_recovered, find_output_dirs

# Live status line, e.g. "Pass 1 - Reading sector    12345/65536, " or the
# fat_unformat variant "Reading sector    12345/65536, 3 files found".
_SECTOR_RE = re.compile(
    rb"(?:Pass\s+(\d+)\s*-\s*)?Reading sector\s+(\d+)\s*/\s*(\d+)(?:,\s*(\d+)\s*files found)?"
)
_NOT_ROOT = b"User is not root!"
_SUCCESS_MARKER = "PhotoRec exited normally."

_CHUNK_SIZE = 8192
_TAIL_KEEP = 4096  # bytes of trailing stdout kept around for regex matching across chunk boundaries
_PROGRESS_INTERVAL = 0.2  # ~5/sec
_OUTPUT_BASE = "recovered"


class PhotoRecEngine:
    def __init__(self, binary: Path | None = None) -> None:
        located = binary if binary is not None else self.locate_binary()
        if located is None:
            raise FileNotFoundError("photorec binary not found; install testdisk or pass an explicit path")
        self.binary = Path(located)

    @staticmethod
    def locate_binary() -> Path | None:
        which = shutil.which("photorec")
        if which:
            return Path(which)

        system = platform.system()
        candidates: list[Path] = []
        if system == "Windows":
            for base in glob.glob(r"C:\Program Files\testdisk*"):
                candidates.append(Path(base) / "photorec_win.exe")
            bundled = Path(__file__).resolve().parent.parent / "bin" / "windows" / "photorec_win.exe"
        else:
            candidates += [
                Path("/opt/homebrew/bin/photorec"),
                Path("/usr/local/bin/photorec"),
                Path("/usr/bin/photorec"),
            ]
            plat_dir = "macos" if system == "Darwin" else "linux"
            bundled = Path(__file__).resolve().parent.parent / "bin" / plat_dir / "photorec"
        candidates.append(bundled)

        for c in candidates:
            if c.exists():
                return c
        return None

    def scan(
        self,
        source: str | Path,
        workdir: Path,
        mode: ScanMode = ScanMode.DEEP,
        extensions: list[str] | None = None,
        on_progress: Callable[[ScanProgress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> ScanResult:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        # A stale session file can make PhotoRec resume onto a TUI prompt and stall.
        session_file = workdir / "photorec.ses"
        if session_file.exists():
            session_file.unlink()

        out_base = workdir / _OUTPUT_BASE
        cmd = self._build_cmd(mode, extensions)
        args = [str(self.binary), "/log", "/d", str(out_base), "/cmd", str(source), cmd]

        # PhotoRec's stdio is fully block-buffered when stdout isn't a tty, so a plain
        # pipe delivers almost nothing until the process exits. Give it a pty so output
        # streams as it's produced, which is what makes live progress possible.
        master_fd: int | None = None
        if pty is not None:
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                args,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
        else:
            proc = subprocess.Popen(
                args,
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        out_q: "queue.Queue[bytes | None]" = queue.Queue()

        def _reader() -> None:
            try:
                while True:
                    if master_fd is not None:
                        try:
                            chunk = os.read(master_fd, _CHUNK_SIZE)
                        except OSError:
                            return
                    else:
                        assert proc.stdout is not None
                        chunk = proc.stdout.read(_CHUNK_SIZE)
                    if not chunk:
                        return
                    out_q.put(chunk)
            finally:
                out_q.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        progress = ScanProgress()
        start = time.monotonic()
        last_emit = 0.0
        tail = b""
        saw_not_root = False
        cancelled = False

        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                self._terminate(proc)
                break
            try:
                chunk = out_q.get(timeout=0.1)
            except queue.Empty:
                chunk = b""
            if chunk is None:
                break

            if chunk:
                tail = (tail + chunk)[-_TAIL_KEEP:]
                if _NOT_ROOT in tail:
                    saw_not_root = True

                match = None
                for match in _SECTOR_RE.finditer(tail):
                    pass  # keep the last match in the tail window
                if match is not None:
                    pass_num, sector, total_sectors = match.group(1), match.group(2), match.group(3)
                    progress.sector = int(sector)
                    progress.total_sectors = int(total_sectors)
                    progress.phase = f"Pass {pass_num.decode()} - Reading sector" if pass_num else "Reading sector"

            now = time.monotonic()
            if on_progress is not None and now - last_emit >= _PROGRESS_INTERVAL:
                progress.files_found = self._count_files(find_output_dirs(workdir, _OUTPUT_BASE))
                progress.elapsed_s = now - start
                on_progress(ScanProgress(**progress.__dict__))
                last_emit = now

        reader_thread.join(timeout=2)
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate(proc)

        log_path = workdir / "photorec.log"
        log_text = log_path.read_text(errors="replace") if log_path.exists() else ""

        output_dirs = find_output_dirs(workdir, _OUTPUT_BASE)
        files = collect_recovered(output_dirs)

        success = (not cancelled) and _SUCCESS_MARKER in log_text and len(output_dirs) > 0
        error = None
        if cancelled:
            error = None
        elif not success:
            if saw_not_root or _NOT_ROOT.decode() in log_text:
                error = (
                    "PhotoRec needs administrator/root privileges to read raw devices on this OS. "
                    "Re-run Salvage as root, or scan a disk image file instead."
                )
            else:
                error = "PhotoRec did not finish successfully; see log for details."

        return ScanResult(
            success=success,
            files=files,
            output_dirs=output_dirs,
            log_text=log_text,
            error=error,
            cancelled=cancelled,
        )

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _count_files(output_dirs: list[Path]) -> int:
        total = 0
        for d in output_dirs:
            with os.scandir(d) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower() != "report.xml":
                        total += 1
        return total

    @staticmethod
    def _build_cmd(mode: ScanMode, extensions: list[str] | None) -> str:
        parts = ["partition_none", "options", mode.value, "fileopt", "everything"]
        if extensions:
            parts.append("disable")
            for ext in extensions:
                parts += [ext.lower().lstrip("."), "enable"]
        else:
            parts.append("enable")
        parts.append("search")
        return ",".join(parts)
