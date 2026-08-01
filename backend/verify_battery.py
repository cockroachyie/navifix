import urllib.request, json, ssl, base64

base = "https://192.168.2.210"
USERNAME = "Admin"
PASSWORD = "Admin@123"

def fetch(url):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url)
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

cha = fetch(f"{base}/redfish/v1/Chassis/1/Power/")
if cha:
    print("Power Oem Hp Links:", json.dumps(cha.get("Oem", {}).get("Hp", {}).get("Links", {}), indent=2))
    
    # Are there any other voltages?
    print("Standard Voltages:", cha.get("Voltages"))

ss = fetch(f"{base}/redfish/v1/Systems/1/SmartStorage/")
if ss:
    print("SmartStorage Links:", json.dumps(ss.get("Links", {}), indent=2))
    ac = fetch(f"{base}" + ss.get("Links", {}).get("ArrayControllers", {}).get("@odata.id"))
    if ac:
        for member in ac.get("Members", []):
            print("ArrayController:", member.get("@odata.id"))
            ctrl = fetch(f"{base}" + member.get("@odata.id"))
            if ctrl:
                print("  Controller links:", json.dumps(ctrl.get("Links", {}), indent=2))
                
mem = fetch(f"{base}/redfish/v1/Systems/1/Memory/")
if mem:
    print("Memory members count:", len(mem.get("Members", [])))
