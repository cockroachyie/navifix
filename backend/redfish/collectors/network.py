"""
redfish/collectors/network.py
================================
Redfish resources consumed
---------------------------
- Systems/{id}/EthernetInterfaces (collection) - OS-visible NICs with IP
- Systems/{id}/NetworkInterfaces (collection) -> NetworkInterfaces/{id}
  -> NetworkPorts and NetworkDeviceFunctions (physical adapter detail)
- Chassis/{id}/NetworkAdapters (collection) -> NetworkAdapters/{id}
  -> NetworkPorts -> NetworkDeviceFunctions (PCIe card detail)
- Managers/{id}/EthernetInterfaces (BMC management port)

Every NIC exposed gets its own Component row. The raw_json contains ALL
fields: MACAddress, SpeedMbps, LinkStatus, AutoNeg, FullDuplex, IPv4/IPv6
addresses, VLANs, MTU, PermanentMACAddress, FirmwareVersion, and OEM.
"""
from .common import component, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components = []
    seen = set()

    def add_nic(body, extra_name=None):
        uri = body.get("@odata.id")
        if not uri or uri in seen:
            return
        seen.add(uri)
        name = extra_name or body.get("Name") or body.get("Id") or "NIC"
        components.append(component(
            ComponentCategory.NETWORK_INTERFACE, uri, name, body,
        ))

    # ── Per-system EthernetInterfaces ───────────────────────────────────
    for system_uri, links in topology.get("per_system", {}).items():
        eth_uri = links.get("ethernet_interfaces")
        if eth_uri:
            for nic in collection_members(client, eth_uri):
                add_nic(nic)

        # NetworkInterfaces -> drill to DeviceFunctions for full adapter detail
        ni_uri = links.get("network_interfaces")
        if ni_uri:
            for ni in collection_members(client, ni_uri):
                ni_body = ni if ni.get("NetworkDeviceFunctions") else client.get(ni.get("@odata.id"))
                if not ni_body:
                    continue
                ndf_coll = (ni_body.get("NetworkDeviceFunctions") or {}).get("@odata.id")
                if ndf_coll:
                    for ndf in collection_members(client, ndf_coll):
                        add_nic(ndf)
                np_coll = (ni_body.get("NetworkPorts") or {}).get("@odata.id")
                if np_coll:
                    for np_body in collection_members(client, np_coll):
                        add_nic(np_body)

    # ── Per-chassis NetworkAdapters (PCIe NIC cards) ────────────────────
    for chassis_uri, links in topology.get("per_chassis", {}).items():
        na_uri = links.get("network_adapters")
        if na_uri:
            for adapter in collection_members(client, na_uri):
                a_uri = adapter.get("@odata.id")
                if not a_uri:
                    continue
                # Get full adapter body to find its ports/functions
                a_body = client.get(a_uri)
                if not a_body:
                    continue
                add_nic(a_body, extra_name=a_body.get("Name") or "Network Adapter")

                # Ports
                ports_link = (a_body.get("NetworkPorts") or {}).get("@odata.id")
                if ports_link:
                    for port in collection_members(client, ports_link):
                        add_nic(port)

                # Device functions (carry firmware version, link speed, etc.)
                funcs_link = (a_body.get("NetworkDeviceFunctions") or {}).get("@odata.id")
                if funcs_link:
                    for func in collection_members(client, funcs_link):
                        add_nic(func)

    # ── Manager / BMC ethernet (management port) ─────────────────────────
    for manager_uri, links in topology.get("per_manager", {}).items():
        eth_uri = links.get("ethernet_interfaces")
        if eth_uri:
            for nic in collection_members(client, eth_uri):
                add_nic(nic)

    return components, []
