from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from salvage.engine.privileged import build_helper_script, require_safe_for_elevation, resolve_trusted_binary


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
# resolve_trusted_binary / require_safe_for_elevation
#
# See docs/security-review.md finding #4: shutil.which() searches the
# *unprivileged* process's own PATH, so a user-writable directory ahead of the
# trusted ones lets a local attacker plant a binary that's then run as root
# via run_privileged(). Bundled/fixed locations must win over PATH, and a
# PATH match must be refused for elevated use unless it's root-owned and
# non-writable.
# ---------------------------------------------------------------------------


def test_resolve_trusted_binary_prefers_bundled_over_fixed_dir_and_path(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled" / "photorec"
    bundled.parent.mkdir()
    bundled.write_text("bundled copy")

    fixed_dir = tmp_path / "fixed"
    fixed_dir.mkdir()
    (fixed_dir / "photorec").write_text("fixed-dir copy")

    monkeypatch.setattr("shutil.which", lambda name: str(tmp_path / "on_path" / name))

    path, from_path = resolve_trusted_binary("photorec", bundled, (fixed_dir,))
    assert path == bundled
    assert from_path is False


def test_resolve_trusted_binary_prefers_fixed_dir_over_path(tmp_path, monkeypatch):
    fixed_dir = tmp_path / "fixed"
    fixed_dir.mkdir()
    (fixed_dir / "photorec").write_text("fixed-dir copy")

    monkeypatch.setattr("shutil.which", lambda name: str(tmp_path / "on_path" / name))

    # bundled=None (not present in this build) and no file at the fixed dir -> falls
    # through; but here the fixed dir *does* have it, so PATH must not be consulted
    # for the result even though `which` was stubbed to return something.
    path, from_path = resolve_trusted_binary("photorec", None, (fixed_dir,))
    assert path == fixed_dir / "photorec"
    assert from_path is False


def test_resolve_trusted_binary_falls_back_to_path_last(tmp_path, monkeypatch):
    on_path = tmp_path / "on_path" / "photorec"
    on_path.parent.mkdir()
    on_path.write_text("only found via PATH")
    monkeypatch.setattr("shutil.which", lambda name: str(on_path))

    missing_fixed = tmp_path / "does_not_exist"
    path, from_path = resolve_trusted_binary("photorec", None, (missing_fixed,))
    assert path == on_path
    assert from_path is True


def test_resolve_trusted_binary_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    path, from_path = resolve_trusted_binary("photorec", None, (tmp_path / "nope",))
    assert path is None
    assert from_path is False


def test_require_safe_for_elevation_allows_non_path_binaries(tmp_path):
    # from_path=False means we resolved it ourselves (bundled/fixed) - always trusted,
    # regardless of its actual ownership/mode (e.g. a dev machine's own user-owned
    # bundled copy under version control must still work for testing).
    user_owned = tmp_path / "photorec"
    user_owned.write_text("x")
    require_safe_for_elevation(user_owned, from_path=False)  # must not raise


def test_require_safe_for_elevation_refuses_user_writable_path_binary(tmp_path):
    hostile = tmp_path / "photorec"
    hostile.write_text("planted by an unprivileged attacker")
    os.chmod(hostile, 0o644)  # owned by the current (non-root) user - exactly the attack

    with pytest.raises(PermissionError):
        require_safe_for_elevation(hostile, from_path=True)


def test_require_safe_for_elevation_allows_root_owned_non_writable_path_binary(tmp_path):
    # /bin/ls is root-owned and not group/other-writable on every real macOS/Linux
    # install - stands in for "PATH happened to resolve to a genuinely trusted binary".
    trusted = Path("/bin/ls")
    if not trusted.exists():
        pytest.skip("no /bin/ls on this platform")
    require_safe_for_elevation(trusted, from_path=True)  # must not raise
