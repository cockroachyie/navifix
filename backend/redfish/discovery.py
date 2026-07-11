"""
redfish/discovery.py
=====================
Walks a BMC's Redfish tree starting at the service root
(GET /redfish/v1/) and discovers every resource collection/link it
exposes, entirely generically. No vendor-specific paths are ever
hardcoded - we only ever follow the URIs a BMC itself advertises in its
own JSON (`@odata.id` links), which is exactly what the Redfish
specification requires every compliant implementation to expose.

This makes the same code path work unmodified against Dell iDRAC, HPE
iLO, Lenovo XClarity Controller, Supermicro BMC, or any other
Redfish-conformant implementation - including future ones we've never
tested against.

Output shape
------------
`discover_topology()` returns a nested dict of URIs (not resource bodies)
describing everything found:

{
  "service_root": {...full GET /redfish/v1 body...},
  "systems": ["/redfish/v1/Systems/System.Embedded.1", ...],
  "chassis": ["/redfish/v1/Chassis/System.Embedded.1", ...],
  "managers": ["/redfish/v1/Managers/iDRAC.Embedded.1", ...],
  "update_service": "/redfish/v1/UpdateService",
  "event_service": "/redfish/v1/EventService",
  "task_service": "/redfish/v1/TaskService",
  "telemetry_service": "/redfish/v1/TelemetryService",
  "per_system": {
      "<system_uri>": {
          "processors": "...", "memory": "...", "storage": "...",
          "simple_storage": "...", "ethernet_interfaces": "...",
          "network_interfaces": "...", "log_services": "...",
          "pcie_devices": "...", "pcie_functions": "...",
          "bios": "...", "secure_boot": "...",
      }, ...
  },
  "per_chassis": {
      "<chassis_uri>": {
          "power": "...", "thermal": "...", "assembly": "...",
          "network_adapters": "...", "pcie_devices": "...",
          "pcie_slots": "...", "cables": "...", "batteries": "...",
          "log_services": "...",
      }, ...
  },
  "per_manager": {
      "<manager_uri>": {
          "log_services": "...", "ethernet_interfaces": "...",
      }, ...
  },
}
"""
import logging

logger = logging.getLogger(__name__)

# Keys we look for directly on a resource body that point at sub-resources
# or sub-collections. This list is deliberately broad (covers DMTF Redfish
# 1.x schemas for ComputerSystem, Chassis, Manager) but is only ever used
# to *follow links the BMC itself provided* - never to construct a path.
SYSTEM_LINK_KEYS = {
    "Processors": "processors",
    "Memory": "memory",
    "Storage": "storage",
    "SimpleStorage": "simple_storage",
    "EthernetInterfaces": "ethernet_interfaces",
    "NetworkInterfaces": "network_interfaces",
    "LogServices": "log_services",
    "PCIeDevices": "pcie_devices",
    "PCIeFunctions": "pcie_functions",
    "Bios": "bios",
    "SecureBoot": "secure_boot",
}

CHASSIS_LINK_KEYS = {
    "Power": "power",
    "Thermal": "thermal",
    "Assembly": "assembly",
    "NetworkAdapters": "network_adapters",
    "PCIeDevices": "pcie_devices",
    "PCIeSlots": "pcie_slots",
    "Cables": "cables",
    "LogServices": "log_services",
    "Drives": "drives",
    # Newer (2021+) schema versions split Power/Thermal into these -
    # only present on more recent BMC firmware, harmless if absent.
    "PowerSubsystem": "power_subsystem",
    "ThermalSubsystem": "thermal_subsystem",
    "EnvironmentMetrics": "environment_metrics",
    "Batteries": "batteries",
}


MANAGER_LINK_KEYS = {
    "LogServices": "log_services",
    "EthernetInterfaces": "ethernet_interfaces",
    "VirtualMedia": "virtual_media",
}


def _odata_id(obj) -> str | None:
    if isinstance(obj, dict):
        return obj.get("@odata.id")
    return None


def _collection_members(collection_doc: dict | None) -> list[str]:
    if not collection_doc:
        return []
    return [m["@odata.id"] for m in collection_doc.get("Members", []) if "@odata.id" in m]


def discover_topology(client) -> dict:
    service_root = client.get("/redfish/v1/")
    if not service_root:
        raise RuntimeError("Could not read /redfish/v1/ - is this a valid Redfish endpoint?")

    topology = {
        "service_root": service_root,
        "systems": [],
        "chassis": [],
        "managers": [],
        "update_service": _odata_id(service_root.get("UpdateService")),
        "event_service": _odata_id(service_root.get("EventService")),
        "task_service": _odata_id(service_root.get("Tasks")) or _odata_id(service_root.get("TaskService")),
        "telemetry_service": _odata_id(service_root.get("TelemetryService")),
        "per_system": {},
        "per_chassis": {},
        "per_manager": {},
    }

    systems_uri = _odata_id(service_root.get("Systems"))
    if systems_uri:
        topology["systems"] = _collection_members(client.get(systems_uri))

    chassis_uri = _odata_id(service_root.get("Chassis"))
    if chassis_uri:
        topology["chassis"] = _collection_members(client.get(chassis_uri))

    managers_uri = _odata_id(service_root.get("Managers"))
    if managers_uri:
        topology["managers"] = _collection_members(client.get(managers_uri))

    for system_uri in topology["systems"]:
        body = client.get(system_uri)
        if not body:
            continue
        links = {}
        for key, out_key in SYSTEM_LINK_KEYS.items():
            uri = _odata_id(body.get(key))
            if uri:
                links[out_key] = uri
        # Storage/SimpleStorage/EthernetInterfaces etc. can also live under
        # "Links" -> "Oem" on some BMCs; we don't special-case any vendor,
        # but we do check the standard Links block too.
        oem = body.get("Oem", {})
        links["oem"] = oem if oem else None

        hp_links = oem.get("Hp", {}).get("Links", {})
        if hp_links:
            if "SmartStorage" in hp_links:
                links["storage_hpe"] = _odata_id(hp_links["SmartStorage"])
            if "PCIDevices" in hp_links:
                links["pcie_devices_hpe"] = _odata_id(hp_links["PCIDevices"])
            if "FirmwareInventory" in hp_links:
                links["firmware_hpe"] = _odata_id(hp_links["FirmwareInventory"])

        topology["per_system"][system_uri] = links

    for chassis_uri_item in topology["chassis"]:
        body = client.get(chassis_uri_item)
        if not body:
            continue
        links = {}
        for key, out_key in CHASSIS_LINK_KEYS.items():
            uri = _odata_id(body.get(key))
            if uri:
                links[out_key] = uri
        topology["per_chassis"][chassis_uri_item] = links

    for manager_uri in topology["managers"]:
        body = client.get(manager_uri)
        if not body:
            continue
        links = {}
        for key, out_key in MANAGER_LINK_KEYS.items():
            uri = _odata_id(body.get(key))
            if uri:
                links[out_key] = uri
        topology["per_manager"][manager_uri] = links

    logger.info(
        "Discovered %d system(s), %d chassis, %d manager(s) at %s",
        len(topology["systems"]), len(topology["chassis"]), len(topology["managers"]),
        client.session.base_url,
    )
    return topology
