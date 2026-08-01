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

sys_data = fetch(f"{base}/redfish/v1/Systems/1/")
if sys_data:
    oem = sys_data.get("Oem", {})
    print("System OEM keys:", list(oem.keys()))
    if "Hp" in oem:
        print("Hp OEM keys:", list(oem["Hp"].keys()))
        if "SmartStorage" in oem["Hp"]:
             print("SmartStorage in Hp:", oem["Hp"]["SmartStorage"])
    if "Hpe" in oem:
        print("Hpe OEM keys:", list(oem["Hpe"].keys()))
        if "SmartStorage" in oem["Hpe"]:
             print("SmartStorage in Hpe:", oem["Hpe"]["SmartStorage"])

cha_data = fetch(f"{base}/redfish/v1/Chassis/1/")
if cha_data:
    p_url = cha_data.get("Power", {}).get("@odata.id")
    if p_url:
        p_data = fetch(f"{base}{p_url}")
        if p_data:
            print("Power Voltages count:", len(p_data.get("Voltages", [])))
            p_oem = p_data.get("Oem", {})
            print("Power OEM keys:", list(p_oem.keys()))
            if "Hp" in p_oem:
                print("Power Hp OEM keys:", list(p_oem["Hp"].keys()))
                print("Voltages in Hp?", "Voltages" in p_oem["Hp"])
            if "Hpe" in p_oem:
                print("Power Hpe OEM keys:", list(p_oem["Hpe"].keys()))
                print("Voltages in Hpe?", "Voltages" in p_oem["Hpe"])
