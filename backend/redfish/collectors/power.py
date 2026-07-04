"""
redfish/collectors/power.py
=============================
Redfish resources consumed
---------------------------
- Chassis/{id}/Power -> PowerSupplies[] (schema 2020.x and earlier - most common)
  Also reads Power.PowerControl[] for system-level consumption
- Chassis/{id}/PowerSubsystem/PowerSupplies (2021.x+ Swordfish schema)
- Chassis/{id}/PowerSubsystem/PowerAllocation (aggregate consumption metrics)

Captures full power supply detail: InputVoltage, InputCurrentAmps,
LastPowerOutputWatts, LineInputVoltage, LineInputVoltageType,
FirmwareVersion, Manufacturer, Model, PartNumber, SerialNumber, Status,
Redundancy, and the system-level wattage readings (PowerConsumedWatts,
AveragePowerUsedWatts, MaxPowerUsedWatts, MinPowerUsedWatts).
"""
from .common import component, reading, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for chassis_uri, links in topology.get("per_chassis", {}).items():

        # ── Legacy Power resource ───────────────────────────────────────
        power_uri = links.get("power")
        if power_uri:
            power_body = client.get(power_uri)
            if power_body:
                # Power supplies
                for psu in power_body.get("PowerSupplies", []):
                    odata_id = (
                        psu.get("@odata.id")
                        or f"{chassis_uri}#psu#{psu.get('MemberId', psu.get('Name'))}"
                    )
                    name     = psu.get("Name") or psu.get("PowerSupplyType") or "PSU"
                    location = psu.get("Location", {}).get("PartLocation", {}).get("ServiceLabel") \
                               if isinstance(psu.get("Location"), dict) else None

                    components.append(component(
                        ComponentCategory.POWER_SUPPLY, odata_id, name, psu, location=location,
                    ))

                    # PSU output wattage time-series
                    out_w = psu.get("LastPowerOutputWatts") or psu.get("PowerOutputWatts")
                    if out_w is not None:
                        readings.append(reading("psu_wattage", name, out_w, "W"))

                # System-level power consumption (PowerControl array)
                for pc in power_body.get("PowerControl", []):
                    consumed = pc.get("PowerConsumedWatts")
                    if consumed is not None:
                        src = pc.get("Name") or "System Power"
                        readings.append(reading("power_consumption", src, consumed, "W"))
                    avg = pc.get("PowerMetrics", {}).get("AverageConsumedWatts")
                    if avg is not None:
                        readings.append(reading("power_avg_w", pc.get("Name") or "System Power", avg, "W"))

        # ── 2021.x PowerSubsystem ────────────────────────────────────────
        power_subsystem_uri = links.get("power_subsystem")
        if power_subsystem_uri:
            ps_body = client.get(power_subsystem_uri)
            if ps_body:
                psu_coll_link = (ps_body.get("PowerSupplies") or {}).get("@odata.id")
                if psu_coll_link:
                    for psu in collection_members(client, psu_coll_link):
                        odata_id = psu.get("@odata.id")
                        name     = psu.get("Name") or "PSU"
                        components.append(component(
                            ComponentCategory.POWER_SUPPLY, odata_id, name, psu,
                        ))
                        out_w = psu.get("OutputPowerWatts")
                        if out_w is not None:
                            readings.append(reading("psu_wattage", name, out_w, "W"))

                # Aggregate allocation metrics
                alloc_link = (ps_body.get("PowerAllocation") or {}).get("@odata.id")
                if alloc_link:
                    alloc = client.get(alloc_link)
                    if alloc:
                        consumed = alloc.get("AllocatedWatts") or alloc.get("RequestedWatts")
                        if consumed is not None:
                            readings.append(reading("power_consumption", "System Power", consumed, "W"))

    readings = [r for r in readings if r]
    return components, readings
