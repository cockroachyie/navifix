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

sys_data = fetch(f"{base}/redfish/v1/Systems/1/")
if sys_data:
    oem = sys_data.get("Oem", {})
    if "Hp" in oem:
        print("Hp Links:", json.dumps(oem["Hp"].get("Links", {}), indent=2))
    
    print("Top level links:", json.dumps(sys_data.get("Links", {}), indent=2))
    
    # Try fetching SmartStorage directly just in case it exists but wasn't advertised
    ss = fetch(f"{base}/redfish/v1/Systems/1/SmartStorage/")
    if ss:
        print("SmartStorage DIRECT exists!")
        print(list(ss.keys()))
    else:
        print("SmartStorage DIRECT does not exist.")

    # Try standard storage
    st = fetch(f"{base}/redfish/v1/Systems/1/Storage/")
    if st:
        print("Standard Storage DIRECT exists!")
    
cha_data = fetch(f"{base}/redfish/v1/Chassis/1/")
if cha_data:
    oem = cha_data.get("Oem", {})
    if "Hp" in oem:
         print("Chassis Hp OEM keys:", list(oem["Hp"].keys()))
    
root = fetch(f"{base}/redfish/v1/")
print("Root schema:", root.get("@odata.type"))
