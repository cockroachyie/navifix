import urllib.request
import ssl
import base64
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

class RibclClient:
    def __init__(self, host, username, password, verify_tls=False):
        self.host = host
        self.username = username
        self.password = password
        self.verify_tls = verify_tls

    def _build_ssl_context(self):
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except Exception:
            try:
                ctx.options &= ~ssl.OP_NO_TLSv1
                ctx.options &= ~ssl.OP_NO_TLSv1_1
            except Exception:
                pass
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except Exception:
            try:
                ctx.set_ciphers("ALL")
            except Exception:
                pass
        return ctx

    def _build_ribcl_envelope(self, command_xml):
        return f'''<?xml version="1.0"?>
<RIBCL VERSION="2.0">
  <LOGIN USER_LOGIN="{self.username}" PASSWORD="{self.password}">
    <SERVER_INFO MODE="read">
      {command_xml}
    </SERVER_INFO>
  </LOGIN>
</RIBCL>'''

    def send(self, command_xml):
        body = self._build_ribcl_envelope(command_xml).encode('utf-8')
        req = urllib.request.Request(
            f"https://{self.host}/ribcl",
            data=body,
            method="POST"
        )
        req.add_header("Content-Type", "text/xml")
        ctx = self._build_ssl_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
                return response.read().decode('utf-8', errors='ignore')
        except Exception as e:
            logger.error(f"RIBCL request failed: {e}")
            raise

    def get_embedded_health(self):
        xml_req = '''
        <GET_EMBEDDED_HEALTH>
            <GET_ALL_HEALTH_STATUS/>
            <GET_ALL_FANS/>
            <GET_ALL_TEMPERATURES/>
            <GET_ALL_PROCESSORS/>
            <GET_ALL_MEMORY/>
            <GET_ALL_POWER_SUPPLIES/>
        </GET_EMBEDDED_HEALTH>
        '''
        raw_xml = self.send(xml_req)
        return self.parse_health_response(raw_xml)

    def parse_health_response(self, xml_str):
        data = {
            "health_status": {},
            "fans": [],
            "temperatures": [],
            "processors": [],
            "memory": [],
            "power_supplies": []
        }
        try:
            # iLO XML often has multiple roots or trailing garbage. Wrap it.
            wrapped = f"<ROOT>{xml_str}</ROOT>"
            root = ET.fromstring(wrapped)
            for elem in root.iter("HEALTH_AT_A_GLANCE"):
                for child in elem:
                    status = child.attrib.get("STATUS") or child.attrib.get("VALUE")
                    if status:
                        data["health_status"][child.tag] = status
            
            for elem in root.iter("FAN"):
                fan = {k: v for k, v in elem.attrib.items()}
                for child in elem:
                    fan[child.tag] = child.attrib.get("VALUE", child.text)
                data["fans"].append(fan)
                
            for elem in root.iter("TEMPERATURE"):
                temp = {k: v for k, v in elem.attrib.items()}
                for child in elem:
                    temp[child.tag] = child.attrib.get("VALUE", child.text)
                data["temperatures"].append(temp)
                
            for elem in root.iter("PROCESSOR"):
                proc = {k: v for k, v in elem.attrib.items()}
                for child in elem:
                    proc[child.tag] = child.attrib.get("VALUE", child.text)
                data["processors"].append(proc)
                
            for elem in root.iter("MEMORY"):
                # Memory might be nested
                mem = {k: v for k, v in elem.attrib.items()}
                for child in elem:
                    mem[child.tag] = child.attrib.get("VALUE", child.text)
                data["memory"].append(mem)
                
            for elem in root.iter("POWER_SUPPLY"):
                psu = {k: v for k, v in elem.attrib.items()}
                for child in elem:
                    psu[child.tag] = child.attrib.get("VALUE", child.text)
                data["power_supplies"].append(psu)
                
        except ET.ParseError as e:
            logger.error(f"Failed to parse RIBCL XML: {e}")
        return data
