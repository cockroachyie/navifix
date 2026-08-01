"""
redfish/collectors/processor.py
================================
Redfish resources consumed
---------------------------
- Systems/{id}/Processors (collection) -> Systems/{id}/Processors/{cpu}
- Systems/{id}/Processors/{cpu}/ProcessorMetrics (live metrics sub-resource)

Captures every Redfish property exposed per CPU socket: Model, Manufacturer,
Architecture, InstructionSet, TotalCores, TotalThreads, MaxSpeedMHz,
TurboCapable, Status, ProcessorId (microcode/firmware), and the Cache
summary array.  Temperature and utilization come from ProcessorMetrics
when the BMC exposes them (HPE iLO, newer iDRAC).

iDRAC 7 compatibility (inline ProcessorSummary fallback)
----------------------------------------------------------
iDRAC 7 (Redfish 1.0.x) often does not expose a Processors collection link.
Instead, it embeds a ``ProcessorSummary`` object directly on the
ComputerSystem body.  discovery.py detects this and stores the summary under
``links["processor_summary"]``.  When a collection link is absent, this
collector synthesizes one Component per CPU socket from the summary data.
The synthesized components carry ``_synthetic: "ProcessorSummary"`` in
``raw_json`` so the UI can optionally display a note.

Existing iDRAC 8/9 behavior is completely unaffected — if ``processors``
link is present, the standard collection path runs unchanged.
"""
import logging
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory

logger = logging.getLogger(__name__)


def collect(client, server, topology):
    components, readings = [], []

    for system_uri, links in topology.get("per_system", {}).items():
        processors_uri = links.get("processors")

        if processors_uri:
            # ── Standard path: Processors collection exists ──────────────
            # Unchanged from original — runs for iDRAC 8/9/10.
            for cpu in collection_members(client, processors_uri):
                odata_id = cpu.get("@odata.id")
                socket   = cpu.get("Socket", cpu.get("ProcessorId", {}).get("IdentificationRegisters", ""))
                name     = cpu.get("Name") or cpu.get("Model") or f"CPU {socket}"
                location = f"Socket {socket}" if socket else None

                components.append(component(
                    ComponentCategory.PROCESSOR, odata_id, name, cpu, location=location,
                ))

                # --- ProcessorMetrics sub-resource (optional but very useful) ---
                metrics_link = (cpu.get("Metrics") or {}).get("@odata.id")
                if metrics_link:
                    metrics = client.get(metrics_link)
                    if metrics:
                        # Temperature
                        temp_c = (
                            metrics.get("TemperatureCelsius")
                            or (metrics.get("CoreMetrics") or [{}])[0].get("CoreTemperatureCelsius")
                        )
                        if temp_c is not None:
                            readings.append(reading("cpu_temperature", name, temp_c, "Cel"))

                        # CPU utilization %
                        util = metrics.get("AverageFrequencyMHz") or metrics.get("OperatingSpeedMHz")
                        if util is not None:
                            readings.append(reading("cpu_speed_mhz", name, util, "MHz"))

                # --- Inline temperature (some older BMC firmware puts it here) ---
                inline_temp = (cpu.get("Oem") or {}).get("Temperature") or (cpu.get("Oem") or {}).get("Dell", {}).get("ProcessorTemperatureCelsius")
                if inline_temp is not None and not any(r and r.get("metric") == "cpu_temperature" and r.get("source_name") == name for r in readings):
                    readings.append(reading("cpu_temperature", name, inline_temp, "Cel"))

        else:
            # ── Fallback: inline ProcessorSummary (iDRAC 7 early Redfish 1.0.x) ──
            # discovery.py stores the summary under links["processor_summary"]
            # when no collection link was found.
            ps = links.get("processor_summary")
            if ps:
                _collect_from_processor_summary(system_uri, ps, components)
                logger.info(
                    "iDRAC 7 compat [%s]: synthesized processor(s) from inline "
                    "ProcessorSummary (Count=%s, Model=%s)",
                    system_uri, ps.get("Count"), ps.get("Model"),
                )
            else:
                logger.debug(
                    "No Processors collection or ProcessorSummary found for %s — "
                    "processor card will show 'Not Supported'", system_uri,
                )
            # If neither processors_uri nor processor_summary exist,
            # we fall through → unsupported_marker at end of function.

    readings = [r for r in readings if r]

    # If no processors were collected from any system, emit an unsupported
    # marker so the UI shows "Not Supported" rather than a blank/hidden card.
    if not components:
        components.append(unsupported_marker(ComponentCategory.PROCESSOR))

    return components, readings


def _collect_from_processor_summary(system_uri: str, ps: dict, components: list) -> None:
    """Synthesize processor Component rows from an inline ProcessorSummary object.

    Called only when the standard Processors collection link is absent
    (iDRAC 7 early Redfish 1.0.x firmware).  Produces one Component per
    CPU socket as reported by ``ProcessorSummary.Count``.

    Parameters
    ----------
    system_uri  : The ComputerSystem @odata.id, used to build stable synthetic IDs.
    ps          : The ``ProcessorSummary`` dict from the ComputerSystem body.
    components  : The running list to append to (mutated in-place).
    """
    count = ps.get("Count") or 1
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1

    model     = ps.get("Model") or "Unknown Processor"
    status    = ps.get("Status") or {}

    for i in range(max(count, 1)):
        odata_id = f"{system_uri}#processor_summary#{i}"
        name     = f"{model} (Socket {i})" if count > 1 else model

        body = {
            "@odata.id":    odata_id,
            "Name":         name,
            "Model":        model,
            "ProcessorType":"CPU",
            "Status":       status,
            # Marker so the UI/API can note this came from a summary, not a full resource.
            "_synthetic":   "ProcessorSummary",
        }
        components.append(component(
            ComponentCategory.PROCESSOR, odata_id, name, body,
            location=f"Socket {i}",
        ))
