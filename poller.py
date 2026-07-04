"""
scheduler/poller.py
====================
The engine room. Separate from the web UI process concerns entirely - this
module could just as well run as its own worker process (recommended for
fleets of hundreds/thousands of servers; see README "Scaling out").

For each enabled Server row, on its own polling_interval_seconds cadence:
  1. Get (or create/refresh) a RedfishSession via SessionManager.
  2. Periodically (INVENTORY_REFRESH_INTERVAL_SECONDS) re-run discovery +
     inventory.refresh_inventory() to catch topology/identity changes.
  3. Run every collector in COLLECTOR_REGISTRY against the cached
     topology.
  4. Upsert resulting Component rows, insert SensorReading rows, evaluate
     alerts, upsert new LogEntry rows.
  5. Update the Server row's connection/health/power-state summary.
  6. Push updates to browsers over WebSocket.

Concurrency is bounded by MAX_CONCURRENT_POLLS via a ThreadPoolExecutor -
BMC HTTP calls are I/O-bound, so threads (not processes) are the right
tool, and httpx releases the GIL during network waits.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from database import db
from database.models import Server, Component, SensorReading, LogEntry, ConnectionStatus, HealthStatus
from redfish.session import SessionManager, RedfishAuthError, RedfishUnreachableError
from redfish.client import RedfishClient
from redfish import inventory, discovery, events as redfish_events
from redfish.collectors import COLLECTOR_REGISTRY
from redfish.collectors import logs as logs_collector
from alerts import engine as alert_engine
from auth.credentials import get_cipher
from websocket import events as ws_events

logger = logging.getLogger(__name__)


class PollingEngine:
    def __init__(self, app, socketio):
        self.app = app
        self.socketio = socketio
        self.config = app.config
        self.session_manager = SessionManager(self.config)
        self.executor = ThreadPoolExecutor(max_workers=self.config["MAX_CONCURRENT_POLLS"])
        self.scheduler = BackgroundScheduler()
        # topology is expensive to (re)discover; cache per server between
        # inventory refreshes.
        self._topology_cache: dict[str, dict] = {}
        self._last_inventory_refresh: dict[str, datetime] = {}

    # -- lifecycle -----------------------------------------------------

    def start(self):
        self.scheduler.add_job(
            self._schedule_due_servers,
            "interval",
            seconds=5,
            id="dispatch_loop",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._prune_old_readings,
            "interval",
            hours=6,
            id="prune_readings",
        )
        self.scheduler.start()
        logger.info("Polling engine started (max_concurrent_polls=%s)", self.config["MAX_CONCURRENT_POLLS"])

    def shutdown(self):
        self.scheduler.shutdown(wait=False)
        self.executor.shutdown(wait=False)

    # -- dispatch --------------------------------------------------------

    def _schedule_due_servers(self):
        """Runs every 5s in the scheduler thread; figures out which
        servers are due for a poll based on their own interval and hands
        them to the thread pool. Keeps the DB query itself cheap and the
        actual HTTP work off the scheduler thread."""
        with self.app.app_context():
            now = datetime.utcnow()
            servers = Server.query.filter_by(enabled=True).all()
            for server in servers:
                last = server.last_poll_attempt
                interval = server.polling_interval_seconds or self.config["DEFAULT_POLLING_INTERVAL_SECONDS"]
                if last and (now - last).total_seconds() < interval:
                    continue
                self.executor.submit(self._poll_server_safe, server.id)

    def poll_server_now(self, server_id: str):
        """Used by the 'poll now' API action for immediate on-demand refresh."""
        self.executor.submit(self._poll_server_safe, server_id)

    def _poll_server_safe(self, server_id: str):
        with self.app.app_context():
            try:
                self._poll_server(server_id)
            except Exception:
                logger.exception("Unhandled error polling server %s", server_id)

    # -- core poll cycle ---------------------------------------------------

    def _poll_server(self, server_id: str):
        server = Server.query.get(server_id)
        if not server or not server.enabled:
            return

        server.last_poll_attempt = datetime.utcnow()
        db.session.add(server)
        db.session.commit()

        base_url = f"https://{server.ip_address}"
        cipher = get_cipher(self.app.config)
        password = cipher.decrypt(server.password_encrypted)

        redfish_session = self.session_manager.get_session(server.id, base_url, server.username, password)
        client = RedfishClient(redfish_session, self.config)

        try:
            topology = self._get_topology(client, server)
        except RedfishAuthError:
            self._mark_connection(server, ConnectionStatus.AUTH_FAILED)
            alert_engine.raise_connection_alert(
                db.session, server.id, alert_engine.AlertSeverity.CRITICAL,
                f"Authentication failed for {server.hostname} ({server.ip_address})", "auth_failed",
            )
            return
        except RedfishUnreachableError as exc:
            self._mark_connection(server, ConnectionStatus.UNREACHABLE)
            alert_engine.raise_connection_alert(
                db.session, server.id, alert_engine.AlertSeverity.CRITICAL,
                f"{server.hostname} ({server.ip_address}) unreachable: {exc}", "unreachable",
            )
            return

        alert_engine.resolve_connection_alerts(db.session, server.id, "auth_failed")
        alert_engine.resolve_connection_alerts(db.session, server.id, "unreachable")

        # -- run every category collector --------------------------------
        for category_name, collector_module in COLLECTOR_REGISTRY.items():
            try:
                components, readings = collector_module.collect(client, server, topology)
            except Exception:
                logger.exception("Collector '%s' failed for server %s", category_name, server.id)
                continue
            self._upsert_components(server, category_name, components)
            self._insert_readings(server, readings)
            alert_engine.evaluate_components(db.session, server.id, category_name, components, self.config)
            ws_events.emit_component_update(
                self.socketio, server.id, category_name, [c for c in [self._component_dict(x) for x in components]]
            )

        # -- logs (separate shape: append-only event stream) --------------
        try:
            log_dicts = logs_collector.collect(client, server, topology)
            new_entries = self._upsert_logs(server, log_dicts)
            if new_entries:
                ws_events.emit_log_entries(self.socketio, server.id, new_entries)
        except Exception:
            logger.exception("Log collection failed for server %s", server.id)

        # -- event subscription (best-effort, opportunistic) ---------------
        webhook_base = self.config.get("PUBLIC_WEBHOOK_BASE_URL")
        if webhook_base and not server.supports_event_service is False:
            try:
                redfish_events.subscribe(client, topology, f"{webhook_base}/api/redfish/webhook", server.id)
            except Exception:
                logger.debug("Event subscription attempt failed for %s (non-fatal)", server.id)

        # -- roll up overall server health/connection/power state ----------
        self._recompute_server_summary(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    # -- helpers -----------------------------------------------------------

    def _get_topology(self, client, server):
        refresh_interval = self.config["INVENTORY_REFRESH_INTERVAL_SECONDS"]
        last_refresh = self._last_inventory_refresh.get(server.id)
        now = datetime.utcnow()
        if (
            server.id not in self._topology_cache
            or last_refresh is None
            or (now - last_refresh).total_seconds() > refresh_interval
        ):
            topology = inventory.refresh_inventory(client, server, db.session)
            self._topology_cache[server.id] = topology
            self._last_inventory_refresh[server.id] = now
        return self._topology_cache[server.id]

    def _upsert_components(self, server, category_name, components):
        for c in components:
            existing = Component.query.filter_by(
                server_id=server.id, category=c["category"], odata_id=c["odata_id"]
            ).first()
            if existing:
                existing.name = c["name"]
                existing.health = c["health"]
                existing.state = c["state"]
                existing.location = c["location"]
                existing.raw_json = c["raw_json"]
                existing.last_updated_at = datetime.utcnow()
                db.session.add(existing)
            else:
                db.session.add(Component(
                    server_id=server.id,
                    category=c["category"],
                    odata_id=c["odata_id"],
                    name=c["name"],
                    health=c["health"],
                    state=c["state"],
                    location=c["location"],
                    raw_json=c["raw_json"],
                ))
        db.session.commit()

    def _insert_readings(self, server, readings):
        now = datetime.utcnow()
        for r in readings:
            db.session.add(SensorReading(
                server_id=server.id, metric=r["metric"], source_name=r["source_name"],
                value=r["value"], unit=r.get("unit"), recorded_at=now,
            ))
        if readings:
            db.session.commit()

    def _upsert_logs(self, server, log_dicts):
        from dateutil import parser as dtparser  # local import: optional dep, only needed here
        new_entries = []
        for entry in log_dicts:
            exists = LogEntry.query.filter_by(
                server_id=server.id, log_service=entry["log_service"], entry_id=entry["entry_id"]
            ).first()
            if exists:
                continue
            created_at = datetime.utcnow()
            if entry.get("created_raw"):
                try:
                    created_at = dtparser.parse(entry["created_raw"]).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            row = LogEntry(
                server_id=server.id, log_service=entry["log_service"], entry_id=entry["entry_id"],
                severity=entry["severity"], message=entry["message"], message_id=entry["message_id"],
                sensor_type=entry["sensor_type"], created_at=created_at, raw_json=entry["raw_json"],
            )
            db.session.add(row)
            new_entries.append(entry)
        if new_entries:
            db.session.commit()
        return new_entries

    def _mark_connection(self, server, status: ConnectionStatus):
        server.connection_status = status
        server.last_poll_error = status.value
        db.session.add(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    def _recompute_server_summary(self, server):
        server.connection_status = ConnectionStatus.CONNECTED
        server.last_successful_poll = datetime.utcnow()
        server.last_poll_error = None

        worst = HealthStatus.OK
        order = [HealthStatus.OK, HealthStatus.WARNING, HealthStatus.CRITICAL]
        components = Component.query.filter_by(server_id=server.id).all()
        for c in components:
            if not c.health:
                continue
            try:
                h = HealthStatus(c.health)
            except ValueError:
                continue
            if order.index(h) > order.index(worst):
                worst = h
        server.health_status = worst
        db.session.add(server)

    def _prune_old_readings(self):
        with self.app.app_context():
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=self.config["SENSOR_HISTORY_RETENTION_DAYS"])
            deleted = SensorReading.query.filter(SensorReading.recorded_at < cutoff).delete()
            db.session.commit()
            if deleted:
                logger.info("Pruned %d sensor readings older than %d days", deleted, self.config["SENSOR_HISTORY_RETENTION_DAYS"])

    @staticmethod
    def _component_dict(c):
        return {
            "category": c["category"].value if hasattr(c["category"], "value") else c["category"],
            "odata_id": c["odata_id"],
            "name": c["name"],
            "health": c["health"],
            "state": c["state"],
            "location": c["location"],
            "properties": c["raw_json"],
        }
