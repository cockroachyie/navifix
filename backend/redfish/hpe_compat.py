"""
redfish/hpe_compat.py
======================
Centralized HPE iLO compatibility layer.

Motivation
----------
HPE iLO 4 (Gen9) implements Redfish 1.0.x with significant structural
differences from both later HPE generations (iLO 5/6) and from Dell iDRAC.
Instead of scattering iLO-version-specific checks throughout every collector,
this module provides clean, tested helpers:

  - ``is_hpe(server)``                 — True when server vendor is HPE.
  - ``get_ilo_generation(root, mgr)``  — Detects "ilo4", "ilo5", "ilo6" from
                                          the service root and optional manager
                                          body.
  - ``get_hpe_oem_block(body)``        — Returns the active HPE OEM sub-dict
                                          from any resource body, transparently
                                          handling both the legacy "Hp" key
                                          (iLO 4 pre-rebrand firmware) and the
                                          current "Hpe" key (iLO 4 post-rebrand
                                          and all iLO 5/6 firmware).
  - ``get_smart_storage_uri(body)``    — Extracts the SmartStorage OEM link
                                          from a ComputerSystem body.
  - ``get_hpe_battery_units_uri(ctrl_body)``
                                       — Returns the BackupUnits collection URI
                                          from an ArrayController OEM body, or
                                          None when absent.

iLO Generation Overview
------------------------
  iLO 2 / iLO 3 — No Redfish API (RIBCL/XML only). NOT supported.
  iLO 4  (Gen8/Gen9, fw ≥ 2.30):
            Redfish 1.0.x.  OEM key may be "Hp" (early fw) or "Hpe"
            (post-rebrand fw).  No standard Storage/Memory collections;
            SmartStorage and Memory exposed via Oem.Hp / Oem.Hpe links.
            Batteries under SmartStorage ArrayController BackupUnits.
  iLO 5  (Gen10):
            Redfish 1.6+.  OEM key is "Hpe".  Standard Storage, Memory,
            Chassis/Batteries collections present.  SmartStorage OEM path
            may still be present as a parallel view.
  iLO 6  (Gen10+):
            Redfish 1.15+.  Full DMTF Redfish including 2021+ subsystem
            schemas (PowerSubsystem, ThermalSubsystem, Batteries collection).

Design constraints
------------------
- Pure functions: no HTTP calls, no global state.
- Returns safe defaults (None / empty dict) for non-HPE or unknown servers.
- All callers must gate on ``is_hpe()`` before calling OEM helpers to avoid
  unnecessary HTTP 404 round-trips on non-HPE systems.
- Existing callers that pass only service_root continue to work (manager_body
  is an optional keyword argument with default None).
"""
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vendor detection
# ---------------------------------------------------------------------------

def is_hpe(server) -> bool:
    """Return True if the server object's vendor field identifies it as HPE."""
    vendor = getattr(server, "vendor", None) or ""
    vendor_lower = vendor.lower()
    return "hpe" in vendor_lower or vendor_lower in ("hp", "hewlett packard", "hewlett-packard")


# ---------------------------------------------------------------------------
# OEM block helper — handles Hp vs Hpe key transparently
# ---------------------------------------------------------------------------

def get_hpe_oem_block(body: dict) -> dict:
    """Return the HPE-specific OEM sub-dict from any Redfish resource body.

    HPE iLO 4 firmware released before the HP → HPE rebrand (roughly before
    fw 2.40) uses the OEM key ``"Hp"``.  Post-rebrand firmware and all iLO 5/6
    firmware use ``"Hpe"``.  Both keys may appear on the same server if firmware
    was updated incrementally.

    This helper checks ``"Hpe"`` first (the current standard) and falls back to
    ``"Hp"`` so callers never need to worry about which key is present.

    Parameters
    ----------
    body : Any Redfish resource body dict (ComputerSystem, Chassis, Power, …).

    Returns
    -------
    The HPE OEM sub-dict, or an empty dict ``{}`` when neither key is present.
    Never returns ``None``.
    """
    oem = body.get("Oem") or {}
    hpe_block = oem.get("Hpe") or oem.get("Hp") or {}
    return hpe_block


# ---------------------------------------------------------------------------
# iLO generation detection
# ---------------------------------------------------------------------------

