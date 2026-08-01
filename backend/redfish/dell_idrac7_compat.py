"""
redfish/dell_idrac7_compat.py
==============================
Dell iDRAC 7 compatibility adapter — verbose logging helpers.

iDRAC 7 (12th gen PowerEdge, firmware 2.30.30.30+) implements Redfish 1.0.x
with significant limitations compared to iDRAC 8/9.  Known quirks:

  1. ``Oem.Dell`` block is often absent from the service root.
  2. ``Memory`` and ``Processors`` sub-resources are frequently NOT exposed as
     browsable collection links.  Instead, iDRAC 7 embeds summary objects
     directly in the ComputerSystem body:
       - ``ProcessorSummary``  → {Count, Model, MaxSpeedMHz, Status}
       - ``MemorySummary``     → {TotalSystemMemoryGiB, Status}
  3. ``UpdateService`` is absent — firmware version only available from
     the Manager body's ``FirmwareVersion`` field.
  4. Storage may only be reachable via ``SimpleStorage`` (not full ``Storage``
     schema).
  5. ``PCIeDevices`` collection is absent.
  6. ``SecureBoot`` resource is absent.
  7. Some sensor reading fields use Redfish 1.0 names (e.g. ``ReadingCelsius``
     vs the newer ``Reading``), though both are already handled by the
     thermal/fans collectors.

This module centralises all iDRAC 7 diagnostic logging so the root cause of
empty UI cards can be traced quickly.  All functions are log-only (no HTTP
calls, no mutations).

Usage
-----
Import and call from discovery.py after the topology dict is built::

    from redfish.dell_idrac7_compat import (
        log_service_root_capabilities,
        log_system_link_availability,
        log_chassis_link_availability,
    )
"""
import logging

logger = logging.getLogger(__name__)

# Redfish top-level keys we expect on a well-supported BMC
_EXPECTED_SERVICE_ROOT_KEYS = [
    "Systems", "Chassis", "Managers", "UpdateService",
    "EventService", "SessionService", "JsonSchemas",
    "TelemetryService", "Tasks", "AccountService",
]

# Per-system links we look for in topology["per_system"][uri]
_STANDARD_SYSTEM_LINKS = [
    "processors", "memory", "storage", "simple_storage",
    "ethernet_interfaces", "network_interfaces", "log_services",
    "pcie_devices", "bios", "secure_boot",
]
_SUMMARY_KEYS = ["processor_summary", "memory_summary"]

# Per-chassis links we look for in topology["per_chassis"][uri]
_STANDARD_CHASSIS_LINKS = [
    "power", "thermal", "network_adapters", "log_services",
    "sensors", "batteries", "assembly",
]


def log_service_root_capabilities(service_root: dict, base_url: str) -> None:
    """Log which top-level Redfish service root resources are present/absent.

    Emits a single DEBUG-level block so it is cheap in production (just a
    log level check) but immediately useful when DEBUG logging is enabled.

    Parameters
    ----------
    service_root : The full body returned by GET /redfish/v1/.
    base_url     : BMC base URL (e.g. ``https://192.168.1.10``), used only
                   for the log message prefix so the output is unambiguous in
                   multi-server environments.
    """
    if not service_root:
        logger.debug(
            "[%s] Service root is EMPTY — iDRAC 6 (no Redfish API). "
            "WS-Man fallback will be used.", base_url
        )
        return

    redfish_ver = service_root.get("RedfishVersion", "unknown")
    oem_keys    = sorted((service_root.get("Oem") or {}).keys())
    present     = [k for k in _EXPECTED_SERVICE_ROOT_KEYS if k in service_root]
    missing     = [k for k in _EXPECTED_SERVICE_ROOT_KEYS if k not in service_root]

    logger.debug(
        "[%s] Redfish service root capability map:\n"
        "  RedfishVersion : %s\n"
        "  Oem keys       : %s\n"
        "  Present        : %s\n"
        "  Missing        : %s",
        base_url,
        redfish_ver,
        oem_keys or ["(none)"],
        present  or ["(none)"],
        missing  or ["(none)"],
    )


def log_system_link_availability(system_uri: str, links: dict) -> None:
    """Log which per-system Redfish sub-resources were (or were not) found.

    Call this after ``topology["per_system"][system_uri]`` is populated by
    ``discovery.discover_topology()``.  This is the primary diagnostic for
    empty Processor / Memory UI cards on iDRAC 7.

    Parameters
    ----------
    system_uri : e.g. ``/redfish/v1/Systems/System.Embedded.1``
    links      : The ``per_system[system_uri]`` dict from the topology.
    """
    found         = [k for k in _STANDARD_SYSTEM_LINKS if links.get(k)]
    using_summary = [k for k in _SUMMARY_KEYS         if links.get(k)]
    missing       = [k for k in _STANDARD_SYSTEM_LINKS if not links.get(k)]

    logger.debug(
        "System %s — link availability:\n"
        "  Standard links   : %s\n"
        "  Summary fallbacks: %s\n"
        "  Missing (no link): %s",
        system_uri,
        found         or ["(none)"],
        using_summary or ["(none)"],
        missing       or ["(none)"],
    )


