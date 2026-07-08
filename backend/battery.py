"""
redfish/collectors/battery.py
==============================
Redfish resources consumed
---------------------------
Primary (standard Redfish 2021.x+):
- Chassis/{id}/PowerSubsystem/Batteries

Fallback (older schema):
- Chassis/{id}/Power -> Batteries[]

Dell OEM fallback (R640 / iDRAC firmware < 6.x):
- Dell/Chassis/{id}/DellControllerBattery  (RAID controller cache battery)
- Dell/Systems/{id}/DellPresenceAndStatusSensorCollection (CMOS battery)

Why battery was previously missing:
- The R640 with older iDRAC firmware does not expose standard Redfish
  Battery resources under PowerSubsystem or Power.
- Battery data is only available via Dell OEM extensions.
- The original collector never checked OEM endpoints.
"""
import logging
from .common import component, reading
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def _get_standard_batteries(client, chassis_uri, links):
    """Check standard Redfish 2021.x+ PowerSubsystem/Batteries endpoint."""
    members = []

    power_subsystem_uri = links.get("power_subsystem")
    if power_subsystem_uri:
        ps_body = client.get(power_subsystem_uri)
        if ps_body:
            batteries_link = (ps_body.get("Batteries") or {}).get("@odata.id")
            if batteries_link:
                coll = client.get(batteries_link)
                for m in (coll or {}).get("Members", []):
                    uri = m.get("@odata.id")
                    body = client.get(uri) if uri else None
                    if body:
                        members.append(body)
                logger.debug("Standard PowerSubsystem/Batteries: found %d", len(members))
            else:
                logger.debug("No Batteries link under PowerSubsystem at %s", power_subsystem_uri)
        else:
            logger.debug("Could not fetch PowerSubsystem at %s", power_subsystem_uri)
    else:
        logger.debug("No power_subsystem link for chassis %s", chassis_uri)

    # Older schema: batteries embedded in Power resource
    power_uri = links.get("power")
    if power_uri:
        power_body = client.get(power_uri)
        embedded = (power_body or {}).get("Batteries", [])
        if embedded:
            logger.debug("Found %d batteries in Power resource at %s", len(embedded), power_uri)
            members.extend(embedded)
        else:
            logger.debug("No Batteries array in Power resource at %s", power_uri)

    return members


def _get_dell_oem_batteries(client, chassis_uri, system_uri):
    """
    Check Dell OEM endpoints for battery data.
    These are used on R640/R740 with iDRAC firmware < 6.x where standard
    Redfish battery resources are not exposed.
    """
    members = []

    # 1. RAID controller cache battery
    # Extract chassis ID from URI (e.g. "System.Embedded.1")
    chassis_id = chassis_uri.rstrip("/").split("/")[-1]
    dell_battery_collection = f"/redfish/v1/Dell/Chassis/{chassis_id}/DellControllerBatteryCollection"
    logger.debug("Checking Dell OEM battery collection: %s", dell_battery_collection)

    coll = client.get(dell_battery_collection)
    if coll and coll.get("Members"):
        for m in coll["Members"]:
            uri = m.get("@odata.id")
            body = client.get(uri) if uri else None
            if body:
                # Normalize Dell OEM battery to standard-ish shape
                normalized = {
                    "@odata.id": uri,
                    "Name": body.get("Name", "RAID Controller Battery"),
                    "Health": body.get("PrimaryStatus"),
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
                logger.debug("Found Dell OEM controller battery: %s", body.get("Name"))
    else:
        logger.debug("No Dell OEM controller battery collection at %s", dell_battery_collection)

    # 2. CMOS/system battery via DellPresenceAndStatusSensorCollection
    if system_uri:
        system_id = system_uri.rstrip("/").split("/")[-1]
        sensor_uri = f"/redfish/v1/Dell/Systems/{system_id}/DellPresenceAndStatusSensorCollection"
        logger.debug("Checking Dell presence sensors for CMOS battery: %s", sensor_uri)

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
                        "PrimaryStatus": body.get("PrimaryStatus"),
                        "CurrentState": body.get("CurrentState"),
                        "_source": "Dell OEM DellPresenceAndStatusSensor",
                        "_raw_oem": body,
                    }
                    members.append(normalized)
                    logger.debug("Found Dell OEM CMOS battery sensor: %s", body.get("ElementName"))
        else:
            logger.debug("Could not fetch Dell presence sensors at %s", sensor_uri)

    return members


def _normalize_battery(b, chassis_uri):
    """Extract all useful fields from a battery resource (standard or OEM)."""
    odata_id = b.get("@odata.id") or \
        f"{chassis_uri}#battery#{b.get('MemberId', b.get('Name', 'unknown'))}"

    # Location
    location = None
    loc = b.get("Location")
    if isinstance(loc, dict):
        location = loc.get("PartLocation", {}).get("ServiceLabel") or \
                   loc.get("PartLocation", {}).get("LocationOrdinalValue")

    # Capacity
    cap = b.get("StateOfHealthPercent", {})
    capacity_reading = cap.get("Reading") if isinstance(cap, dict) else None

    return {
        "odata_id": odata_id,
        "name": b.get("Name", "Battery"),
        "location": location,
        "capacity_reading": capacity_reading,
        "charge_percent": b.get("ChargePercent"),
        "raw": b,
    }


def collect(client, server, topology):
    components, readings = [], []

    systems = topology.get("systems", [])
    system_uri = systems[0] if systems else None

    for chassis_uri, links in topology.get("per_chassis", {}).items():
        battery_members = []

        # 1. Try standard Redfish endpoints first
        standard = _get_standard_batteries(client, chassis_uri, links)
        if standard:
            logger.info("Found %d standard Redfish batteries for chassis %s", len(standard), chassis_uri)
            battery_members.extend(standard)
        else:
            logger.info("No standard Redfish batteries found for %s — trying Dell OEM endpoints", chassis_uri)

            # 2. Fall back to Dell OEM endpoints
            oem = _get_dell_oem_batteries(client, chassis_uri, system_uri)
            if oem:
                logger.info("Found %d Dell OEM batteries for chassis %s", len(oem), chassis_uri)
                battery_members.extend(oem)
            else:
                logger.info(
                    "No battery resources found for chassis %s after checking: "
                    "PowerSubsystem/Batteries, Power.Batteries, "
                    "Dell/Chassis/.../DellControllerBatteryCollection, "
                    "Dell/Systems/.../DellPresenceAndStatusSensorCollection",
                    chassis_uri
                )

        for b in battery_members:
            norm = _normalize_battery(b, chassis_uri)

            components.append(component(
                ComponentCategory.BATTERY,
                norm["odata_id"],
                norm["name"],
                norm["raw"],
                location=norm["location"],
            ))

            if norm["capacity_reading"] is not None:
                readings.append(reading(
                    "battery_health_percent", norm["name"], norm["capacity_reading"], "%"
                ))
            if norm["charge_percent"] is not None:
                readings.append(reading(
                    "battery_charge_percent", norm["name"], norm["charge_percent"], "%"
                ))

    readings = [r for r in readings if r]
    return components, readings
