"""
redfish/collectors/firmware.py
================================
Redfish resources consumed
---------------------------
- UpdateService -> FirmwareInventory (collection) -> FirmwareInventory/{id}
  Contains SoftwareIdentity resources for every firmware component:
  BIOS, BMC/iDRAC/iLO, NIC firmware, RAID firmware, drive firmware, PSU
  firmware, CPLD, etc.  Each resource has: Version, Status, Manufacturer,
  Name, SoftwareId (what it applies to), Updateable (bool), RelatedItem.
- UpdateService -> SoftwareInventory (host-side OS software; we collect it
  too for completeness but mark the location accordingly)

iDRAC 7 compatibility (Manager.FirmwareVersion fallback)
---------------------------------------------------------
iDRAC 7 does not expose ``UpdateService``, so the firmware inventory is
unavailable via the standard path.  As a fallback, this collector queries
each Manager resource and synthesizes one firmware Component from the
Manager's ``FirmwareVersion`` field.  This ensures the firmware UI card
shows at minimum the BMC firmware version rather than "Not Supported".

The synthesized component carries ``_synthetic: "ManagerFirmwareVersion"``
in ``raw_json`` and is only created when no UpdateService data was found.
Existing iDRAC 8/9 behavior (full firmware inventory) is completely
unaffected.
"""
import logging
from .common import component, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def collect(client, server, topology):
    components = []
    seen = set()

    # ── Primary path: UpdateService FirmwareInventory / SoftwareInventory ─
    update_svc_uri = topology.get("update_service")
    if update_svc_uri:
        svc = client.get(update_svc_uri)
        if svc:
            for coll_key in ("FirmwareInventory", "SoftwareInventory"):
                coll_link = (svc.get(coll_key) or {}).get("@odata.id")
                if not coll_link:
                    continue
                for item in collection_members(client, coll_link):
                    uri = item.get("@odata.id")
                    if not uri or uri in seen:
                        continue
                    seen.add(uri)
                    name = item.get("Name") or item.get("Id") or "Firmware Component"
                    # Derive a human-readable location from RelatedItem links
                    related = item.get("RelatedItem", [])
                    location = None
                    if related and isinstance(related, list):
                        loc_parts = []
                        for r in related[:2]:
                            if isinstance(r, dict) and r.get("@odata.id"):
                                loc_parts.append(r["@odata.id"].split("/")[-1])
                        if loc_parts:
                            location = ", ".join(loc_parts)

                    components.append(component(
                        ComponentCategory.FIRMWARE, uri, name, item, location=location,
                    ))

    # ── HPE FirmwareInventory fallback (iLO 4/5) ─────────────────────────
    for system_uri, links in topology.get("per_system", {}).items():
        hpe_fw_uri = links.get("firmware_hpe")
        if hpe_fw_uri:
            body = client.get(hpe_fw_uri)
            if body and body.get("Current"):
                for category, items in body["Current"].items():
                    if isinstance(items, list):
                        for idx, item in enumerate(items):
                            if not item or not isinstance(item, dict):
                                continue
                            name = item.get("Name", "Firmware Component")
                            loc = item.get("Location")
                            uri = f"{hpe_fw_uri}#{category}/{idx}"
                            if uri not in seen:
                                seen.add(uri)
                                components.append(component(
                                    ComponentCategory.FIRMWARE, uri, name, item, location=loc,
                                ))

    # ── HP iLO 4 manager firmware (no UpdateService on Redfish v1.0.0) ───
    # On Gen9, /redfish/v1/Managers/1/ contains FirmwareVersion directly.
    # Also try the HP-specific manager FirmwareInventory sub-resource.
    for manager_uri, mgr_links in topology.get("per_manager", {}).items():
        # From manager body FirmwareVersion field (always available on iLO 4)
        mgr_body = client.get(manager_uri)
        if mgr_body:
            fw_ver = mgr_body.get("FirmwareVersion")
            if fw_ver:
                uri = f"{manager_uri}#ilo_firmware"
                if uri not in seen:
                    seen.add(uri)
                    name = mgr_body.get("Name") or "iLO Firmware"
                    components.append(component(
                        ComponentCategory.FIRMWARE, uri, name,
                        {"Version": fw_ver, "Name": name, "Updateable": True,
                         "@odata.id": manager_uri},
                        location="BMC",
                    ))

        # HP manager FirmwareInventory link (iLO 5 and some iLO 4)
        hpe_fw_uri = mgr_links.get("firmware_hpe")
        if hpe_fw_uri:
            body = client.get(hpe_fw_uri)
            if body and body.get("Current"):
                for category, items in body["Current"].items():
                    if isinstance(items, list):
                        for idx, item in enumerate(items):
                            if not item or not isinstance(item, dict):
                                continue
                            name = item.get("Name", "Firmware Component")
                            loc = item.get("Location")
                            uri = f"{hpe_fw_uri}#{category}/{idx}"
                            if uri not in seen:
                                seen.add(uri)
                                components.append(component(
                                    ComponentCategory.FIRMWARE, uri, name, item, location=loc,
                                ))

    # ── iDRAC 7 fallback: synthesize from Manager.FirmwareVersion ────────
    # iDRAC 7 (and some iDRAC 8 firmware) does not expose UpdateService.
    # Query Manager bodies and synthesize one firmware entry per manager
    # so the UI card shows at minimum the BMC firmware version.
    if not components:
        for manager_uri in topology.get("managers", []):
            mgr = client.get(manager_uri)
            if not mgr:
                continue
            fw_ver = mgr.get("FirmwareVersion")
            if not fw_ver:
                continue

            mgr_name = mgr.get("Name") or mgr.get("Id") or "BMC"
            uri = f"{manager_uri}#firmware"
            if uri in seen:
                continue
            seen.add(uri)

            # Extract a clean location label from the manager URI
            location = manager_uri.split("/")[-1] if manager_uri else "Manager"

            fw_body = {
                "@odata.id":  uri,
                "Name":       mgr_name,
                "Version":    fw_ver,
                "SoftwareId": mgr_name,
                "Updateable": False,
                "Status":     {"State": "Enabled", "Health": "OK"},
                # Marker so callers know this was inferred, not from UpdateService.
                "_synthetic": "ManagerFirmwareVersion",
            }
            components.append(component(
                ComponentCategory.FIRMWARE, uri,
                f"{mgr_name} Firmware", fw_body,
                location=location,
            ))
            logger.info(
                "iDRAC compat [%s]: synthesized firmware entry from "
                "Manager.FirmwareVersion = %r (no UpdateService available)",
                manager_uri, fw_ver,
            )

    # Emit unsupported marker when no firmware items were found from any path.
    if not components:
        components.append(unsupported_marker(ComponentCategory.FIRMWARE))

    return components, []

