"""
database/models.py
===================
Normalized PostgreSQL schema for the Redfish Fleet Monitor.

Tables
------
Server              — one row per monitored BMC endpoint
RedfishSessionRecord— optional: cached session token (avoids duplicate logins)
Component           — current state of every hardware component (upserted each poll)
SensorReading       — time-series values (temperature, fan RPM, voltage, etc.)
LogEntry            — append-only hardware event / SEL log entries
Alert               — active and resolved alert records with deduplication key
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy import types
from sqlalchemy.orm import relationship
import enum

from . import db


# Cross-DB JSON: uses PostgreSQL JSONB when available, plain JSON otherwise.
# This lets us test with SQLite and deploy to PostgreSQL without changes.
class _JSONB_or_JSON(types.TypeDecorator):
    """Stores JSON as PostgreSQL JSONB when using Postgres, JSON otherwise."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


# UUID: PostgreSQL native UUID when available, String(36) otherwise.
class _UUID(types.TypeDecorator):
    impl = types.String
    cache_ok = True

    def __init__(self, as_uuid=True):
        super().__init__(36)
        self.as_uuid = as_uuid

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            return dialect.type_descriptor(PG_UUID(as_uuid=self.as_uuid))
        return dialect.type_descriptor(types.String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if self.as_uuid and not isinstance(value, uuid.UUID):
            return uuid.UUID(value)
        return value


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionStatus(str, enum.Enum):
    UNKNOWN      = "unknown"
    CONNECTED    = "connected"
    UNREACHABLE  = "unreachable"
    AUTH_FAILED  = "auth_failed"


class HealthStatus(str, enum.Enum):
    OK       = "OK"
    WARNING  = "Warning"
    CRITICAL = "Critical"
    UNKNOWN  = "Unknown"


class AlertSeverity(str, enum.Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class OperationStatus(str, enum.Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"


class ComponentCategory(str, enum.Enum):
    BATTERY           = "battery"
    CHASSIS           = "chassis"
    FAN               = "fans"
    MEMORY            = "memory"
    PROCESSOR         = "processor"
    STORAGE_CONTROLLER= "storage_controller"
    STORAGE_DRIVE     = "storage_drive"
    STORAGE_VOLUME    = "storage_volume"
    POWER_SUPPLY      = "power"
    THERMAL_SENSOR    = "thermal"
    VOLTAGE_SENSOR    = "voltage"
    NETWORK_INTERFACE = "network"
    PCIE_DEVICE       = "pcie"
    FIRMWARE          = "firmware"
    SECURITY          = "security"


# ─────────────────────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────────────────────

class Server(db.Model):
    __tablename__ = "servers"

    id = Column(
        _UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        doc="Stable identifier; never recycled even if the server is re-added.",
    )

    # ── Identity / display ──────────────────────────────────────────────
    hostname        = Column(String(255), nullable=False)
    display_name    = Column(String(255))
    ip_address      = Column(String(64), nullable=False, unique=True)
    vendor          = Column(String(128))
    model           = Column(String(255))
    serial_number   = Column(String(128))
    service_tag     = Column(String(128))
    asset_tag       = Column(String(128))
    firmware_version= Column(String(128))
    part_number     = Column(String(128))

    # ── BMC credentials (password encrypted with Fernet) ────────────────
    username           = Column(String(128), nullable=False)
    password_encrypted = Column(Text, nullable=False)

    # ── Polling settings ────────────────────────────────────────────────
    polling_interval_seconds = Column(Integer, default=30)
    enabled                  = Column(Boolean, default=True, nullable=False)

    # ── Runtime state ───────────────────────────────────────────────────
    connection_status  = Column(
        Enum(ConnectionStatus, name="connection_status_enum"),
        default=ConnectionStatus.UNKNOWN,
    )
    health_status      = Column(String(32), default="Unknown")
    power_state        = Column(String(32))
    last_poll_attempt  = Column(DateTime)
    last_successful_poll = Column(DateTime)
    last_poll_error    = Column(Text)
    supports_event_service = Column(Boolean)

    # ── Redfish topology cache (avoids re-discovering every poll) ────────
    redfish_service_root  = Column(_JSONB_or_JSON())
    redfish_system_uri    = Column(String(512))
    redfish_chassis_uri   = Column(String(512))
    redfish_manager_uri   = Column(String(512))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Relationships ────────────────────────────────────────────────────
    components      = relationship("Component",     back_populates="server", cascade="all, delete-orphan")
    sensor_readings = relationship("SensorReading", back_populates="server", cascade="all, delete-orphan")
    log_entries     = relationship("LogEntry",      back_populates="server", cascade="all, delete-orphan")
    alerts          = relationship("Alert",         back_populates="server", cascade="all, delete-orphan")
    redfish_sessions= relationship("RedfishSessionRecord", back_populates="server", cascade="all, delete-orphan")
    operations      = relationship("Operation",     back_populates="server", cascade="all, delete-orphan")

    def to_summary_dict(self):
        """Minimal dict pushed to every browser via WebSocket (fleet room).
        Keep it small — only what the sidebar needs."""
        return {
            "id": str(self.id),
            "hostname": self.hostname,
            "display_name": self.display_name,
            "ip_address": self.ip_address,
            "vendor": self.vendor,
            "model": self.model,
            "service_tag": self.service_tag,
            "firmware_version": self.firmware_version,
            "connection_status": self.connection_status.value if self.connection_status else "unknown",
            "health_status": self.health_status,
            "power_state": self.power_state,
            "last_successful_poll": self.last_successful_poll.isoformat() if self.last_successful_poll else None,
            "last_poll_error": self.last_poll_error,
        }

    def to_dict(self):
        d = self.to_summary_dict()
        d.update({
            "serial_number": self.serial_number,
            "asset_tag": self.asset_tag,
            "polling_interval_seconds": self.polling_interval_seconds,
            "enabled": self.enabled,
            "last_poll_attempt": self.last_poll_attempt.isoformat() if self.last_poll_attempt else None,
            "last_poll_error": self.last_poll_error,
            "supports_event_service": self.supports_event_service,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        })
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Redfish Session Record  (optional cross-process token cache)
# ─────────────────────────────────────────────────────────────────────────────

class RedfishSessionRecord(db.Model):
    __tablename__ = "redfish_sessions"

    id         = Column(
        _UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id  = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    token      = Column(Text)
    session_uri= Column(String(512))
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    server = relationship("Server", back_populates="redfish_sessions")


# ─────────────────────────────────────────────────────────────────────────────
# Component — current hardware state
# ─────────────────────────────────────────────────────────────────────────────

class Component(db.Model):
    """One row per discoverable hardware component. Upserted (not inserted)
    on every poll cycle so the table always reflects current state."""

    __tablename__ = "components"
    __table_args__ = (
        UniqueConstraint("server_id", "category", "odata_id", name="uq_component_identity"),
    )

    id          = Column(
        _UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id   = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    category    = Column(String(64), nullable=False, index=True)   # ComponentCategory value
    odata_id    = Column(String(512), nullable=False)              # Redfish @odata.id — stable identity
    name        = Column(String(255))
    health      = Column(String(32))
    state       = Column(String(64))
    location    = Column(String(255))
    raw_json    = Column(_JSONB_or_JSON())                                    # Full Redfish resource body
    last_updated_at = Column(DateTime, default=datetime.utcnow)

    server = relationship("Server", back_populates="components")

    def to_dict(self):
        return {
            "id": str(self.id),
            "category": self.category,
            "odata_id": self.odata_id,
            "name": self.name,
            "health": self.health,
            "state": self.state,
            "location": self.location,
            "properties": self.raw_json or {},
            "last_updated_at": self.last_updated_at.isoformat() if self.last_updated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# SensorReading — time-series data
# ─────────────────────────────────────────────────────────────────────────────

class SensorReading(db.Model):
    """Append-only table. One row per numeric sensor value per poll cycle.
    Older rows are pruned by the scheduler's prune_readings job."""

    __tablename__ = "sensor_readings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    server_id   = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    metric      = Column(String(64), nullable=False, index=True)
    source_name = Column(String(255))       # e.g. "Fan.1", "CPU1 Temp", "DIMM_A1"
    value       = Column(Float, nullable=False)
    unit        = Column(String(32))        # "Cel", "RPM", "V", "W", "%", …
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    server = relationship("Server", back_populates="sensor_readings")

    def to_dict(self):
        return {
            "metric": self.metric,
            "source_name": self.source_name,
            "value": self.value,
            "unit": self.unit,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# LogEntry — SEL / Lifecycle / IML / event log entries
# ─────────────────────────────────────────────────────────────────────────────

class LogEntry(db.Model):
    """Append-only hardware event log entries pulled from Redfish LogServices.
    Keyed on (server_id, log_service, entry_id) for deduplication."""

    __tablename__ = "log_entries"
    __table_args__ = (
        UniqueConstraint("server_id", "log_service", "entry_id", name="uq_log_entry"),
    )

    id          = Column(
        _UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id   = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    log_service = Column(String(255))        # e.g. "SEL", "Lifecycle Log", "IML"
    entry_id    = Column(String(255))        # Redfish Entry Id (stable per BMC)
    severity    = Column(String(32))         # "OK", "Warning", "Critical"
    message     = Column(Text)
    message_id  = Column(String(255))
    sensor_type = Column(String(128))
    created_at  = Column(DateTime, index=True)
    raw_json    = Column(_JSONB_or_JSON())

    server = relationship("Server", back_populates="log_entries")

    def to_dict(self):
        return {
            "id": str(self.id),
            "log_service": self.log_service,
            "entry_id": self.entry_id,
            "severity": self.severity,
            "message": self.message,
            "message_id": self.message_id,
            "sensor_type": self.sensor_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Operation(db.Model):
    """A user-requested, asynchronous vendor operation and its result."""

    __tablename__ = "operations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    operation_type = Column(String(64), nullable=False, index=True)
    vendor = Column(String(128))
    status = Column(
        Enum(OperationStatus, name="operation_status_enum"),
        nullable=False, default=OperationStatus.QUEUED, index=True,
    )
    progress_percent = Column(Integer)
    status_message = Column(Text)
    error_message = Column(Text)
    result_path = Column(Text)
    result_filename = Column(String(512))
    result_content_type = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    server = relationship("Server", back_populates="operations")

    def to_dict(self):
        return {
            "id": self.id,
            "server_id": str(self.server_id),
            "operation_type": self.operation_type,
            "vendor": self.vendor,
            "status": self.status.value if self.status else None,
            "progress_percent": self.progress_percent,
            "status_message": self.status_message,
            "error_message": self.error_message,
            "result_filename": self.result_filename,
            "result_content_type": self.result_content_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Alert
# ─────────────────────────────────────────────────────────────────────────────

class Alert(db.Model):
    """Active and resolved alerts.  dedupe_key prevents alert storms: a
    repeated condition bumps occurrence_count instead of inserting a new row."""

    __tablename__ = "alerts"

    id              = Column(
        _UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id       = Column(
        _UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    category        = Column(String(64), nullable=False)
    severity        = Column(Enum(AlertSeverity, name="alert_severity_enum"), nullable=False)
    message         = Column(Text, nullable=False)
    source_property = Column(String(512))  # @odata.id of the offending resource
    dedupe_key      = Column(String(64), nullable=False, index=True)
    acknowledged    = Column(Boolean, default=False)
    resolved        = Column(Boolean, default=False, index=True)
    occurrence_count= Column(Integer, default=1)
    first_occurred_at = Column(DateTime, default=datetime.utcnow)
    last_occurred_at  = Column(DateTime, default=datetime.utcnow)
    resolved_at     = Column(DateTime)
    acknowledged_at = Column(DateTime)

    server = relationship("Server", back_populates="alerts")

    def to_dict(self):
        return {
            "id": str(self.id),
            "server_id": str(self.server_id),
            "category": self.category,
            "severity": self.severity.value if self.severity else None,
            "message": self.message,
            "source_property": self.source_property,
            "acknowledged": self.acknowledged,
            "resolved": self.resolved,
            "occurrence_count": self.occurrence_count,
            "first_occurred_at": self.first_occurred_at.isoformat() if self.first_occurred_at else None,
            "last_occurred_at": self.last_occurred_at.isoformat() if self.last_occurred_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
