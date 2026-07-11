from app import create_app
from database import db
from database.models import Server

app, socketio = create_app()

with app.app_context():
    servers = Server.query.all()
    for s in servers:
        s.last_error = "Decryption key changed. Please click 'Update credentials' and re-enter the password."
        s.status = "unreachable"
    db.session.commit()
    print("Updated all servers.")