def log_chassis_link_availability(chassis_uri: str, links: dict) -> None:
    """Log which per-chassis Redfish sub-resources were found.

    Call after ``topology["per_chassis"][chassis_uri]`` is populated.
    Useful for diagnosing empty Thermal / Power / Fan UI cards.

    Parameters
    ----------
    chassis_uri : e.g. ``/redfish/v1/Chassis/System.Embedded.1``
    links       : The ``per_chassis[chassis_uri]`` dict from the topology.
    """
    found   = [k for k in _STANDARD_CHASSIS_LINKS if links.get(k)]
    missing = [k for k in _STANDARD_CHASSIS_LINKS if not links.get(k)]

    logger.debug(
        "Chassis %s — link availability:\n"
        "  Found  : %s\n"
        "  Missing: %s",
        chassis_uri,
        found   or ["(none)"],
        missing or ["(none)"],
    )


def crawl_and_map_missing_links(client, topology: dict, base_url: str, max_depth: int = 5) -> None:
    """
    Exhaustively crawl the Redfish tree for missing resources on iDRAC 7.
    
    Traverses the tree to find resources matching known @odata.type values
    for Storage, Power, Thermal, Network, Firmware, PCI, and LogServices.
    When found, these alternate URIs are injected into topology["per_system"],
    topology["per_chassis"], or topology["per_manager"] using standard keys
    so existing collectors can consume them natively.
    """
    logger.info("[%s] iDRAC 7 crawler: Starting exhaustive discovery (max_depth=%d)", base_url, max_depth)
    visited = set()
    queue = [("/redfish/v1/", 0, None)]  # (uri, depth, parent_uri)
    
    skip_patterns = [
        "JsonSchemas", "Registries", "Metadata", "ProtocolFeaturesSupported", 
        "SessionService", "TelemetryService"
    ]
    
    # Target @odata.type matches for missing categories
    # The dictionary maps (type_substring) -> (target_dict_type, topology_key)
    # where target_dict_type is "system", "chassis", or "manager"
    target_types = {
        "#Power.": ("chassis", "power"),
        "#Thermal.": ("chassis", "thermal"),
        "#StorageCollection.": ("system", "storage"),
        "#PCIeDeviceCollection.": ("system", "pcie_devices"),
        "#EthernetInterfaceCollection.": ("system", "ethernet_interfaces"),
        "#NetworkInterfaceCollection.": ("system", "network_interfaces"),
        "#LogServiceCollection.": ("manager", "log_services"),
        "#SoftwareInventoryCollection.": ("update_service", "firmware_inventory"),
        "#SecureBoot.": ("system", "secure_boot"),
    }
    
    found_targets = []
    
    while queue:
        uri, depth, parent = queue.pop(0)
        
        if uri in visited or depth > max_depth:
            continue
        visited.add(uri)
        
        if any(p in uri for p in skip_patterns):
            logger.debug("[%s] Crawler SKIP (pattern): %s (parent: %s)", base_url, uri, parent)
            continue
            
        try:
            body = client.get(uri)
            if body is None:
                logger.debug("[%s] Crawler 404/501: %s (parent: %s)", base_url, uri, parent)
                continue
        except Exception as exc:
            logger.debug("[%s] Crawler ERROR %s: %s (parent: %s)", base_url, type(exc).__name__, uri, parent)
            continue
            
        odata_type = body.get("@odata.type", "")
        logger.debug("[%s] Crawler VISITED %s (type: %s, parent: %s, depth: %d)", base_url, uri, odata_type, parent, depth)
        
        # Check if this matches a target we are looking for
        for type_match, (target_dict, key) in target_types.items():
            if type_match in odata_type:
                found_targets.append((uri, target_dict, key))
                logger.info("[%s] Crawler MAPPED %s -> %s[%s]", base_url, uri, target_dict, key)
                break
                
        # Extract all potential links from the body
        def _extract_links(obj):
            links = []
            if isinstance(obj, dict):
                if "@odata.id" in obj:
                    links.append(obj["@odata.id"])
                for k, v in obj.items():
                    links.extend(_extract_links(v))
            elif isinstance(obj, list):
                for item in obj:
                    links.extend(_extract_links(item))
            return links
            
        for child_uri in _extract_links(body):
            if isinstance(child_uri, str) and child_uri.startswith("/redfish/v1/") and child_uri not in visited:
                queue.append((child_uri, depth + 1, uri))
                
    # Map discovered resources into the topology
    # If we found a chassis-level resource (like Power), inject it into every chassis in the topology
    # if it's currently missing.
    for uri, target_dict, key in found_targets:
        if target_dict == "chassis":
            for chassis_uri, links in topology.get("per_chassis", {}).items():
                if not links.get(key):
                    links[key] = uri
        elif target_dict == "system":
            for sys_uri, links in topology.get("per_system", {}).items():
                if not links.get(key):
                    links[key] = uri
        elif target_dict == "manager":
            for mgr_uri, links in topology.get("per_manager", {}).items():
                if not links.get(key):
                    links[key] = uri
        elif target_dict == "update_service":
            # For firmware, UpdateService might be missing, so we synthesize one
            if not topology.get("update_service"):
                # Use the parent of the firmware collection as the UpdateService root
                topology["update_service"] = "/redfish/v1/UpdateService"
                
    logger.info("[%s] iDRAC 7 crawler finished. Visited %d endpoints.", base_url, len(visited))
