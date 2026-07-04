"""
redfish/collectors/thermal.py
================================
Redfish resources consumed
---------------------------
- Chassis/{id}/Thermal -> Temperatures[] (schema 2020.x, by far the most
  common shape across Dell/HPE/Lenovo/Supermicro today)
- Chassis/{id}/ThermalSubsystem/ThermalMetrics (2021.x+ Swordfish schema)
  and ThermalSubsystem/Sensors (collection of temperature sensors)

Captures every temperature sensor reading with all associated thresholds:
ReadingCelsius, UpperThresholdCritical, UpperThresholdFatal,
UpperThresholdNonCritical, LowerThresholdCritical, LowerThresholdFatal,
LowerThresholdNonCritical, MaxReadingRangeTemp, MinReadingRangeTemp,
PhysicalContext (location), SensorNumber, Status.
"""
from .common import component, reading, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for chassis_uri, links in topology.get("per_chassis", {}).items():

        sensors = []

        # ── Legacy Thermal resource ─────────────────────────────────────
        thermal_uri = links.get("thermal")
        if thermal_uri:
            body = client.get(thermal_uri)
            if body:
                sensors.extend(body.get("Temperatures", []))

        # ── 2021.x ThermalSubsystem ─────────────────────────────────────
        ts_uri = links.get("thermal_subsystem")
        if ts_uri:
            ts_body = client.get(ts_uri)
            if ts_body:
                # Sensors collection
                sensors_link = (ts_body.get("ThermalMetrics") or {}).get("@odata.id")
                if sensors_link:
                    metrics = client.get(sensors_link)
                    if metrics:
                        for s in metrics.get("TemperatureSummaryCelsius", {}).values():
                            if isinstance(s, dict) and s.get("Reading") is not None:
                                sensors.append(s)

                        for s in (metrics.get("TemperatureReadingsCelsius") or []):
                            sensors.append(s)

                # Individual sensor collection
                sensors_coll = (ts_body.get("Sensors") or {}).get("@odata.id")
                if sensors_coll:
                    for s in collection_members(client, sensors_coll):
                        if s.get("ReadingType") == "Temperature" or "Celsius" in s.get("ReadingUnits", ""):
                            sensors.append(s)

        # ── Emit one Component + one SensorReading per sensor ───────────
        for sensor in sensors:
            # Synthetic stable identity (Thermal[] members don't always have @odata.id)
            odata_id = (
                sensor.get("@odata.id")
                or f"{chassis_uri}#temp#{sensor.get('MemberId', sensor.get('Name', ''))}"
            )
            name    = sensor.get("Name") or sensor.get("PhysicalContext") or "Temperature Sensor"
            location= sensor.get("PhysicalContext")

            components.append(component(
                ComponentCategory.THERMAL_SENSOR, odata_id, name, sensor, location=location,
            ))

            temp = sensor.get("ReadingCelsius") or sensor.get("Reading")
            if temp is not None:
                readings.append(reading("temperature", name, temp, "Cel"))

    readings = [r for r in readings if r]
    return components, readings
