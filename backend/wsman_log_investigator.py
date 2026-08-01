import sys
import os
import json
import ssl
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from app import create_app
from database import db
from database.models import Server
from auth.credentials import get_cipher
from redfish.dell_wsman import WsManClient
import logging

logging.basicConfig(level=logging.ERROR)

app, socketio = create_app()

def fetch_url(url, user, password):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = Request(url, headers={"Authorization": f"Basic {creds}", "Accept": "application/json"})
    
    try:
        with urlopen(req, context=ctx, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, None

def check_redfish(ip, user, password):
    print("\n--- Checking Redfish LogServices ---")
    base_url = f"https://{ip}"
    
    paths_to_check = [
        "/redfish/v1/Managers/iDRAC.Embedded.1/LogServices",
        "/redfish/v1/Managers/iDRAC.Embedded.1/Logs",
        "/redfish/v1/Systems/System.Embedded.1/LogServices",
        "/redfish/v1/Systems/System.Embedded.1/Logs",
        "/redfish/v1/Chassis/System.Embedded.1/LogServices",
    ]
    
    status, data = fetch_url(f"{base_url}/redfish/v1", user, password)
    print(f"Redfish root /redfish/v1 : {status}")

    for p in paths_to_check:
        status, data = fetch_url(f"{base_url}{p}", user, password)
        if status == 200 and data:
            members = data.get("Members", [])
            print(f"GET {p} : OK, Found {len(members)} members")
            for m in members:
                m_id = m.get("@odata.id")
                print(f"  -> Member: {m_id}")
                if m_id:
                    m_status, m_data = fetch_url(f"{base_url}{m_id}", user, password)
                    if m_status == 200 and m_data:
                        entries = m_data.get("Entries", {}).get("@odata.id")
                        if entries:
                            e_status, e_data = fetch_url(f"{base_url}{entries}", user, password)
                            if e_status == 200 and e_data:
                                print(f"    -> Entries: {len(e_data.get('Members', []))} events")
        else:
            print(f"GET {p} : {status}")

def dump_record_logs(client):
    print("\n--- Checking WS-Man CIM_RecordLog ---")
    uri = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_RecordLog"
    try:
        items = client.enumerate(uri)
        print(f"Found {len(items)} CIM_RecordLog instances.")
        for i, item in enumerate(items):
            print(f"\nInstance {i}:")
            print(f"  ElementName: {item.get('ElementName')}")
            print(f"  Description: {item.get('Description')}")
            print(f"  CurrentNumberOfRecords: {item.get('CurrentNumberOfRecords')}")
            print(f"  MaxNumberOfRecords: {item.get('MaxNumberOfRecords')}")
            print(f"  LogState: {item.get('LogState')}  (EnabledState: {item.get('EnabledState')})")
    except Exception as e:
        print(f"ERROR: {e}")

classes_to_test = [
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_LifecycleLogView",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_SELRecord",
    "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/DCIM_SystemEventLog",
    "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_LifecycleLogView",
    "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_SELRecord",
]

def dump_log_classes(client):
    print("\n--- Checking alternative WS-Man Log classes ---")
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
    
    print(f"Using server {server.hostname} ({server.ip_address})")
    
    cipher = get_cipher(app.config["REDFISH_CONFIG"])
    password = cipher.decrypt(server.password_encrypted)
    
    check_redfish(server.ip_address, server.username, password)
    
    client = WsManClient(server.ip_address, server.username, password)
    dump_record_logs(client)
    dump_log_classes(client)
