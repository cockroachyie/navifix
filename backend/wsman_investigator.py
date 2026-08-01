import sys
import json
import os

from app import create_app
from database import db
from database.models import Server
from auth.credentials import get_cipher
from redfish.dell_wsman import WsManClient
import logging

logging.basicConfig(level=logging.ERROR)

app, socketio = create_app()

def dump_class(client, class_uri):
    print(f"\n--- {class_uri} ---")
    try:
        items = client.enumerate(class_uri)
        print(f"Items: {len(items)}")
        for i, item in enumerate(items[:2]):
            print(f"Item {i}:")
            for k, v in item.items():
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"ERROR: {e}")

classes_to_test = [
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_RecordLog",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_FanView",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_NumericSensor",
    "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_EnclosureView",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Chassis",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Account",
]

with app.app_context():
    server = Server.query.filter(Server.hostname.ilike("%idrac7%")).first()
    if not server:
        server = Server.query.first() # fallback
    
    cipher = get_cipher(app.config["REDFISH_CONFIG"])
    password = cipher.decrypt(server.password_encrypted)
    client = WsManClient(server.ip_address, server.username, password)
    
    for uri in classes_to_test:
        dump_class(client, uri)
