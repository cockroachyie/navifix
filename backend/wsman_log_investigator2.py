import sys
import os

from app import create_app
from database import db
from database.models import Server
from auth.credentials import get_cipher
from redfish.dell_wsman import WsManClient
import logging

logging.basicConfig(level=logging.ERROR)

app, socketio = create_app()

classes_to_test = [
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_LogRecord",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_RecordForLog",
    "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_LogRecord",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_RecordLog",
]

def dump_log_classes(client):
    print("\n--- Checking MORE alternative WS-Man Log classes ---")
    for uri in classes_to_test:
        print(f"Trying {uri} ...")
        try:
            items = client.enumerate(uri)
            print(f" -> SUCCESS: {len(items)} items")
            if items:
                for k in list(items[0].keys())[:10]:
                    print(f"    Sample key: {k}")
        except Exception as e:
            print(f" -> ERROR: {e}")
        print()

with app.app_context():
    server = Server.query.filter(Server.hostname.ilike("%idrac7%")).first()
    if not server:
        server = Server.query.first() # fallback
    
    cipher = get_cipher(app.config["REDFISH_CONFIG"])
    password = cipher.decrypt(server.password_encrypted)
    
    client = WsManClient(server.ip_address, server.username, password)
    dump_log_classes(client)
