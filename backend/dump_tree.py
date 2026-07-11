import sys, json
from app import create_app
from database.models import Server
from auth.credentials import get_cipher
from redfish.client import RedfishClient
from redfish.session import RedfishSession
from config import build_app_config
from redfish.discovery import discover_topology

app, socketio = create_app()
with app.app_context():
    s = Server.query.filter_by(ip_address="192.168.2.204").first()
    cipher = get_cipher(app.config)
    pwd = cipher.decrypt(s.password_encrypted)

cfg = build_app_config()
session = RedfishSession(
    base_url=f"https://192.168.2.204",
    username=s.username,
    password=pwd,
    config=cfg,
    server_id="test"
)
client = RedfishClient(session, cfg)

topology = discover_topology(client)

out = {
    "UpdateService": topology.get("update_service"),
    "systems": topology["systems"],
    "chassis": topology["chassis"],
    "per_system": topology["per_system"],
    "per_chassis": topology["per_chassis"],
}

for sys_uri in topology["systems"]:
    body = client.get(sys_uri)
    if body:
        out[sys_uri + "_body"] = {k: body.get(k) for k in ["Storage", "SimpleStorage", "SmartStorage", "PCIeDevices", "Memory", "Processors", "Oem", "Links"]}

for cha_uri in topology["chassis"]:
    body = client.get(cha_uri)
    if body:
        out[cha_uri + "_body"] = {k: body.get(k) for k in ["Power", "Thermal", "PCIeDevices", "PCIeSlots", "Cables", "Drives", "Batteries", "Oem", "Links"]}

print(json.dumps(out, indent=2))
