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

iDRAC 7 compatibility (inline MemorySummary fallback)
-------------------------------------------------------
iDRAC 7 (Redfish 1.0.x) frequently does not expose a Memory collection link.
Instead, it embeds a ``MemorySummary`` object directly on the ComputerSystem
body (``TotalSystemMemoryGiB``, ``Status``).  discovery.py detects this and
stores the summary under ``links["memory_summary"]``.

When the Memory collection link is absent **and** a summary exists, this
collector synthesizes one aggregate Component representing total installed RAM.
The component carries ``_synthetic: "MemorySummary"`` in ``raw_json``.

HPE iLO 4 compatibility (same MemorySummary fallback)
-------------------------------------------------------
HPE iLO 4 (Gen9, Redfish 1.0.x) also lacks a standard Memory collection link
on most firmware versions.  discovery.py stores the ``MemorySummary`` object
under ``links["memory_summary"]`` for both iLO 4 and iDRAC 7.  The same
synthesis path runs for both.

Some HPE iLO 4 firmware returns a minimal ``MemorySummary`` that contains
only a ``Status`` block (no ``TotalSystemMemoryGiB``).  The synthesis code
tolerates this: it produces a component with ``CapacityMiB=None`` rather than
skipping the component entirely.  This ensures the Memory card shows at least
health state rather than an empty/zero count.

iLO 4 firmware ≥ 2.50 may expose individual DIMM resources at the standard
``Systems/{id}/Memory`` path.  discovery.py also checks the HPE OEM block
(``Oem.Hp/Hpe.Memory``) as a fallback, so the collection path works
correctly regardless of which location the firmware advertises.

Existing iDRAC 8/9 behavior is completely unaffected.
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def collect(client, server, topology):
    components, readings = [], []

    for system_uri, links in topology.get("per_system", {}).items():
        memory_uri = links.get("memory")

        if memory_uri:
            # ── Standard path: Memory collection exists ──────────────────
            # Unchanged from original — runs for iDRAC 8/9/10.
            dimm_count = 0
            for dimm in collection_members(client, memory_uri):
                odata_id = dimm.get("@odata.id")
                location = None
                loc = dimm.get("MemoryLocation") or {}
                if loc:
                    location = f"Socket {loc.get('Socket')} Channel {loc.get('Channel')} Slot {loc.get('Slot')}"
                components.append(component(
                    ComponentCategory.MEMORY, odata_id, dimm.get("Name", "DIMM"), dimm, location=location,
                ))
                dimm_count += 1

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

            if dimm_count == 0:
                # Memory collection exists but returned no accessible members.
                # Show "Not Supported" rather than a misleading 0 count.
                components.append(unsupported_marker(ComponentCategory.MEMORY))

        else:
            # ── Fallback: inline MemorySummary (iDRAC 7 / HPE iLO 4) ─────────
            # Both iDRAC 7 and HPE iLO 4 (early firmware) lack a standard
            # Memory collection link and provide only a MemorySummary object
            # embedded in the ComputerSystem body.  discovery.py stores it
            # under links["memory_summary"] for both vendors.
            #
            # Guard: synthesize a component when the MemorySummary dict is
            # non-empty AND contains at least one useful field.  We accept
            # a MemorySummary that has only a Status block (no
            # TotalSystemMemoryGiB) — seen on some HPE iLO 4 firmware —
            # because the UI can still display health state and the
            # _synthetic marker makes the source clear.
            ms = links.get("memory_summary")
            if ms and (ms.get("TotalSystemMemoryGiB") is not None or ms.get("Status")):
                _collect_from_memory_summary(system_uri, ms, components)
                logger.info(
                    "MemorySummary fallback [%s]: synthesized memory component "
                    "(TotalSystemMemoryGiB=%s, Status=%s)",
                    system_uri,
                    ms.get("TotalSystemMemoryGiB"),
                    ms.get("Status"),
                )
            else:
                logger.debug(
                    "No Memory collection or usable MemorySummary found for %s — "
                    "memory card will show 'Not Supported'", system_uri,
                )
                components.append(unsupported_marker(ComponentCategory.MEMORY))

    readings = [r for r in readings if r]
    return components, readings


def _collect_from_memory_summary(system_uri: str, ms: dict, components: list) -> None:
    """Synthesize a memory Component from an inline MemorySummary object.

    Called only when the standard Memory collection link is absent
    (iDRAC 7 early Redfish 1.0.x).  Produces one aggregate Component
    representing total installed system RAM.

    Parameters
    ----------
    system_uri  : The ComputerSystem @odata.id, used to build a stable synthetic ID.
    ms          : The ``MemorySummary`` dict from the ComputerSystem body.
    components  : The running list to append to (mutated in-place).
    """
    total_gib = ms.get("TotalSystemMemoryGiB")
    status    = ms.get("Status") or {}

    odata_id = f"{system_uri}#memory_summary"

    # Convert GiB to MiB for UI consistency (CapacityMiB is the standard field)
    capacity_mib = None
    if total_gib is not None:
        try:
            capacity_mib = int(float(total_gib) * 1024)
        except (TypeError, ValueError):
            pass

    name = f"System Memory ({total_gib} GiB)" if total_gib else "System Memory"
    body = {
        "@odata.id":            odata_id,
        "Name":                 name,
        "TotalSystemMemoryGiB": total_gib,
        "CapacityMiB":          capacity_mib,
        "Status":               status,
        # Marker so the UI/API can note this came from a summary, not individual DIMMs.
        "_synthetic":           "MemorySummary",
    }
    components.append(component(
        ComponentCategory.MEMORY, odata_id, name, body,
    ))
