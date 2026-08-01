"""
redfish/dell_wsman.py
======================
Dell WS-Man (Web Services for Management) client for iDRAC 6.

iDRAC 6 does NOT support the Redfish API.  It supports WS-Man — a
SOAP-over-HTTP management protocol that exposes hardware inventory through
Dell DCIM (Dell Common Information Model) schema URIs.

This implementation uses raw HTTP POST with hand-crafted SOAP/XML envelopes
so there are NO external pip dependencies beyond the Python standard library.

WS-Man primer
-------------
All requests are HTTP POST to ``https://{host}/wsman`` with:
  Content-Type: application/soap+xml;charset=UTF-8
  Authorization: Basic <base64 credentials>

Two operations are used:
  Enumerate  — start a new enumeration of all instances of a DCIM class
  Pull       — retrieve the next page of results (when > MaxElements items)

The response is a SOAP envelope containing a list of CIM instances, each
represented as a set of child XML elements whose text values carry the
property values.

DCIM schemas queried
---------------------
  DCIM_SystemView         — server identity (model, serial, service tag)
  DCIM_CPUView            — processor inventory
  DCIM_MemoryView         — DIMM inventory
  DCIM_PhysicalDiskView   — physical drive inventory
  DCIM_ControllerView     — storage controller inventory
  DCIM_NICView            — NIC inventory
  DCIM_PowerSupplyView    — PSU inventory
  DCIM_NumericSensorView  — thermal / voltage / fan sensors
  DCIM_SoftwareIdentityView — firmware components (best-effort; may be absent)

SSL / TLS
---------
iDRAC 6 uses older TLS 1.0 cipher suites.  The client attempts to widen
the cipher list and minimum version to accommodate this.  ``verify_tls``
defaults to False because iDRAC 6 self-signed certs are common in the field.
"""
import base64
import logging
import ssl
import uuid as _uuid
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Base namespace URI for all Dell DCIM schemas
_DCIM_BASE = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/root/dcim/"

# Well-known DCIM resource URIs
DCIM_RESOURCES = {
    "system":     [_DCIM_BASE + "DCIM_SystemView"],
    "cpu":        [_DCIM_BASE + "DCIM_CPUView"],
    "memory":     [_DCIM_BASE + "DCIM_MemoryView"],
    "disk":       [
        _DCIM_BASE + "DCIM_PhysicalDiskView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_PhysicalDiskView"
    ],
    "controller": [
        _DCIM_BASE + "DCIM_ControllerView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_ControllerView"
    ],
    "nic":        [
        _DCIM_BASE + "DCIM_NICView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_NICView"
    ],
    "psu":        [
        _DCIM_BASE + "DCIM_PowerSupplyView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_PowerSupplyView",
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_PowerSupply"
    ],
    "sensor":     [
        _DCIM_BASE + "DCIM_NumericSensorView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_NumericSensorView",
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_NumericSensor"
    ],
    "firmware":   [
        _DCIM_BASE + "DCIM_SoftwareIdentityView",
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_SoftwareIdentity"
    ],
    "pci":        [
        _DCIM_BASE + "DCIM_PCIDeviceView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_PCIDeviceView"
    ],
    "log":        [
        _DCIM_BASE + "DCIM_LifecycleLogView",
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/DCIM_LogEntry"
    ],
    "fans":       [
        _DCIM_BASE + "DCIM_FanView"
    ],
    "chassis":    [
        _DCIM_BASE + "DCIM_EnclosureView",
        "http://schemas.dell.com/wbem/wscim/1/cim-schema/2/DCIM_EnclosureView",
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Chassis"
    ],
    "account":    [
        "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_Account"
    ]
}

# WS-Man action URIs
_WSMAN_NS = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
_ACTION_ENUMERATE = "http://schemas.xmlsoap.org/ws/2004/09/enumeration/Enumerate"
_ACTION_PULL      = "http://schemas.xmlsoap.org/ws/2004/09/enumeration/Pull"
_WSA_ANON        = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
_WSEN_NS         = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
_END_OF_SEQ      = f"{{{_WSEN_NS}}}EndOfSequence"


def _build_ssl_context(verify: bool) -> ssl.SSLContext:
    """Build an SSL context that accepts legacy iDRAC 6 TLS configurations."""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # iDRAC 6 uses TLS 1.0 — widen the minimum version
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except Exception:
        try:
            ctx.options &= ~ssl.OP_NO_TLSv1
            ctx.options &= ~ssl.OP_NO_TLSv1_1
        except Exception:
            pass
    # Accept older cipher suites
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except Exception:
        try:
            ctx.set_ciphers("ALL")
        except Exception:
            pass
    return ctx


