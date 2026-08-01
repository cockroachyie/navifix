# NaviFix — Stop Services
# Gracefully stops all Docker containers.
# Called by the system tray "Stop" action and the uninstaller.

$AppName    = "NaviFix"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$ComposeFile = Join-Path $InstallDir "docker-compose.yml"

Write-Host "  [$AppName] Stopping services…" -ForegroundColor Yellow

docker compose -f $ComposeFile down

if ($LASTEXITCODE -eq 0) {
    Write-Host "  [$AppName] All services stopped." -ForegroundColor Green
} else {
    Write-Host "  [$AppName] Warning: docker compose down exited with code $LASTEXITCODE" -ForegroundColor Red
}
