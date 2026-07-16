"""
redfish/inventory.py
=====================
Runs once at server-add time and periodically thereafter (see
scheduler/poller.py -> INVENTORY_REFRESH_INTERVAL_SECONDS) to (re)populate
the slow-changing identity fields of a Server row: vendor, model, service
tag, serial number, asset tag, BMC firmware version, and the cached
top-level Redfish URIs (system/chassis/manager) that the live-polling
collectors use so they don't have to re-run full discovery every 30
seconds.
"""
import logging
from datetime import datetime

from . import discovery

logger = logging.getLogger(__name__)


def _guess_vendor(service_root: dict, system_body: dict | None) -> str | None:
    # Redfish does not mandate a single "Vendor" field on the service root,
    # so we check the handful of places vendors actually put it, in order
    # of reliability, without assuming any one of them exists.
    candidates = [
        service_root.get("Vendor"),
        service_root.get("Oem", {}).get("Dell", {}) and "Dell",
        service_root.get("Oem", {}).get("Hpe", {}) and "HPE",
        service_root.get("Oem", {}).get("Hp", {}) and "HPE",
        service_root.get("Oem", {}).get("Lenovo", {}) and "Lenovo",
        service_root.get("Oem", {}).get("Supermicro", {}) and "Supermicro",
    ]
    if system_body:
        server_row.model = system_body.get("Model")
        server_row.serial_number = system_body.get("SerialNumber")
        server_row.asset_tag = system_body.get("AssetTag")
        # SKU means different things per vendor: Dell puts the Service Tag
        # there, but HPE (and possibly others) put the Product ID/SKU
        # instead - which is not a unique-per-unit identifier and must
        # never be shown as if it were one. Only trust SKU as a service
        # tag for Dell; every other vendor's unique identifier is
        # SerialNumber, already captured above.
        is_dell = bool(server_row.vendor) and "dell" in server_row.vendor.lower()
        server_row.service_tag = system_body.get("SKU") if is_dell else None
        power_state = system_body.get("PowerState")


def refresh_inventory(client, server_row, db_session):
    """Discover topology and update the Server row's identity fields.
    Returns the discovered topology dict so callers can reuse it
    immediately for a first collection pass without discovering twice.
    """
    topology = discovery.discover_topology(client)
    service_root = topology["service_root"]

    primary_system_uri = topology["systems"][0] if topology["systems"] else None
    primary_chassis_uri = topology["chassis"][0] if topology["chassis"] else None
    primary_manager_uri = topology["managers"][0] if topology["managers"] else None

    system_body = client.get(primary_system_uri) if primary_system_uri else None
    chassis_body = client.get(primary_chassis_uri) if primary_chassis_uri else None
    manager_body = client.get(primary_manager_uri) if primary_manager_uri else None

    server_row.redfish_service_root = service_root
    server_row.redfish_system_uri = primary_system_uri
    server_row.redfish_chassis_uri = primary_chassis_uri
    server_row.redfish_manager_uri = primary_manager_uri

    server_row.vendor = _guess_vendor(service_root, system_body)

    if system_body:
        server_row.model = system_body.get("Model")
        server_row.serial_number = system_body.get("SerialNumber")
        server_row.asset_tag = system_body.get("AssetTag")
        # Dell exposes the service tag as SKU on ComputerSystem; other
        # vendors don't have a separate concept, so we fall back sensibly.
        server_row.service_tag = system_body.get("SKU") or system_body.get("SerialNumber")
        power_state = system_body.get("PowerState")
        if power_state:
            server_row.power_state = power_state

        status = system_body.get("Status", {})
        if status.get("Health"):
            server_row.health_status = status.get("Health")

    if manager_body:
        server_row.firmware_version = manager_body.get("FirmwareVersion")

    server_row.supports_event_service = bool(topology.get("event_service"))
    server_row.updated_at = datetime.utcnow()

    db_session.add(server_row)
    db_session.commit()

    logger.info(
        "Inventory refreshed for %s (%s): vendor=%s model=%s serial=%s",
        server_row.hostname, server_row.ip_address,
        server_row.vendor, server_row.model, server_row.serial_number,
    )
    return topology
