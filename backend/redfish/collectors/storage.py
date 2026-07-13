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

Additional fields collected per drive (extended):
- Predictive Failure  : Drive.FailurePredicted (standard) or
                        OEM.Dell.DellPhysicalDisk.PredictiveFailureState
- Block Size          : Drive.BlockSizeBytes (standard)
- Product ID          : Drive.Model -> Drive.SKU (standard fallback chain)
- Device Description  : Drive.Description (standard)
- Controller          : resolved from the controller body passed at collection time
"""
import logging
from .common import component, reading, collection_members
from database.models import ComponentCategory

logger = logging.getLogger(__name__)

_BYTES_TO_GB = 1 / (1024 ** 3)


def collect(client, server, topology):
    components, readings = [], []
    seen_drive_uris = set()

    for system_uri, links in topology.get("per_system", {}).items():

        # ── Full Storage schema ─────────────────────────────────────────
        storage_uri = links.get("storage")
        if storage_uri:
            for ctrl in collection_members(client, storage_uri):
                ctrl_id   = ctrl.get("@odata.id")
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
                        _collect_drive(client, uri, components, readings, ctrl_name)
                elif isinstance(drives_link, dict):
                    coll_uri = drives_link.get("@odata.id")
                    if coll_uri:
                        for d in collection_members(client, coll_uri):
                            uri = d.get("@odata.id")
                            if not uri or uri in seen_drive_uris:
                                continue
                            seen_drive_uris.add(uri)
                            _collect_drive_body(d, components, readings, ctrl_name)

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

        # ── HPE SmartStorage fallback (iLO 4/5) ─────────────────────────
        hpe_uri = links.get("storage_hpe")
        if hpe_uri:
            smart_storage = client.get(hpe_uri)
            if smart_storage:
                controllers_link = (smart_storage.get("Links") or {}).get("ArrayControllers", {}).get("@odata.id")
                if controllers_link:
                    for ctrl in collection_members(client, controllers_link):
                        ctrl_id = ctrl.get("@odata.id")
                        if not ctrl_id:
                            continue
                        ctrl_name = ctrl.get("Name") or ctrl.get("Model") or "Smart Array Controller"
                        components.append(component(
                            ComponentCategory.STORAGE_CONTROLLER, ctrl_id, ctrl_name, ctrl,
                        ))
                        
                        # Drives
                        drives_link = (ctrl.get("Links") or {}).get("PhysicalDrives", {}).get("@odata.id")
                        if drives_link:
                            for d in collection_members(client, drives_link):
                                uri = d.get("@odata.id")
                                if uri and uri not in seen_drive_uris:
                                    seen_drive_uris.add(uri)
                                    _collect_drive_body(d, components, readings)
                                    
                        # Logical Drives
                        logical_link = (ctrl.get("Links") or {}).get("LogicalDrives", {}).get("@odata.id")
                        if logical_link:
                            for vol in collection_members(client, logical_link):
                                vol_id = vol.get("@odata.id")
                                if vol_id:
                                    components.append(component(
                                        ComponentCategory.STORAGE_VOLUME, vol_id,
                                        vol.get("Name") or vol.get("LogicalDriveName") or "Logical Drive", vol,
                                    ))

        # ── Chassis-level drives ────────────────────────────────────────
        for chassis_uri, chassis_links in topology.get("per_chassis", {}).items():
            drives_uri = chassis_links.get("drives")
            if drives_uri:
                for drive in collection_members(client, drives_uri):
                    uri = drive.get("@odata.id")
                    if not uri or uri in seen_drive_uris:
                        continue
                    seen_drive_uris.add(uri)
                    _collect_drive_body(drive, components, readings, controller_name=None)

    readings = [r for r in readings if r]
    return components, readings


def _collect_drive(client, uri, components, readings, controller_name=None):
    body = client.get(uri)
    if not body:
        logger.debug("Could not fetch drive body at %s", uri)
        return
    _collect_drive_body(body, components, readings, controller_name)


def _extract_additional_drive_properties(body, controller_name):
    """
    Extract the five additional properties required:
      1. Predictive Failure
      2. Block Size
      3. Product ID
      4. Device Description
      5. Controller

    Field mapping:
      - predictive_failure: Drive.FailurePredicted (standard Redfish)
            fallback: OEM.Dell.DellPhysicalDisk.PredictiveFailureState
            Chosen because FailurePredicted is the standard field; Dell OEM
            provides a more descriptive string ("SmartAlertAbsent") as fallback.
      - block_size_bytes:   Drive.BlockSizeBytes (standard Redfish)
            No OEM fallback needed — universally supported.
      - product_id:         Drive.Model (standard) -> Drive.SKU (standard)
            Model matches iDRAC "Product ID" field exactly (e.g. ST1200MM0108).
            SKU used as secondary fallback per requirements.
      - device_description: Drive.Description (standard Redfish)
            Maps directly to iDRAC "Device Description" field.
            No OEM fallback needed.
      - controller:         passed in from the parent controller's Name field.
            Not available in the Drive resource itself — must be resolved
            from the controller that linked to this drive.
    """
    dell_oem = (body.get("Oem") or {}).get("Dell") or {}
    dell_disk = dell_oem.get("DellPhysicalDisk") or {}

    # 1. Predictive Failure
    predictive_failure = body.get("FailurePredicted")
    if predictive_failure is None:
        # Dell OEM fallback: PredictiveFailureState is a string
        # "SmartAlertAbsent" = no failure predicted, anything else = alert
        oem_state = dell_disk.get("PredictiveFailureState")
        if oem_state is not None:
            predictive_failure = oem_state != "SmartAlertAbsent"
            logger.debug(
                "FailurePredicted not in standard fields for %s — "
                "using Dell OEM PredictiveFailureState: %s",
                body.get("@odata.id"), oem_state
            )
        else:
            logger.debug(
                "FailurePredicted unavailable for %s (no standard or OEM field)",
                body.get("@odata.id")
            )

    # 2. Block Size
    block_size_bytes = body.get("BlockSizeBytes")
    if block_size_bytes is None:
        logger.debug("BlockSizeBytes unavailable for %s", body.get("@odata.id"))

    # 3. Product ID — Model -> SKU fallback chain
    product_id = body.get("Model") or body.get("SKU")
    if product_id is None:
        logger.debug("Product ID unavailable for %s (no Model or SKU)", body.get("@odata.id"))

    # 4. Device Description
    device_description = body.get("Description")
    if device_description is None:
        logger.debug("Description unavailable for %s", body.get("@odata.id"))

    # 5. Controller — passed in from parent, not in drive body
    controller = controller_name
    if controller is None:
        logger.debug("Controller name not passed for drive %s", body.get("@odata.id"))

    return {
        "predictive_failure":  predictive_failure,
        "block_size_bytes":    block_size_bytes,
        "product_id":          product_id,
        "device_description":  device_description,
        "controller":          controller,
    }


def _collect_drive_body(body, components, readings, controller_name=None):
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

    # Merge additional properties into the raw body dict so the frontend
    # receives them automatically via raw_json without any API changes.
    extra = _extract_additional_drive_properties(body, controller_name)
    enriched_body = {**body, **extra}

    components.append(component(
        ComponentCategory.STORAGE_DRIVE, odata_id, name, enriched_body, location=slot,
    ))

    # Time-series readings
    wear = body.get("PredictedMediaLifeLeftPercent")
    if wear is not None:
        readings.append(reading("disk_wear", name, wear, "%"))

    temp = body.get("TemperatureCelsius")
    if temp is not None:
        readings.append(reading("disk_temperature", name, temp, "Cel"))
