"""
redfish/collectors/dell_wsman_collector.py
============================================
Collector for iDRAC 6 via WS-Man.

Since iDRAC 6 lacks Redfish, we map Dell DCIM classes (fetched via
WsManClient) into the exact same Component and SensorReading structures
that the Redfish collectors produce.  This allows the polling engine to
upsert them into the database seamlessly without schema changes.
"""
import logging
from redfish.dell_wsman import (
    WsManClient,
    query_processors,
    query_memory,
    query_disks,
    query_controllers,
    query_nics,
    query_psus,
    query_firmware,
    query_pci,
    query_logs,
    query_fans,
    query_chassis,
    query_accounts,
)
from database.models import ComponentCategory
from .common import component, reading

logger = logging.getLogger(__name__)


def collect_wsman(client: WsManClient, server_id: str, requested_categories: list[str] | None = None) -> tuple[dict, list]:
    """
    Run WS-Man queries and return (components_by_category, readings).
    
    If requested_categories is provided, only those categories are polled.
    """
    components_by_category = {
        "processor": [],
        "memory": [],
        "storage": [],
        "network": [],
        "power": [],
        "thermal": [],
        "voltage": [],
        "fans": [],
        "firmware": [],
        "pcie_devices": [],
        "chassis": [],
        "security": [],
    }
    readings = []
    logs = []

    if requested_categories is None or "processor" in requested_categories:
        try:
            _collect_processors(client, components_by_category["processor"])
        except Exception as exc:
            logger.exception("WS-Man CPU collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "memory" in requested_categories:
        try:
            _collect_memory(client, components_by_category["memory"])
        except Exception as exc:
            logger.exception("WS-Man memory collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "storage" in requested_categories:
        try:
            _collect_storage(client, components_by_category["storage"])
        except Exception as exc:
            logger.exception("WS-Man storage collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "network" in requested_categories:
        try:
            _collect_network(client, components_by_category["network"])
        except Exception as exc:
            logger.exception("WS-Man network collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "power" in requested_categories:
        try:
            _collect_power(client, components_by_category["power"])
        except Exception as exc:
            logger.exception("WS-Man power collection failed for %s: %s", server_id, exc)

    if requested_categories is None or any(c in requested_categories for c in ["thermal", "voltage"]):
        try:
            _collect_sensors(client, components_by_category, readings)
        except Exception as exc:
            logger.exception("WS-Man sensor collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "fans" in requested_categories:
        try:
            _collect_fans(client, components_by_category["fans"], readings)
        except Exception as exc:
            logger.exception("WS-Man fans collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "firmware" in requested_categories:
        try:
            _collect_firmware(client, components_by_category["firmware"])
        except Exception as exc:
            logger.exception("WS-Man firmware collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "pcie_devices" in requested_categories:
        try:
            _collect_pci(client, components_by_category["pcie_devices"])
        except Exception as exc:
            logger.exception("WS-Man PCI collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "logs" in requested_categories:
        try:
            _collect_logs(client, logs)
        except Exception as exc:
            logger.exception("WS-Man log collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "chassis" in requested_categories:
        try:
            _collect_chassis(client, components_by_category["chassis"])
        except Exception as exc:
            logger.exception("WS-Man chassis collection failed for %s: %s", server_id, exc)

    if requested_categories is None or "security" in requested_categories:
        try:
            _collect_security(client, components_by_category["security"])
        except Exception as exc:
            logger.exception("WS-Man security collection failed for %s: %s", server_id, exc)

    return components_by_category, readings, logs


def _wsman_status_to_health(status_str: str) -> str:
    if not status_str:
        return "OK"
    s = status_str.lower()
    if "ok" in s or "normal" in s or "good" in s:
        return "OK"
    if "warning" in s or "non-critical" in s or "degraded" in s:
        return "Warning"
    if "critical" in s or "failed" in s or "error" in s:
        return "Critical"
    return "OK"


def _collect_processors(client, components: list):
    cpus = query_processors(client)
    for c in cpus:
        try:
            fqdd = c.get("FQDD") or c.get("InstanceID")
            if not fqdd:
                continue
            name = c.get("Model") or fqdd
            components.append(component(
                ComponentCategory.PROCESSOR,
                f"wsman:{fqdd}",
                name,
                c,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man CPU item parsing failed. Python type: %s. Object: %s", type(c), c)


def _collect_memory(client, components: list):
    mem = query_memory(client)
    for m in mem:
        try:
            fqdd = m.get("FQDD") or m.get("InstanceID")
            if not fqdd:
                continue
            
            size_mb_str = m.get("Size")
            try:
                size_mb = int(size_mb_str) if size_mb_str else 0
                name = f"{size_mb // 1024}GB DIMM" if size_mb >= 1024 else f"{size_mb}MB DIMM"
            except ValueError:
                name = "DIMM"
                
            components.append(component(
                ComponentCategory.MEMORY,
                f"wsman:{fqdd}",
                name,
                m,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man memory item parsing failed. Python type: %s. Object: %s", type(m), m)


def _collect_storage(client, components: list):
    ctrls = query_controllers(client)
    for c in ctrls:
        try:
            fqdd = c.get("FQDD") or c.get("InstanceID")
            if not fqdd:
                continue
            name = c.get("ProductName") or c.get("Model") or fqdd
            components.append(component(
                ComponentCategory.STORAGE_CONTROLLER,
                f"wsman:{fqdd}",
                name,
                c,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man storage controller parsing failed. Python type: %s. Object: %s", type(c), c)

    disks = query_disks(client)
    for d in disks:
        try:
            fqdd = d.get("FQDD") or d.get("InstanceID")
            if not fqdd:
                continue
            name = d.get("Model") or d.get("SerialNumber") or fqdd
            components.append(component(
                ComponentCategory.STORAGE_DRIVE,
                f"wsman:{fqdd}",
                name,
                d,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man storage drive parsing failed. Python type: %s. Object: %s", type(d), d)


def _collect_network(client, components: list):
    nics = query_nics(client)
    for n in nics:
        try:
            fqdd = n.get("FQDD") or n.get("InstanceID")
            if not fqdd:
                continue
            name = n.get("ProductName") or n.get("Model") or fqdd
            components.append(component(
                ComponentCategory.NETWORK_INTERFACE,
                f"wsman:{fqdd}",
                name,
                n,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man network item parsing failed. Python type: %s. Object: %s", type(n), n)


def _collect_power(client, components: list):
    psus = query_psus(client)
    for p in psus:
        try:
            fqdd = p.get("FQDD") or p.get("InstanceID")
            if not fqdd:
                continue
            name = p.get("Model") or p.get("PartNumber") or fqdd
            components.append(component(
                ComponentCategory.POWER_SUPPLY,
                f"wsman:{fqdd}",
                name,
                p,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man power item parsing failed. Python type: %s. Object: %s", type(p), p)


def _collect_sensors(client, comp_dict: dict, readings: list):
    sensors = query_sensors(client)
    for s in sensors:
        try:
            fqdd = s.get("FQDD") or s.get("InstanceID")
            if not fqdd:
                continue
            sensor_type = s.get("SensorType")
            name = s.get("ElementName") or fqdd
            val_str = s.get("CurrentReading")
            try:
                val = float(val_str) if val_str is not None else None
            except ValueError:
                val = None

            c = component(
                ComponentCategory.THERMAL_SENSOR,  # default, changed below
                f"wsman:{fqdd}",
                name,
                s,
                location=fqdd,
            )

            if sensor_type == "2":  # Voltage
                c["category"] = ComponentCategory.VOLTAGE_SENSOR
                comp_dict["voltage"].append(c)
                if val is not None:
                    readings.append(reading("voltage", name, val, "V"))
            elif sensor_type == "3":  # Current
                c["category"] = ComponentCategory.POWER_SUPPLY
                comp_dict["power"].append(c)
            elif sensor_type == "4":  # Fan / Tachometer
                c["category"] = ComponentCategory.FAN
                comp_dict["fans"].append(c)
                if val is not None:
                    readings.append(reading("fan_speed_rpm", name, val, "RPM"))
            elif sensor_type == "1":  # Temperature
                c["category"] = ComponentCategory.THERMAL_SENSOR
                comp_dict["thermal"].append(c)
                if val is not None:
                    readings.append(reading("temperature", name, val, "Cel"))
            else:
                # Fallback
                c["category"] = ComponentCategory.THERMAL_SENSOR
                comp_dict["thermal"].append(c)
        except Exception as exc:
            logger.exception("WS-Man sensor item parsing failed. Python type: %s. Object: %s", type(s), s)


def _collect_firmware(client, components: list):
    fw = query_firmware(client)
    for f in fw:
        try:
            fqdd = f.get("FQDD") or f.get("InstanceID")
            if not fqdd:
                continue
            name = f.get("ElementName") or fqdd
            components.append(component(
                ComponentCategory.FIRMWARE,
                f"wsman:{fqdd}",
                name,
                f,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man firmware item parsing failed. Python type: %s. Object: %s", type(f), f)

def _collect_pci(client, components: list):
    pci = query_pci(client)
    for p in pci:
        try:
            fqdd = p.get("FQDD") or p.get("InstanceID")
            if not fqdd:
                continue
            name = p.get("Description") or p.get("ElementName") or fqdd
            components.append(component(
                ComponentCategory.PCIE_DEVICE,
                f"wsman:{fqdd}",
                name,
                p,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man PCI item parsing failed. Python type: %s. Object: %s", type(p), p)

def _collect_logs(client, logs: list):
    log_entries = query_logs(client)
    for l in log_entries:
        try:
            entry_id = l.get("InstanceID") or l.get("RecordID") or l.get("MessageID")
            if not entry_id:
                continue
            
            logs.append({
                "log_service": "wsman:LifecycleLog",
                "entry_id": str(entry_id),
                "severity": _wsman_status_to_health(l.get("Severity")),
                "message": l.get("Message") or l.get("Description", "WS-Man Event"),
                "message_id": l.get("MessageID") or "",
                "sensor_type": l.get("Category") or "",
                "created_raw": l.get("CreationTimeStamp") or l.get("RecordTimeStamp"),
                "raw_json": l,
            })
        except Exception as exc:
            logger.exception("WS-Man log item parsing failed. Python type: %s. Object: %s", type(l), l)


def _collect_fans(client, components: list, readings: list):
    fans = query_fans(client)
    for f in fans:
        try:
            fqdd = f.get("FQDD") or f.get("InstanceID")
            if not fqdd:
                continue
            name = f.get("ElementName") or fqdd
            val_str = f.get("CurrentReading")
            try:
                val = float(val_str) if val_str is not None else None
            except ValueError:
                val = None

            components.append(component(
                ComponentCategory.FAN,
                f"wsman:{fqdd}",
                name,
                f,
                location=fqdd,
            ))
            if val is not None:
                readings.append(reading("fan_speed_rpm", name, val, "RPM"))
        except Exception as exc:
            logger.exception("WS-Man fan item parsing failed. Python type: %s. Object: %s", type(f), f)


def _collect_chassis(client, components: list):
    chassis = query_chassis(client)
    for c in chassis:
        try:
            fqdd = c.get("InstanceID") or c.get("FQDD")
            if not fqdd:
                continue
            name = c.get("ElementName") or c.get("Name") or fqdd
            components.append(component(
                ComponentCategory.CHASSIS,
                f"wsman:{fqdd}",
                name,
                c,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man chassis item parsing failed. Python type: %s. Object: %s", type(c), c)


def _collect_security(client, components: list):
    accounts = query_accounts(client)
    for a in accounts:
        try:
            fqdd = a.get("InstanceID") or a.get("Name")
            if not fqdd:
                continue
            name = a.get("ElementName") or fqdd
            components.append(component(
                ComponentCategory.SECURITY,
                f"wsman:account:{fqdd}",
                name,
                a,
                location=fqdd,
            ))
        except Exception as exc:
            logger.exception("WS-Man account item parsing failed. Python type: %s. Object: %s", type(a), a)

