"""
redfish/collectors/battery.py
==============================
Redfish resources consumed
---------------------------
Standard paths (all vendors):
  - Chassis/{id}/Batteries (collection, Redfish 2021+)
  - Chassis/{id}/PowerSubsystem/Batteries (Redfish 2021+ subsystem shape)
  - Chassis/{id}/Power -> Batteries[] (embedded array, 2020.x and earlier)

Storage controller batteries (standard):
  - Systems/{id}/Storage -> StorageControllers[].BackupPowerSourceStatus
  - Systems/{id}/Storage -> Controllers collection members

Dell OEM paths (iDRAC 9/10 only):
  - /redfish/v1/Dell/Chassis/{id}/DellControllerBatteryCollection
  - /redfish/v1/Dell/Systems/{id}/DellPresenceAndStatusSensorCollection

HPE OEM paths (iLO 4 Gen9/Gen8):
  - Systems/{id}/SmartStorage/ArrayControllers/{id}/BackupUnits/
    (discovered via 'storage_hpe' topology key, not hardcoded)
    Individual BackupUnit body fields:
      Name, Status, ChargeLevelPercent, ErrorCode,
      RemainingChargeTimeSeconds, PresentedCapacityWattHours
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory
from redfish.dell_compat import is_dell, get_idrac_generation, dell_oem_battery_paths
from redfish.hpe_compat import is_hpe, get_hpe_battery_units_uri, normalize_hpe_battery

logger = logging.getLogger(__name__)



def _get_dell_oem_batteries(client, chassis_uri, system_uri, service_root=None):
    """
    Attempt to read battery information from Dell iDRAC OEM-specific endpoints.

    These paths only exist on iDRAC 9 and iDRAC 10.  iDRAC 8 does not expose
    them (returns 404/501).  The generation is detected from the service root
    so we can skip the calls entirely on iDRAC 8, avoiding unnecessary 404s.

    All log calls here are at DEBUG level because a 404 response on these OEM
    paths is completely expected for non-iDRAC-9+ firmware and must not fill
    production logs with spurious warnings on every 30-second poll cycle.
    """
    members = []
    chassis_id = chassis_uri.rstrip("/").split("/")[-1]
    system_id = system_uri.rstrip("/").split("/")[-1] if system_uri else None

    # Detect generation so we skip OEM calls that are known to 404 on iDRAC 8.
    generation = get_idrac_generation(service_root or {})
    paths = dell_oem_battery_paths(chassis_id, system_id, generation)

    if not paths:
        logger.debug(
            "Skipping Dell OEM battery paths for chassis %s (generation=%r — no OEM paths for this generation)",
            chassis_id, generation,
        )
        return members

    for path_info in paths:
        uri = path_info["uri"]
        kind = path_info["kind"]
        logger.debug("Checking Dell OEM battery collection (%s): %s", kind, uri)
        coll = client.get(uri)
        if not coll:
            logger.debug("No Dell OEM battery collection at %s (404 or unsupported)", uri)
            continue

        if kind == "controller_battery":
            for m in coll.get("Members", []):
                member_uri = m.get("@odata.id")
                body = client.get(member_uri) if member_uri else None
                if body:
                    normalized = {
                        "@odata.id": member_uri,
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
                    logger.debug("Found Dell OEM controller battery: %s", body.get("Name"))

        elif kind == "cmos_battery":
            for m in coll.get("Members", []):
                member_uri = m.get("@odata.id")
                body = client.get(member_uri) if member_uri else None
                if body and "battery" in (body.get("ElementName") or "").lower():
                    normalized = {
                        "@odata.id": member_uri,
                        "Name": body.get("ElementName", "CMOS Battery"),
                        "Status": {
                            "Health": "OK" if body.get("PrimaryStatus") == "OK" else "Warning",
                            "State": body.get("EnabledState", "Unknown"),
                        },
                        "_source": "Dell OEM DellPresenceAndStatusSensor",
                        "_raw_oem": body,
                    }
                    members.append(normalized)
                    logger.debug("Found Dell OEM CMOS battery: %s", body.get("ElementName"))

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

        if not battery_members and is_dell(server):
            # Dell OEM battery paths only exist on iDRAC 9 and iDRAC 10.
            # Gate this call on vendor so HPE/Lenovo/Supermicro servers do not
            # incur two unnecessary HTTP 404 round-trips per chassis per poll.
            service_root = topology.get("service_root") or {}
            logger.debug("No standard batteries for %s — trying Dell OEM paths", chassis_uri)
            battery_members = _get_dell_oem_batteries(
                client, chassis_uri, system_uri, service_root=service_root
            )

        if not battery_members and is_hpe(server):
            # HPE iLO 4 (Gen9/Gen8): batteries are under SmartStorage
            # ArrayController BackupUnits collections.  The SmartStorage root
            # URI was injected into the topology by discovery.py as
            # 'storage_hpe' (extracted from Oem.Hp/Hpe.SmartStorage).
            # We iterate per_system to find all SmartStorage roots and
            # harvest every controller's BackupUnits collection.
            logger.debug(
                "No standard batteries for %s — trying HPE SmartStorage BackupUnits",
                chassis_uri,
            )
            battery_members = _get_hpe_smartstorage_batteries(
                client, topology, seen_uris
            )

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
    # Two shapes of storage controller exist across Redfish schema versions:
    #
    #  Redfish 2019.x / iDRAC 9 shape:
    #    Storage/{id} body has a top-level "StorageControllers" array,
    #    each entry being a controller sub-object (not a linked resource).
    #
    #  Redfish 2021+ / iDRAC 10 shape:
    #    Storage/{id} body has a "Controllers" link pointing to a collection;
    #    each member is a full StorageController resource.
    #
    #  iDRAC 8 shape:
    #    "StorageControllers" array may be absent; controllers may be exposed
    #    inline under the storage body directly, or not at all via Redfish.
    #    We check all known shapes and fall back gracefully when absent.
    for system_uri, links in topology.get("per_system", {}).items():
        storage_uri = links.get("storage")
        if not storage_uri:
            continue
        for storage_body in collection_members(client, storage_uri):
            storage_odata = storage_body.get("@odata.id", "")

            # Shape 1: top-level StorageControllers array (iDRAC 9 / Redfish 2019.x)
            ctrl_list = storage_body.get("StorageControllers", [])

            # Shape 2: Controllers collection link (Redfish 2021+ / iDRAC 10)
            if not ctrl_list:
                controllers_link = (storage_body.get("Controllers") or {}).get("@odata.id")
                if controllers_link:
                    for ctrl_member in collection_members(client, controllers_link):
                        ctrl_list.append(ctrl_member)

            # Shape 3: controller info embedded directly on the storage body (iDRAC 8 fallback)
            # iDRAC 8 sometimes puts BackupPowerSourceStatus at the storage body level.
            if not ctrl_list:
                if storage_body.get("BackupPowerSourceStatus"):
                    ctrl_list = [storage_body]  # treat the storage body itself as a controller

            for ctrl in ctrl_list:
                status = ctrl.get("BackupPowerSourceStatus")
                if status and status != "NotPresent":
                    ctrl_uri = (
                        ctrl.get("@odata.id")
                        or f"{storage_odata}#controller#{ctrl.get('MemberId', ctrl.get('Id', ''))}"
                    )
                    bat_uri = f"{ctrl_uri}#battery"
                    if bat_uri not in seen_uris:
                        seen_uris.add(bat_uri)
                        name = f"{ctrl.get('Model', ctrl.get('Name', 'Storage Controller'))} Backup Battery"
                        components.append(component(
                            ComponentCategory.BATTERY, bat_uri, name, ctrl,
                            location=ctrl.get("Location")
                        ))
                        readings.append(reading(
                            "battery_health", name,
                            100 if status in ("Present", "Ready", "OK") else 0, "%"
                        ))


    # If no battery components were found from any path, mark as not supported
    # so the UI shows "Not Supported" instead of a misleading "0" count.
    real_components = [c for c in components if c.get("odata_id") != "meta:unsupported"]
    if not real_components:
        components = [unsupported_marker(ComponentCategory.BATTERY)]

    readings = [r for r in readings if r]
    return components, readings


def _get_hpe_smartstorage_batteries(client, topology: dict, seen_uris: set) -> list:
    """Collect batteries from HPE SmartStorage ArrayController BackupUnits.

    Called only when:
      - No standard Redfish battery path returned results.
      - ``is_hpe(server)`` is True (HPE iLO 4/5).

    HPE iLO 4 exposes Smart Storage batteries (energy-pack / flash-backed write
    cache modules) as a ``BackupUnits`` collection linked from each
    ArrayController body:

    .. code-block:: text

        SmartStorage root  (topology["storage_hpe"])
          └─ Links.ArrayControllers  @odata.id
               └─ ArrayControllers collection
                    └─ member: ArrayController body
                         └─ Links.BackupUnits  @odata.id
                              └─ BackupUnits collection
                                   └─ member: BackupUnit body
                                        ├─ Name
                                        ├─ Status.Health / .State
                                        ├─ ChargeLevelPercent
                                        ├─ ErrorCode  (0 = healthy)
                                        ├─ RemainingChargeTimeSeconds
                                        └─ PresentedCapacityWattHours

    Each member body is normalized via ``normalize_hpe_battery()`` so the
    main collection loop in ``collect()`` can process it identically to a
    standard battery resource.

    Parameters
    ----------
    client     : RedfishClient instance.
    topology   : The topology dict returned by ``discover_topology()``.
    seen_uris  : Shared set used for deduplication; mutated in-place.

    Returns
    -------
    List of normalized battery body dicts (may be empty).
    """
    members = []

    for system_uri, sys_links in topology.get("per_system", {}).items():
        smart_storage_uri = sys_links.get("storage_hpe")
        if not smart_storage_uri:
            logger.debug(
                "HPE SmartStorage battery: no 'storage_hpe' link for system %s — "
                "SmartStorage OEM path was not discovered (check Oem.Hp/Hpe.SmartStorage "
                "in the ComputerSystem body for this server)",
                system_uri,
            )
            continue

        logger.debug(
            "HPE SmartStorage battery: querying root %s", smart_storage_uri
        )
        smart_storage = client.get(smart_storage_uri)
        if not smart_storage:
            logger.debug(
                "HPE SmartStorage battery: GET %s returned nothing", smart_storage_uri
            )
            continue

        # ArrayControllers link is nested in Links on the SmartStorage root body
        ctrl_coll_uri = (
            smart_storage.get("Links") or {}
        ).get("ArrayControllers", {}).get("@odata.id")
        if not ctrl_coll_uri:
            logger.debug(
                "HPE SmartStorage battery: no Links.ArrayControllers on %s",
                smart_storage_uri,
            )
            continue

        for ctrl_body in collection_members(client, ctrl_coll_uri):
            ctrl_uri = ctrl_body.get("@odata.id", "")
            ctrl_name = ctrl_body.get("Model") or ctrl_body.get("Name") or "Smart Array Controller"

            backup_units_uri = get_hpe_battery_units_uri(ctrl_body)
            if not backup_units_uri:
                logger.debug(
                    "HPE SmartStorage battery: controller %s has no BackupUnits link "
                    "(may have no battery / energy pack installed)",
                    ctrl_uri,
                )
                continue

            logger.debug(
                "HPE SmartStorage battery: fetching BackupUnits at %s", backup_units_uri
            )
            for bat_body in collection_members(client, backup_units_uri):
                u = bat_body.get("@odata.id")
                if u and u in seen_uris:
                    continue
                if u:
                    seen_uris.add(u)

                normalized = normalize_hpe_battery(bat_body)
                # Tag the battery with its parent controller name so the UI
                # can display something meaningful in the "Controller" column.
                normalized.setdefault("_controller", ctrl_name)
                members.append(normalized)
                logger.debug(
                    "HPE SmartStorage battery: found '%s' on controller '%s' "
                    "(Health=%s, Charge=%s%%)",
                    normalized.get("Name"), ctrl_name,
                    (normalized.get("Status") or {}).get("Health"),
                    normalized.get("ChargeLevelPercent"),
                )

    if not members:
        logger.debug(
            "HPE SmartStorage battery: no BackupUnit members found across all "
            "SmartStorage roots — server may have no Smart Storage Battery / "
            "energy pack installed, or the BackupUnits collection is empty"
        )
    return members
