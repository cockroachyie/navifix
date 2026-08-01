"""
redfish/collectors/pcie.py
============================
Redfish resources consumed
---------------------------
- Systems/{id}/PCIeDevices (collection) -> PCIeDevices/{id}
  -> PCIeDevices/{id}/PCIeFunctions (collection) -> PCIeFunctions/{id}
- Chassis/{id}/PCIeDevices (collection) — same device, exposed at chassis level
  on some vendors (Dell iDRAC in particular links them from both places)
- Chassis/{id}/PCIeSlots — physical slot information
- Chassis/{id}/Cables — physical cable/connector inventory (newer schema, some
  vendors use this for PCIe cables, SAS/NVMe backplane cables, etc.)

Captures every PCIe device and its functions: DeviceClass (GPU, NIC, RAID,
HBA, etc.), Manufacturer, DeviceId, VendorId, SubsystemId, SubsystemVendorId,
ClassCode, FunctionId, PCIeInterface (PCIeType, MaxPCIeType, LanesInUse,
MaxLanes), FirmwareVersion, Status, and any OEM extensions.
"""
from .common import component, collection_members
from database.models import ComponentCategory


def collect(client, server, topology):
    components = []
    seen = set()

    def _add_device(dev_body):
        uri = dev_body.get("@odata.id")
        if not uri or uri in seen:
            return
        seen.add(uri)
        name = (
            dev_body.get("Name")
            or dev_body.get("Manufacturer", "")
            + " "
            + dev_body.get("Model", dev_body.get("Id", "PCIe Device"))
        ).strip()
        components.append(component(
            ComponentCategory.PCIE_DEVICE, uri, name, dev_body,
        ))

        # Drill into PCIeFunctions for per-function detail (device class, IDs)
        funcs_link = (dev_body.get("PCIeFunctions") or {}).get("@odata.id")
        if funcs_link:
            for func in collection_members(client, funcs_link):
                func_uri = func.get("@odata.id")
                if func_uri and func_uri not in seen:
                    seen.add(func_uri)
                    func_name = (
                        func.get("Name")
                        or f"{name} Function {func.get('FunctionId', func.get('Id', ''))}"
                    )
                    components.append(component(
                        ComponentCategory.PCIE_DEVICE, func_uri, func_name, func,
                    ))

    # ── System-level PCIeDevices & Slots ──────────────────────────────────
    for system_uri, links in topology.get("per_system", {}).items():
        pcie_uri = links.get("pcie_devices")
        if pcie_uri:
            for dev in collection_members(client, pcie_uri):
                _add_device(dev)

        hpe_pcie_uri = links.get("pcie_devices_hpe") or links.get("pci_devices_hpe")
        if hpe_pcie_uri:
            for dev in collection_members(client, hpe_pcie_uri):
                _add_device(dev)

        # HP iLO 4 / Gen9 PCISlots are collections of individual slot resources
        hpe_slots_uri = links.get("pcie_slots_hpe") or links.get("pci_slots_hpe")
        if hpe_slots_uri:
            for slot in collection_members(client, hpe_slots_uri):
                slot_uri = slot.get("@odata.id")
                if slot_uri and slot_uri not in seen:
                    seen.add(slot_uri)
                    slot_name = slot.get("Name") or f"PCIe Slot {slot.get('Id', '')}"
                    components.append(component(
                        ComponentCategory.PCIE_DEVICE, slot_uri, slot_name, slot,
                    ))

        # Direct PCIeFunction links (some systems expose them directly)
        func_uri = links.get("pcie_functions")
        if func_uri:
            for func in collection_members(client, func_uri):
                func_id = func.get("@odata.id")
                if func_id and func_id not in seen:
                    seen.add(func_id)
                    components.append(component(
                        ComponentCategory.PCIE_DEVICE, func_id,
                        func.get("Name") or f"PCIe Function {func.get('Id', '')}",
                        func,
                    ))

    # ── Chassis-level PCIeDevices ────────────────────────────────────────
    for chassis_uri, links in topology.get("per_chassis", {}).items():
        pcie_uri = links.get("pcie_devices")
        if pcie_uri:
            for dev in collection_members(client, pcie_uri):
                _add_device(dev)

        pcie_slots_uri = links.get("pcie_slots")
        if pcie_slots_uri:
            slots_body = client.get(pcie_slots_uri)
            if slots_body:
                for slot in (slots_body.get("Slots") or []):
                    # Slots is an array embedded in the PCIeSlots resource
                    slot_id = f"{chassis_uri}#pcieslot#{slot.get('SlotNumber', slot.get('PCIeType',''))}"
                    if slot_id not in seen:
                        seen.add(slot_id)
                        name = f"PCIe Slot {slot.get('SlotNumber', '')}"
                        components.append(component(
                            ComponentCategory.PCIE_DEVICE, slot_id, name, slot,
                        ))

        # ── Cables collection (newer Redfish schema, some vendors) ───────
        # Chassis/{id}/Cables exposes physical cable/connector inventory.
        # Included here because cables are most naturally shown alongside
        # PCI/expansion devices in the "PCI / Cables" UI card.
        cables_uri = links.get("cables")
        if cables_uri:
            for cable in collection_members(client, cables_uri):
                cable_uri = cable.get("@odata.id")
                if cable_uri and cable_uri not in seen:
                    seen.add(cable_uri)
                    cable_name = (
                        cable.get("Name")
                        or cable.get("CableType", "Cable")
                        + " "
                        + cable.get("Id", "")
                    ).strip()
                    components.append(component(
                        ComponentCategory.PCIE_DEVICE, cable_uri, cable_name, cable,
                    ))

    return components, []
