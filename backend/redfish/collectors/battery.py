"""
redfish/collectors/battery.py
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def _get_dell_oem_batteries(client, chassis_uri, system_uri):
    members = []
    chassis_id = chassis_uri.rstrip("/").split("/")[-1]
    
    dell_battery_collection = f"/redfish/v1/Dell/Chassis/{chassis_id}/DellControllerBatteryCollection"
    logger.info("Checking Dell OEM battery collection: %s", dell_battery_collection)
    coll = client.get(dell_battery_collection)
    if coll and coll.get("Members"):
        for m in coll["Members"]:
            uri = m.get("@odata.id")
            body = client.get(uri) if uri else None
            if body:
                normalized = {
                    "@odata.id": uri,
                    "Name": body.get("Name", "RAID Controller Battery"),
                    "Status": {
                        "Health": body.get("PrimaryStatus", "Unknown"),
                        "State": "Enabled" if body.get("PrimaryStatus") else "Unknown"
                    },
                    "RAIDState": body.get("RAIDState"),
                    "FQDD": body.get("FQDD"),
                    "_source": "Dell OEM DellControllerBattery",
                    "_raw_oem": body,
                }
                members.append(normalized)
                logger.info("Found Dell OEM controller battery: %s", body.get("Name"))
    else:
        logger.info("No Dell OEM controller battery at %s", dell_battery_collection)

    if system_uri:
        system_id = system_uri.rstrip("/").split("/")[-1]
        sensor_uri = f"/redfish/v1/Dell/Systems/{system_id}/DellPresenceAndStatusSensorCollection"
        logger.info("Checking Dell presence sensors: %s", sensor_uri)
        sensor_coll = client.get(sensor_uri)
        if sensor_coll:
            for m in sensor_coll.get("Members", []):
                uri = m.get("@odata.id")
                body = client.get(uri) if uri else None
                if body and "battery" in (body.get("ElementName") or "").lower():
                    normalized = {
                        "@odata.id": uri,
                        "Name": body.get("ElementName", "CMOS Battery"),
                        "Status": {
                            "Health": "OK" if body.get("PrimaryStatus") == "OK" else "Warning",
                            "State": body.get("EnabledState", "Unknown"),
                        },
                        "_source": "Dell OEM DellPresenceAndStatusSensor",
                        "_raw_oem": body,
                    }
                    members.append(normalized)
                    logger.info("Found Dell OEM CMOS battery: %s", body.get("ElementName"))
    return members


def collect(client, server, topology):
    components, readings = [], []
    seen_uris = set()

    systems = topology.get("systems", [])
    system_uri = systems[0] if systems else None

    for chassis_uri, links in topology.get("per_chassis", {}).items():
        battery_members = []

        # Standard paths
        for link_key in ["batteries", "power_subsystem", "power"]:
            uri = links.get(link_key)
            if not uri:
                continue
            body = client.get(uri)
            if not body:
                continue
            if link_key == "power_subsystem":
                bat_link = (body.get("Batteries") or {}).get("@odata.id")
                if bat_link:
                    coll = client.get(bat_link)
                    for m in (coll or {}).get("Members", []):
                        u = m.get("@odata.id")
                        if u and u not in seen_uris:
                            b = client.get(u)
                            if b:
                                seen_uris.add(u)
                                battery_members.append(b)
            elif link_key == "batteries":
                for m in body.get("Members", []):
                    u = m.get("@odata.id")
                    if u and u not in seen_uris:
                        b = client.get(u)
                        if b:
                            seen_uris.add(u)
                            battery_members.append(b)
            elif link_key == "power":
                for b in body.get("Batteries", []):
                    u = b.get("@odata.id")
                    if not u or u not in seen_uris:
                        if u:
                            seen_uris.add(u)
                        battery_members.append(b)

        if not battery_members:
            logger.info("No standard batteries for %s — trying Dell OEM", chassis_uri)
            battery_members = _get_dell_oem_batteries(client, chassis_uri, system_uri)

        for b in battery_members:
            odata_id = b.get("@odata.id") or f"{chassis_uri}#battery#{b.get('Name', 'unknown')}"
            components.append(component(
                ComponentCategory.BATTERY, odata_id, b.get("Name", "Battery"), b,
                location=None,
            ))
            cap = b.get("StateOfHealthPercent", {})
            if isinstance(cap, dict):
                readings.append(reading("battery_health_percent", b.get("Name"), cap.get("Reading"), "%"))
            charge = b.get("ChargePercent")
            if charge is not None:
                readings.append(reading("battery_charge_percent", b.get("Name"), charge, "%"))

    # ── Storage Controller Batteries ──────────────────────────────────────
    for system_uri, links in topology.get("per_system", {}).items():
        storage_uri = links.get("storage")
        if storage_uri:
            for storage_sys in collection_members(client, storage_uri):
                for ctrl in storage_sys.get("StorageControllers", []):
                    status = ctrl.get("BackupPowerSourceStatus")
                    if status and status != "NotPresent":
                        uri = ctrl.get('@odata.id') or f"{storage_sys.get('@odata.id')}#controller#{ctrl.get('MemberId', '')}"
                        bat_uri = f"{uri}#battery"
                        if bat_uri not in seen_uris:
                            seen_uris.add(bat_uri)
                            name = f"{ctrl.get('Model', ctrl.get('Name', 'Storage Controller'))} Backup Battery"
                            components.append(component(
                                ComponentCategory.BATTERY, bat_uri, name, ctrl,
                                location=ctrl.get("Location")
                            ))
                            readings.append(reading(
                                "battery_health", name, 100 if status in ("Present", "Ready", "OK") else 0, "%"
                            ))

    # If no battery components were found from any path, mark as not supported
    # so the UI shows "Not Supported" instead of a misleading "0" count.
    real_components = [c for c in components if c.get("odata_id") != "meta:unsupported"]
    if not real_components:
        components = [unsupported_marker(ComponentCategory.BATTERY)]

    readings = [r for r in readings if r]
    return components, readings

