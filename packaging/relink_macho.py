#!/usr/bin/env python3
"""Bundle the non-system dylib dependencies of arbitrary added-in binaries.

PyInstaller's own binary analysis only walks/relinks dependencies for things it
discovers itself (the Python interpreter, extension modules, Qt libs, ...). It
does nothing for binaries we drop into the bundle after the fact (photorec,
libimobiledevice's CLI tools), so their Homebrew-absolute LC_LOAD_DYLIB entries
would still point at /opt/homebrew on a machine without Homebrew installed.

This script, given one or more "root" binaries already copied into the .app:
  1. Walks `otool -L` recursively to find every non-system dylib they load.
  2. Copies each into Contents/Frameworks (deduped by basename).
  3. Rewrites every LC_LOAD_DYLIB reference (on both the root binaries and the
     copied dylibs' own cross-dependencies) to an @loader_path-relative path,
     and normalizes each copied dylib's own install name (LC_ID_DYLIB) the
     same way.

Usage: relink_macho.py <path/to/Foo.app> <binary1> [<binary2> ...]
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

_SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
_DEP_RE = re.compile(r"^\s*(\S+)\s+\(compatibility")


def _is_system(dep: str) -> bool:
    return dep.startswith(_SYSTEM_PREFIXES)


def _is_dylib(path: Path) -> bool:
    out = subprocess.run(["otool", "-D", str(path)], capture_output=True, text=True, check=True)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return len(lines) > 1  # header line + an LC_ID_DYLIB line


def _deps(path: Path) -> list[str]:
    out = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()[1:]  # drop the header ("path:") line
    if _is_dylib(path):
        lines = lines[1:]  # drop the self LC_ID_DYLIB line
    deps = []
    for line in lines:
        m = _DEP_RE.match(line)
        if m:
            deps.append(m.group(1))
    return deps


def _loader_path_ref(binary: Path, frameworks: Path, basename: str) -> str:
    depth = len(binary.resolve().parent.relative_to(frameworks.resolve()).parts)
    prefix = "/".join([".."] * depth)
    return f"@loader_path/{prefix}/{basename}" if prefix else f"@loader_path/{basename}"


def relink(app_root: Path, tool_paths: list[Path]) -> None:
    frameworks = app_root / "Contents" / "Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)

    copied: dict[str, Path] = {}  # basename -> dest path in Contents/Frameworks
    queue: list[Path] = list(tool_paths)

    # Discover and copy the closure of non-system dylib dependencies.
    while queue:
        current = queue.pop()
        for dep in _deps(current):
            if _is_system(dep):
                continue
            src = Path(dep)
            if not src.exists():
                continue  # e.g. a dylib's self-id recorded under a different (symlinked) path
            basename = src.name
            if basename in copied:
                continue
            dest = frameworks / basename
            shutil.copy2(src.resolve(), dest)
            dest.chmod(0o755)
            copied[basename] = dest
            queue.append(dest)

    # Rewrite install names: tool binaries + every copied dylib's cross-references.
    for binary in [*tool_paths, *copied.values()]:
        for dep in _deps(binary):
            if _is_system(dep):
                continue
            basename = Path(dep).name
            if basename not in copied:
                continue
            new = _loader_path_ref(binary, frameworks, basename)
            subprocess.run(["install_name_tool", "-change", dep, new, str(binary)], check=True)
        if binary in copied.values():
            subprocess.run(
                ["install_name_tool", "-id", f"@loader_path/{binary.name}", str(binary)],
                check=True,
            )

    print(f"Copied {len(copied)} dylib(s) into {frameworks}: {sorted(copied)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    relink(Path(sys.argv[1]).resolve(), [Path(p).resolve() for p in sys.argv[2:]])
