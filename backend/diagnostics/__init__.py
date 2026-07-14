from .base import DiagnosticsAdapter
from .dell import DellDiagnosticsAdapter
from .exceptions import DiagnosticsError, DiagnosticsUnsupported, DiagnosticsTimeout

_ADAPTERS = {
    "dell": DellDiagnosticsAdapter(),
}


def get_adapter(manufacturer: str) -> DiagnosticsAdapter:
    """Resolve a vendor string (from server.vendor / Redfish Manufacturer
    field) to the adapter that knows how to pull a support bundle for it.
    Adding a new vendor later = one adapter file + one line here."""
    if not manufacturer:
        raise DiagnosticsUnsupported("No vendor detected for this server")
    m = manufacturer.lower()
    if "dell" in m:
        return _ADAPTERS["dell"]
    raise DiagnosticsUnsupported(f"No diagnostics adapter available for vendor {manufacturer!r}")
