import os
from dotenv import load_dotenv
load_dotenv()
from config import build_app_config
from redfish.client import RedfishClient
from auth.credentials import get_cipher

cfg = build_app_config()
cipher = get_cipher(cfg)

client = RedfishClient("192.168.2.203", "root", cipher.decrypt("gAAAAABnj-f2V8Nn7yO76q5hP162p4mZ57Q2H7qQO8gYmB5BqT9Y2z7Lq8l0E9Yn_P_H4R3Q=="), cfg)
client.connect()
sys = client.get("/redfish/v1/Systems/System.Embedded.1")
import json
print("PCIeDevices in top:", "PCIeDevices" in sys)
print("PCIeDevices in Links:", "PCIeDevices" in sys.get("Links", {}))
if "PCIeDevices" in sys:
    print("Top PCIeDevices:", sys["PCIeDevices"])
if "PCIeDevices" in sys.get("Links", {}):
    print("Links PCIeDevices:", sys["Links"]["PCIeDevices"])
