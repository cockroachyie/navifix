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
"""
from .common import component, reading, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for system_uri, links in topology.get("per_system", {}).items():
        processors_uri = links.get("processors")
        if not processors_uri:
            continue

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

    readings = [r for r in readings if r]
    return components, readings
