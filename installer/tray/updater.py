"""
installer/tray/updater.py
==========================
NaviFix — Automatic update checker.

Polls a hosted version.json every hour. When a newer version is found,
shows a Windows toast notification with an "Update Now" action.

version.json schema (hosted at UPDATE_CHECK_URL):
  {
    "version":       "1.1.0",
    "release_notes": "Bug fixes and performance improvements",
    "download_url":  "https://example.com/releases/NaviFix-1.1.0-setup.exe",
    "mandatory":     false
  }
"""

import os
import sys
import subprocess
import tempfile
import threading
import time
import logging

import httpx
from packaging.version import Version

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
# Update this URL to wherever you host your version.json
UPDATE_CHECK_URL  = "https://your-org.github.io/navifix/version.json"
CHECK_INTERVAL    = 3600   # seconds (1 hour)
REQUEST_TIMEOUT   = 10     # seconds


class UpdateChecker:
    def __init__(self, current_version: str):
        self.current = current_version

    # ── Public API ───────────────────────────────────────────────────────

    def start_background(self) -> None:
        """Start the hourly update check loop in a daemon thread."""
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def check_now(self) -> None:
        """Perform an immediate one-off check (called from tray menu)."""
        self._check()

    # ── Internal ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            time.sleep(CHECK_INTERVAL)
            self._check()

    def _check(self) -> None:
        try:
            resp = httpx.get(UPDATE_CHECK_URL, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("Update check failed: %s", exc)
            return

        latest_ver    = data.get("version", "")
        release_notes = data.get("release_notes", "")
        download_url  = data.get("download_url", "")
        mandatory     = data.get("mandatory", False)

        if not latest_ver or not download_url:
            return

        try:
            if Version(latest_ver) <= Version(self.current):
                logger.debug("NaviFix is up to date (%s).", self.current)
                return
        except Exception:
            return

        logger.info("New version available: %s (current: %s)", latest_ver, self.current)
        self._notify(latest_ver, release_notes, download_url, mandatory)

    def _notify(self, version: str, notes: str, url: str, mandatory: bool) -> None:
        """Show a Windows toast notification about the available update."""
        title = f"NaviFix {version} is available"
        body  = notes if notes else "A new version of NaviFix is ready to install."
        if mandatory:
            body += "\n⚠️ This update is required."

        try:
            from winotify import Notification, audio
            toast = Notification(
                app_id   = "NaviFix",
                title    = title,
                msg      = body,
                duration = "long",
                launch   = url,   # clicking the toast opens the download URL
            )
            toast.add_actions(label="Update Now", launch=url)
            toast.set_audio(audio.Default, loop=False)
            toast.show()
        except Exception:
            # Fallback: open the download URL directly in the browser
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass

    def download_and_install(self, download_url: str) -> None:
        """
        Download the new installer to a temp directory and run it silently.
        The /SILENT flag means Inno Setup installs without showing any UI.
        """
        try:
            tmp_dir  = tempfile.mkdtemp(prefix="navifix_update_")
            filename = download_url.split("/")[-1]
            dest     = os.path.join(tmp_dir, filename)

            logger.info("Downloading update: %s → %s", download_url, dest)

            with httpx.stream("GET", download_url, follow_redirects=True, timeout=300) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=8192):
                        f.write(chunk)

            logger.info("Launching installer: %s", dest)
            subprocess.Popen([dest, "/SILENT", "/NORESTART"])

        except Exception as exc:
            logger.error("Update download/install failed: %s", exc)
