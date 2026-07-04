#!/usr/bin/env bash
# setup.sh — First-time setup for Redfish Fleet Monitor
# Run from the backend/ directory: bash setup.sh
set -euo pipefail

echo ""
echo "=========================================="
echo "  Redfish Fleet Monitor — Setup"
echo "=========================================="
echo ""

# Create .env from example if it doesn't already exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✓ Created .env from .env.example"
else
    echo "→ .env already exists — updating keys in-place"
fi

# ── Generate keys ──────────────────────────────────────────────────────────
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
FERNET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Validate the Fernet key length (must be 44 chars)
if [ ${#FERNET} -ne 44 ]; then
    echo "ERROR: Generated Fernet key has unexpected length ${#FERNET} (expected 44)"
    exit 1
fi

echo "  Generated SECRET_KEY:    ${SECRET:0:16}..."
echo "  Generated ENCRYPTION_KEY: ${FERNET:0:16}..."
echo "  Key lengths: SECRET=${#SECRET} FERNET=${#FERNET} (must be 44)"
echo ""

# ── Write keys into .env using Python for reliable substitution ────────────
# Using Python instead of sed avoids the quoting and in-place-edit portability
# issues on macOS vs Linux that caused setup.sh to concatenate keys before.
python3 - <<PYEOF
import re

with open('.env', 'r') as f:
    content = f.read()

# Replace SECRET_KEY line
content = re.sub(
    r'^SECRET_KEY=.*$',
    f'SECRET_KEY=${SECRET}',
    content,
    flags=re.MULTILINE
)

# Replace ENCRYPTION_KEY line
content = re.sub(
    r'^ENCRYPTION_KEY=.*$',
    f'ENCRYPTION_KEY=${FERNET}',
    content,
    flags=re.MULTILINE
)

with open('.env', 'w') as f:
    f.write(content)

print("✓ SECRET_KEY and ENCRYPTION_KEY written to .env")
PYEOF

# ── Verify the written key ─────────────────────────────────────────────────
python3 - <<PYEOF
import re
with open('.env') as f:
    content = f.read()
m = re.search(r'^ENCRYPTION_KEY=(.+)$', content, re.MULTILINE)
if not m:
    print("ERROR: ENCRYPTION_KEY not found in .env after writing")
    exit(1)
written_key = m.group(1).strip()
if len(written_key) != 44:
    print(f"ERROR: ENCRYPTION_KEY in .env has length {len(written_key)}, expected 44")
    print(f"Value: {written_key}")
    exit(1)
print(f"✓ Verified ENCRYPTION_KEY length = {len(written_key)} (correct)")
try:
    from cryptography.fernet import Fernet
    Fernet(written_key.encode())
    print("✓ Verified ENCRYPTION_KEY is a valid Fernet key")
except Exception as e:
    print(f"ERROR: ENCRYPTION_KEY is not a valid Fernet key: {e}")
    exit(1)
PYEOF

echo ""
echo "Setup complete. Next steps:"
echo ""
echo "  1. Edit backend/.env if you need to change DATABASE_URL or other settings"
echo "  2. Start PostgreSQL:  docker compose up -d db"
echo "  3. Run the app:       docker compose up app"
echo "     OR locally:        pip install -r requirements.txt && python app.py"
echo "  4. Open:              http://localhost:5000"
echo ""
echo "IMPORTANT: Every time you re-run setup.sh, a new ENCRYPTION_KEY is generated."
echo "If you already have servers in the database, do NOT re-run setup.sh."
echo "Instead, keep the ENCRYPTION_KEY that is currently in .env."
echo ""
