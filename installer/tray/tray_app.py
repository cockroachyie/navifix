"""
installer/tray/tray_app.py
===========================
NaviFix — System tray manager.

Runs as a background process (NaviFixTray.exe) and provides:
  - A system tray icon with colour-coded status (green / red / yellow)
  - Right-click context menu: Open, Start, Stop, Restart, Check for Updates, Exit
  - Health polling every 30 seconds
  - Windows toast notifications on status changes
  - Delegates update checking to updater.py
"""

import os
import sys
import subprocess
import threading
import time
import ctypes

import pystray
from PIL import Image, ImageDraw
import httpx

from updater import UpdateChecker

# ── Configuration ──────────────────────────────────────────────────────────────
APP_NAME       = "NaviFix"
APP_VERSION    = "1.0.0"
HEALTH_URL     = "http://localhost:5000/api/health"
POLL_INTERVAL  = 30    # seconds between health checks

# Resolve install paths (works for both .py and compiled .exe)
BASE_DIR     = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
INSTALL_DIR  = os.path.dirname(BASE_DIR)           # one level up from tray/
APP_EXE      = os.path.join(INSTALL_DIR, "NaviFixApp.exe")
COMPOSE_FILE = os.path.join(INSTALL_DIR, "docker-compose.yml")
ICON_PATH    = os.path.join(BASE_DIR, "assets", "icon.ico")


# ── Tray icon drawing ──────────────────────────────────────────────────────────

def _make_icon(status: str) -> Image.Image:
    """
    Draw a simple circular icon in memory.
    status: 'running' (green), 'stopped' (red), 'starting' (yellow)
    """
    colours = {"running": "#22c55e", "stopped": "#ef4444", "starting": "#f59e0b"}
    colour = colours.get(status, "#64748b")

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer circle (dark border)
    draw.ellipse([2, 2, size - 2, size - 2], fill="#1e293b")
    # Inner status circle
    pad = 10
    draw.ellipse([pad, pad, size - pad, size - pad], fill=colour)

    return img


# ── Windows toast notifications ────────────────────────────────────────────────

def _toast(title: str, message: str) -> None:
    """Send a Windows toast notification (best-effort)."""
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id   = APP_NAME,
            title    = title,
            msg      = message,
            duration = "short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass   # winotify may not be available in all envs


# ── Docker compose helpers ─────────────────────────────────────────────────────

def _run_compose(args: list[str]) -> subprocess.CompletedProcess:
    """Run a docker compose command from the install directory."""
    return subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE] + args,
        capture_output=True,
        text=True,
        cwd=INSTALL_DIR,
    )


def _is_backend_healthy() -> bool:
    try:
        r = httpx.get(HEALTH_URL, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ── Tray application ───────────────────────────────────────────────────────────

class NaviFixTray:
    def __init__(self):
        self._status     = "starting"   # 'running' | 'stopped' | 'starting'
        self._tray       = None
        self._app_proc   = None         # NaviFixApp.exe subprocess

    # ── Menu actions ────────────────────────────────────────────────────

    def _open_app(self, icon=None, item=None):
        """Show / focus the NaviFix native window."""
        if self._app_proc and self._app_proc.poll() is None:
            # Already running — bring to foreground via Windows API
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, "NaviFix")
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        else:
            # Start fresh
            self._launch_app_window()

    def _start(self, icon=None, item=None):
        """Start Docker containers and open the app window."""
        _toast(APP_NAME, "Starting NaviFix services…")
        self._status = "starting"
        self._update_icon()
        threading.Thread(target=self._do_start, daemon=True).start()

    def _do_start(self):
        result = _run_compose(["up", "-d"])
        if result.returncode == 0:
            # Wait for health before launching window
            for _ in range(60):
                if _is_backend_healthy():
                    self._status = "running"
                    self._update_icon()
                    self._launch_app_window()
                    _toast(APP_NAME, "NaviFix is running.")
                    return
                time.sleep(2)
            _toast(APP_NAME, "Services started but health check timed out.")
        else:
            self._status = "stopped"
            self._update_icon()
            _toast(APP_NAME, "Failed to start services. Check Docker Desktop.")

    def _stop(self, icon=None, item=None):
        """Stop Docker containers."""
        _toast(APP_NAME, "Stopping NaviFix services…")
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self):
        _run_compose(["down"])
        self._status = "stopped"
        self._update_icon()
        _toast(APP_NAME, "NaviFix stopped.")

    def _restart(self, icon=None, item=None):
        threading.Thread(target=self._do_restart, daemon=True).start()

    def _do_restart(self):
        self._do_stop()
        time.sleep(2)
        self._do_start()

    def _check_updates(self, icon=None, item=None):
        threading.Thread(
            target=UpdateChecker(APP_VERSION).check_now,
            daemon=True,
        ).start()

    def _exit(self, icon=None, item=None):
        """Quit the tray app (containers keep running)."""
        if self._tray:
            self._tray.stop()

    # ── Internal helpers ─────────────────────────────────────────────────

    def _launch_app_window(self):
        """Launch NaviFixApp.exe as a subprocess."""
        if os.path.exists(APP_EXE):
            self._app_proc = subprocess.Popen([APP_EXE])
        else:
            # Dev mode — run via Python
            py = sys.executable
            main_py = os.path.join(INSTALL_DIR, "app", "main.py")
            self._app_proc = subprocess.Popen([py, main_py])

    def _update_icon(self):
        if self._tray:
            self._tray.icon  = _make_icon(self._status)
            self._tray.title = f"{APP_NAME} — {self._status.capitalize()}"

    def _status_label(self, item=None):
        labels = {"running": "● Running", "stopped": "○ Stopped", "starting": "◌ Starting…"}
        return labels.get(self._status, APP_NAME)

    # ── Health poll loop ─────────────────────────────────────────────────

    def _poll_loop(self):
        while True:
            healthy = _is_backend_healthy()
            new_status = "running" if healthy else "stopped"
            if new_status != self._status and self._status != "starting":
                self._status = new_status
                self._update_icon()
                if new_status == "stopped":
                    _toast(APP_NAME, "NaviFix services stopped unexpectedly.")
                else:
                    _toast(APP_NAME, "NaviFix services are back online.")
            elif self._status == "starting" and healthy:
                self._status = "running"
                self._update_icon()
            time.sleep(POLL_INTERVAL)

    # ── Run ──────────────────────────────────────────────────────────────

    def run(self):
        icon_img = _make_icon("starting")

        menu = pystray.Menu(
            pystray.MenuItem(self._status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open NaviFix",        self._open_app, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start",               self._start),
            pystray.MenuItem("Stop",                self._stop),
            pystray.MenuItem("Restart",             self._restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check for Updates…",  self._check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit",                self._exit),
        )

        self._tray = pystray.Icon(
            name  = APP_NAME,
            icon  = icon_img,
            title = f"{APP_NAME} — Starting…",
            menu  = menu,
        )

        # Start background threads
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._do_start,  daemon=True).start()

        # Start update checker (hourly)
        UpdateChecker(APP_VERSION).start_background()

        # Run tray (blocks until _exit() is called)
        self._tray.run()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    NaviFixTray().run()
