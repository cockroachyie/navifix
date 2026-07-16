"""
scheduler/poller.py
====================
The engine room. For each enabled Server row, on its own
polling_interval_seconds cadence:
  1. Decrypt the stored BMC password with the Fernet cipher.
  2. Get (or create/refresh) a RedfishSession via SessionManager.
  3. Periodically re-run discovery + inventory refresh.
  4. Run every collector in COLLECTOR_REGISTRY.
  5. Upsert Component rows, insert SensorReading rows, evaluate alerts,
     upsert new LogEntry rows.
  6. Update the Server row's connection/health/power-state summary.
  7. Push updates to browsers over WebSocket.

Concurrency is bounded by MAX_CONCURRENT_POLLS via ThreadPoolExecutor.
BMC HTTP calls are I/O-bound so threads (not processes) are appropriate,
and httpx releases the GIL during network waits.
"""
import logging
import threading
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

# Process-wide singleton so app.py's _trigger_poll() can reach it.
_polling_engine_instance = None


def _set_engine_instance(engine):
    global _polling_engine_instance
    _polling_engine_instance = engine


class PollingEngine:
    def __init__(self, app, socketio, config=None):
        """
        Parameters
        ----------
        app     : Flask application
        socketio: SocketIO instance
        config  : AppConfig (from config.build_app_config). If omitted,
                  falls back to app.config['REDFISH_CONFIG'] so that callers
                  that already have both work without changes.
        """
        self.app = app
        self.socketio = socketio

        # Accept either an explicit AppConfig or pull it from app.config.
        # AppConfig supports BOTH attribute (.KEY) and dict (['KEY']) access
        # so session.py, client.py and poller.py can all use it safely.
        if config is not None:
            self.config = config
        elif "REDFISH_CONFIG" in app.config:
            self.config = app.config["REDFISH_CONFIG"]
        else:
            # Legacy fallback: should not happen with the current app.py
            logger.warning("REDFISH_CONFIG not found in app.config — using app.config directly")
            self.config = app.config

        self.session_manager = SessionManager(self.config)
        self.executor = ThreadPoolExecutor(max_workers=self.config["MAX_CONCURRENT_POLLS"])
        self.scheduler = BackgroundScheduler(daemon=True)
        self._topology_cache: dict = {}
        self._last_inventory_refresh: dict = {}
        self._active_polls = set()
        self._active_polls_lock = threading.Lock()

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
        logger.info(
            "Polling engine started (max_concurrent_polls=%s, interval=%ss)",
            self.config["MAX_CONCURRENT_POLLS"],
            self.config["DEFAULT_POLLING_INTERVAL_SECONDS"],
        )

    def shutdown(self):
        self.scheduler.shutdown(wait=False)
        self.executor.shutdown(wait=False)

    # -- dispatch --------------------------------------------------------

    def _schedule_due_servers(self):
        with self.app.app_context():
            now = datetime.utcnow()
            # Only poll servers that are NOT managed by a remote agent
            servers = Server.query.filter_by(enabled=True, agent_id=None).all()
            for server in servers:
                last = server.last_poll_attempt
                interval = server.polling_interval_seconds or self.config["DEFAULT_POLLING_INTERVAL_SECONDS"]
                if last and (now - last).total_seconds() < interval:
                    continue
                
                server_id_str = str(server.id)
                with self._active_polls_lock:
                    if server_id_str in self._active_polls:
                        logger.warning("Skipping scheduled poll for %s: previous poll still executing", server.hostname)
                        continue
                    self._active_polls.add(server_id_str)
                    
                self.executor.submit(self._poll_server_safe, server.id)

    def poll_server_now(self, server_id: str):
        """Immediate on-demand refresh (called from /api/servers/<id>/poll-now)."""
        with self.app.app_context():
            server = Server.query.get(server_id)
            if server and server.agent_id is not None:
                logger.warning("Cannot manual poll server %s directly; it is managed by agent %s", server_id, server.agent_id)
                return

        server_id_str = str(server_id)
        with self._active_polls_lock:
            if server_id_str in self._active_polls:
                logger.warning("Ignoring manual poll request for %s: poll already in progress", server_id_str)
                return
            self._active_polls.add(server_id_str)
            
        self.executor.submit(self._poll_server_safe, server_id)

    def _poll_server_safe(self, server_id: str):
        """Wrapper that catches all exceptions so a broken server never kills
        the thread pool worker."""
        try:
            with self.app.app_context():
                try:
                    self._poll_server(server_id)
                except Exception:
                    logger.exception("Unhandled error polling server %s", server_id)
        finally:
            with self._active_polls_lock:
                self._active_polls.discard(str(server_id))

    # -- core poll cycle ---------------------------------------------------

    def _poll_server(self, server_id: str):
        server = Server.query.get(server_id)
        if not server or not server.enabled:
            return

        server.last_poll_attempt = datetime.utcnow()
        db.session.add(server)
        db.session.commit()

        # Decrypt BMC password. Provide a clear error if the key is wrong.
        cipher = get_cipher(self.config)
        try:
            password = cipher.decrypt(server.password_encrypted)
        except ValueError as exc:
            msg = (
                f"Cannot decrypt BMC password for {server.hostname} ({server.ip_address}). "
                f"This usually means ENCRYPTION_KEY changed since the server was added. "
                f"Re-add the server via the UI to store its password with the current key. "
                f"Detail: {exc}"
            )
            logger.error(msg)
            server.connection_status = ConnectionStatus.AUTH_FAILED
            server.last_poll_error = "Decryption failed — ENCRYPTION_KEY may have changed. Re-add this server."
            db.session.add(server)
            db.session.commit()
            ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())
            return

        base_url = f"https://{server.ip_address}"
        redfish_session = self.session_manager.get_session(server.id, base_url, server.username, password)
        client = RedfishClient(redfish_session, self.config)

        try:
            topology = self._get_topology(client, server)
        except RedfishAuthError:
            self._mark_connection(server, ConnectionStatus.AUTH_FAILED, "Authentication failed")
            alert_engine.raise_connection_alert(
                db.session, server.id, alert_engine.AlertSeverity.CRITICAL,
                f"Authentication failed for {server.hostname} ({server.ip_address})", "auth_failed",
            )
            return
        except RedfishUnreachableError as exc:
            self._mark_connection(server, ConnectionStatus.UNREACHABLE, str(exc))
            alert_engine.raise_connection_alert(
                db.session, server.id, alert_engine.AlertSeverity.CRITICAL,
                f"{server.hostname} ({server.ip_address}) unreachable: {exc}", "unreachable",
            )
            return

        alert_engine.resolve_connection_alerts(db.session, server.id, "auth_failed")
        alert_engine.resolve_connection_alerts(db.session, server.id, "unreachable")

        # -- run every category collector ----------------------------------
        for category_name, collector_module in COLLECTOR_REGISTRY.items():
            try:
                components, readings = collector_module.collect(client, server, topology)
            except Exception:
                logger.exception("Collector '%s' failed for server %s", category_name, server.id)
                continue
            self._upsert_components(server, category_name, components)
            self._insert_readings(server, readings)
            alert_engine.evaluate_components(db.session, str(server.id), category_name, components, self.config)
            ws_events.emit_component_update(
                self.socketio, str(server.id), category_name,
                [self._component_dict(x) for x in components],
            )

        # -- logs (append-only event stream) --------------------------------
        try:
            log_dicts = logs_collector.collect(client, server, topology)
            new_entries = self._upsert_logs(server, log_dicts)
            if new_entries:
                ws_events.emit_log_entries(self.socketio, str(server.id), new_entries)
        except Exception:
            logger.exception("Log collection failed for server %s", server.id)

        # -- event subscription (best-effort) --------------------------------
        webhook_base = self.config.get("PUBLIC_WEBHOOK_BASE_URL")
        if webhook_base and server.supports_event_service is not False:
            try:
                redfish_events.subscribe(
                    client, topology,
                    f"{webhook_base}/api/redfish/webhook",
                    server.id,
                )
            except Exception:
                logger.debug("Event subscription failed for %s (non-fatal)", server.id)

        # -- roll up health/connection/power --------------------------------
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
                server_id=server.id,
                metric=r["metric"],
                source_name=r["source_name"],
                value=r["value"],
                unit=r.get("unit"),
                recorded_at=now,
            ))
        if readings:
            db.session.commit()

    def _upsert_logs(self, server, log_dicts):
        from dateutil import parser as dtparser
        new_entries = []
        for entry in log_dicts:
            exists = LogEntry.query.filter_by(
                server_id=server.id,
                log_service=entry["log_service"],
                entry_id=entry["entry_id"],
            ).first()
            if exists:
                continue
            created_at = datetime.utcnow()
            if entry.get("created_raw"):
                try:
                    created_at = dtparser.parse(entry["created_raw"]).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
            db.session.add(LogEntry(
                server_id=server.id,
                log_service=entry["log_service"],
                entry_id=entry["entry_id"],
                severity=entry["severity"],
                message=entry["message"],
                message_id=entry["message_id"],
                sensor_type=entry["sensor_type"],
                created_at=created_at,
                raw_json=entry["raw_json"],
            ))
            new_entries.append(entry)
        if new_entries:
            db.session.commit()
        return new_entries

    def _mark_connection(self, server, status: ConnectionStatus, error_msg: str = ""):
        server.connection_status = status
        server.last_poll_error = error_msg
        db.session.add(server)
        db.session.commit()
        ws_events.emit_server_summary_update(self.socketio, server.to_summary_dict())

    def _recompute_server_summary(self, server):
        server.connection_status = ConnectionStatus.CONNECTED
        server.last_successful_poll = datetime.utcnow()
        server.last_poll_error = None
        worst = HealthStatus.OK
        order = [HealthStatus.OK, HealthStatus.WARNING, HealthStatus.CRITICAL]
        for c in Component.query.filter_by(server_id=server.id).all():
            if not c.health:
                continue
            try:
                h = HealthStatus(c.health)
                if order.index(h) > order.index(worst):
                    worst = h
            except ValueError:
                continue
        server.health_status = worst
        db.session.add(server)

    def _prune_old_readings(self):
        with self.app.app_context():
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=self.config["SENSOR_HISTORY_RETENTION_DAYS"])
            deleted = SensorReading.query.filter(SensorReading.recorded_at < cutoff).delete()
            db.session.commit()
            if deleted:
                logger.info("Pruned %d sensor readings older than %d days",
                            deleted, self.config["SENSOR_HISTORY_RETENTION_DAYS"])

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
