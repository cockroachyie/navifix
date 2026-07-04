"""
redfish/events.py
==================
Redfish resources consumed
---------------------------
- EventService -> Subscriptions (collection): POST a Subscription
  resource pointing at our own webhook so the BMC pushes
  StatusChange/Alert/ResourceAdded/ResourceRemoved events to us in near
  real time, instead of us having to wait for the next 30-second poll.

Not every BMC implements EventService (it's optional in the spec), so the
poller always keeps 30-second polling as the baseline and layers event
subscriptions on top opportunistically - see scheduler/poller.py.
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_EVENT_TYPES = ["StatusChange", "Alert", "ResourceAdded", "ResourceRemoved", "ResourceUpdated"]


def subscribe(client, topology, webhook_url: str, server_id: str) -> str | None:
    """Create (or reuse) a subscription pointed at our webhook. Returns the
    subscription's @odata.id on success, or None if EventService isn't
    supported by this BMC."""
    event_service_uri = topology.get("event_service")
    if not event_service_uri:
        return None

    service_body = client.get(event_service_uri)
    if not service_body:
        return None

    subs_uri = (service_body.get("Subscriptions") or {}).get("@odata.id")
    if not subs_uri:
        return None

    # Avoid creating duplicate subscriptions on every inventory refresh -
    # check if one pointing at our webhook already exists.
    existing = client.get(subs_uri)
    for member in (existing or {}).get("Members", []):
        sub_body = client.get(member["@odata.id"])
        if sub_body and sub_body.get("Destination") == webhook_url:
            return sub_body.get("@odata.id")

    payload = {
        "Destination": webhook_url,
        "EventTypes": DEFAULT_EVENT_TYPES,
        "Protocol": "Redfish",
        "Context": server_id,
    }
    try:
        result = client.post(subs_uri, json_body=payload)
    except Exception as exc:  # noqa: BLE001 - subscription is best-effort
        logger.warning("Event subscription failed for %s: %s", event_service_uri, exc)
        return None

    if result:
        logger.info("Subscribed to Redfish events for server %s", server_id)
        return result.get("@odata.id")
    return None


def parse_event_payload(body: dict) -> list[dict]:
    """Normalize an inbound Redfish EventRecord payload (POSTed to our
    webhook by a BMC) into a flat list of {message, severity, message_id,
    origin, context} dicts for the alert engine to consume."""
    events = []
    for record in body.get("Events", []):
        events.append({
            "message": record.get("Message"),
            "severity": record.get("Severity", "OK"),
            "message_id": record.get("MessageId"),
            "origin": record.get("OriginOfCondition", {}).get("@odata.id") if isinstance(record.get("OriginOfCondition"), dict) else None,
            "context": body.get("Context"),
        })
    return events
