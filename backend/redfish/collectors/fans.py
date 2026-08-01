"""
redfish/collectors/fans.py
============================
Redfish resources consumed
---------------------------
- Chassis/{id}/Thermal -> Fans[] (schema 2020.x and earlier - most common
  today across Dell/HPE/Lenovo/Supermicro)
- Chassis/{id}/ThermalSubsystem/Fans (2021.x+ Swordfish-aligned schema,
  used by newer firmware)

Both shapes are checked; whichever exists is used. Reading, thresholds and
Status are captured as returned - some BMCs only populate a subset
(e.g. no LowerThreshold on a fixed-speed fan), which is normal.
"""
from .common import component, reading
from database.models import ComponentCategory


def _from_legacy_thermal(client, chassis_uri, thermal_uri):
    body = client.get(thermal_uri)
    if not body:
        return []
    return body.get("Fans", [])


def _from_thermal_subsystem(client, thermal_subsystem_uri):
    body = client.get(thermal_subsystem_uri)
    if not body:
        return []
    fans_link = (body.get("Fans") or {}).get("@odata.id")
    if not fans_link:
        return []
    coll = client.get(fans_link)
    out = []
    for m in (coll or {}).get("Members", []):
        uri = m.get("@odata.id")
        member_body = client.get(uri) if uri else None
        if member_body:
            out.append(member_body)
    return out


def collect(client, server, topology):
    components, readings = [], []

    for chassis_uri, links in topology.get("per_chassis", {}).items():
        fans = []
        if links.get("thermal"):
            fans.extend(_from_legacy_thermal(client, chassis_uri, links["thermal"]))
        if links.get("thermal_subsystem"):
            fans.extend(_from_thermal_subsystem(client, links["thermal_subsystem"]))

        for idx, fan in enumerate(fans):
            fan_name = fan.get("FanName") or fan.get("Name") or f"Fan {idx + 1}"
            unique_id = fan.get("MemberId") or fan.get("FanName") or fan.get("Name") or str(idx + 1)
            odata_id = fan.get("@odata.id") or f"{chassis_uri}#fan#{unique_id}"

            components.append(component(
                ComponentCategory.FAN, odata_id, fan_name, fan,
                location=fan.get("PhysicalContext"),
            ))
            rpm = fan.get("Reading") or fan.get("SpeedRPM") or fan.get("CurrentReading")
            unit = fan.get("ReadingUnits") or fan.get("Units") or "RPM"
            readings.append(reading("fan_rpm", fan_name, rpm, unit))

    readings = [r for r in readings if r]
    return components, readings
