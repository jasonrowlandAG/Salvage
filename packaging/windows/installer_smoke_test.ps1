# Install -> use -> uninstall smoke test for Salvage-Setup-<version>.exe.
#
# Run from the repo root on a Windows machine with an elevated shell (CI runners are;
# the installer itself requests admin). Every check is recorded and the script exits
# non-zero at the end if any failed, so one run reports every defect rather than the
# first. Phases print a "== PHASE" header and "PASS"/"FAIL" lines for diagnosis from
# the log alone.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Version = (Select-String -Path (Join-Path $RepoRoot "pyproject.toml") -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$Installer = Join-Path $PSScriptRoot "Salvage-Setup-$Version.exe"
$DistDir = Join-Path $RepoRoot "dist\Salvage"
$InstallDir = Join-Path $env:ProgramFiles "Salvage"
$Uninstaller = Join-Path $InstallDir "Uninstall.exe"
$UninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Salvage"
$AppKey = "HKLM:\Software\Salvage"
$StartMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Salvage"

$script:Failures = [System.Collections.Generic.List[string]]::new()

function Phase($name) { Write-Host ""; Write-Host "== PHASE: $name" }
function Check($ok, $what) {
    if ($ok) { Write-Host "  PASS  $what" }
    else { Write-Host "  FAIL  $what"; $script:Failures.Add($what) }
}
function Wait-Until($timeoutSec, [scriptblock]$cond) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $cond) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return [bool](& $cond)
}
function Get-Reg($path) { Get-ItemProperty -Path $path -ErrorAction SilentlyContinue }
function Get-ShortcutTarget($lnk) {
    if (-not (Test-Path $lnk)) { return $null }
    (New-Object -ComObject WScript.Shell).CreateShortcut($lnk).TargetPath
}

# ---- Phase 0: preconditions -------------------------------------------------
Phase "preconditions"
Write-Host "  version=$Version installer=$Installer"
if (-not (Test-Path $Installer)) { throw "installer not found: $Installer" }
Check (-not (Test-Path $InstallDir)) "no pre-existing install at $InstallDir"
Check (-not (Test-Path $UninstallKey)) "no pre-existing uninstall registry key"
if ($script:Failures.Count -gt 0) { throw "runner is not clean; refusing to continue" }

# ---- Phase 1: silent install ------------------------------------------------
Phase "silent install (/S)"
# NSIS installers relaunch/detach in places, so Start-Process -Wait is not proof of
# completion. Wait for the process, then poll for the last thing the Install section
# writes (the NoRepair registry value) with a generous timeout.
$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Process -FilePath $Installer -ArgumentList "/S" -PassThru
$exited = $p.WaitForExit(300000)
Check $exited "installer process exited within 300s"
$done = Wait-Until 120 { (Get-Reg $UninstallKey).NoRepair -eq 1 }
Check $done "install completed (final registry value written) in $([int]$sw.Elapsed.TotalSeconds)s"
if ($exited) { Write-Host "  installer exit code: $($p.ExitCode)"; Check ($p.ExitCode -eq 0) "installer exit code is 0" }
if (-not $done) { Write-Host "FATAL: install did not complete; skipping to summary"; $script:Failures | ForEach-Object { Write-Host "  - $_" }; exit 1 }

# ---- Phase 2: verify install ------------------------------------------------
Phase "verify installed state"
Check (Test-Path $InstallDir) "install dir exists: $InstallDir"
foreach ($rel in @("Salvage.exe", "Uninstall.exe",
                   "_internal\salvage\bin\windows\photorec_win.exe",
                   "_internal\salvage\bin\windows\fls.exe",
                   "_internal\salvage\bin\windows\icat.exe")) {
    Check (Test-Path (Join-Path $InstallDir $rel)) "file present: $rel"
}
$srcCount = (Get-ChildItem $DistDir -Recurse -File).Count
$dstCount = (Get-ChildItem $InstallDir -Recurse -File | Where-Object { $_.Name -ne "Uninstall.exe" }).Count
Check ($srcCount -eq $dstCount) "installed file count matches build ($dstCount installed vs $srcCount in dist)"