def get_ilo_generation(service_root: dict, manager_body: dict | None = None) -> str | None:
    """Detect the iLO generation from the service root and optional manager body.

    Parameters
    ----------
    service_root  : Full body of GET /redfish/v1/.
    manager_body  : Optional body of GET /redfish/v1/Managers/<id>.
                    When supplied, the ``Model`` / ``Name`` / ``Description``
                    fields are checked first for explicit version strings.

    Returns
    -------
    One of: ``"ilo4"``, ``"ilo5"``, ``"ilo6"``, or ``None``.
    Returns ``None`` for non-HPE systems or when the generation cannot be
    determined.

    Detection strategy
    ------------------
    1. Manager body ``Model`` / ``Name`` / ``Description`` — most reliable.
    2. Service root ``RedfishVersion`` — correlates with known iLO firmware
       Redfish implementation versions.
    3. OEM block presence — iLO 5+ always uses ``"Hpe"``; iLO 4 may use
       ``"Hp"`` or ``"Hpe"`` depending on firmware age.
    """
    if not service_root and not manager_body:
        return None

    # 1. Manager body is the most reliable discriminator
    if manager_body:
        gen = _ilo_gen_from_manager(manager_body)
        if gen:
            return gen

    if not service_root:
        return None

    oem = service_root.get("Oem") or {}

    # Quick non-HPE check — if neither "Hpe" nor "Hp" is in OEM, not an HPE BMC
    if "Hpe" not in oem and "Hp" not in oem:
        # Could still be HPE if service_root.Vendor is set
        vendor = service_root.get("Vendor", "").lower()
        if "hpe" not in vendor and "hp" not in vendor:
            return None

    # 2. RedfishVersion discriminator
    #    iLO 4:  Redfish 1.0.x
    #    iLO 5:  Redfish 1.6.x – 1.14.x
    #    iLO 6:  Redfish 1.15+
    redfish_ver = service_root.get("RedfishVersion", "")
    if redfish_ver:
        try:
            major, minor, *_ = redfish_ver.split(".")
            major, minor = int(major), int(minor)
            if major == 1:
                if minor <= 5:
                    logger.debug(
                        "HPE iLO generation: iLO 4 (RedfishVersion=%s)", redfish_ver
                    )
                    return "ilo4"
                elif minor <= 14:
                    logger.debug(
                        "HPE iLO generation: iLO 5 (RedfishVersion=%s)", redfish_ver
                    )
                    return "ilo5"
                else:
                    logger.debug(
                        "HPE iLO generation: iLO 6 (RedfishVersion=%s)", redfish_ver
                    )
                    return "ilo6"
        except (ValueError, AttributeError):
            pass

    # 3. OEM key fallback — "Hp" key present → almost certainly iLO 4
    if "Hp" in oem and "Hpe" not in oem:
        logger.debug(
            "HPE iLO generation: iLO 4 (Oem.Hp present, Oem.Hpe absent)"
        )
        return "ilo4"

    # 4. Default for confirmed HPE when version is unclear
    logger.debug(
        "HPE iLO generation could not be precisely determined — defaulting to iLO 5"
    )
    return "ilo5"


def _ilo_gen_from_manager(manager_body: dict) -> str | None:
    """Detect iLO generation from a Manager resource body.

    Checks ``Model``, ``Name``, and ``Description`` for explicit version
    strings ("iLO 4", "iLO 5", "iLO 6", including common variants without
    space).
    """
    if not manager_body:
        return None

    model = (manager_body.get("Model") or "").lower()
    desc  = (manager_body.get("Description") or "").lower()
    name  = (manager_body.get("Name") or "").lower()
    combined = f"{model} {desc} {name}"

    # Check newest first to avoid "ilo 4" matching inside "ilo 40" (hypothetical).
    for gen_str, gen_key in [
        ("ilo 6",  "ilo6"),  ("ilo6",  "ilo6"),
        ("ilo 5",  "ilo5"),  ("ilo5",  "ilo5"),
        ("ilo 4",  "ilo4"),  ("ilo4",  "ilo4"),
    ]:
        if gen_str in combined:
            logger.debug(
                "HPE iLO generation '%s' detected from Manager body "
                "(Model=%r, Name=%r)", gen_key,
                manager_body.get("Model"), manager_body.get("Name"),
            )
            return gen_key

    return None


# ---------------------------------------------------------------------------
# SmartStorage OEM link extraction
# ---------------------------------------------------------------------------

