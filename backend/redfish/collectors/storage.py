"""
redfish/collectors/storage.py
===============================
Redfish resources consumed
---------------------------
- Systems/{id}/Storage (collection) -> Storage/{controller}
  - Storage/{controller}/Drives (collection) -> Drives/{id}
  - Storage/{controller}/Volumes (collection) -> Volumes/{id}  (RAID virtual disks)
- Systems/{id}/SimpleStorage (collection) -> SimpleStorage/{id} (fallback for
  BMCs that don't support the full Storage schema, e.g. some older HPE iLO)

For every storage controller we collect the full controller body (including
RAID level capabilities, supported protocols, firmware version).  For every
physical drive we collect ALL available properties: Capacity, Manufacturer,
Model, SerialNumber, MediaType, Protocol, FirmwareVersion, FailurePredicted,
PredictedMediaLifeLeftPercent (SSD wear), PowerOnHours, RotationSpeedRPM,
CapableSpeedGbs, CapacityBytes, Status, and any OEM fields.  For every
virtual disk (Volume) we capture the RAID type, capacity, and status.
"""
from .common import component, reading, collection_members
from database.models import ComponentCategory

_BYTES_TO_GB = 1 / (1024 ** 3)


def collect(client, server, topology):
    components, readings = [], []
    seen_drive_uris = set()

    for system_uri, links in topology.get("per_system", {}).items():

        # ── Full Storage schema ─────────────────────────────────────────
        storage_uri = links.get("storage")
        if storage_uri:
            for ctrl in collection_members(client, storage_uri):
                ctrl_id  = ctrl.get("@odata.id")
                ctrl_name = ctrl.get("Name") or ctrl.get("Id") or "Storage Controller"

                # The controller itself
                components.append(component(
                    ComponentCategory.STORAGE_CONTROLLER, ctrl_id, ctrl_name, ctrl,
                ))

                # Physical drives linked from controller
                drives_link = (ctrl.get("Drives") or [])
                if isinstance(drives_link, list):
                    for d_ref in drives_link:
                        uri = d_ref.get("@odata.id") if isinstance(d_ref, dict) else None
                        if not uri or uri in seen_drive_uris:
                            continue
                        seen_drive_uris.add(uri)
                        _collect_drive(client, uri, components, readings)
                elif isinstance(drives_link, dict):
                    # Some firmware wraps it as a collection link
                    coll_uri = drives_link.get("@odata.id")
                    if coll_uri:
                        for d in collection_members(client, coll_uri):
                            uri = d.get("@odata.id")
                            if not uri or uri in seen_drive_uris:
                                continue
                            seen_drive_uris.add(uri)
                            _collect_drive_body(d, components, readings)

                # Virtual disks / Volumes
                volumes_link = (ctrl.get("Volumes") or {}).get("@odata.id")
                if volumes_link:
                    for vol in collection_members(client, volumes_link):
                        vol_id = vol.get("@odata.id")
                        components.append(component(
                            ComponentCategory.STORAGE_VOLUME, vol_id,
                            vol.get("Name") or vol.get("Id") or "Volume", vol,
                        ))

        # ── SimpleStorage fallback ──────────────────────────────────────
        simple_uri = links.get("simple_storage")
        if simple_uri:
            for ss in collection_members(client, simple_uri):
                for dev in (ss.get("Devices") or []):
                    odata_id = f"{ss.get('@odata.id')}#device#{dev.get('Name')}"
                    if odata_id in seen_drive_uris:
                        continue
                    seen_drive_uris.add(odata_id)
                    components.append(component(
                        ComponentCategory.STORAGE_DRIVE, odata_id,
                        dev.get("Name") or "Device", dev,
                    ))

        # ── Chassis-level drives (some BMCs link drives at chassis level) ─
        for chassis_uri, chassis_links in topology.get("per_chassis", {}).items():
            drives_uri = chassis_links.get("drives")
            if drives_uri:
                for drive in collection_members(client, drives_uri):
                    uri = drive.get("@odata.id")
                    if not uri or uri in seen_drive_uris:
                        continue
                    seen_drive_uris.add(uri)
                    _collect_drive_body(drive, components, readings)

    readings = [r for r in readings if r]
    return components, readings


def _collect_drive(client, uri, components, readings):
    body = client.get(uri)
    if not body:
        return
    _collect_drive_body(body, components, readings)


def _collect_drive_body(body, components, readings):
    odata_id = body.get("@odata.id")
    name = (
        body.get("Name")
        or body.get("Model")
        or body.get("Id")
        or "Drive"
    )
    slot = None
    loc = body.get("PhysicalLocation") or body.get("Location") or {}
    if isinstance(loc, dict):
        slot = (loc.get("PartLocation") or {}).get("ServiceLabel") or loc.get("Label")
    if slot:
        name = f"{name} ({slot})"

    components.append(component(
        ComponentCategory.STORAGE_DRIVE, odata_id, name, body, location=slot,
    ))

    # Time-series
    wear = body.get("PredictedMediaLifeLeftPercent")
    if wear is not None:
        readings.append(reading("disk_wear", name, wear, "%"))

    temp = body.get("TemperatureCelsius")
    if temp is not None:
        readings.append(reading("disk_temperature", name, temp, "Cel"))