$reg = Get-Reg $UninstallKey
Check ($null -ne $reg) "uninstall registry key exists (64-bit view): $UninstallKey"
if ($reg) {
    Check ($reg.DisplayName -eq "Salvage") "DisplayName == 'Salvage' (got '$($reg.DisplayName)')"
    Check ($reg.DisplayVersion -eq $Version) "DisplayVersion == '$Version' (got '$($reg.DisplayVersion)')"
    Check ($reg.Publisher -eq "Assembly Growth") "Publisher == 'Assembly Growth' (got '$($reg.Publisher)')"
    Check ($reg.UninstallString -eq "`"$Uninstaller`"") "UninstallString is the quoted uninstaller path (got '$($reg.UninstallString)')"
    Check ($reg.InstallLocation -eq $InstallDir) "InstallLocation == $InstallDir (got '$($reg.InstallLocation)')"
}
Check ((Get-Reg $AppKey).InstallDir -eq $InstallDir) "HKLM\Software\Salvage InstallDir == $InstallDir"

$appLnk = Join-Path $StartMenuDir "Salvage.lnk"
$unLnk = Join-Path $StartMenuDir "Uninstall Salvage.lnk"
Check ((Get-ShortcutTarget $appLnk) -eq (Join-Path $InstallDir "Salvage.exe")) "Start Menu shortcut Salvage.lnk -> installed Salvage.exe (got '$(Get-ShortcutTarget $appLnk)')"
Check ((Get-ShortcutTarget $unLnk) -eq $Uninstaller) "Start Menu shortcut 'Uninstall Salvage.lnk' -> Uninstall.exe (got '$(Get-ShortcutTarget $unLnk)')"

# ---- Phase 3: launch the INSTALLED app -------------------------------------
Phase "run installed Salvage.exe --print-engine (PATH restricted)"
$exe = Join-Path $InstallDir "Salvage.exe"
$originalPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    # Piping makes PowerShell wait for this GUI-subsystem exe and capture its stdout.
    $out = & $exe --print-engine 2>&1 | Out-String
} finally { $env:PATH = $originalPath }
Write-Host ($out -replace "(?m)^", "    | ")
# --print-engine prints photorec as a plain string (backslashes) but the Sleuth Kit
# tools as WindowsPath('C:/...') with forward slashes, so normalise both the output
# and the expected path to one separator before matching.
$outNorm = $out.Replace('/', '\')
$escaped = [regex]::Escape($InstallDir)
Check ($out -match "frozen=True") "app reports frozen=True"
Check ($out -match "MEIPASS=$escaped") "MEIPASS is inside the install dir"
Check ($out -match "photorec=$escaped.*photorec_win\.exe") "photorec resolves inside the install dir"
Check ($outNorm -match "sleuthkit=.*$escaped.*fls\.exe") "Sleuth Kit resolves inside the install dir"
Check (($out -notmatch "unavailable") -or ($out -notmatch "photorec=<unavailable")) "engine binaries are available"
Check ($out -notmatch [regex]::Escape($RepoRoot)) "no engine path points into the build workspace"
Check ($out -notmatch "runneradmin\\.*tools|RUNNER_TEMP|\\tools\\") "no engine path points at the CI tool download dir"

# ---- Phase 3b: launch the INSTALLED GUI -------------------------------------
# --print-engine proves the tools resolve, but not that the desktop app actually opens.
# Start the real GUI in fake mode (no devices or admin needed), confirm it stays up and
# shows a top-level window, and keep a screenshot so the Windows UI can be eyeballed.
Phase "launch installed Salvage.exe GUI (fake mode)"
$shot = Join-Path $PWD "salvage-windows-gui.png"
$env:SALVAGE_FAKE = "1"
$gui = Start-Process -FilePath $exe -PassThru
Remove-Item Env:\SALVAGE_FAKE -ErrorAction SilentlyContinue
$up = Wait-Until 40 { $gui.Refresh(); (-not $gui.HasExited) -and $gui.MainWindowHandle -ne 0 }
Check $up "GUI process is running and owns a top-level window"
Check (-not $gui.HasExited) "GUI did not crash on startup (alive after launch)"
$gui.Refresh()
Write-Host "    window title: '$($gui.MainWindowTitle)'"
Check ($gui.MainWindowTitle -like "*Salvage*") "window title mentions Salvage"
Start-Sleep -Seconds 3   # let the first paint land before the screenshot
try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
    $bmp.Save($shot, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Host "    screenshot saved: $shot ($($b.Width)x$($b.Height))"
} catch { Write-Host "    (screenshot unavailable: $($_.Exception.Message))" }
if (-not $gui.HasExited) { Stop-Process -Id $gui.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# ---- Phase 4: silent uninstall ---------------------------------------------
Phase "silent uninstall (/S)"
# The NSIS uninstaller copies itself to %TEMP% and relaunches, so the process we start
# exits almost immediately. Poll for the actual end state instead.
$p = Start-Process -FilePath $Uninstaller -ArgumentList "/S" -PassThru
$null = $p.WaitForExit(120000)
$gone = Wait-Until 120 { (-not (Test-Path $InstallDir)) -and (-not (Test-Path $UninstallKey)) }
Check $gone "uninstall finished (install dir and registry key gone)"

Phase "verify clean uninstall"
Check (-not (Test-Path $InstallDir)) "install dir removed: $InstallDir"
if (Test-Path $InstallDir) {
    Get-ChildItem $InstallDir -Recurse -Force | Select-Object -First 20 | ForEach-Object { Write-Host "    leftover: $($_.FullName)" }
}
Check (-not (Test-Path $UninstallKey)) "uninstall registry key removed"
Check (-not (Test-Path $AppKey)) "HKLM\Software\Salvage removed"
Check (-not (Test-Path $StartMenuDir)) "Start Menu folder removed"
Check (-not (Test-Path (Join-Path $env:Public "Desktop\Salvage.lnk"))) "no desktop shortcut left (all-users)"
Check (-not (Test-Path (Join-Path ([Environment]::GetFolderPath("Desktop")) "Salvage.lnk"))) "no desktop shortcut left (current user)"
foreach ($stray in @("HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Salvage", "HKLM:\Software\WOW6432Node\Salvage")) {
    Check (-not (Test-Path $stray)) "no stray 32-bit-view registry key: $stray"
}

# ---- Summary ---------------------------------------------------------------
Phase "summary"
if ($script:Failures.Count -gt 0) {
    Write-Host "SMOKE TEST FAILED: $($script:Failures.Count) check(s)"
    $script:Failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
Write-Host "SMOKE TEST PASSED: install, launch, uninstall all verified"