def get_smart_storage_uri(system_body: dict) -> str | None:
    """Extract the SmartStorage OEM URI from a ComputerSystem body.

    iLO 4 (and some iLO 5) firmware advertises SmartStorage under the HPE OEM
    block of the ComputerSystem resource rather than as a standard
    ``Systems/{id}/Storage`` link:

    .. code-block:: json

        {
          "Oem": {
            "Hp": {
              "SmartStorage": {"@odata.id": "/redfish/v1/Systems/1/SmartStorage/"}
            }
          }
        }

    Parameters
    ----------
    system_body : Full body of a ComputerSystem resource.

    Returns
    -------
    The SmartStorage ``@odata.id`` URI string, or ``None`` when absent.
    """
    hpe = get_hpe_oem_block(system_body)
    smart_storage = hpe.get("SmartStorage") or hpe.get("Links", {}).get("SmartStorage") or {}
    uri = smart_storage.get("@odata.id")
    if uri:
        logger.debug("HPE SmartStorage OEM URI found: %s", uri)
    return uri


# ---------------------------------------------------------------------------
# SmartStorage BackupUnits (battery) URI extraction
# ---------------------------------------------------------------------------

def get_hpe_battery_units_uri(ctrl_body: dict) -> str | None:
    """Return the BackupUnits collection URI from an ArrayController body.

    HPE iLO 4 exposes Smart Storage batteries under the controller's OEM
    ``Links`` block rather than as a top-level chassis battery collection:

    .. code-block:: json

        {
          "@odata.id": "/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/",
          "Links": {
            "BackupUnits": {
              "@odata.id": ".../ArrayControllers/0/BackupUnits/"
            }
          }
        }

    This helper also checks the standard Redfish ``Links.BackupUnits`` path
    (not wrapped in Oem) because HPE uses both shapes across firmware versions.

    Parameters
    ----------
    ctrl_body : Full body of an ArrayController member resource.

    Returns
    -------
    The BackupUnits collection ``@odata.id`` URI string, or ``None``.
    """
    # Shape 1: standard (non-OEM) Links block — most common on iLO 4/5
    links = ctrl_body.get("Links") or {}
    backup = links.get("BackupUnits") or {}
    uri = backup.get("@odata.id")
    if uri:
        logger.debug("HPE BackupUnits URI (Links.BackupUnits): %s", uri)
        return uri

    # Shape 2: OEM Links block — seen on some older iLO 4 firmware
    hpe = get_hpe_oem_block(ctrl_body)
    hpe_links = hpe.get("Links") or {}
    backup_oem = hpe_links.get("BackupUnits") or {}
    uri = backup_oem.get("@odata.id")
    if uri:
        logger.debug("HPE BackupUnits URI (Oem.Hp/Hpe.Links.BackupUnits): %s", uri)
    return uri


# ---------------------------------------------------------------------------
# HPE battery body normalizer
# ---------------------------------------------------------------------------

def normalize_hpe_battery(bat_body: dict) -> dict:
    """Normalize an HPE SmartStorage BackupUnit body to a common battery shape.

    HPE iLO 4 BackupUnit resources use HPE-proprietary field names.  This
    function maps them to the closest Redfish-standard equivalents so that
    ``battery.py`` can consume HPE batteries without vendor-specific code in
    the collection loop.

    HPE BackupUnit fields:
      Name                   → Name (already standard)
      Status.Health/State    → Status (already standard)
      ChargeLevelPercent     → ChargePercent
      RemainingChargeTimeSeconds → (preserved in raw body)
      ErrorCode              → (preserved in raw body)
      Model                  → Model
      PresentedCapacityWattHours → (preserved in raw body)
      MaximumCapacityWattHours   → (preserved in raw body)

    Returns
    -------
    A new dict that is a superset of ``bat_body`` with added standard-ish keys.
    The ``_source`` key is set to ``"HPE SmartStorage BackupUnit"`` so the UI
    and logs can identify the origin.
    """
    normalized = dict(bat_body)
    normalized.setdefault("Name", "HPE Smart Storage Battery")

    # Map HPE ChargeLevelPercent → ChargePercent (used by battery.py readings)
    if "ChargePercent" not in normalized and "ChargeLevelPercent" in bat_body:
        normalized["ChargePercent"] = bat_body["ChargeLevelPercent"]

    # Map HPE ErrorCode to a synthetic health hint when Status is absent
    if not normalized.get("Status"):
        error_code = bat_body.get("ErrorCode", 0)
        health = "OK" if error_code == 0 else "Warning"
        normalized["Status"] = {"Health": health, "State": "Enabled"}

    normalized["_source"] = "HPE SmartStorage BackupUnit"
    return normalized
