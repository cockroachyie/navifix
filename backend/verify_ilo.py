import urllib.request, json, ssl, base64

base = "https://192.168.2.210"
auth_header = {}
USERNAME = "Admin"
PASSWORD = "Admin@123"

def fetch(url):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url)
    
    if "X-Auth-Token" in auth_header:
        req.add_header('X-Auth-Token', auth_header["X-Auth-Token"])
    else:
        auth = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode('ascii')
        req.add_header('Authorization', f'Basic {auth}')
        
    req.add_header('Accept', 'application/json')
    req.add_header('OData-Version', '4.0')
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            if response.status == 200:
                return json.loads(response.read().decode())
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
    return None

root = fetch(f"{base}/redfish/v1/")
if not root:
    print("Could not fetch root with basic auth, trying POST to Sessions...")
    data = json.dumps({"UserName": USERNAME, "Password": PASSWORD}).encode()
    req = urllib.request.Request(f"{base}/redfish/v1/SessionService/Sessions/", data=data, method="POST")
    req.add_header('Content-Type', 'application/json')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            token = response.headers.get('X-Auth-Token')
            print(f"Session token: {token}")
            auth_header["X-Auth-Token"] = token
    except Exception as e:
        print(f"Session failed: {e}")

sys_members = fetch(f"{base}/redfish/v1/Systems/")
if sys_members:
    for member in sys_members.get("Members", []):
        sys_url = member.get("@odata.id")
        print(f"System: {sys_url}")
        sys_data = fetch(f"{base}{sys_url}")
        if sys_data:
            print("  Storage:", sys_data.get("Storage", {}).get("@odata.id", "None"))
            print("  SimpleStorage:", sys_data.get("SimpleStorage", {}).get("@odata.id", "None"))
            print("  Memory:", sys_data.get("Memory", {}).get("@odata.id", "None"))
            
            ms = sys_data.get("MemorySummary", {})
            print("  MemorySummary keys:", list(ms.keys()))
            if "TotalSystemMemoryGiB" in ms:
                print("    TotalSystemMemoryGiB:", ms["TotalSystemMemoryGiB"])
            if "Status" in ms:
                print("    Status:", ms["Status"])

            oem = sys_data.get("Oem", {})
            hp_oem = oem.get("Hpe", {}) or oem.get("Hp", {})
            
            smart_storage = hp_oem.get("SmartStorage", {}).get("@odata.id")
            if smart_storage:
                print(f"  SmartStorage OEM URL: {smart_storage}")
                ss_data = fetch(f"{base}{smart_storage}")
                if ss_data:
                    ac_url = ss_data.get("Links", {}).get("ArrayControllers", {}).get("@odata.id")
                    if ac_url:
                        ac_data = fetch(f"{base}{ac_url}")
                        if ac_data:
                            for ac in ac_data.get("Members", []):
                                ctrl_url = ac.get("@odata.id")
                                ctrl = fetch(f"{base}{ctrl_url}")
                                if ctrl:
                                    print(f"    Controller: {ctrl_url}")
                                    bu = ctrl.get("Links", {}).get("BackupUnits", {}).get("@odata.id")
                                    if not bu:
                                        # try oem links
                                        bu = ctrl.get("Oem", {}).get("Hp", {}).get("Links", {}).get("BackupUnits", {}).get("@odata.id")
                                    if bu:
                                        print(f"      BackupUnits link: {bu}")
                                        bu_data = fetch(f"{base}{bu}")
                                        if bu_data:
                                            for b in bu_data.get("Members", []):
                                                print(f"        Battery: {b.get('@odata.id')}")

cha_members = fetch(f"{base}/redfish/v1/Chassis/")
if cha_members:
    for member in cha_members.get("Members", []):
        cha_url = member.get("@odata.id")
        print(f"Chassis: {cha_url}")
        cha_data = fetch(f"{base}{cha_url}")
        if cha_data:
            print("  Power:", cha_data.get("Power", {}).get("@odata.id"))
            p_url = cha_data.get("Power", {}).get("@odata.id")
            if p_url:
                p_data = fetch(f"{base}{p_url}")
                if p_data:
                    print(f"    Voltages count: {len(p_data.get('Voltages', []))}")
                    p_oem = p_data.get("Oem", {})
                    p_hp_oem = p_oem.get("Hpe", {}) or p_oem.get("Hp", {})
                    print(f"    OEM Voltages count: {len(p_hp_oem.get('Voltages', []))}")
            print("  Thermal:", cha_data.get("Thermal", {}).get("@odata.id"))
            t_url = cha_data.get("Thermal", {}).get("@odata.id")
            if t_url:
                t_data = fetch(f"{base}{t_url}")
                if t_data:
                    print(f"    Temperatures count: {len(t_data.get('Temperatures', []))}")
                    print(f"    Fans count: {len(t_data.get('Fans', []))}")
