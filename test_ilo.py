import httpx

client = httpx.Client(verify=False, follow_redirects=True)

def fetch(path):
    try:
        res = client.get("https://192.168.2.204" + path, auth=("Administrator", "password"), timeout=5)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"Error fetching {path}: {e}")
    return None

root = fetch("/redfish/v1/")
if not root:
    print("Could not fetch root")
    exit(1)

print("Root UpdateService:", root.get("UpdateService", {}).get("@odata.id"))
print("Systems:")
for s in root.get("Systems", {}).get("Members", []):
    s_id = s.get("@odata.id")
    print(f"  {s_id}")
    s_body = fetch(s_id)
    if s_body:
        for k in ["Storage", "SimpleStorage", "PCIeDevices", "Memory", "Processors", "Links"]:
            if k in s_body:
                print(f"    {k}: {s_body[k]}")

print("Chassis:")
for c in root.get("Chassis", {}).get("Members", []):
    c_id = c.get("@odata.id")
    print(f"  {c_id}")
    c_body = fetch(c_id)
    if c_body:
        for k in ["Power", "Thermal", "PCIeDevices", "PCIeSlots", "Cables", "Drives", "Batteries", "Links"]:
            if k in c_body:
                print(f"    {k}: {c_body[k]}")
