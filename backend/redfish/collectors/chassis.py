"""
redfish/collectors/chassis.py
===============================
Redfish resources consumed
---------------------------
- Chassis/{id} (the chassis resource itself: Manufacturer, Model, Serial,
  AssetTag, PartNumber, IndicatorLED, PhysicalSecurity.IntrusionSensor,
  Status)

One Component row is produced per chassis instance (a server can have more
than one chassis resource, e.g. a blade + its enclosure).
"""
from .common import component, unsupported_marker
from database.models import ComponentCategory


def collect(client, server, topology):
    components = []
    for chassis_uri in topology.get("chassis", []):
        body = client.get(chassis_uri)
        if not body:
            continue
        components.append(component(
            ComponentCategory.CHASSIS, chassis_uri, body.get("Name", "Chassis"), body,
        ))
    if not components:
        components.append(unsupported_marker(ComponentCategory.CHASSIS))

    return components, []
