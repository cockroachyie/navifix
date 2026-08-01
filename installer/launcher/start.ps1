# NaviFix — Start Services
# PowerShell launcher: starts Docker containers and opens the app window.
# Called by the Start Menu shortcut and the system tray "Start" action.

param(
    [switch]$FirstRun   # passed on very first launch after install
)

$ErrorActionPreference = "Stop"
$AppName    = "NaviFix"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$AppExe     = Join-Path $InstallDir "NaviFixApp.exe"
$TrayExe    = Join-Path $InstallDir "NaviFixTray.exe"

# ── Helper: Write coloured status to console ──────────────────────────────────
function Write-Status([string]$Msg, [string]$Color = "Cyan") {
    Write-Host "  [$AppName] $Msg" -ForegroundColor $Color
}

# ── 1. Ensure Docker Desktop is running ───────────────────────────────────────
Write-Status "Checking Docker Desktop…"

$dockerRunning = $false
try {
    $null = docker info 2>&1
    $dockerRunning = ($LASTEXITCODE -eq 0)
} catch {}

if (-not $dockerRunning) {
    Write-Status "Docker Desktop is not running. Starting it…" "Yellow"

    # Find Docker Desktop executable
    $dockerDesktop = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($dockerDesktop) {
        Start-Process $dockerDesktop
        Write-Status "Waiting for Docker to initialise (up to 60s)…" "Yellow"
        $waited = 0
        while ($waited -lt 60) {
            Start-Sleep 3
            $waited += 3
            try {
                $null = docker info 2>&1
                if ($LASTEXITCODE -eq 0) { $dockerRunning = $true; break }
            } catch {}
        }
    }

    if (-not $dockerRunning) {
        $msg = "Docker Desktop did not start in time. Please open Docker Desktop manually and try again."
        Write-Status $msg "Red"
        [System.Windows.Forms.MessageBox]::Show($msg, $AppName, "OK", "Error") | Out-Null
        exit 1
    }
}

Write-Status "Docker is ready." "Green"

# ── 2. First-run setup ────────────────────────────────────────────────────────
$EnvFile = Join-Path $InstallDir "backend\.env"
$FlagFile = Join-Path $InstallDir ".navifix_initialized"

if (-not (Test-Path $FlagFile)) {
    Write-Status "Running first-time setup…" "Yellow"
    & (Join-Path $InstallDir "launcher\first_run.ps1")
    New-Item -ItemType File -Path $FlagFile -Force | Out-Null
}

# ── 3. Start Docker containers ────────────────────────────────────────────────
Write-Status "Starting NaviFix services…"

$composeFile = Join-Path $InstallDir "docker-compose.yml"
docker compose -f $composeFile up -d

if ($LASTEXITCODE -ne 0) {
    Write-Status "docker compose up failed." "Red"
    exit 1
}

Write-Status "Containers started. Waiting for backend to be ready…"

# ── 4. Health check — wait for Flask to respond ───────────────────────────────
$healthUrl = "http://localhost:5000/api/health"
$maxWait   = 120   # seconds
$interval  = 3
$elapsed   = 0
$ready     = $false

while ($elapsed -lt $maxWait) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep $interval
    $elapsed += $interval
    Write-Status "Waiting… ${elapsed}s / ${maxWait}s"
}

if (-not $ready) {
    Write-Status "Backend did not respond in time. Launching app anyway." "Yellow"
}

# ── 5. Launch NaviFixApp.exe (native window) ───────────────────────────────────
Write-Status "Launching NaviFix…" "Green"

if (Test-Path $AppExe) {
    Start-Process $AppExe
} else {
    # Dev mode fallback
    $pythonExe = "python"
    $mainPy = Join-Path $InstallDir "installer\app\main.py"
    Start-Process $pythonExe -ArgumentList $mainPy
}

# ── 6. Ensure tray app is running ─────────────────────────────────────────────
$trayRunning = Get-Process -Name "NaviFixTray" -ErrorAction SilentlyContinue
if (-not $trayRunning) {
    if (Test-Path $TrayExe) {
        Start-Process $TrayExe -WindowStyle Hidden
    }
}

Write-Status "NaviFix started successfully." "Green"
