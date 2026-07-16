"""
agent.py
========
Standalone Collector Agent for Redfish Fleet Monitor.
Runs on remote sites to collect telemetry from local BMCs and pushes it to the Central Server.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

# Import existing modules
from redfish.session import SessionManager, RedfishAuthError, RedfishUnreachableError
from redfish.client import RedfishClient
from redfish import inventory, discovery
from redfish.collectors import COLLECTOR_REGISTRY
from redfish.collectors import logs as logs_collector

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Agent: %(message)s"
)
logger = logging.getLogger("agent")

class AgentConfig(dict):
    def __getattr__(self, name):
        return self.get(name)

CONFIG_PATH = Path("agent_config.json")
QUEUE_DB_PATH = Path("agent_queue.db")

# Mock DB Session for inventory
class DummyDBSession:
    def add(self, obj):
        pass
    def commit(self):
        pass

class ServerStub:
    """Mock Server object to pass into existing redfish modules."""
    def __init__(self, data):
        self.id = data["server_id"]
        self.hostname = data["ip_address"]
        self.ip_address = data["ip_address"]
        self.username = data["username"]
        self._password = data["password"]
        
        # Identity fields to capture
        self.redfish_service_root = None
        self.redfish_system_uri = None
        self.redfish_chassis_uri = None
        self.redfish_manager_uri = None
        self.vendor = None
        self.model = None
        self.serial_number = None
        self.asset_tag = None
        self.service_tag = None
        self.power_state = None
        self.health_status = None
        self.firmware_version = None
        self.supports_event_service = None

    def to_inventory_dict(self):
        return {
            "redfish_service_root": self.redfish_service_root,
            "redfish_system_uri": self.redfish_system_uri,
            "redfish_chassis_uri": self.redfish_chassis_uri,
            "redfish_manager_uri": self.redfish_manager_uri,
            "vendor": self.vendor,
            "model": self.model,
            "serial_number": self.serial_number,
            "asset_tag": self.asset_tag,
            "service_tag": self.service_tag,
            "power_state": self.power_state,
            "health_status": self.health_status,
            "firmware_version": self.firmware_version,
            "supports_event_service": self.supports_event_service,
        }

class CollectorAgent:
    def __init__(self):
        self.load_config()
        self.init_db()
        self.scheduler = BackgroundScheduler(daemon=True)
        self.executor = ThreadPoolExecutor(max_workers=5)
        
        # Setup Redfish config
        self.rf_config = AgentConfig({
            "REDFISH_VERIFY_TLS": False,
            "REDFISH_HTTP_TIMEOUT": 10,
            "REDFISH_MAX_RETRIES": 1,
            "REDFISH_RETRY_BACKOFF_SECONDS": 1,
            "REDFISH_SESSION_REFRESH_MARGIN": 300,
            "INVENTORY_REFRESH_INTERVAL_SECONDS": 3600,
        })
        self.session_manager = SessionManager(self.rf_config)
        self._topology_cache = {}
        self._last_inventory_refresh = {}

    def load_config(self):
        if not CONFIG_PATH.exists():
            default_config = {
                "AGENT_TOKEN": "your-agent-token-here",
                "CENTRAL_SERVER_URL": "http://localhost:5000",
                "POLL_INTERVAL_SECONDS": 30,
                "DEVICES": [
                    {
                        "server_id": "paste-server-id-from-central",
                        "ip_address": "192.168.1.100",
                        "username": "root",
                        "password": "calvin"
                    }
                ]
            }
            CONFIG_PATH.write_text(json.dumps(default_config, indent=2))
            logger.error("Created default agent_config.json. Please edit and restart.")
            exit(1)
            
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)
            
    def init_db(self):
        self.conn = sqlite3.connect(QUEUE_DB_PATH, check_same_thread=False)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def enqueue_payload(self, payload):
        self.conn.execute("INSERT INTO queue (payload) VALUES (?)", (json.dumps(payload),))
        self.conn.commit()

    def push_queue(self):
        cursor = self.conn.execute("SELECT id, payload FROM queue ORDER BY id ASC LIMIT 50")
        rows = cursor.fetchall()
        if not rows:
            return

        headers = {"X-Agent-Token": self.config["AGENT_TOKEN"]}
        for row_id, payload_str in rows:
            try:
                payload = json.loads(payload_str)
                resp = httpx.post(
                    f"{self.config['CENTRAL_SERVER_URL']}/api/ingest/telemetry",
                    json=payload,
                    headers=headers,
                    timeout=10
                )
                resp.raise_for_status()
                self.conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))
                self.conn.commit()
            except Exception as e:
                logger.error(f"Failed to push payload {row_id}: {e}")
                break # Stop pushing if central server is down

    def get_topology(self, client, server_stub):
        now = datetime.utcnow()
        last_refresh = self._last_inventory_refresh.get(server_stub.id)
        if (
            server_stub.id not in self._topology_cache
            or last_refresh is None
            or (now - last_refresh).total_seconds() > self.rf_config["INVENTORY_REFRESH_INTERVAL_SECONDS"]
        ):
            topology = inventory.refresh_inventory(client, server_stub, DummyDBSession())
            self._topology_cache[server_stub.id] = topology
            self._last_inventory_refresh[server_stub.id] = now
            return topology, True
        return self._topology_cache[server_stub.id], False

    def _serialize_component(self, c):
        return {
            "category": c["category"].value if hasattr(c["category"], "value") else c["category"],
            "odata_id": c["odata_id"],
            "name": c["name"],
            "health": c["health"],
            "state": c["state"],
            "location": c["location"],
            "raw_json": c["raw_json"],
        }

    def poll_device(self, device_config):
        server = ServerStub(device_config)
        logger.info(f"Polling {server.ip_address}...")
        
        payload = {
            "server_id": server.id,
            "components": {},
            "readings": [],
            "logs": [],
        }

        base_url = f"https://{server.ip_address}"
        try:
            redfish_session = self.session_manager.get_session(server.id, base_url, server.username, server._password)
            client = RedfishClient(redfish_session, self.rf_config)
        except Exception as e:
            logger.error(f"Failed to get session for {server.ip_address}: {e}")
            payload["connection_status"] = "auth_failed"
            payload["connection_error"] = str(e)
            self.enqueue_payload(payload)
            return

        try:
            topology, inventory_updated = self.get_topology(client, server)
            if inventory_updated:
                payload["inventory"] = server.to_inventory_dict()
        except RedfishAuthError:
            payload["connection_status"] = "auth_failed"
            payload["connection_error"] = "Authentication failed"
            self.enqueue_payload(payload)
            return
        except RedfishUnreachableError as exc:
            payload["connection_status"] = "unreachable"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return
        except Exception as exc:
            payload["connection_status"] = "unreachable"
            payload["connection_error"] = str(exc)
            self.enqueue_payload(payload)
            return

        payload["connection_status"] = "connected"

        # Collect components
        for cat_name, collector in COLLECTOR_REGISTRY.items():
            try:
                components, readings = collector.collect(client, server, topology)
                payload["components"][cat_name] = [self._serialize_component(c) for c in components]
                payload["readings"].extend(readings)
            except Exception as e:
                logger.error(f"Collector {cat_name} failed for {server.ip_address}: {e}")

        # Collect logs
        try:
            logs = logs_collector.collect(client, server, topology)
            payload["logs"].extend(logs)
        except Exception as e:
            logger.error(f"Log collection failed for {server.ip_address}: {e}")

        self.enqueue_payload(payload)

    def poll_all(self):
        logger.info("Starting poll cycle...")
        devices = self.config.get("DEVICES", [])
        futures = []
        for dev in devices:
            futures.append(self.executor.submit(self.poll_device, dev))
        
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.error(f"Unhandled error in poll thread: {e}")
                
        # Push after collecting
        self.push_queue()

    def run(self):
        interval = self.config.get("POLL_INTERVAL_SECONDS", 30)
        self.scheduler.add_job(self.poll_all, 'interval', seconds=interval)
        self.scheduler.add_job(self.push_queue, 'interval', seconds=10) # Push more frequently if queue is backed up
        self.scheduler.start()
        
        # Run first poll immediately
        self.poll_all()
        
        logger.info(f"Collector Agent started. Polling every {interval}s.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self.scheduler.shutdown()
            self.executor.shutdown()

if __name__ == "__main__":
    agent = CollectorAgent()
    agent.run()
