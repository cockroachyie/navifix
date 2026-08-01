import sys
import paramiko
import time
from app import create_app
from database import db
from database.models import Server
from auth.credentials import get_cipher
import logging

logging.basicConfig(level=logging.ERROR)
app, socketio = create_app()

def run_ssh_command(ip, user, password, command):
    print(f"\n--- Running RACADM command: {command} ---")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, username=user, password=password, timeout=10, look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        
        print("STDOUT (first 1000 chars):")
        print(output[:1000])
        if len(output) > 1000:
            print("... (truncated)")
            
        if error:
            print("STDERR:")
            print(error)
            
    except Exception as e:
        print(f"SSH Error: {e}")
    finally:
        client.close()

with app.app_context():
    server = Server.query.filter(Server.hostname.ilike("%idrac7%")).first()
    if not server:
        server = Server.query.first() # fallback
    
    print(f"Using server {server.hostname} ({server.ip_address})")
    
    cipher = get_cipher(app.config["REDFISH_CONFIG"])
    password = cipher.decrypt(server.password_encrypted)
    
    run_ssh_command(server.ip_address, server.username, password, "racadm getsel")
    run_ssh_command(server.ip_address, server.username, password, "racadm lclog view")
