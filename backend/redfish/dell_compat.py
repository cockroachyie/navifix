"""
redfish/dell_compat.py
=======================
Centralized Dell iDRAC compatibility layer.

Motivation
----------
Dell iDRAC 6, 7, 8, 9, and 10 differ significantly in the Redfish OEM paths
they expose and the schema versions they implement.  Instead of scattering
version-specific checks throughout every collector, this small module provides:

  - ``is_dell(server)``                    — True when the server is identified as Dell.
  - ``get_idrac_generation(root, manager)``— Detects generation ("idrac6", "idrac7",
                                              "idrac8", "idrac9", "idrac10") from the
                                              service root and optional manager body.
  - ``get_idrac_generation_from_manager()``— Detects generation from Manager body alone.
  - ``is_idrac_legacy(generation)``        — True for iDRAC 6/7 (limited/no Redfish).
  - ``dell_oem_battery_paths(...)``        — Returns the correct OEM battery collection
                                              URIs for the detected generation, or empty
                                              list when none are applicable.

Generation Overview
-------------------
  iDRAC 6   — 11th gen PowerEdge.  NO Redfish API at all (WS-Man / IPMI / RACADM only).
               Detected by empty service_root or manager Model containing "idrac 6".
  iDRAC 7   — 12th gen PowerEdge, fw ≥ 2.30.30.30.  Partial Redfish 1.0.x:
               - Oem.Dell often absent from service root.
               - Memory/Processors only available as inline summary objects on System body
                 (ProcessorSummary, MemorySummary), NOT as collection links.
               - UpdateService absent; firmware only from Manager.FirmwareVersion.
  iDRAC 8   — 13th gen PowerEdge.  Redfish 1.0.x–1.3.x with Oem.Dell present.
               Basic Auth fallback already coded in session.py.
  iDRAC 9   — 14th–16th gen PowerEdge.  Redfish 1.4.x–1.14.x.  Full support.
  iDRAC 10  — 17th gen PowerEdge.  Redfish 1.15+.  Full support.

Design constraints
------------------
- Pure function: no HTTP calls, no global state.
- Returns safe defaults (empty lists / None) for unknown / non-Dell servers.
- All callers must already gate on ``is_dell()`` before calling the OEM helpers
  so that non-Dell servers never incur unnecessary HTTP round-trips.
- Existing callers that pass only service_root continue to work (manager_body
  is an optional keyword argument with default None).
"""
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------

def is_dell(server) -> bool:
    """Return True if the server object's vendor field identifies it as Dell."""
    vendor = getattr(server, "vendor", None) or ""
    return "dell" in vendor.lower()


# ---------------------------------------------------------------------------
# Legacy detection
# ---------------------------------------------------------------------------

def is_idrac_legacy(generation: str | None) -> bool:
    """Return True for iDRAC 6/7 which have limited or no Redfish support.

    - iDRAC 6: NO Redfish API — WS-Man / IPMI / RACADM only.
    - iDRAC 7: Partial Redfish 1.0.x — collection links often absent,
                inline summary objects used instead.

    Callers can use this to gate fallback paths without string-comparing the
    generation value in multiple places.
    """
    return generation in ("idrac6", "idrac7")


# ---------------------------------------------------------------------------
# iDRAC generation detection
# ---------------------------------------------------------------------------

def get_idrac_generation_from_manager(manager_body: dict) -> str | None:
    """Detect iDRAC generation from a Manager resource body.

    The Manager body's ``Model``, ``Name``, and ``Description`` fields
    frequently contain the explicit string "iDRAC 7", "iDRAC7", etc., making
    this the most reliable single-signal discriminator for all generations.

    Returns one of: "idrac6", "idrac7", "idrac8", "idrac9", "idrac10", or None.

    Called as a first-pass from ``get_idrac_generation()`` when manager_body
    is supplied.  Can also be called stand-alone by inventory.py to refine the
    detection after fetching the manager resource.
    """
    if not manager_body:
        return None

    model = (manager_body.get("Model") or "").lower()
    desc  = (manager_body.get("Description") or "").lower()
    name  = (manager_body.get("Name") or "").lower()
    combined = f"{model} {desc} {name}"

    # Check from newest to oldest to avoid false positives (e.g. "idrac 10"
    # must be matched before "idrac 1").
    for gen_str, gen_key in [
        ("idrac 10", "idrac10"), ("idrac10", "idrac10"),
        ("idrac 9",  "idrac9"),  ("idrac9",  "idrac9"),
        ("idrac 8",  "idrac8"),  ("idrac8",  "idrac8"),
        ("idrac 7",  "idrac7"),  ("idrac7",  "idrac7"),
        ("idrac 6",  "idrac6"),  ("idrac6",  "idrac6"),
    ]:
        if gen_str in combined:
            logger.debug(
                "iDRAC generation '%s' detected from Manager body "
                "(Model=%r, Name=%r)", gen_key, manager_body.get("Model"), manager_body.get("Name")
            )
            return gen_key

    return None


