"""
redfish/collectors/battery.py
==============================
Redfish resources consumed (in lookup priority order)
-------------------------------------------------------
- Chassis/{id}/Batteries (direct collection link, captured by discovery as
  CHASSIS_LINK_KEYS["Batteries"] — most direct path when present)
- Chassis/{id}/PowerSubsystem/Batteries (2021.x+ schema)
- Chassis/{id}/Power embedded "Batteries" array (older schema fallback)

Batteries in the Redfish world usually mean RAID controller cache-protect
batteries or CMOS/BIOS backup batteries reported at the chassis level.
Not every server has any - this collector simply returns an empty list
when the resource is absent, which is expected and not an error.
"""
from .common import component, reading
from database.models import ComponentCategory


def collect(client, server, topology):
    components, readings = [], []
    seen_uris = set()

    for chassis_uri, links in topology.get("per_chassis", {}).items():
        battery_members = []

        # Primary path: direct Batteries collection link discovered from the
        # Chassis body (CHASSIS_LINK_KEYS["Batteries"] -> topology["batteries"]).
        # This is the most direct and reliable path when supported.
        batteries_coll_uri = links.get("batteries")
        if batteries_coll_uri:
            coll = client.get(batteries_coll_uri)
            for m in (coll or {}).get("Members", []):
                uri = m.get("@odata.id")
                if uri and uri not in seen_uris:
                    body = client.get(uri)
                    if body:
                        seen_uris.add(uri)
                        battery_members.append(body)

        # Secondary path: Newer (2021+) schema — PowerSubsystem -> Batteries
        power_subsystem_uri = links.get("power_subsystem")
        if power_subsystem_uri:
            ps_body = client.get(power_subsystem_uri)
            if ps_body:
                batteries_link = (ps_body.get("Batteries") or {}).get("@odata.id")
                if batteries_link:
                    coll = client.get(batteries_link)
                    for m in (coll or {}).get("Members", []):
                        uri = m.get("@odata.id")
                        if uri and uri not in seen_uris:
                            body = client.get(uri) if uri else None
                            if body:
                                seen_uris.add(uri)
                                battery_members.append(body)

        # Tertiary path: older implementations embed Batteries directly in the
        # Power resource as an inline array (not a collection link).
        power_uri = links.get("power")
        if power_uri:
            power_body = client.get(power_uri)
            for b in (power_body or {}).get("Batteries", []) if power_body else []:
                uri = b.get("@odata.id")
                if not uri or uri not in seen_uris:
                    if uri:
                        seen_uris.add(uri)
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