class WsManClient:
    """
    Minimal WS-Man client for iDRAC 6 using raw HTTP POST.

    Parameters
    ----------
    host       : BMC hostname or IP address (without scheme).
    username   : iDRAC username.
    password   : iDRAC password (plain-text; encrypted at the caller layer).
    verify_tls : Whether to verify the BMC TLS certificate.  Defaults to
                 False because iDRAC 6 typically uses self-signed certs.
    """

    def __init__(self, host: str, username: str, password: str, verify_tls: bool = False):
        # Normalise: strip scheme and trailing slash
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.url  = f"https://{self.host}/wsman"
        self.username   = username
        self.password   = password
        self.verify_tls = verify_tls

        # Pre-compute Basic Auth header (reused for every request)
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {creds}"
        self._ssl_ctx = _build_ssl_context(verify_tls)

    # ── Public API ────────────────────────────────────────────────────────

    def enumerate(self, resource_uri: str) -> list[dict]:
        """
        Enumerate all instances of a DCIM schema resource.

        Sends an Enumerate request, then issues Pull requests until the
        server signals EndOfSequence (all results returned).

        Returns a (possibly empty) list of dicts, one per CIM instance.
        Property names are the bare XML tag names (namespace stripped).
        """
        envelope = self._build_enumerate_envelope(resource_uri)
        try:
            resp_xml = self._post(envelope)
        except Exception as exc:
            logger.debug(
                "WS-Man Enumerate failed for %s@%s: %s",
                resource_uri.split("/")[-1], self.host, exc,
            )
            return []

        items, enum_ctx = self._parse_response(resp_xml, resource_uri)

        # Follow Pull pages until end-of-sequence
        while enum_ctx:
            pull_env = self._build_pull_envelope(resource_uri, enum_ctx)
            try:
                pull_xml = self._post(pull_env)
            except Exception as exc:
                logger.debug(
                    "WS-Man Pull failed for %s@%s: %s",
                    resource_uri.split("/")[-1], self.host, exc,
                )
                break
            more_items, enum_ctx = self._parse_response(pull_xml, resource_uri)
            items.extend(more_items)

        logger.debug(
            "WS-Man enumerated %d item(s) from %s on %s",
            len(items), resource_uri.split("/")[-1], self.host,
        )
        return items

    # ── SOAP envelope builders ────────────────────────────────────────────

    def _build_enumerate_envelope(self, resource_uri: str) -> str:
        msg_id = str(_uuid.uuid4())
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<s:Envelope\n'
            '    xmlns:s="http://www.w3.org/2003/05/soap-envelope"\n'
            '    xmlns:wsman="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"\n'
            '    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"\n'
            '    xmlns:wsen="http://schemas.xmlsoap.org/ws/2004/09/enumeration">\n'
            '  <s:Header>\n'
            f'    <wsa:To>{self.url}</wsa:To>\n'
            '    <wsa:ReplyTo>\n'
            f'      <wsa:Address s:mustUnderstand="true">{_WSA_ANON}</wsa:Address>\n'
            '    </wsa:ReplyTo>\n'
            f'    <wsa:MessageID>uuid:{msg_id}</wsa:MessageID>\n'
            f'    <wsa:Action s:mustUnderstand="true">{_ACTION_ENUMERATE}</wsa:Action>\n'
            f'    <wsman:ResourceURI s:mustUnderstand="true">{resource_uri}</wsman:ResourceURI>\n'
            '    <wsman:OperationTimeout>PT60S</wsman:OperationTimeout>\n'
            '  </s:Header>\n'
            '  <s:Body>\n'
            '    <wsen:Enumerate>\n'
            '      <wsman:OptimizeEnumeration/>\n'
            '      <wsman:MaxElements>64</wsman:MaxElements>\n'
            '    </wsen:Enumerate>\n'
            '  </s:Body>\n'
            '</s:Envelope>'
        )

    def _build_pull_envelope(self, resource_uri: str, enum_context: str) -> str:
        msg_id = str(_uuid.uuid4())
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<s:Envelope\n'
            '    xmlns:s="http://www.w3.org/2003/05/soap-envelope"\n'
            '    xmlns:wsman="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"\n'
            '    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"\n'
            '    xmlns:wsen="http://schemas.xmlsoap.org/ws/2004/09/enumeration">\n'
            '  <s:Header>\n'
            f'    <wsa:To>{self.url}</wsa:To>\n'
            '    <wsa:ReplyTo>\n'
            f'      <wsa:Address s:mustUnderstand="true">{_WSA_ANON}</wsa:Address>\n'
            '    </wsa:ReplyTo>\n'
            f'    <wsa:MessageID>uuid:{msg_id}</wsa:MessageID>\n'
            f'    <wsa:Action s:mustUnderstand="true">{_ACTION_PULL}</wsa:Action>\n'
            f'    <wsman:ResourceURI s:mustUnderstand="true">{resource_uri}</wsman:ResourceURI>\n'
            '    <wsman:OperationTimeout>PT60S</wsman:OperationTimeout>\n'
            '  </s:Header>\n'
            '  <s:Body>\n'
            '    <wsen:Pull>\n'
            f'      <wsen:EnumerationContext>{enum_context}</wsen:EnumerationContext>\n'
            '      <wsen:MaxElements>64</wsen:MaxElements>\n'
            '    </wsen:Pull>\n'
            '  </s:Body>\n'
            '</s:Envelope>'
        )

    # ── HTTP transport ────────────────────────────────────────────────────

    def _post(self, xml_body: str) -> str:
        """POST a SOAP envelope and return the raw response body string."""
        body_bytes = xml_body.encode("utf-8")
        req = Request(
            self.url,
            data=body_bytes,
            headers={
                "Content-Type":   "application/soap+xml;charset=UTF-8",
                "Authorization":  self._auth_header,
                "Content-Length": str(len(body_bytes)),
            },
            method="POST",
        )
        try:
            with urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise RuntimeError(
                f"WS-Man HTTP {exc.code} {exc.reason} from {self.host}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"WS-Man connection error to {self.host}: {exc.reason}"
            ) from exc

    # ── XML parsing ───────────────────────────────────────────────────────

    def _parse_response(self, xml_str: str, resource_uri: str) -> tuple[list[dict], str | None]:
        """Parse a SOAP EnumerateResponse or PullResponse.

        Returns (items, enum_context_or_None).
        enum_context is None when EndOfSequence is signalled.
        """
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            logger.debug(
                "WS-Man XML parse error for %s@%s: %s",
                resource_uri.split("/")[-1], self.host, exc,
            )
            return [], None

        items = self._extract_items(root)

        # Check for EndOfSequence (Pull only)
        if root.find(f".//{_END_OF_SEQ}") is not None:
            return items, None

        # Extract EnumerationContext for next Pull
        enum_ctx = None
        for el in root.iter():
            if "EnumerationContext" in el.tag and el.text and el.text.strip():
                enum_ctx = el.text.strip()
                break

        return items, enum_ctx

    def _extract_items(self, root: ET.Element) -> list[dict]:
        """Extract all DCIM CIM instances from a response element tree.

        Items live under the first ``Items`` element in the SOAP body.
        Each direct child of ``Items`` is one CIM instance.
        """
        items = []
        for el in root.iter():
            if el.tag.endswith("Items"):
                for child in el:
                    item_dict = self._element_to_dict(child)
                    if item_dict:
                        items.append(item_dict)
                break  # Only the first Items block
        return items

    @staticmethod
    def _element_to_dict(element: ET.Element) -> dict:
        """Convert a CIM instance XML element to a flat Python dict.

        Namespace prefixes are stripped from tag names so callers can use
        plain property names (e.g. ``item["Model"]`` not ``item["{ns}Model"]``).
        """
        result = {}
        for child in element:
            tag = child.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]

            if len(child) == 0:
                # Leaf element
                result[tag] = child.text.strip() if child.text else None
            else:
                # Nested element (rare in DCIM; recurse)
                result[tag] = WsManClient._element_to_dict(child)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Convenience query functions (thin wrappers around WsManClient.enumerate)
