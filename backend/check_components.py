import sys, json, time
from app import create_app
from database.models import Server, Component

app, socketio = create_app()
with app.app_context():
    s = Server.query.filter_by(ip_address="192.168.2.204").first()
    if not s:
        print("Server not found")
        sys.exit(1)

    time.sleep(10) # wait for poller to finish one cycle
    comps = Component.query.filter_by(server_id=s.id).all()
    out = {}
    for c in comps:
        out[c.category] = out.get(c.category, 0) + 1
    
    print("Component counts for HPE iLO:")
    for k, v in out.items():
        print(f"  {k}: {v}")
