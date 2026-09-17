; packaging/windows/installer.nsi — NSIS installer script for Salvage on Windows.
;
; ============================================================================
; STATUS: UNTESTED. Written and reviewed on macOS, which cannot run NSIS's
; compiler (makensis) or exercise the resulting installer. This script has
; never been compiled or run. Build and test it on a real Windows machine (or
; a Windows CI runner) before it ships — see docs/release-checklist.md for
; what "tested" must mean here (a full install/launch/uninstall pass on a
; clean Windows VM, at minimum).
; ============================================================================
;
; What this script assumes:
;   - A PyInstaller onedir build of Salvage already exists at ${BUILD_DIR}
;     (Salvage.exe plus its bundled Python runtime, DLLs, and the Sleuth Kit /
;     libimobiledevice CLI tools for Windows). Producing that build is out of
;     scope for this file — it's part of the separate Windows-support work
;     (PyInstaller spec + CI under .github/workflows/). This script only
;     packages an already-built app into an installer.
;   - NSIS itself is installed (https://nsis.sourceforge.io/). No third-party
;     NSIS plugins are used, only the stock LogicLib.nsh header, so a bare
;     NSIS install is enough.
;
; To build once the above is true:
;   cd packaging\windows
;   makensis installer.nsi
;   REM or, to inject the version from pyproject.toml instead of the
;   REM fallback below: makensis /DAPP_VERSION=0.1.0 installer.nsi
;   -> produces Salvage-Setup-<VERSION>.exe next to this script.
;
; Code signing: this produces an UNSIGNED installer. Windows SmartScreen will
; warn ("Windows protected your PC") on first run until the exe has enough
; download reputation, and an EV (or OV, slower to build reputation) code-
; signing certificate is what lets you sign it at all — see the "Windows code
; signing and SmartScreen" section of docs/release-checklist.md.

!include "LogicLib.nsh"

!ifndef APP_VERSION
  ; Fallback only. Keep pyproject.toml as the single source of truth: the
  ; real build should pass the version in explicitly, e.g.
  ;   makensis /DAPP_VERSION=0.1.0 installer.nsi
  ; with 0.1.0 extracted from pyproject.toml by whatever CI script drives
  ; this (mirrors how packaging/salvage.spec now reads pyproject.toml
  ; directly on macOS — NSIS has no built-in TOML parser, so the version has
  ; to be handed in rather than read from the file here).
  !define APP_VERSION "0.1.0"
!endif

!define APP_NAME "Salvage"
!define COMPANY_NAME "Assembly Growth"
!define APP_EXE "Salvage.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
; Already-built PyInstaller onedir output, relative to this script.
!define BUILD_DIR "..\..\dist\Salvage"

Name "${APP_NAME}"
OutFile "Salvage-Setup-${APP_VERSION}.exe"
SetCompressor /SOLID lzma

InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_NAME}" "InstallDir"

; Writing to Program Files and HKLM both require elevation.
RequestExecutionLevel admin

; TODO once an .ico exists (convert from packaging/assets/Salvage.icns):
;   Icon "..\assets\Salvage.ico"
;   UninstallIcon "..\assets\Salvage.ico"

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Var DesktopShortcut

; Minimal one-question custom page (plain MessageBox) instead of pulling in
; nsDialogs/MUI2, to keep this script dependency-free. Swap for an MUI2
; checkbox page later if a nicer UI is wanted.
Page custom DesktopShortcutPage

Function DesktopShortcutPage
    StrCpy $DesktopShortcut "0"
    MessageBox MB_YESNO "Create a desktop shortcut for ${APP_NAME}?" IDNO skip
    StrCpy $DesktopShortcut "1"
    skip:
FunctionEnd

Section "Install"
    SetOutPath "$INSTDIR"
    ; Copies the entire onedir build (exe + bundled runtime + DLLs + helper
    ; tools) into Program Files, unmodified.
    File /r "${BUILD_DIR}\*.*"

    WriteUninstaller "$INSTDIR\Uninstall.exe"

    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\Uninstall.exe"

    ${If} $DesktopShortcut == "1"
        CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    ${EndIf}

    WriteRegStr HKLM "Software\${APP_NAME}" "InstallDir" "$INSTDIR"

    ; Registers the uninstaller with "Apps & features" / Control Panel.
    WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "Publisher" "${COMPANY_NAME}"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKLM "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKLM "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir /r "$INSTDIR"

    Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
    RMDir "$SMPROGRAMS\${APP_NAME}"
    Delete "$DESKTOP\${APP_NAME}.lnk"

    DeleteRegKey HKLM "${UNINSTALL_KEY}"
    DeleteRegKey HKLM "Software\${APP_NAME}"
SectionEnd