# ─────────────────────────────────────────────────────────────────────────────

def _enumerate_with_fallback(client: WsManClient, uris: list[str]) -> list[dict]:
    """Try a list of alternate URIs. Return the first one that yields > 0 items."""
    for uri in uris:
        logger.debug("WS-Man Enumerate attempt: %s", uri)
        items = client.enumerate(uri)
        if items:
            logger.info("WS-Man Enumerate SUCCESS: %s -> %d items", uri, len(items))
            return items
        logger.debug("WS-Man Enumerate EMPTY or FAIL: %s", uri)
    return []

def query_system_info(client: WsManClient) -> dict:
    """Return the first DCIM_SystemView instance (server identity)."""
    items = _enumerate_with_fallback(client, DCIM_RESOURCES["system"])
    return items[0] if items else {}


def query_processors(client: WsManClient) -> list[dict]:
    """Return all DCIM_CPUView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["cpu"])


def query_memory(client: WsManClient) -> list[dict]:
    """Return all DCIM_MemoryView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["memory"])


def query_disks(client: WsManClient) -> list[dict]:
    """Return all DCIM_PhysicalDiskView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["disk"])


def query_controllers(client: WsManClient) -> list[dict]:
    """Return all DCIM_ControllerView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["controller"])


def query_nics(client: WsManClient) -> list[dict]:
    """Return all DCIM_NICView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["nic"])


def query_psus(client: WsManClient) -> list[dict]:
    """Return all DCIM_PowerSupplyView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["psu"])


def query_sensors(client: WsManClient) -> list[dict]:
    """Return all DCIM_NumericSensorView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["sensor"])


def query_firmware(client: WsManClient) -> list[dict]:
    """Return all DCIM_SoftwareIdentityView instances (may be empty on iDRAC 6)."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["firmware"])


def query_pci(client: WsManClient) -> list[dict]:
    """Return all DCIM_PCIDeviceView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["pci"])


def query_logs(client: WsManClient) -> list[dict]:
    """Return all DCIM_LifecycleLogView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["log"])


def query_fans(client: WsManClient) -> list[dict]:
    """Return all DCIM_FanView instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["fans"])


def query_chassis(client: WsManClient) -> list[dict]:
    """Return all DCIM_EnclosureView or CIM_Chassis instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["chassis"])


def query_accounts(client: WsManClient) -> list[dict]:
    """Return all CIM_Account instances."""
    return _enumerate_with_fallback(client, DCIM_RESOURCES["account"])

