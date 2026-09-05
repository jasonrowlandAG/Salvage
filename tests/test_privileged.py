from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

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
