"""Entry point: `python -m salvage`."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from salvage.ui import engine_facade
from salvage.ui.app import MainWindow
from salvage.ui.style import STYLESHEET


def main() -> None:
    if "--print-engine" in sys.argv[1:]:
        _print_engine_info()
        return
    if "--rawtest" in sys.argv[1:]:
        _raw_read_test(sys.argv[sys.argv.index("--rawtest") + 1 :])
        return

    fake = os.environ.get("SALVAGE_FAKE") == "1"

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    engine, _warning, needs_binary_dialog = engine_facade.make_engine(fake)

    if needs_binary_dialog:
        QMessageBox.information(
            None,
            "PhotoRec not found",
            "Salvage needs the PhotoRec tool from TestDisk to recover files.\n\n"
            "Install it with:\n  brew install testdisk\n\n"
            "or download it from https://www.cgsecurity.org/wiki/TestDisk_Download",
        )

    window = MainWindow(fake, engine)
    window.show()
    sys.exit(app.exec())


def _print_engine_info() -> None:
    """Diagnostic for packaging: report where the engine locates its binaries, with
    no GUI. Used to verify a frozen build finds the bundled photorec/libimobiledevice
    tools rather than falling through to Homebrew."""
    meipass = getattr(sys, "_MEIPASS", None)
    print(f"frozen={getattr(sys, 'frozen', False)} MEIPASS={meipass}")
    try:
        from salvage.engine.photorec import PhotoRecEngine

        print(f"photorec={PhotoRecEngine.locate_binary()}")
    except ImportError as exc:
        print(f"photorec=<unavailable: {exc}>")
    try:
        from salvage.engine.ios import _tool

        print(f"idevice_id={_tool('idevice_id')}")
    except ImportError as exc:
        print(f"idevice_id=<unavailable: {exc}>")


def _raw_read_test(args: list[str]) -> None:
    """Diagnostic: read the first 16 MB of a device node and report whether macOS
    allowed it and whether the bytes look like plaintext. Writes to args[1]."""
    device, out = args[0], args[1]
    as_root = len(args) > 2 and args[2] == "root"
    lines = [f"device={device} uid={os.getuid()} as_root={as_root}"]
    try:
        if as_root:
            import subprocess

            tmp = out + ".bin"
            script = (
                f'do shell script "dd if={device} bs=1m count=16 of={tmp} 2>{tmp}.err; '
                f'chown {os.getuid()} {tmp} {tmp}.err 2>/dev/null; cat {tmp}.err" with administrator privileges'
            )
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
            lines.append(f"osascript rc={proc.returncode} dd_says={proc.stdout.strip()[-300:]} err={proc.stderr.strip()[-200:]}")
            with open(tmp, "rb") as f:
                data = f.read()
            for p in (tmp, tmp + ".err"):
                if os.path.exists(p):
                    os.unlink(p)
        else:
            with open(device, "rb", buffering=0) as f:
                data = f.read(16 * 1024 * 1024)
        blocks = range(0, len(data), 4096)
        zero = sum(1 for i in blocks if not any(data[i : i + 4096]))
        printable = sum(1 for i in blocks if sum(32 <= b < 127 for b in data[i : i + 4096]) > 3000)
        lines += [f"read_ok bytes={len(data)}", f"zero_blocks={zero} of {len(data)//4096}", f"mostly_text_blocks={printable}"]
    except OSError as exc:
        lines.append(f"read_failed: {exc}")
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
