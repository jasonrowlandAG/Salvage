# Build dist/Salvage: PyInstaller one-dir build + bundled photorec_win.exe /
# Sleuth Kit CLI tools, mirroring packaging/build_mac.sh for Windows.
#
# Assumes photorec_win.exe and the Sleuth Kit exes (fls/icat/fsstat/mmls) are
# already resolvable via PATH -- e.g. via the same download-and-add-to-PATH step
# CI's test job uses (see .github/workflows/ci.yml) -- since that's also exactly
# what locate_binary()/locate_binaries() check first at runtime.

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$PyInstaller = ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $PyInstaller)) {
    # No local .venv (e.g. CI, which installs straight into the runner's Python) --
    # fall back to whatever `pyinstaller` is on PATH.
    $cmd = Get-Command pyinstaller -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Error "pyinstaller not found. Install packaging deps first: uv pip install --python .venv\Scripts\python.exe -e `".[dev]`""
        exit 1
    }
    $PyInstaller = $cmd.Source
}

Write-Host "==> Running PyInstaller"
& $PyInstaller --noconfirm --clean packaging\salvage_windows.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$App = "dist\Salvage"
$Exe = "$App\Salvage.exe"
if (-not (Test-Path $Exe)) { throw "$Exe was not produced by PyInstaller" }

Write-Host "==> Locating the frozen build's MEIPASS (where bundled datas/salvage/bin should live)"
$probeBefore = & $Exe --print-engine 2>&1 | Out-String
Write-Host $probeBefore
if ($probeBefore -notmatch "MEIPASS=(?<p>.+)") { throw "could not parse MEIPASS from: $probeBefore" }
$meipass = $Matches["p"].Trim()

$BinDest = Join-Path $meipass "salvage\bin\windows"
New-Item -ItemType Directory -Force -Path $BinDest | Out-Null

Write-Host "==> Bundling photorec_win.exe + Sleuth Kit tools into $BinDest"
$photorecCmd = Get-Command photorec_win.exe -ErrorAction SilentlyContinue
if (-not $photorecCmd) { throw "photorec_win.exe not found on PATH -- install it before running this script" }
$photorecDir = Split-Path $photorecCmd.Source -Parent
Copy-Item "$photorecDir\*" -Destination $BinDest -Recurse -Force

$flsCmd = Get-Command fls.exe -ErrorAction SilentlyContinue
if (-not $flsCmd) { throw "fls.exe (Sleuth Kit) not found on PATH -- install it before running this script" }
$sleuthkitDir = Split-Path $flsCmd.Source -Parent
Copy-Item "$sleuthkitDir\*" -Destination $BinDest -Recurse -Force

Write-Host "==> Verifying the frozen build finds its BUNDLED tools, not PATH (PATH deliberately restricted for this check)"
$originalPath = $env:PATH
$probeAfter = $null
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $probeAfter = & $Exe --print-engine 2>&1 | Out-String
} finally {
    $env:PATH = $originalPath
}
Write-Host $probeAfter

if ($probeAfter -match "photorec=None" -or $probeAfter -notmatch "photorec_win\.exe") {
    throw "frozen build did not resolve a bundled photorec_win.exe (PATH was restricted for this check):`n$probeAfter"
}
if ($probeAfter -match "sleuthkit=None" -or $probeAfter -notmatch "fls\.exe") {
    throw "frozen build did not resolve the bundled Sleuth Kit tools (PATH was restricted for this check):`n$probeAfter"
}

Write-Host "==> Bundle size"
$size = (Get-ChildItem $App -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("{0:N1} MB" -f $size)
