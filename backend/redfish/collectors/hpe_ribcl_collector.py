import logging
from . import common

logger = logging.getLogger(__name__)

def collect_ribcl(client, server_id):
    """
    Collects health data via RIBCL and returns (components_dict, readings_list).
    components_dict maps category name (e.g. 'processor', 'fans') to a list of Component dicts.
    """
    components = {
        "processor": [],
        "memory": [],
        "fans": [],
        "power": [],
        "thermal": [],
        "system": []
    }
    readings = []

    try:
        data = client.get_embedded_health()
    except Exception as e:
        logger.error(f"Failed to get RIBCL data: {e}")
        return components, readings

    _collect_processors(data.get("processors", []), components, server_id)
    _collect_memory(data.get("memory", []), components, server_id)
    _collect_fans(data.get("fans", []), components, readings, server_id)
    _collect_temperatures(data.get("temperatures", []), components, readings, server_id)
    _collect_power_supplies(data.get("power_supplies", []), components, server_id)

    # System Health
    health_status = data.get("health_status", {})
    if health_status:
        components["system"].append(common.component(
            category="system",
            odata_id="/ribcl/system",
            name="System Health",
            health=_ribcl_status_to_health(health_status.get("SYSTEM_BOARD", "OK")),
            state="Enabled",
            raw_json=health_status
        ))

    return components, readings

def _ribcl_status_to_health(status_str):
    s = str(status_str).lower()
    if s in ("ok", "good"):
        return "OK"
    elif s in ("degraded", "warning"):
        return "Warning"
    elif s in ("failed", "critical"):
        return "Critical"
    return None

def _collect_processors(procs, components, server_id):
    for i, p in enumerate(procs):
        label = p.get("LABEL", f"CPU {i}")
        status = p.get("STATUS", p.get("VALUE", "Unknown"))
        components["processor"].append(common.component(
            category="processor",
            odata_id=f"/ribcl/processors/{i}",
            name=label,
            health=_ribcl_status_to_health(status),
            state="Enabled",
            raw_json=p
        ))

def _collect_memory(mems, components, server_id):
    for i, m in enumerate(mems):
        label = m.get("LABEL", f"DIMM {i}")
        status = m.get("STATUS", m.get("VALUE", "Unknown"))
        size = m.get("SIZE", "")
        name = f"{label} {size}".strip()
        components["memory"].append(common.component(
            category="memory",
            odata_id=f"/ribcl/memory/{i}",
            name=name,
            health=_ribcl_status_to_health(status),
            state="Enabled",
            raw_json=m
        ))

def _collect_fans(fans, components, readings, server_id):
    for i, f in enumerate(fans):
        label = f.get("LABEL", f"Fan {i}")
        status = f.get("STATUS", f.get("VALUE", "Unknown"))
        speed = f.get("SPEED")
        components["fans"].append(common.component(
            category="fans",
            odata_id=f"/ribcl/fans/{i}",
            name=label,
            health=_ribcl_status_to_health(status),
            state="Enabled",
            raw_json=f
        ))
        if speed and speed.strip().isdigit():
            readings.append(common.reading(
                metric="fan_speed",
                source_name=label,
                value=float(speed.strip()),
                unit="RPM"
            ))

def _collect_temperatures(temps, components, readings, server_id):
    for i, t in enumerate(temps):
        label = t.get("LABEL", f"Temp {i}")
        status = t.get("STATUS", t.get("VALUE", "Unknown"))
        reading = t.get("CURRENTREADING")
        components["thermal"].append(common.component(
            category="thermal",
            odata_id=f"/ribcl/temps/{i}",
            name=label,
            health=_ribcl_status_to_health(status),
            state="Enabled",
            raw_json=t
        ))
        if reading and reading.strip().isdigit():
            readings.append(common.reading(
                metric="temperature",
                source_name=label,
                value=float(reading.strip()),
                unit="Celsius"
            ))

def _collect_power_supplies(psus, components, server_id):
    for i, p in enumerate(psus):
        label = p.get("LABEL", f"PSU {i}")
        status = p.get("STATUS", p.get("VALUE", "Unknown"))
        components["power"].append(common.component(
            category="power",
            odata_id=f"/ribcl/psu/{i}",
            name=label,
            health=_ribcl_status_to_health(status),
            state="Enabled",
            raw_json=p
        ))
