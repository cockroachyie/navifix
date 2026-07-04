"""
redfish/collectors/battery.py
==============================
Redfish resources consumed
---------------------------
- Chassis/{id}/PowerSubsystem/Batteries (2021.x+ schema, primary source)
- Chassis/{id}/Power (older schema sometimes lists "Batteries" here too)

Batteries in the Redfish world usually mean RAID controller cache-protect
batteries or CMOS/BIOS backup batteries reported at the chassis level.
Not every server has any - this collector simply returns an empty list
when the resource is absent, which is expected and not an error.
"""
from ..collectors.common import component, reading
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []

    for chassis_uri, links in topology.get("per_chassis", {}).items():
        battery_members = []

        power_subsystem_uri = links.get("power_subsystem")
        if power_subsystem_uri:
            ps_body = client.get(power_subsystem_uri)
            if ps_body:
                batteries_link = (ps_body.get("Batteries") or {}).get("@odata.id")
                if batteries_link:
                    coll = client.get(batteries_link)
                    for m in (coll or {}).get("Members", []):
                        uri = m.get("@odata.id")
                        body = client.get(uri) if uri else None
                        if body:
                            battery_members.append(body)

        # Older/simpler implementations sometimes expose Batteries directly
        # as an array embedded in the Power resource.
        power_uri = links.get("power")
        if power_uri:
            power_body = client.get(power_uri)
            for b in (power_body or {}).get("Batteries", []) if power_body else []:
                battery_members.append(b)

        for b in battery_members:
            odata_id = b.get("@odata.id") or f"{chassis_uri}#battery#{b.get('MemberId', b.get('Name'))}"
            components.append(component(
                ComponentCategory.BATTERY, odata_id, b.get("Name", "Battery"), b,
                location=b.get("Location", {}).get("PartLocation", {}).get("ServiceLabel")
                if isinstance(b.get("Location"), dict) else None,
            ))
            cap = b.get("StateOfHealthPercent", {})
            if isinstance(cap, dict):
                readings.append(reading("battery_health_percent", b.get("Name"), cap.get("Reading"), "%"))
            charge = b.get("ChargePercent")
            if charge is not None:
                readings.append(reading("battery_charge_percent", b.get("Name"), charge, "%"))

    readings = [r for r in readings if r]
    return components, readings
