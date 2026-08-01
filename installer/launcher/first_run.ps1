# NaviFix — First-Run Setup
# Generates SECRET_KEY and ENCRYPTION_KEY, writes them into backend\.env.
# Run automatically by start.ps1 on the very first launch.
# Do NOT re-run after servers have been added to the database.

$AppName    = "NaviFix"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$EnvFile    = Join-Path $InstallDir "backend\.env"
$EnvExample = Join-Path $InstallDir "backend\.env.example"

Write-Host ""
Write-Host "  [$AppName] First-time setup" -ForegroundColor Cyan
Write-Host ""

# ── 1. Create .env from example if it doesn't exist ──────────────────────────
if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "  ✓ Created backend\.env from template" -ForegroundColor Green
} else {
    Write-Host "  → backend\.env already exists, updating keys in place" -ForegroundColor Yellow
}

# ── 2. Generate keys using Python ────────────────────────────────────────────
Write-Host "  Generating encryption keys…"

$pythonScript = @'
import secrets, sys
from cryptography.fernet import Fernet

secret_key    = secrets.token_hex(32)
fernet_key    = Fernet.generate_key().decode()

if len(fernet_key) != 44:
    print(f"ERROR: Fernet key length is {len(fernet_key)}, expected 44", file=sys.stderr)
    sys.exit(1)

print(f"SECRET_KEY={secret_key}")
print(f"ENCRYPTION_KEY={fernet_key}")
'@

$keyOutput = python -c $pythonScript 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Key generation failed: $keyOutput" -ForegroundColor Red
    exit 1
}

$secretKey    = ($keyOutput | Select-String "SECRET_KEY=").ToString().Replace("SECRET_KEY=", "")
$encryptionKey = ($keyOutput | Select-String "ENCRYPTION_KEY=").ToString().Replace("ENCRYPTION_KEY=", "")

Write-Host "  ✓ Generated SECRET_KEY:    $($secretKey.Substring(0,16))…" -ForegroundColor Green
Write-Host "  ✓ Generated ENCRYPTION_KEY: $($encryptionKey.Substring(0,16))…" -ForegroundColor Green

# ── 3. Write keys into .env using Python (cross-platform safe) ───────────────
$updateScript = @"
import re

with open(r'$EnvFile', 'r') as f:
    content = f.read()

content = re.sub(r'^SECRET_KEY=.*$',    'SECRET_KEY=$secretKey',       content, flags=re.MULTILINE)
content = re.sub(r'^ENCRYPTION_KEY=.*$','ENCRYPTION_KEY=$encryptionKey', content, flags=re.MULTILINE)

with open(r'$EnvFile', 'w') as f:
    f.write(content)

print('Keys written to .env')
"@

python -c $updateScript

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: Failed to write keys to .env" -ForegroundColor Red
    exit 1
}

Write-Host "  ✓ Keys written to backend\.env" -ForegroundColor Green

# ── 4. Pull Docker images ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "  Pulling Docker images (this may take a few minutes on first run)…" -ForegroundColor Cyan

$ComposeFile = Join-Path $InstallDir "docker-compose.yml"
docker compose -f $ComposeFile pull

if ($LASTEXITCODE -ne 0) {
    Write-Host "  Warning: docker compose pull encountered errors. Will try to start anyway." -ForegroundColor Yellow
} else {
    Write-Host "  ✓ Docker images ready." -ForegroundColor Green
}

Write-Host ""
Write-Host "  [$AppName] First-time setup complete!" -ForegroundColor Green
Write-Host ""
