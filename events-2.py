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
import logging

from flask_socketio import join_room, leave_room, emit

logger = logging.getLogger(__name__)

FLEET_ROOM = "fleet"


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
            emit("subscribed", {"server_id": server_id})

    @socketio.on("unsubscribe_server")
    def on_unsubscribe_server(data):
        server_id = data.get("server_id") if isinstance(data, dict) else None
        if server_id:
            leave_room(f"server:{server_id}")


def emit_server_summary_update(socketio, server_summary: dict):
    """Sidebar-level update: connection indicator, health color, power
    state - sent to everyone (fleet room)."""
    socketio.emit("server_summary_update", server_summary, room=FLEET_ROOM)


def emit_component_update(socketio, server_id: str, category: str, components: list[dict]):
    """Full detail update for one expandable card - sent only to clients
    that currently have this server open."""
    socketio.emit(
        "component_update",
        {"server_id": server_id, "category": category, "components": components},
        room=f"server:{server_id}",
    )


def emit_alert(socketio, server_id: str, alert: dict):
    socketio.emit("alert", alert, room=FLEET_ROOM)


def emit_log_entries(socketio, server_id: str, entries: list[dict]):
    socketio.emit("log_entries", {"server_id": server_id, "entries": entries}, room=f"server:{server_id}")
