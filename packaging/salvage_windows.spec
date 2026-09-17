# PyInstaller spec for Salvage on Windows (one-dir build).
#
# Mirrors packaging/salvage.spec (the macOS .app spec) but without the macOS-only
# BUNDLE() step -- a Windows onedir build is just dist/Salvage/Salvage.exe plus its
# support files, no app-bundle wrapper. The photorec_win.exe / Sleuth Kit CLI tools
# are NOT added here (PyInstaller's own binary analysis does not walk or vendor
# arbitrary external tools); packaging/build_windows.ps1 runs this spec first, then
# copies them in afterwards, exactly like build_mac.sh does for the macOS bundle.

from pathlib import Path

block_cipher = None

# PyInstaller exec()s this file without `__file__`; it injects `SPEC` (the path
# to this .spec file) into the namespace instead.
project_root = Path(SPEC).resolve().parent.parent  # noqa: F821
entry_script = project_root / "salvage" / "__main__.py"

# QtMultimedia/QtMultimediaWidgets back the preview panel's video/audio playback
# (salvage/ui/preview_panel.py) -- see packaging/salvage.spec for why these are
# listed explicitly even though PyInstaller's PySide6 hooks normally find them on
# their own. winpty backs photorec.py's Windows live-progress path (see
# salvage/engine/photorec.py); it's only ever imported inside a try/except, which
# PyInstaller's static analysis can miss bundling correctly, so it's listed too.
_hiddenimports = ["PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "winpty"]

a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
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
    icon=str(project_root / "packaging" / "assets" / "Salvage.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