def get_idrac_generation(service_root: dict, manager_body: dict | None = None):
    """
    Detect the iDRAC generation from the service root document.

    Parameters
    ----------
    service_root  : The full body of GET /redfish/v1/.
                    Pass an empty dict ``{}`` when Redfish is unavailable (iDRAC 6).
    manager_body  : Optional body of GET /redfish/v1/Managers/<id>.
                    When supplied this is checked first and is the most reliable
                    discriminator.  Existing callers that omit it continue to work.

    Returns
    -------
    One of:
        "idrac6"   — No Redfish API.  WS-Man / IPMI required.
        "idrac7"   — Partial Redfish 1.0.x.  Oem.Dell often absent.
        "idrac8"   — Full Redfish 1.0.x–1.3.x with Oem.Dell.
        "idrac9"   — Redfish 1.4.x–1.14.x.
        "idrac10"  — Redfish 1.15+.
        None       — Not Dell, or generation cannot be determined.

    Detection strategy
    ------------------
    1. Empty service_root → iDRAC 6 (no Redfish at all).
    2. manager_body provided → check Model/Name/Description for explicit string.
    3. Oem.Dell absent from service root → iDRAC 7 (early firmware omitted it).
    4. RedfishVersion present → discriminate 8/9/10 by minor version.
    5. Oem.Dell product string fallback.
    6. Dell OEM present but version unknown → default to "idrac9".
    """
    # 1. Empty service_root → no Redfish API at all → iDRAC 6
    if not service_root:
        logger.debug("Empty service_root — no Redfish API, classifying as iDRAC 6")
        return "idrac6"

    # 2. Manager body is the most reliable discriminator
    if manager_body:
        gen = get_idrac_generation_from_manager(manager_body)
        if gen:
            return gen

    oem = service_root.get("Oem") or {}

    # 3. No Oem.Dell block → iDRAC 7 early firmware
    #    (iDRAC 8 and later always expose Oem.Dell; iDRAC 7 early fw did not).
    #    Callers should already have confirmed the server is Dell (via is_dell())
    #    before reaching here, so returning "idrac7" is a safe assumption.
    if "Dell" not in oem:
        redfish_ver = service_root.get("RedfishVersion", "unknown")
        logger.debug(
            "No Oem.Dell in service root (RedfishVersion=%r) — "
            "likely iDRAC 7 early firmware; classifying as 'idrac7'", redfish_ver
        )
        return "idrac7"

    # 4. RedfishVersion — reliable discriminator when Oem.Dell is present
    #    iDRAC 8:  Redfish 1.0.x – 1.3.x
    #    iDRAC 9:  Redfish 1.4.x – 1.14.x
    #    iDRAC 10: Redfish 1.15+
    redfish_ver = service_root.get("RedfishVersion", "")
    if redfish_ver:
        try:
            major, minor, *_ = redfish_ver.split(".")
            major, minor = int(major), int(minor)
            if major == 1:
                if minor <= 3:
                    return "idrac8"
                elif minor <= 14:
                    return "idrac9"
                else:
                    return "idrac10"
        except (ValueError, AttributeError):
            pass

    # 5. Fallback: check OEM Product field
    oem_dell = oem.get("Dell") or {}
    product = (
        (oem_dell.get("Manager") or {}).get("Model", "")
        or (oem_dell.get("ServiceRoot") or {}).get("ProductType", "")
    )
    product_lower = product.lower()
    for gen_str, gen_key in [
        ("idrac 10", "idrac10"), ("idrac10", "idrac10"),
        ("idrac 9",  "idrac9"),  ("idrac9",  "idrac9"),
        ("idrac 8",  "idrac8"),  ("idrac8",  "idrac8"),
        ("idrac 7",  "idrac7"),  ("idrac7",  "idrac7"),
    ]:
        if gen_str in product_lower:
            return gen_key

    # 6. Dell server but generation unknown — default to iDRAC 9 behaviour
    #    (most common generation with the broadest OEM support).
    logger.debug(
        "Dell iDRAC generation could not be determined from service root "
        "(RedfishVersion=%r, Oem.Dell present). Treating as iDRAC 9.", redfish_ver
    )
    return "idrac9"


# ---------------------------------------------------------------------------
# OEM battery path helpers
# ---------------------------------------------------------------------------

def dell_oem_battery_paths(chassis_id: str, system_id, generation) -> list:
    """
    Return the list of Dell OEM battery endpoint descriptors for the given
    iDRAC generation.  Each descriptor is a dict::

        {
            "uri": "/redfish/v1/Dell/...",
            "kind": "controller_battery" | "cmos_battery",
        }

    Returns an empty list when:
    - generation is None or "idrac6"/"idrac7"/"idrac8" — OEM paths absent
    - chassis_id is empty

    The caller (battery.py) is responsible for the actual HTTP calls, so
    this helper remains a pure function with no side-effects.
    """
    if not chassis_id:
        return []

    # iDRAC 6/7/8 do NOT expose these OEM paths.
    if generation in (None, "idrac6", "idrac7", "idrac8"):
        return []

    paths = [
        {
            "uri": f"/redfish/v1/Dell/Chassis/{chassis_id}/DellControllerBatteryCollection",
            "kind": "controller_battery",
        },
    ]

    if system_id:
        paths.append({
            "uri": f"/redfish/v1/Dell/Systems/{system_id}/DellPresenceAndStatusSensorCollection",
            "kind": "cmos_battery",
        })

    return paths
