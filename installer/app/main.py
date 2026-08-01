"""
installer/app/main.py
======================
NaviFix — Native desktop window application.

Uses PyWebView to render the Flask web UI inside a native Windows
WebView2 window (Edge engine, pre-installed on all Windows 11 machines).

Behaviour:
- Shows a splash screen while the backend Docker containers start up.
- Once /api/health responds OK, loads the full NaviFix UI.
- Closing the window minimises to the system tray (containers keep running).
- Reopening from tray restores the window.
"""

import os
import sys
import threading
import time
import subprocess
import ctypes

import webview
import httpx

# ── Configuration ──────────────────────────────────────────────────────────────
APP_NAME        = "NaviFix"
APP_VERSION     = "1.0.0"
BACKEND_URL     = "http://localhost:5000"
HEALTH_URL      = f"{BACKEND_URL}/api/health"
HEALTH_TIMEOUT  = 120   # seconds to wait for backend before showing error
HEALTH_INTERVAL = 2     # seconds between health check polls
WINDOW_WIDTH    = 1400
WINDOW_HEIGHT   = 860
MIN_WIDTH       = 900
MIN_HEIGHT      = 600

# Path to this script's directory (works for both .py and PyInstaller .exe)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
SPLASH_PATH = os.path.join(BASE_DIR, "splash.html")
ICON_PATH   = os.path.join(BASE_DIR, "assets", "icon.ico")


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_backend_ready() -> bool:
    """Return True if the Flask backend responds to /api/health."""
    try:
        r = httpx.get(HEALTH_URL, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def wait_for_backend(window: webview.Window) -> None:
    """
    Poll /api/health until the backend is ready, then load the main UI.
    Updates the splash screen progress text via JavaScript.
    Runs in a background thread so the GUI stays responsive.
    """
    elapsed = 0
    while elapsed < HEALTH_TIMEOUT:
        if is_backend_ready():
            # Backend is ready — navigate to the main app
            window.load_url(BACKEND_URL)
            return
        # Update splash screen status message
        msg = f"Starting NaviFix services… ({elapsed}s)"
        try:
            window.evaluate_js(f"updateStatus({elapsed!r}, {msg!r})")
        except Exception:
            pass
        time.sleep(HEALTH_INTERVAL)
        elapsed += HEALTH_INTERVAL

    # Timeout — show error in splash
    try:
        window.evaluate_js(
            "showError('Backend did not start in time. Please restart NaviFix.')"
        )
    except Exception:
        pass


# ── Window close → minimise to tray ───────────────────────────────────────────

class NaviFixApp:
    def __init__(self):
        self.window: webview.Window | None = None
        self._closing = False

    def on_closing(self) -> bool:
        """
        Intercept the close button.
        Instead of quitting, minimise the window to the system tray.
        The tray app (NaviFixTray.exe) manages the actual lifecycle.
        Returns False to prevent webview from destroying the window.
        """
        if self._closing:
            return True   # allow real quit when tray sends exit signal
        if self.window:
            self.window.minimize()
        return False      # cancel default close → window stays alive minimised

    def show(self):
        """Create and display the native window."""
        splash_url = f"file:///{SPLASH_PATH.replace(os.sep, '/')}"

        self.window = webview.create_window(
            title        = APP_NAME,
            url          = splash_url,
            width        = WINDOW_WIDTH,
            height       = WINDOW_HEIGHT,
            min_size     = (MIN_WIDTH, MIN_HEIGHT),
            resizable    = True,
            text_select  = False,
            confirm_close= False,
        )

        # Attach close handler
        self.window.closing += self.on_closing

        # Start backend health watcher in background
        t = threading.Thread(
            target=wait_for_backend,
            args=(self.window,),
            daemon=True,
        )
        t.start()

        # Start PyWebView event loop (blocks until window is destroyed)
        webview.start(
            gui      = "edgechromium",   # use WebView2 (Edge) — best on Windows 11
            debug    = os.environ.get("NAVIFIX_DEBUG", "0") == "1",
            icon     = ICON_PATH if os.path.exists(ICON_PATH) else None,
        )

    def quit(self):
        """Called by the tray app when the user chooses Exit."""
        self._closing = True
        if self.window:
            self.window.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    # Hide the console window on Windows (we're a GUI app)
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0
            )
        except Exception:
            pass

    app = NaviFixApp()
    app.show()


if __name__ == "__main__":
    main()
