# NaviFix — Dev Team Build Script
# ============================================================================
# Runs on a Windows 11 machine with Python 3.11+, PyInstaller, and Inno Setup.
#
# Usage:
#   .\installer\build.ps1
#   .\installer\build.ps1 -Version "1.2.0"
#   .\installer\build.ps1 -SkipTray     # skip tray build (faster iteration)
#
# Output:
#   installer\dist\NaviFix-<version>-setup.exe
# ============================================================================

param(
    [string]$Version   = "1.0.0",
    [switch]$SkipApp   = $false,
    [switch]$SkipTray  = $false,
    [switch]$SkipInno  = $false
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $ScriptDir
$BuildDir   = Join-Path $ScriptDir "build"
$DistDir    = Join-Path $ScriptDir "dist"

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Blue
    Write-Host "  $Msg" -ForegroundColor Cyan
    Write-Host "══════════════════════════════════════════════" -ForegroundColor Blue
}

function Assert-Command([string]$Cmd, [string]$InstallHint) {
    if (-not (Get-Command $Cmd -ErrorAction SilentlyContinue)) {
        Write-Host "  ERROR: '$Cmd' not found. $InstallHint" -ForegroundColor Red
        exit 1
    }
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────
Write-Step "Pre-flight checks"

Assert-Command "python"     "Install Python 3.11+ from https://python.org"
Assert-Command "iscc"       "Install Inno Setup 6+ from https://jrsoftware.org/isinfo.php and add to PATH"

Write-Host "  ✓ Python:      $(python --version)"      -ForegroundColor Green

# ── Update version in setup.iss ───────────────────────────────────────────────
Write-Step "Setting version to $Version"

$IssFile = Join-Path $ScriptDir "setup.iss"
(Get-Content $IssFile) -replace '#define AppVersion\s+"[^"]+"', "#define AppVersion  `"$Version`"" |
    Set-Content $IssFile

# Also update version in tray/updater.py and app/main.py
(Get-Content (Join-Path $ScriptDir "app\main.py"))  -replace 'APP_VERSION\s+=\s+"[^"]+"', "APP_VERSION    = `"$Version`"" |
    Set-Content (Join-Path $ScriptDir "app\main.py")

(Get-Content (Join-Path $ScriptDir "tray\tray_app.py")) -replace 'APP_VERSION\s+=\s+"[^"]+"', "APP_VERSION   = `"$Version`"" |
    Set-Content (Join-Path $ScriptDir "tray\tray_app.py")

Write-Host "  ✓ Version set to $Version" -ForegroundColor Green

# ── Install Python deps ───────────────────────────────────────────────────────
Write-Step "Installing Python dependencies"

python -m pip install -r (Join-Path $ScriptDir "app\requirements.txt")  -q
if ($LASTEXITCODE -ne 0) { throw "pip install failed for app requirements.txt" }

python -m pip install -r (Join-Path $ScriptDir "tray\requirements.txt") -q
if ($LASTEXITCODE -ne 0) { throw "pip install failed for tray requirements.txt" }

python -m pip install pyinstaller Pillow -q
if ($LASTEXITCODE -ne 0) { throw "pip install failed for build tools" }

Write-Host "  ✓ Dependencies installed" -ForegroundColor Green

# ── Prepare assets (icon.ico, wizard bitmaps) ─────────────────────────────────
Write-Step "Preparing installer assets"

python (Join-Path $ScriptDir "prepare_assets.py")

Write-Host "  ✓ Assets ready" -ForegroundColor Green

# ── Build NaviFixApp.exe (PyWebView native window) ────────────────────────────
if (-not $SkipApp) {
    Write-Step "Building NaviFixApp.exe"

    python -m PyInstaller `
        --name "NaviFixApp" `
        --onedir `
        --windowed `
        --icon (Join-Path $ScriptDir "assets\icon.ico") `
        --add-data "$ScriptDir\app\splash.html;." `
        --add-data "$ScriptDir\assets\icon.ico;assets" `
        --distpath (Join-Path $BuildDir "NaviFixApp_dist") `
        --workpath (Join-Path $BuildDir "NaviFixApp_work") `
        --specpath (Join-Path $BuildDir "specs") `
        --noconfirm `
        (Join-Path $ScriptDir "app\main.py")

    # Move output to build\NaviFixApp\
    $appOut = Join-Path $BuildDir "NaviFixApp"
    if (Test-Path $appOut) { Remove-Item $appOut -Recurse -Force }
    Move-Item (Join-Path $BuildDir "NaviFixApp_dist\NaviFixApp") $appOut

    Write-Host "  ✓ NaviFixApp.exe built → $appOut" -ForegroundColor Green
}

# ── Build NaviFixTray.exe (system tray manager) ───────────────────────────────
if (-not $SkipTray) {
    Write-Step "Building NaviFixTray.exe"

    python -m PyInstaller `
        --name "NaviFixTray" `
        --onedir `
        --windowed `
        --icon (Join-Path $ScriptDir "assets\icon.ico") `
        --add-data "$ScriptDir\assets\icon.ico;assets" `
        --hidden-import "pystray._win32" `
        --hidden-import "winotify" `
        --distpath (Join-Path $BuildDir "NaviFixTray_dist") `
        --workpath (Join-Path $BuildDir "NaviFixTray_work") `
        --specpath (Join-Path $BuildDir "specs") `
        --noconfirm `
        (Join-Path $ScriptDir "tray\tray_app.py")

    # Move output to build\NaviFixTray\
    $trayOut = Join-Path $BuildDir "NaviFixTray"
    if (Test-Path $trayOut) { Remove-Item $trayOut -Recurse -Force }
    Move-Item (Join-Path $BuildDir "NaviFixTray_dist\NaviFixTray") $trayOut

    Write-Host "  ✓ NaviFixTray.exe built → $trayOut" -ForegroundColor Green
}

# ── Run Inno Setup ────────────────────────────────────────────────────────────
if (-not $SkipInno) {
    Write-Step "Building installer with Inno Setup"

    if (-not (Test-Path $DistDir)) { New-Item -ItemType Directory -Path $DistDir | Out-Null }

    iscc (Join-Path $ScriptDir "setup.iss") /O"$DistDir"

    $installer = Join-Path $DistDir "NaviFix-$Version-setup.exe"
    if (Test-Path $installer) {
        $size = [math]::Round((Get-Item $installer).Length / 1MB, 1)
        Write-Host ""
        Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Green
        Write-Host "  ║  BUILD SUCCESSFUL                        ║" -ForegroundColor Green
        Write-Host "  ║  NaviFix-$Version-setup.exe ($size MB)" -ForegroundColor Green
        Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Green
        Write-Host ""
        Write-Host "  Output: $installer" -ForegroundColor Cyan
    } else {
        Write-Host "  ERROR: Installer not found at $installer" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "  Done! Distribute: installer\dist\NaviFix-$Version-setup.exe" -ForegroundColor Green
