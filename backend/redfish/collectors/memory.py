"""
redfish/collectors/memory.py
==============================
Redfish resources consumed
---------------------------
- Systems/{id}/Memory (collection) -> Systems/{id}/Memory/{dimm}

Every member is captured verbatim in raw_json (Capacity, Manufacturer,
SerialNumber, PartNumber, OperatingSpeedMhz, AllowedSpeedsMHz, RankCount,
ErrorCorrection, MemoryDeviceType, FirmwareRevision, MemoryLocation,
Status, and any OEM temperature/error-count extensions). Empty DIMM slots
(State=Absent) are still surfaced so the UI can show the full slot map.
"""
from .common import component, reading, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for system_uri, links in topology.get("per_system", {}).items():
        memory_uri = links.get("memory")
        if not memory_uri:
            continue
        for dimm in collection_members(client, memory_uri):
            odata_id = dimm.get("@odata.id")
            location = None
            loc = dimm.get("MemoryLocation") or {}
            if loc:
                location = f"Socket {loc.get('Socket')} Channel {loc.get('Channel')} Slot {loc.get('Slot')}"
            components.append(component(
                ComponentCategory.MEMORY, odata_id, dimm.get("Name", "DIMM"), dimm, location=location,
            ))

            temp = (dimm.get("Oem") or {}).get("Temperature")
            if temp is not None:
                readings.append(reading("memory_temperature", dimm.get("Name"), temp, "Cel"))

            # DIMM error counters live in a separate Metrics sub-resource on
            # most implementations (Systems/.../Memory/{dimm}/Metrics), not
            # embedded inline - follow the link if present.
            metrics_uri = (dimm.get("Metrics") or {}).get("@odata.id")
            if metrics_uri:
                metrics_body = client.get(metrics_uri)
                if metrics_body:
                    correctable = metrics_body.get("HealthData", {}).get("CorrectableECCErrorCount")
                    if correctable is not None:
                        readings.append(reading("memory_errors", dimm.get("Name"), correctable, "count"))

    readings = [r for r in readings if r]
    return components, readings
