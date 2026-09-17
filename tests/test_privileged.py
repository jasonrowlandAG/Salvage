from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from salvage.engine import privileged
from salvage.engine.privileged import build_helper_script


def test_helper_script_quotes_paths_with_spaces_and_apostrophes():
    workdir = Path("/Volumes/Jay's Drive/scan output")
    args = ["/opt/homebrew/bin/photorec", "/log", "/d", f"{workdir}/recovered", "/cmd", "/dev/rdisk2s1", "search"]

    script = build_helper_script(args, workdir, uid=501, gid=20)

    # Outer form must be `launchctl submit -l <label> -o /dev/null -e /dev/null -- /bin/sh -c '<inner>'`
    # (plain `nohup ... &` backgrounding is killed when the elevated call returns; see module docstring).
    tokens = shlex.split(script)
    assert tokens[0] == "launchctl"
    assert tokens[1] == "submit"
    assert tokens[2] == "-l"
    assert tokens[3].startswith("com.salvage.photorec.")
    assert tokens[4:9] == ["-o", "/dev/null", "-e", "/dev/null", "--"]
    assert tokens[9:11] == ["/bin/sh", "-c"]
    inner = tokens[11]

    # The apostrophe in the path must be escaped correctly for nested single-quoting.
    assert shlex.quote(str(workdir / "photorec.out")) in inner

    # The whole inner script must be syntactically valid shell (real parser check, not
    # just our own string matching).
    check = subprocess.run(["sh", "-n", "-c", inner], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_helper_script_has_cancel_and_done_markers():
    args = ["/opt/homebrew/bin/photorec", "/log", "/d", "/tmp/out/recovered", "/cmd", "/dev/rdisk3", "search"]
    script = build_helper_script(args, "/tmp/out", uid=501, gid=20)
    inner = shlex.split(script)[11]

    assert "/tmp/out/cancel" in inner
    assert "/tmp/out/done" in inner
    assert "/tmp/out/photorec.pid" in inner
    assert "if [ -f " in inner
    assert "kill -0 $pid" in inner
    assert "kill $pid" in inner
    assert "chown -R 501:20" in inner
    assert "launchctl remove com.salvage.photorec." in inner
    assert inner.rstrip().endswith("exit 0")


def test_linux_variant_uses_script_dash_c():
    args = ["/usr/bin/photorec", "/log", "/d", "/tmp/out/recovered", "/cmd", "/dev/sdb1", "search"]
    script = build_helper_script(args, "/tmp/out", uid=1000, gid=1000, linux=True)
    inner = shlex.split(script)[3]

    assert "script -q -f -c" in inner
    assert "script -q -F" not in inner


# ---------------------------------------------------------------------------
# Windows: admin detection / elevation gating (pure logic, runs on every OS)
# ---------------------------------------------------------------------------


def test_is_windows_admin_false_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert privileged.is_windows_admin() is False


def test_needs_windows_elevation_false_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert privileged.needs_windows_elevation(r"\\.\PhysicalDrive0") is False


def test_needs_windows_elevation_false_for_image_file(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(privileged, "is_windows_admin", lambda: False)
    assert privileged.needs_windows_elevation(r"C:\images\disk.raw") is False


def test_needs_windows_elevation_true_for_raw_device_when_not_admin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(privileged, "is_windows_admin", lambda: False)
    assert privileged.needs_windows_elevation(r"\\.\PhysicalDrive0") is True
    assert privileged.needs_windows_elevation(r"\\.\E:") is True


def test_needs_windows_elevation_false_when_already_admin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(privileged, "is_windows_admin", lambda: True)
    # This is the path GitHub's windows-latest runners take: already elevated, so
    # no prompt is needed even for a raw device path.
    assert privileged.needs_windows_elevation(r"\\.\PhysicalDrive0") is False


# ---------------------------------------------------------------------------
# Windows: PowerShell helper script construction (pure string logic, runs on
# every OS -- only actually *executing* the script needs Windows, see below)
# ---------------------------------------------------------------------------


def test_ps_quote_escapes_single_quotes():
    assert privileged._ps_quote("plain") == "'plain'"
    assert privileged._ps_quote("O'Brien's Drive") == "'O''Brien''s Drive'"


def test_build_windows_ps_script_starts_process_and_watches_cancel(tmp_path):
    args = [r"C:\tools\photorec_win.exe", "/log", "/d", str(tmp_path / "recovered"), "/cmd", r"\\.\PhysicalDrive1", "search"]
    script = privileged._build_windows_ps_script(args, tmp_path)

    assert "Start-Process" in script
    assert "-PassThru" in script
    assert f"-RedirectStandardOutput '{tmp_path / 'photorec.out'}'" in script
    assert f"-RedirectStandardError '{tmp_path / 'photorec.err'}'" in script
    assert "-WindowStyle Hidden" in script
    assert f"Test-Path '{tmp_path / 'cancel'}'" in script
    assert "Stop-Process -Id $p.Id -Force" in script
    assert f"New-Item -ItemType File -Path '{tmp_path / 'done'}'" in script
    # Every arg after the binary itself is passed through as its own quoted
    # -ArgumentList element, not reassembled into one string PowerShell would
    # have to re-split (and could split wrong on an embedded space).
    assert "'/log', '/d'" in script


def test_build_windows_ps_script_is_syntactically_plausible_powershell(tmp_path):
    args = ["C:\\tools\\fls.exe", "-r", "-p", r"\\.\E:"]
    script = privileged._build_windows_ps_script(args, tmp_path)
    # Balanced braces/parens is a cheap sanity check that we didn't forget to close
    # the while-loop or the argument-list array literal.
    assert script.count("{") == script.count("}")
    assert script.count("(") == script.count(")")


def test_win_quote_cmdline_quotes_only_when_needed():
    assert privileged._win_quote_cmdline("-NoProfile") == "-NoProfile"
    assert privileged._win_quote_cmdline(r"C:\Users\Jay\My Scan") == '"C:\\Users\\Jay\\My Scan"'


# ---------------------------------------------------------------------------
# Windows: actually running the helper -- only meaningful on Windows, and only
# exercises the "already Administrator" branch, since GitHub's windows-latest
# runners execute every step already elevated (no interactive session exists
# to click a UAC prompt through).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UAC elevation is Windows-only")
def test_run_privileged_windows_already_admin_runs_helper_directly(tmp_path):
    assert privileged.is_windows_admin(), (
        "expected to already be Administrator on a GitHub windows-latest runner; "
        "if this fails, the elevation-skip path this test exists to prove is untested"
    )
    proc = privileged.run_privileged(["cmd.exe", "/c", "echo hello-from-privileged"], tmp_path)
    proc.wait(timeout=20)
    assert (tmp_path / "done").exists()
    assert b"hello-from-privileged" in proc.read_new_output()
