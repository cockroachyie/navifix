"""
redfish/collectors/voltage.py
================================
Redfish resources consumed
---------------------------
- Chassis/{id}/Thermal -> Voltages[] (yes, Voltages are embedded in the
  Thermal resource in the 2020.x schema — a historical quirk of Redfish)
- Chassis/{id}/Power -> Voltages[] (some BMCs also report voltages here)
- Chassis/{id}/EnvironmentMetrics (2021.x+): PowerWatts, Voltages

Captures every voltage rail: ReadingVolts, UpperThresholdCritical,
UpperThresholdFatal, UpperThresholdNonCritical, LowerThresholdCritical,
LowerThresholdFatal, LowerThresholdNonCritical, PhysicalContext, Status.
"""
from .common import component, reading, collection_members, unsupported_marker
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for chassis_uri, links in topology.get("per_chassis", {}).items():

        voltage_items = []
        supported = False

        # ── Voltages embedded in Thermal resource ───────────────────────
        thermal_uri = links.get("thermal")
        if thermal_uri:
            supported = True
            body = client.get(thermal_uri)
            if body:
                voltage_items.extend(body.get("Voltages", []))

        # ── Voltages embedded in Power resource ─────────────────────────
        power_uri = links.get("power")
        if power_uri:
            supported = True
            body = client.get(power_uri)
            if body:
                voltage_items.extend(body.get("Voltages", []))

                # Fallback: extract LineInputVoltage from PowerSupplies (e.g. on HPE iLO)
                for ps in body.get("PowerSupplies", []):
                    volts = ps.get("LineInputVoltage")
                    if volts is not None and volts > 0:
                        member_id = ps.get("MemberId") or ps.get("Id") or ps.get("SerialNumber", "PowerSupply")
                        voltage_items.append({
                            "@odata.id": ps.get("@odata.id", f"{power_uri}#PowerSupplies#{member_id}"),
                            "Name": f"{ps.get('Name', 'Power Supply')} Input Voltage",
                            "ReadingVolts": volts,
                            "Status": ps.get("Status", {"Health": "OK", "State": "Enabled"}),
                            "PhysicalContext": "PowerSupply"
                        })

        # ── 2021.x EnvironmentMetrics ───────────────────────────────────
        env_uri = links.get("environment_metrics")
        if env_uri:
            supported = True
            env = client.get(env_uri)
            if env:
                for v in (env.get("Voltages") or env.get("PowerVoltages") or []):
                    voltage_items.append(v)
                    
        # ── Voltages in Sensors collection ──────────────────────────────
        sensors_uri = links.get("sensors")
        if sensors_uri:
            supported = True
            for sensor in collection_members(client, sensors_uri):
                if sensor.get("ReadingType") == "Voltage":
                    volts = sensor.get("Reading")
                    if volts is not None:
                        sensor["ReadingVolts"] = volts
                    voltage_items.append(sensor)
                    
        if not supported:
            components.append(unsupported_marker(ComponentCategory.VOLTAGE_SENSOR))
            continue

        if not voltage_items:
            # Resources exist but none report voltages — mark as not available
            # rather than returning an empty list that the UI shows as 0.
            components.append(unsupported_marker(ComponentCategory.VOLTAGE_SENSOR))
            continue

        for v in voltage_items:
            odata_id = (
                v.get("@odata.id")
                or f"{chassis_uri}#voltage#{v.get('MemberId', v.get('Name', ''))}"
            )
            name     = v.get("Name") or "Voltage Sensor"
            location = v.get("PhysicalContext")

            components.append(component(
                ComponentCategory.VOLTAGE_SENSOR, odata_id, name, v, location=location,
            ))

            volts = v.get("ReadingVolts")
            if volts is not None:
                readings.append(reading("voltage", name, volts, "V"))

    readings = [r for r in readings if r]
    return components, readings
