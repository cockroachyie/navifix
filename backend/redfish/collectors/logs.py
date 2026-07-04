"""
redfish/collectors/logs.py
============================
Redfish resources consumed
---------------------------
- Systems/{id}/LogServices (collection) -> LogServices/{svc}/Entries
- Managers/{id}/LogServices (collection) -> LogServices/{svc}/Entries
- Chassis/{id}/LogServices (collection) -> LogServices/{svc}/Entries

Covers whatever log services a given BMC exposes under any of these three
resource types - "SEL" and "Lifecycle Log" (Dell), "IML" (HPE), "Active
Log" (Lenovo XCC), etc. We never assume a specific service name; we just
enumerate whatever LogServices collection(s) exist and pull entries from
each.

Unlike other collectors this returns LogEntry-shaped dicts (not
Component-shaped), because log entries are an append-only event stream,
not a piece of hardware with current state.
"""
import logging

logger = logging.getLogger(__name__)


def _entries_from_service(client, log_service_uri):
    body = client.get(log_service_uri)
    if not body:
        return []
    entries_uri = (body.get("Entries") or {}).get("@odata.id")
    if not entries_uri:
        return []
    coll = client.get(entries_uri)
    if not coll:
        return []
    out = []
    for e in coll.get("Members", []):
        # Entries are sometimes embedded directly in the collection, and
        # sometimes only linked - handle both.
        if "@odata.id" in e and len(e) == 1:
            full = client.get(e["@odata.id"])
            if full:
                out.append(full)
        else:
            out.append(e)
    return out, body.get("Name", "Log Service")


def collect(client, server, topology):
    """Returns a flat list of dicts ready to be upserted into LogEntry:
    {log_service, entry_id, severity, message, message_id, sensor_type,
    created_at (raw ISO string from Redfish), raw_json}
    """
    log_entries = []

    log_service_collections = []
    for uris in topology.get("per_system", {}).values():
        if uris.get("log_services"):
            log_service_collections.append(uris["log_services"])
    for uris in topology.get("per_manager", {}).values():
        if uris.get("log_services"):
            log_service_collections.append(uris["log_services"])
    for uris in topology.get("per_chassis", {}).values():
        if uris.get("log_services"):
            log_service_collections.append(uris["log_services"])

    for collection_uri in log_service_collections:
        coll = client.get(collection_uri)
        if not coll:
            continue
        for svc_member in coll.get("Members", []):
            svc_uri = svc_member.get("@odata.id")
            if not svc_uri:
                continue
            try:
                result = _entries_from_service(client, svc_uri)
            except Exception as exc:  # noqa: BLE001 - log services are optional/best-effort
                logger.warning("Failed reading log service %s: %s", svc_uri, exc)
                continue
            if not result:
                continue
            entries, service_name = result
            for e in entries:
                log_entries.append({
                    "log_service": service_name,
                    "entry_id": e.get("Id") or e.get("@odata.id"),
                    "severity": (e.get("Severity") or "OK"),
                    "message": e.get("Message"),
                    "message_id": e.get("MessageId"),
                    "sensor_type": e.get("SensorType"),
                    "created_raw": e.get("Created"),
                    "raw_json": e,
                })

    return log_entries
