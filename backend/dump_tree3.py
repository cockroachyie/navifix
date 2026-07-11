import sys, json
from app import create_app
from database.models import Server
from auth.credentials import get_cipher
from redfish.client import RedfishClient
from redfish.session import RedfishSession
from config import build_app_config

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

out = {}
out["Thermal"] = client.get("/redfish/v1/Chassis/1/Thermal/")
print(json.dumps(out, indent=2))
