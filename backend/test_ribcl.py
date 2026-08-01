from redfish.hpe_ribcl import RibclClient
import json

def test_host(ip, user, password):
    print(f"Testing {ip}...")
    client = RibclClient(ip, user, password)
    try:
        data = client.get_embedded_health()
        print(f"Success for {ip}:")
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Failed for {ip}: {e}")

test_host("192.168.2.204", "Administrator", "Admin@123")
test_host("192.168.2.211", "Admin", "Admin@123")
