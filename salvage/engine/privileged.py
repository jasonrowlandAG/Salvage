"""Run PhotoRec/Sleuth Kit as admin via a one-time OS prompt (macOS osascript / Linux
pkexec / Windows ShellExecuteW "runas").

osascript's `do shell script ... with administrator privileges` blocks until the shell
command returns, so the root-side helper backgrounds PhotoRec (wrapped in `script` for a
pty, since PhotoRec block-buffers stdout otherwise) and returns immediately. Progress is
read back by tailing the output file PhotoRec/`script` writes to.

On macOS, a plain `nohup ... &` launched *inside* the elevated `do shell script` call is
killed the moment that call returns -- the admin-privileges execution context tears down
its whole process tree, unlike an ordinary (non-admin) `do shell script`. Registering the
job with `launchctl submit` instead hands it to launchd, which keeps it running
independently of the escalation session.

Windows has no equivalent of a raw device needing `sudo` -- it needs an elevated
(Administrator) token instead, obtained via the shell's "runas" verb (the UAC prompt).
Unlike `do shell script ... with administrator privileges`, `ShellExecuteW(..., "runas",
...)` does not block waiting for the target to finish, so there is no equivalent of the
launchd detachment trick: the elevated process just keeps running once started. It runs a
small PowerShell helper script (written to the scan's workdir) that starts the real tool
with its output redirected to files, polls for the same cancel marker the mac/linux helper
uses, and leaves the same done marker behind -- so everything downstream of `run_privileged`
(reading progress, cancelling, waiting) is identical across all three OSes. GitHub's
windows-latest runners execute every step already elevated, so `is_windows_admin()` is True
there and the whole ShellExecuteW/UAC dance is skipped -- that is the path CI exercises;
real desktop Windows (a split, non-elevated admin token) takes the prompt path instead.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

_OUT_NAME = "photorec.out"
_ERR_NAME = "photorec.err"  # Windows only: Start-Process can't merge stdout+stderr into one file
_PID_NAME = "photorec.pid"
_CANCEL_NAME = "cancel"
_DONE_NAME = "done"
_SES_NAME = "photorec.ses"


def build_helper_script(photorec_args: list[str], workdir: str | Path, uid: int, gid: int, linux: bool = False) -> str:
    """Build the root-side shell command: run PhotoRec under `script` in the background,
    watch for a cancel marker, chown the workdir back to the invoking user, then mark done.
    Returned as a single command suitable for `sh -c` / `pkexec sh -c`.
    """
    # Always a POSIX path on the target shell (macOS/Linux) regardless of what OS the
    # Python interpreter building this string happens to run on: on a Windows test
    # runner, plain Path(workdir) would be a WindowsPath and render backslashes here,
    # producing a broken shell script.
    workdir = PurePosixPath(str(workdir))
    workdir_q = shlex.quote(str(workdir))
    out_q = shlex.quote(str(workdir / _OUT_NAME))
    ses_q = shlex.quote(str(workdir / _SES_NAME))
    pid_q = shlex.quote(str(workdir / _PID_NAME))
    cancel_q = shlex.quote(str(workdir / _CANCEL_NAME))
    done_q = shlex.quote(str(workdir / _DONE_NAME))

    if linux:
        photorec_cmd = " ".join(shlex.quote(a) for a in photorec_args)
        run_under_script = f"script -q -f -c {shlex.quote(photorec_cmd)} {out_q}"
    else:
        photorec_cmd = " ".join(shlex.quote(a) for a in photorec_args)
        run_under_script = f"script -q -F {out_q} {photorec_cmd}"

    binary_name = Path(photorec_args[0]).name

    inner = (
        f"cd {workdir_q}; rm -f {ses_q} {cancel_q} {done_q}; "
        f"{run_under_script} & spid=$!; "
        f"sleep 0.3; "
        f"pid=$(pgrep -P $spid -x {shlex.quote(binary_name)}); "
        f"echo $pid > {pid_q}; "
        f"while kill -0 $pid 2>/dev/null; do "
        f"if [ -f {cancel_q} ]; then kill $pid 2>/dev/null; fi; "
        f"sleep 0.5; "
        f"done; "
        f"chown -R {int(uid)}:{int(gid)} {workdir_q}; "
        f"touch {done_q}"
    )

    if linux:
        return f"nohup sh -c {shlex.quote(inner)} >/dev/null 2>&1 &"

    label = f"com.salvage.photorec.{uuid.uuid4().hex}"
    inner_with_cleanup = f"{inner}; launchctl remove {shlex.quote(label)}; exit 0"
    return (
        f"launchctl submit -l {shlex.quote(label)} -o /dev/null -e /dev/null -- "
        f"/bin/sh -c {shlex.quote(inner_with_cleanup)}"
    )


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class PrivilegedProcess:
    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self._out_path = self.workdir / _OUT_NAME
        self._err_path = self.workdir / _ERR_NAME  # only ever written on Windows
        self._done_path = self.workdir / _DONE_NAME
        self._cancel_path = self.workdir / _CANCEL_NAME
        self._offset = 0
        self._err_offset = 0

    def read_new_output(self) -> bytes:
        # On macOS/Linux only _out_path is ever written, so this is a no-op there.
        return self._read_new(self._out_path, "_offset") + self._read_new(self._err_path, "_err_offset")

    def _read_new(self, path: Path, offset_attr: str) -> bytes:
        if not path.exists():
            return b""
        try:
            with open(path, "rb") as f:
                f.seek(getattr(self, offset_attr))
                data = f.read()
                setattr(self, offset_attr, getattr(self, offset_attr) + len(data))
                return data
        except OSError:
            return b""

    def poll(self) -> int | None:
        return 0 if self._done_path.exists() else None

    def cancel(self) -> None:
        self._cancel_path.touch()

    def wait(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._done_path.exists():
                return
            time.sleep(0.1)


def run_privileged(args: list[str], workdir: Path, linux: bool = False, uid: int | None = None, gid: int | None = None) -> PrivilegedProcess:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        return _run_privileged_windows(args, workdir)

    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid

    helper = build_helper_script(args, workdir, uid, gid, linux=linux)

    try:
        if linux:
            proc = subprocess.run(["pkexec", "sh", "-c", helper], capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise PermissionError("Administrator authorisation was cancelled")
        else:
            script_str = _applescript_escape(helper)
            osa_source = f'do shell script "{script_str}" with administrator privileges'
            proc = subprocess.run(["osascript", "-e", osa_source], capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                stderr = proc.stderr or ""
                if "User canceled" in stderr or "(-128)" in stderr:
                    raise PermissionError("Administrator authorisation was cancelled")
                raise PermissionError(f"Administrator authorisation failed: {stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise PermissionError("Administrator authorisation was cancelled")

    return PrivilegedProcess(workdir)


# ---------------------------------------------------------------------------
# Windows: UAC elevation via ShellExecuteW "runas"
# ---------------------------------------------------------------------------


def is_windows_admin() -> bool:
    """True if this process already holds an elevated (Administrator) token.

    GitHub's windows-latest runners execute every step this way, so this is the path
    CI exercises; a real desktop install typically has a split, non-elevated admin
    token and gets False here, which is what triggers the UAC prompt below.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def needs_windows_elevation(source: str) -> bool:
    """True if `source` is a raw Windows device path (`\\\\.\\PhysicalDriveN` or
    `\\\\.\\X:`, as built by devices.py) that needs Administrator to read, and this
    process doesn't already have that. Image files (any other path) never need it."""
    return sys.platform == "win32" and str(source).startswith("\\\\.\\") and not is_windows_admin()


