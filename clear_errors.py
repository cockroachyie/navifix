from backend.app import app
from backend.database import db
from backend.database.models import Server

with app.app_context():
    servers = Server.query.all()
    for s in servers:
        s.last_error = "Please click 'Update credentials' and re-enter the password. The backend was restarted and the encryption key was reset."
        s.status = "unknown"
    db.session.commit()
    print("Updated all servers.")
