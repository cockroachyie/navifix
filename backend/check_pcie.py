import sys, json
from app import create_app
from database.models import Server
from auth.credentials import get_cipher
from redfish.client import RedfishClient
from redfish.session import RedfishSession
from config import build_app_config
from redfish.discovery import discover_topology
from redfish.collectors import pcie, battery, firmware

app, socketio = create_app()
with app.app_context():
    s = Server.query.filter_by(ip_address="192.168.2.204").first()
    cipher = get_cipher(app.config)
    pwd = cipher.decrypt(s.password_encrypted)

    cfg = build_app_config()
    session = RedfishSession(base_url="https://192.168.2.204", username=s.username, password=pwd, config=cfg, server_id="test")
    client = RedfishClient(session, cfg)
    topology = discover_topology(client)
    
    print("PCIe:")
    comps, _ = pcie.collect(client, s, topology)
    print(len(comps))
    
    print("Battery:")
    comps, _ = battery.collect(client, s, topology)
    print(len(comps))
    
    print("Firmware:")
    comps, _ = firmware.collect(client, s, topology)
    print(len(comps))
