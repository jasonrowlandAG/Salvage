# PyInstaller spec for Salvage.app (macOS, one-dir bundle).
#
# One-dir (not one-file) because a one-file build unpacks Qt into a temp dir
# on every launch, which is slow and fragile for a Qt app. This spec only
# handles the PyInstaller/Qt side of packaging; the CLI helper tools
# (photorec, idevice_id, ideviceinfo, idevicebackup2, afcclient) and their
# Homebrew dylib dependencies are copied in and re-linked by
# packaging/build_mac.sh AFTER this spec runs, because PyInstaller's own
# binary analysis does not walk or rewrite install names for arbitrary
# binaries added here.

import tomllib
from pathlib import Path

block_cipher = None

# PyInstaller exec()s this file without `__file__`; it injects `SPEC` (the path
# to this .spec file) into the namespace instead.
project_root = Path(SPEC).resolve().parent.parent  # noqa: F821
entry_script = project_root / "salvage" / "__main__.py"

# Single source of truth for the version: pyproject.toml. Previously this spec
# hardcoded its own copy, which could silently drift from pyproject.toml's.
with open(project_root / "pyproject.toml", "rb") as _f:
    _version = tomllib.load(_f)["project"]["version"]

# QtMultimedia/QtMultimediaWidgets back the preview panel's video/audio playback
# (salvage/ui/preview_panel.py). PyInstaller's PySide6 hooks (add_qt6_dependencies) already
# walk the real dependency graph and correctly pull in the "multimedia" plugin directory
# (libffmpegmediaplugin.dylib + libdarwinmediaplugin.dylib) and the FFmpeg shared libraries
# it links against (libavformat/libavcodec/libavutil/libswscale/libswresample) — verified by
# inspecting a built dist/Salvage.app, launching it, and playing a real video through it.
# These two are already detected automatically because preview_panel.py imports them at
# module level, but they're listed explicitly anyway: PyInstaller can only discover a plugin
# category for a Qt module it has actually traced into the dependency graph, so if a future
# refactor moves these imports somewhere PyInstaller's static analysis can't see (e.g. behind
# a runtime conditional), the multimedia backend would silently disappear from the build.
_hiddenimports = ["PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]

# Bundled so the in-app Licences/About screen (docs/release-checklist.md) can
# read and display them directly instead of duplicating their text in UI
# code -- packaging/THIRD_PARTY.md stays the single source of truth for
# third-party notices. Frozen, these land under sys._MEIPASS/licenses/.
_datas = [
    (str(project_root / "LICENSE"), "licenses"),
    (str(project_root / "packaging" / "THIRD_PARTY.md"), "licenses"),
]

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=[],
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Salvage",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Salvage",
)

app = BUNDLE(
    coll,
    name="Salvage.app",
    icon='assets/Salvage.icns',
    bundle_identifier="com.salvage.app",
    info_plist={
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSHumanReadableCopyright': 'Copyright Assembly Growth. MIT licensed. Bundles PhotoRec/TestDisk (GPLv2) and libimobiledevice (LGPLv2.1). See Licences in the app for details.',
        "NSHighResolutionCapable": True,
        "CFBundleName": "Salvage",
        "CFBundleDisplayName": "Salvage",
        "CFBundleShortVersionString": _version,
        "CFBundleVersion": _version,
    },
)
