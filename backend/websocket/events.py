"""
websocket/events.py
====================
The browser never talks to Redfish directly (per the architecture
requirement) - it only ever receives pushes over this WebSocket layer,
which the poller (scheduler/poller.py) drives after every successful
collection cycle.

Rooms are used so a browser only receives traffic for servers it's
actually looking at (the currently-selected server in the left sidebar),
plus a global "fleet" room for the sidebar's connection/health indicators
and the alert badge count, which every connected client needs regardless
of which server is open.
"""
import json
import logging
import uuid
from datetime import datetime, date, timedelta

from flask_socketio import join_room, leave_room, emit

logger = logging.getLogger(__name__)

FLEET_ROOM = "fleet"


# ── Safe JSON serializer ──────────────────────────────────────────────────────
# PostgreSQL UUID columns return uuid.UUID objects; datetime columns return
# datetime objects. Neither is JSON-serializable by default — this encoder
# converts them transparently so no caller needs to str() manually.

class _SafeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, timedelta):
            return obj.total_seconds()
        return super().default(obj)


def _safe_dump(data) -> str:
    """Serialize data to a JSON string, handling UUID and datetime objects."""
    return json.loads(json.dumps(data, cls=_SafeEncoder))


# ── WebSocket event handlers ──────────────────────────────────────────────────

def register_handlers(socketio):
    @socketio.on("connect")
    def on_connect():
        join_room(FLEET_ROOM)
        logger.debug("Client connected, joined fleet room")

    @socketio.on("subscribe_server")
    def on_subscribe_server(data):
        server_id = data.get("server_id") if isinstance(data, dict) else None
        if server_id:
            join_room(f"server:{server_id}")
            emit("subscribed", {"server_id": str(server_id)})

    @socketio.on("unsubscribe_server")
    def on_unsubscribe_server(data):
        server_id = data.get("server_id") if isinstance(data, dict) else None
        if server_id:
            leave_room(f"server:{server_id}")


# ── Emit helpers (called from poller.py) ─────────────────────────────────────

def emit_server_summary_update(socketio, server_summary: dict):
    """Sidebar-level update: connection indicator, health, power state."""
    try:
        socketio.emit("server_summary_update", _safe_dump(server_summary), room=FLEET_ROOM)
    except Exception:
        logger.exception("emit_server_summary_update failed")


def emit_component_update(socketio, server_id: str, category: str, components: list):
    """Full detail update for one hardware category card."""
    try:
        socketio.emit(
            "component_update",
            _safe_dump({"server_id": str(server_id), "category": category, "components": components}),
            room=f"server:{server_id}",
        )
    except Exception:
        logger.exception("emit_component_update failed for server %s category %s", server_id, category)


def emit_alert(socketio, server_id: str, alert: dict):
    try:
        socketio.emit("alert", _safe_dump(alert), room=FLEET_ROOM)
    except Exception:
        logger.exception("emit_alert failed")


def emit_log_entries(socketio, server_id: str, entries: list):
    try:
        socketio.emit(
            "log_entries",
            _safe_dump({"server_id": str(server_id), "entries": entries}),
            room=f"server:{server_id}",
        )
    except Exception:
        logger.exception("emit_log_entries failed for server %s", server_id)
