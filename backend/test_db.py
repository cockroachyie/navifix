import os
os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "postgresql://redfish:redfish@localhost:5432/redfishmonitor")
from app import create_app
from database.models import Server
from auth.credentials import get_cipher

app, socketio = create_app()
with app.app_context():
    s = Server.query.filter_by(ip_address="192.168.2.204").first()
    if s:
        cipher = get_cipher(app.config)
        print("Username:", s.username)
        print("Password:", cipher.decrypt(s.password_encrypted))
