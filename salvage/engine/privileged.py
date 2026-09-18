"""Run PhotoRec as root via a one-time OS admin prompt (macOS osascript / Linux pkexec).

osascript's `do shell script ... with administrator privileges` blocks until the shell
command returns, so the root-side helper backgrounds PhotoRec (wrapped in `script` for a
pty, since PhotoRec block-buffers stdout otherwise) and returns immediately. Progress is
read back by tailing the output file PhotoRec/`script` writes to.

On macOS, a plain `nohup ... &` launched *inside* the elevated `do shell script` call is
killed the moment that call returns -- the admin-privileges execution context tears down
its whole process tree, unlike an ordinary (non-admin) `do shell script`. Registering the
job with `launchctl submit` instead hands it to launchd, which keeps it running
independently of the escalation session.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

_OUT_NAME = "photorec.out"
_PID_NAME = "photorec.pid"
_CANCEL_NAME = "cancel"
_DONE_NAME = "done"
_SES_NAME = "photorec.ses"


# ---------------------------------------------------------------------------
# Trusted binary resolution: shared by photorec.py, filesystem.py and ios.py.
#
# `shutil.which()` searches the *unprivileged* process's own PATH, which a
# local attacker with ordinary user-level code execution can influence (a
# writable directory ahead of the trusted ones - common for developers:
# ~/.cargo/bin, ~/go/bin, nvm/pyenv/rbenv shims). If the tool that resolution
# picks is then run through run_privileged() (elevated to root), that's a
# straight path from "planted a file on PATH" to "ran it as root". Resolving
# our own bundled copy and known fixed system locations first, and only
# falling back to PATH as a last resort, closes most of that off; the
# require_safe_for_elevation() check below is the backstop for the case
# where a PATH match is still the only thing found and the caller is about
# to run it elevated.
# ---------------------------------------------------------------------------


def resolve_trusted_binary(
    name: str, bundled: Path | None, fixed_dirs: tuple[Path, ...]
) -> tuple[Path | None, bool]:
    """Resolve `name`, preferring our own bundled copy, then a fixed/known system
    location, and only falling back to PATH (`shutil.which`) last. Returns
    (path, from_path); `from_path` is True only when nothing but a PATH match
    was found, which is what require_safe_for_elevation() needs to know."""
    if bundled is not None and bundled.exists():
        return bundled, False
    for base in fixed_dirs:
        candidate = base / name
        if candidate.exists():
            return candidate, False
    which = shutil.which(name)
    if which:
        return Path(which), True
    return None, False


def require_safe_for_elevation(path: Path, from_path: bool) -> None:
    """Raise PermissionError if `path` must not be run with administrator
    privileges. A binary we resolved ourselves (bundled or a fixed location) is
    always fine. A binary that was only found via PATH is fine *only* if it's
    root-owned and not writable by anyone else - otherwise the PATH that
    produced it could have been influenced by an unprivileged local attacker,
    and running it as root would hand them a privilege escalation."""
    if not from_path:
        return
    if os.name == "nt":
        # No POSIX ownership/mode bits to check, and run_privileged() here is
        # POSIX-only (macOS osascript / Linux pkexec) - nothing to enforce yet.
        return
    try:
        st = path.stat()
    except OSError as exc:
        raise PermissionError(f"Could not verify '{path}' before running it as root: {exc}") from exc
    if st.st_uid != 0 or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise PermissionError(
            f"Refusing to run '{path}' with administrator privileges: it was found on PATH "
            "rather than a trusted location, and is not a root-owned, non-writable file. "
            "Install it in a standard location (e.g. via Homebrew) or reinstall Salvage."
        )


def build_helper_script(photorec_args: list[str], workdir: str | Path, uid: int, gid: int, linux: bool = False) -> str:
    """Build the root-side shell command: run PhotoRec under `script` in the background,
    watch for a cancel marker, chown the workdir back to the invoking user, then mark done.
    Returned as a single command suitable for `sh -c` / `pkexec sh -c`.
    """
    workdir = Path(workdir)
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
        self._done_path = self.workdir / _DONE_NAME
        self._cancel_path = self.workdir / _CANCEL_NAME
        self._offset = 0

    def read_new_output(self) -> bytes:
        if not self._out_path.exists():
            return b""
        try:
            with open(self._out_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
                self._offset += len(data)
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
