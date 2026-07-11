import sys
import logging
logging.basicConfig(level=logging.INFO)
from backend.redfish.session import RedfishSession
from backend.config import AppConfig

config = AppConfig(
    REDFISH_VERIFY_TLS=False,
    REDFISH_HTTP_TIMEOUT=5.0,
    REDFISH_SESSION_REFRESH_MARGIN=0,
)

session = RedfishSession("https://192.168.2.204", "Administrator", "password", config, "test")
try:
    client = session.get_http_client()
    print("Success! Token:", session.token)
except Exception as e:
    print("Failed:", type(e).__name__, str(e))