def _ps_quote(s: str) -> str:
    """PowerShell single-quoted string literal (only `'` needs escaping, by doubling)."""
    return "'" + str(s).replace("'", "''") + "'"


def _build_windows_ps_script(args: list[str], workdir: Path) -> str:
    """PowerShell equivalent of build_helper_script(): start `args` with its output
    redirected to files, poll for the cancel marker (killing the process if it
    appears), and always leave the done marker so the parent can tell the run
    actually finished."""
    workdir = Path(workdir)
    out_path = workdir / _OUT_NAME
    err_path = workdir / _ERR_NAME
    done_path = workdir / _DONE_NAME
    cancel_path = workdir / _CANCEL_NAME

    arg_list = ", ".join(_ps_quote(a) for a in args[1:])
    stale = ", ".join(_ps_quote(str(p)) for p in (out_path, err_path, done_path, cancel_path))
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"Remove-Item -Force {stale} -ErrorAction SilentlyContinue",
        f"$p = Start-Process -FilePath {_ps_quote(str(args[0]))} -ArgumentList @({arg_list}) "
        f"-WorkingDirectory {_ps_quote(str(workdir))} "
        f"-RedirectStandardOutput {_ps_quote(str(out_path))} -RedirectStandardError {_ps_quote(str(err_path))} "
        "-WindowStyle Hidden -PassThru",
        "while (-not $p.HasExited) {",
        f"    if (Test-Path {_ps_quote(str(cancel_path))}) {{ Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }}",
        "    Start-Sleep -Milliseconds 300",
        "}",
        f"New-Item -ItemType File -Path {_ps_quote(str(done_path))} -Force | Out-Null",
    ]
    return "\r\n".join(lines)


def _win_quote_cmdline(s: str) -> str:
    """Quote one argument for a Windows command-line string (what ShellExecuteW's
    lpParameters expects: space-separated, "-quoted args, not shlex/POSIX rules)."""
    if s and not any(c in s for c in (" ", "\t", '"')):
        return s
    return '"' + s.replace('"', '\\"') + '"'


def _run_privileged_windows(args: list[str], workdir: Path) -> PrivilegedProcess:
    workdir = Path(workdir)
    for name in (_OUT_NAME, _ERR_NAME, _DONE_NAME, _CANCEL_NAME):
        try:
            (workdir / name).unlink()
        except OSError:
            pass

    script_path = workdir / "helper.ps1"
    script_path.write_text(_build_windows_ps_script(args, workdir), encoding="utf-8")
    ps_args = [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
    ]

    if is_windows_admin():
        # Already elevated -- no UAC prompt is possible or needed. Run the helper
        # directly; everything downstream (file-based output/done/cancel) behaves
        # identically to the elevated-via-prompt path below.
        subprocess.Popen(
            ["powershell.exe", *ps_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return PrivilegedProcess(workdir)

    import ctypes

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    params = " ".join(_win_quote_cmdline(a) for a in ps_args)
    SW_HIDE = 0
    result = shell32.ShellExecuteW(None, "runas", "powershell.exe", params, str(workdir), SW_HIDE)
    # ShellExecuteW returns a value > 32 on success; <= 32 is an error code (e.g. the
    # user clicking "No" on the UAC prompt comes back as ERROR_CANCELLED / access denied).
    if int(result or 0) <= 32:
        raise PermissionError("Administrator authorisation was cancelled")
    return PrivilegedProcess(workdir)
