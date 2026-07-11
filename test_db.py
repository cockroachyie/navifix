from backend.app import create_app
from backend.database.models import Server
from backend.auth.credentials import get_cipher

app, socketio = create_app()
with app.app_context():
    s = Server.query.filter_by(ip_address="192.168.2.204").first()
    if s:
        cipher = get_cipher(app.config)
        print("Username:", s.username)
        print("Password:", cipher.decrypt(s.password_encrypted))
