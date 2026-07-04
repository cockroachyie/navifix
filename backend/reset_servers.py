#!/usr/bin/env python3
"""
reset_servers.py — Wipe all server records from the database.

Run this when ENCRYPTION_KEY changes (e.g. after re-running setup.sh).
After running this, re-add your servers through the UI.

Usage (from backend/ directory):
    python reset_servers.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

# ── Fix: when running locally (not inside Docker), "db" is unreachable.
# Replace the Docker service hostname with localhost so this script works
# from the Mac terminal while PostgreSQL is running in Docker with port 5432
# mapped to the host.
_db_url = os.environ.get("DATABASE_URL", "")
if "@db:" in _db_url:
    _db_url = _db_url.replace("@db:", "@localhost:")
    os.environ["DATABASE_URL"] = _db_url
    print(f"INFO: Running locally — using localhost instead of Docker hostname 'db'")
    print(f"INFO: DATABASE_URL = {_db_url}")
    print()

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from flask import Flask
from database import db
from database.models import Server

# Build a minimal Flask app just for DB access
app = Flask(__name__)
app.config.update(
    SQLALCHEMY_DATABASE_URI=_db_url or "postgresql://redfish:redfish@localhost:5432/redfishmonitor",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)
db.init_app(app)

with app.app_context():
    try:
        count = Server.query.count()
    except Exception as e:
        print(f"ERROR: Cannot connect to PostgreSQL.")
        print(f"       Make sure Docker is running: docker compose up -d db")
        print(f"       Then try again.")
        print(f"\nDetails: {e}")
        sys.exit(1)

    if count == 0:
        print("No servers in database. Nothing to reset.")
        sys.exit(0)

    print(f"Found {count} server(s) in the database:")
    for s in Server.query.all():
        print(f"  - {s.hostname} ({s.ip_address})")

    print()
    answer = input(f"Delete all {count} server(s) and their data? [yes/no]: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        sys.exit(0)

    deleted = Server.query.delete()
    db.session.commit()
    print(f"\n✓ Deleted {deleted} server(s) and all associated data.")
    print("You can now re-add servers through the UI at http://localhost:5000")
