"""
redfish/collectors/common.py
=============================
Small normalization helpers shared by every collector, so category
modules stay focused on "which Redfish endpoints do I read" rather than
repeating boilerplate for pulling Status.Health/State out of a resource.
"""


def status_health(resource: dict) -> str | None:
    return (resource.get("Status") or {}).get("Health")


def status_state(resource: dict) -> str | None:
    return (resource.get("Status") or {}).get("State")


def component(category, odata_id, name, raw_json, location=None, health=None, state=None):
    return {
        "category": category,
        "odata_id": odata_id,
        "name": name,
        "health": health if health is not None else status_health(raw_json),
        "state": state if state is not None else status_state(raw_json),
        "location": location,
        "raw_json": raw_json,
    }


def reading(metric, source_name, value, unit=None):
    if value is None:
        return None
    return {"metric": metric, "source_name": source_name, "value": float(value), "unit": unit}


def collection_members(client, collection_uri):
    """GET a Redfish collection and return the list of full member
    resources (not just their URIs) - this is the shape almost every
    collector needs."""
    if not collection_uri:
        return []
    coll = client.get(collection_uri)
    if not coll:
        return []
    members = []
    for m in coll.get("Members", []):
        uri = m.get("@odata.id")
        if not uri:
            continue
        body = client.get(uri)
        if body:
            members.append(body)
    return members
