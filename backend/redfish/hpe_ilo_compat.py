import urllib.request
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

def is_hpe(server):
    if server.vendor:
        v = server.vendor.lower()
        return "hpe" in v or "hp" in v
    return False

def is_ilo_legacy(generation):
    return generation in ("ilo2", "ilo3")

def get_ilo_generation(service_root, host, verify_tls=False):
    # Try the unauthenticated xmldata endpoint first
    try:
        import ssl
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ctx.options &= ~ssl.OP_NO_TLSv1
        ctx.options &= ~ssl.OP_NO_TLSv1_1
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except Exception:
            pass

        req = urllib.request.Request(f"https://{host}/xmldata?item=All", method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            xml_str = resp.read().decode('utf-8', errors='ignore')
            parsed = parse_xmldata(xml_str)
            if parsed and parsed.get("generation"):
                # "Integrated Lights-Out 2 (iLO 2)" -> "ilo2"
                pn = parsed["generation"].lower()
                if "ilo 2" in pn or "ilo2" in pn:
                    return "ilo2"
                elif "ilo 3" in pn or "ilo3" in pn:
                    return "ilo3"
                elif "ilo 4" in pn or "ilo4" in pn:
                    return "ilo4"
                elif "ilo 5" in pn or "ilo5" in pn:
                    return "ilo5"
                elif "ilo 6" in pn or "ilo6" in pn:
                    return "ilo6"
    except Exception as e:
        logger.debug(f"Failed to fetch /xmldata?item=All from {host}: {e}")

    # Fallback to service root heuristics
    if service_root:
        return "ilo_unknown_redfish"
    
    return None

def parse_xmldata(xml_str):
    try:
        root = ET.fromstring(xml_str)
        mp = root.find("MP")
        if mp is not None:
            pn = mp.findtext("PN")
            fwri = mp.findtext("FWRI")
            return {"generation": pn, "firmware_version": fwri}
    except ET.ParseError:
        pass
    return {}
