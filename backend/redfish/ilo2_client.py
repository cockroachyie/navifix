import logging
import re
import socket
import ssl
import warnings
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class ILO2UnreachableError(ConnectionError):
    """Raised when the iLO 2 target could not be reached or authenticated at all.
    Callers (poller.py) should handle this the same way they handle a RedfishClient
    connection failure — mark the server as unreachable, do not fabricate telemetry."""
    pass


class ILO2AuthError(ILO2UnreachableError):
    """Raised specifically when iLO 2 rejects the login credentials (0x005F).
    Distinct from a network-level failure so callers can surface a clearer
    error message to the operator."""
    pass


UNAVAILABLE = "unavailable"  # Oem.DataSource value for fields RIBCL didn't return


class ILO2Client:
    """GET interface matching RedfishClient. Every value returned is either parsed
    from a live RIBCL response or explicitly marked unavailable — nothing is
    invented. If the target can't be reached at all, .get() raises
    ILO2UnreachableError rather than returning placeholder data."""

    def __init__(self, ip_address, username, password, config):
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.config = config

        self._ribcl_fetched = False
        self._ribcl_roots = []
        self._unreachable_reason = None

        self._storage_controllers = {}
        self._physical_drives = {}
        self._logical_drives = {}

    # -- Public interface --------------------------------------------------

    def get(self, path: str) -> dict | None:
        path = path.rstrip("/")

        if not self._ribcl_fetched:
            self._do_fetch()
            self._ribcl_fetched = True

        if self._unreachable_reason:
            raise ILO2UnreachableError(
                f"iLO 2 target {self.ip_address} unreachable: {self._unreachable_reason}"
            )

        roots = self._ribcl_roots

        if path == "/redfish/v1" or path == "/redfish/v1/":
            return {
                "@odata.id": "/redfish/v1",
                "RedfishVersion": "1.0.0",
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                "Managers": {"@odata.id": "/redfish/v1/Managers"},
            }

        if path == "/redfish/v1/Systems":
            return {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}
        if path == "/redfish/v1/Chassis":
            return {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]}
        if path == "/redfish/v1/Managers":
            return {"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]}

        if path == "/redfish/v1/Systems/1":
            return self._build_system(roots)
        if path == "/redfish/v1/Chassis/1":
            return self._build_chassis(roots)
        if path == "/redfish/v1/Managers/1":
            return self._build_manager(roots)
        if path == "/redfish/v1/Chassis/1/Thermal":
            return self._build_thermal(roots)
        if path == "/redfish/v1/Chassis/1/Power":
            return self._build_power(roots)

        if path == "/redfish/v1/Systems/1/Processors":
            return self._build_processor_collection(roots)
        if path.startswith("/redfish/v1/Systems/1/Processors/"):
            return self._build_processor(roots, path)

        if path == "/redfish/v1/Systems/1/Memory":
            return self._build_memory_collection(roots)

        if path == "/redfish/v1/Systems/1/Storage":
            return {"Members": [{"@odata.id": uri} for uri in self._storage_controllers.keys()]}
        if path in self._storage_controllers:
            return self._storage_controllers[path]
        if path in self._physical_drives:
            return self._physical_drives[path]
        if path in self._logical_drives:
            return self._logical_drives[path]
        if path.endswith("/Volumes"):
            ctrl_uri = path[:-8]
            if ctrl_uri in self._storage_controllers:
                members = [
                    {"@odata.id": vol_uri}
                    for vol_uri in self._logical_drives.keys()
                    if vol_uri.startswith(ctrl_uri)
                ]
                return {"Members": members}

        if path == "/redfish/v1/Systems/1/EthernetInterfaces":
            return self._build_ethernet_collection(roots)
        if path.startswith("/redfish/v1/Systems/1/EthernetInterfaces/"):
            return self._build_ethernet_interface(roots, path)

        if path == "/redfish/v1/Systems/1/LogServices":
            return {"Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/IML"}]}
        if path == "/redfish/v1/Systems/1/LogServices/IML":
            return {
                "@odata.id": "/redfish/v1/Systems/1/LogServices/IML",
                "Name": "Integrated Management Log",
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries"},
            }
        if path == "/redfish/v1/Systems/1/LogServices/IML/Entries":
            return self._build_iml_entries(roots)

        return None

    # -- Fetch orchestration --------------------------------------------

    def _do_fetch(self):
        if not self.username or not self.password:
            self._unreachable_reason = "no credentials configured for RIBCL login"
            return

        try:
            xml_data = self._fetch_ribcl()
        except ILO2AuthError as e:
            self._unreachable_reason = str(e)
            return

        if not xml_data:
            self._unreachable_reason = "RIBCL request failed or timed out (no response)"
            return

        roots = self._parse_ribcl_blocks(xml_data)
        if not roots:
            self._unreachable_reason = "RIBCL response received but could not be parsed as XML"
            return

        self._ribcl_roots = roots
        self._parse_storage(roots)

    # -- Shared RIBCL parsing helpers ---------------------------------------

    def _parse_ribcl_blocks(self, xml_data: str | None) -> list[ET.Element]:
        """RIBCL responses to multi-command logins can arrive as several concatenated
        top-level <RIBCL>...</RIBCL> documents rather than one well-formed document.
        Split on block boundaries and parse each independently."""
        if not xml_data:
            return []
        roots = []
        blocks = re.findall(r"<RIBCL.*?>.*?</RIBCL>", xml_data, re.DOTALL | re.IGNORECASE)
        for block in blocks:
            try:
                clean_block = re.sub(r"&\s", "&amp; ", block)
                roots.append(ET.fromstring(clean_block))
            except ET.ParseError as e:
                logger.debug("Failed parsing RIBCL block from %s: %s", self.ip_address, e)
        return roots

    @staticmethod
    def _find_first(roots: list[ET.Element], xpath: str) -> ET.Element | None:
        for root in roots:
            found = root.find(xpath)
            if found is not None:
                return found
        return None

    @staticmethod
    def _find_all(roots: list[ET.Element], xpath: str) -> list[ET.Element]:
        results = []
        for root in roots:
            results.extend(root.findall(xpath))
        return results

    @staticmethod
    def _health_from_status(status: str | None) -> str:
        status = (status or "").upper()
        if status == "OK":
            return "OK"
        if status in ("WARNING", "WARN"):
            return "Warning"
        if status:
            return "Critical"
        return "Unknown"

    # -- Storage parsing (real data only) --------------------------------

    def _parse_storage(self, roots: list[ET.Element]):
        self._storage_controllers = {}
        self._physical_drives = {}
        self._logical_drives = {}

        for root in roots:
            try:
                for ctrl_el in root.findall(".//CONTROLLER"):
                    label_el = ctrl_el.find("LABEL")
                    ctrl_label = label_el.get("VALUE").strip() if (label_el is not None and label_el.get("VALUE")) else "Storage Controller"

                    status_el = ctrl_el.find("STATUS")
                    ctrl_status = status_el.get("VALUE").strip() if (status_el is not None and status_el.get("VALUE")) else None

                    ctrl_id = f"Controller{len(self._storage_controllers) + 1}"
                    ctrl_uri = f"/redfish/v1/Systems/1/Storage/{ctrl_id}"
                    drives_refs = []

                    for log_el in ctrl_el.findall(".//LOGICAL_DRIVE"):
                        log_label_el = log_el.find("LABEL")
                        log_label = log_label_el.get("VALUE").strip() if (log_label_el is not None and log_label_el.get("VALUE")) else "Logical Drive"

                        log_status_el = log_el.find("STATUS")
                        log_status = log_status_el.get("VALUE").strip() if (log_status_el is not None and log_status_el.get("VALUE")) else None

                        log_raid_el = log_el.find("FAULT_TOLERANCE")
                        log_raid = log_raid_el.get("VALUE").strip() if (log_raid_el is not None and log_raid_el.get("VALUE")) else None

                        log_id = f"Volume{len(self._logical_drives) + 1}"
                        vol_uri = f"{ctrl_uri}/Volumes/{log_id}"

                        for phys_el in log_el.findall(".//PHYSICAL_DRIVE"):
                            self._add_physical_drive(phys_el, ctrl_uri, drives_refs)

                        self._logical_drives[vol_uri] = {
                            "@odata.id": vol_uri,
                            "Name": log_label,
                            "VolumeType": log_raid or UNAVAILABLE,
                            "Status": {"State": "Enabled", "Health": self._health_from_status(log_status)},
                            "Oem": {"DataSource": "live"},
                        }

                    for phys_el in ctrl_el.findall("PHYSICAL_DRIVE"):
                        self._add_physical_drive(phys_el, ctrl_uri, drives_refs)

                    self._storage_controllers[ctrl_uri] = {
                        "@odata.id": ctrl_uri,
                        "Name": ctrl_label,
                        "Status": {"State": "Enabled", "Health": self._health_from_status(ctrl_status)},
                        "StorageControllers": [{
                            "MemberId": "0",
                            "Name": ctrl_label,
                            "Manufacturer": "HP",
                            "Status": {"State": "Enabled", "Health": self._health_from_status(ctrl_status)},
                        }],
                        "Drives": drives_refs,
                        "Volumes": {"@odata.id": f"{ctrl_uri}/Volumes"},
                        "Oem": {"DataSource": "live"},
                    }
            except Exception as e:
                logger.debug("Error parsing storage block from %s: %s", self.ip_address, e)

    def _add_physical_drive(self, phys_el: ET.Element, ctrl_uri: str, drives_refs: list):
        def attr(tag, default=None):
            el = phys_el.find(tag)
            return el.get("VALUE").strip() if (el is not None and el.get("VALUE")) else default

        phys_label = attr("LABEL", "Physical Drive")
        phys_status = attr("STATUS")
        phys_serial = attr("SERIAL_NUMBER")
        phys_model = attr("MODEL")
        phys_cap = attr("CAPACITY")

        cap_bytes = None
        if phys_cap:
            try:
                num = float(re.findall(r"[\d\.]+", phys_cap)[0])
                cap_bytes = int(num * (1024 ** 4 if "T" in phys_cap.upper() else 1024 ** 3))
            except Exception as e:
                logger.debug("Could not parse drive capacity '%s': %s", phys_cap, e)

        drive_id = f"Drive{len(self._physical_drives)}"
        drive_uri = f"{ctrl_uri}/Drives/{drive_id}"
        if drive_uri in self._physical_drives:
            return
        drives_refs.append({"@odata.id": drive_uri})

        self._physical_drives[drive_uri] = {
            "@odata.id": drive_uri,
            "Name": phys_label,
            "CapacityBytes": cap_bytes if cap_bytes is not None else UNAVAILABLE,
            "Manufacturer": "HP",
            "Model": phys_model or UNAVAILABLE,
            "SerialNumber": phys_serial or UNAVAILABLE,
            "Status": {"State": "Enabled", "Health": self._health_from_status(phys_status)},
            "Oem": {"DataSource": "live"},
        }

    # -- RIBCL transport --------------------------------------------------

    def _make_ssl_context(self) -> ssl.SSLContext:
        """Return an SSLContext that can negotiate TLS 1.0 with iLO 2.

        Python's ssl module (with OPENSSL_CONF=/app/openssl_legacy.cnf already
        set in the container environment) supports minimum_version=TLSv1 when
        the legacy OpenSSL provider is active.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("DEFAULT@SECLEVEL=0")
            # Allow legacy renegotiation that older iLO firmware initiates.
            ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        return ctx

    def _fetch_ribcl(self) -> str | None:
        """Send RIBCL commands to iLO 2 using the two-step RAW streaming protocol.

        iLO 2 uses a streaming XML protocol over a raw TLS socket (not HTTP):
          1. Client sends <?xml version="1.0"?>\r\n
          2. Server echoes the same header (handshake acknowledgment)
          3. Client sends the <RIBCL>…</RIBCL> body
          4. Server streams one <RIBCL>…</RIBCL> block per command response
             and closes the connection when finished.

        Raises ILO2AuthError if the server explicitly rejects the credentials.
        Returns None on transport failure so the caller marks the server
        unreachable rather than fabricating telemetry.
        """
        _XML_DECL = b'<?xml version="1.0"?>\r\n'
        ribcl_body = (
            '<RIBCL VERSION="2.0">\n'
            f'  <LOGIN USER_LOGIN="{self.username}" PASSWORD="{self.password}">\n'
            '    <SERVER_INFO MODE="read">\n'
            '      <GET_EMBEDDED_HEALTH />\n'
            '      <GET_SHORT_NAMES />\n'
            '      <GET_EVENT_LOG />\n'
            '    </SERVER_INFO>\n'
            '    <RIB_INFO MODE="read">\n'
            '      <GET_FW_VERSION />\n'
            '    </RIB_INFO>\n'
            '  </LOGIN>\n'
            '</RIBCL>\n'
        ).encode()

        try:
            ctx = self._make_ssl_context()
            raw = socket.create_connection((self.ip_address, 443), timeout=10)
            conn = ctx.wrap_socket(raw, server_hostname=self.ip_address)
        except OSError as e:
            logger.warning("iLO 2 %s: TCP/TLS connection failed: %s", self.ip_address, e)
            return None

        try:
            # --- Step 1: XML declaration handshake ---
            conn.sendall(_XML_DECL)
            conn.settimeout(10)
            hdr = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    hdr += chunk
                    if b"<?xml" in hdr:
                        break
            except socket.timeout:
                pass

            if b"<?xml" not in hdr:
                logger.warning(
                    "iLO 2 %s: no XML handshake echo (got %r)",
                    self.ip_address, hdr[:80]
                )
                return None

            # --- Step 2: Send RIBCL commands ---
            conn.sendall(ribcl_body)

            # --- Step 3: Read all response blocks until connection closes ---
            buf = b""
            conn.settimeout(30)
            try:
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                logger.debug(
                    "iLO 2 %s: read timeout after %d bytes", self.ip_address, len(buf)
                )
            except Exception as e:
                logger.debug("iLO 2 %s: read ended: %s", self.ip_address, e)

        finally:
            try:
                conn.close()
            except Exception:
                pass

        text = buf.decode(errors="ignore")

        # Check for authentication failure before returning
        if "0x005F" in text or "Login credentials rejected" in text:
            raise ILO2AuthError(
                f"iLO 2 {self.ip_address}: login credentials rejected — "
                "check username/password in server settings"
            )

        if "<RIBCL" not in text:
            logger.warning(
                "iLO 2 %s: unexpected response (%d bytes, no RIBCL): %r",
                self.ip_address, len(text), text[:200]
            )
            return None

        logger.debug(
            "iLO 2 %s: RIBCL fetch succeeded (%d bytes)", self.ip_address, len(text)
        )
        return text

    # -- Builders: System / Chassis / Manager ----------------------------

    def _build_system(self, roots: list[ET.Element]) -> dict:
        health = "Unknown"
        power = UNAVAILABLE
        model = UNAVAILABLE
        serial = UNAVAILABLE
        bios = UNAVAILABLE

        health_el = self._find_first(roots, ".//HEALTH_AT_A_GLANCE")
        if health_el is not None:
            statuses = [el.get("STATUS", "") for el in health_el]
            if "CRITICAL" in statuses:
                health = "Critical"
            elif "WARNING" in statuses:
                health = "Warning"
            elif statuses:
                health = "OK"

        prop_model = self._find_first(roots, ".//PROPERTY[@NAME='PRODUCT_NAME']")
        if prop_model is not None and prop_model.get("VALUE"):
            model = prop_model.get("VALUE")

        prop_serial = self._find_first(roots, ".//PROPERTY[@NAME='SERIAL_NUMBER']")
        if prop_serial is not None and prop_serial.get("VALUE"):
            serial = prop_serial.get("VALUE")

        prop_bios = self._find_first(roots, ".//PROPERTY[@NAME='ROM_VERSION']")
        if prop_bios is not None and prop_bios.get("VALUE"):
            bios = prop_bios.get("VALUE")

        power_el = self._find_first(roots, ".//HOST_POWER")
        if power_el is not None and power_el.get("VALUE"):
            power = "On" if power_el.get("VALUE").upper() == "ON" else "Off"

        proc_count = len(self._find_all(roots, ".//PROCESSORS/PROCESSOR"))
        mem_el = self._find_first(roots, ".//MEMORY/TOTAL_MEMORY_SIZE")
        mem_gib = None
        if mem_el is not None and mem_el.get("VALUE"):
            try:
                mem_gib = int(float(re.findall(r"[\d\.]+", mem_el.get("VALUE"))[0]))
            except Exception:
                mem_gib = None

        return {
            "@odata.id": "/redfish/v1/Systems/1",
            "Name": "proliant-node-ilo2",
            "Model": model,
            "Manufacturer": "HP",
            "SerialNumber": serial,
            "PowerState": power,
            "Status": {"State": "Enabled", "Health": health},
            "BiosVersion": bios,
            "ProcessorSummary": {
                "Count": proc_count,
                "Status": {"State": "Enabled", "Health": "Unknown" if proc_count == 0 else "OK"},
            },
            "MemorySummary": {
                "TotalSystemMemoryGiB": mem_gib if mem_gib is not None else UNAVAILABLE,
                "Status": {"State": "Enabled", "Health": "Unknown" if mem_gib is None else "OK"},
            },
            "Links": {
                "Chassis": [{"@odata.id": "/redfish/v1/Chassis/1"}],
                "ManagedBy": [{"@odata.id": "/redfish/v1/Managers/1"}],
            },
            "Oem": {"DataSource": "live"},
        }

    def _build_chassis(self, roots: list[ET.Element]) -> dict:
        sys = self._build_system(roots)
        return {
            "@odata.id": "/redfish/v1/Chassis/1",
            "Name": "Chassis",
            "ChassisType": "RackMount",
            "Model": sys["Model"],
            "Manufacturer": "HP",
            "SerialNumber": sys["SerialNumber"],
            "Status": sys["Status"],
            "Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"},
            "Thermal": {"@odata.id": "/redfish/v1/Chassis/1/Thermal"},
            "Links": {
                "ComputerSystems": [{"@odata.id": "/redfish/v1/Systems/1"}],
                "ManagedBy": [{"@odata.id": "/redfish/v1/Managers/1"}],
            },
            "Oem": {"DataSource": "live"},
        }

    def _build_manager(self, roots: list[ET.Element]) -> dict:
        fw_ver = UNAVAILABLE
        fw_el = self._find_first(roots, ".//GET_FW_VERSION")
        if fw_el is not None and fw_el.get("FIRMWARE_VERSION"):
            fw_ver = fw_el.get("FIRMWARE_VERSION")

        return {
            "@odata.id": "/redfish/v1/Managers/1",
            "Name": "iLO 2 Manager",
            "ManagerType": "BMC",
            "FirmwareVersion": fw_ver,
            "Status": {"State": "Enabled", "Health": "Unknown" if fw_ver == UNAVAILABLE else "OK"},
            "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/1/EthernetInterfaces"},
            "Oem": {"DataSource": "live"},
        }

    # -- Builders: Thermal / Power ----------------------------------------

    def _build_thermal(self, roots: list[ET.Element]) -> dict:
        fans = []
        for i, f in enumerate(self._find_all(roots, ".//FAN")):
            label = f.get("LABEL", f"Fan {i + 1}")
            speed_raw = f.get("SPEED", "").replace("%", "").strip()
            status = f.get("STATUS")
            fans.append({
                "@odata.id": f"/redfish/v1/Chassis/1/Thermal#/Fans/{i}",
                "MemberId": f"Fan{i + 1}",
                "Name": label,
                "Reading": int(speed_raw) if speed_raw.isdigit() else UNAVAILABLE,
                "ReadingUnits": "Percent",
                "Status": {"State": "Enabled", "Health": self._health_from_status(status)},
                "PhysicalContext": "SystemBoard",
                "Oem": {"DataSource": "live"},
            })

        temps = []
        for i, t in enumerate(self._find_all(roots, ".//TEMP")):
            label = t.get("LABEL", f"Temp {i + 1}")
            val_raw = t.get("VALUE", "")
            status = t.get("STATUS")
            temps.append({
                "@odata.id": f"/redfish/v1/Chassis/1/Thermal#/Temperatures/{i}",
                "MemberId": f"Temp{i + 1}",
                "Name": label,
                "ReadingCelsius": int(val_raw) if val_raw.lstrip("-").isdigit() else UNAVAILABLE,
                "Status": {"State": "Enabled", "Health": self._health_from_status(status)},
                "PhysicalContext": "SystemBoard",
                "Oem": {"DataSource": "live"},
            })

        return {"@odata.id": "/redfish/v1/Chassis/1/Thermal", "Fans": fans, "Temperatures": temps}

    def _build_power(self, roots: list[ET.Element]) -> dict:
        p_supplies = []
        for i, ps in enumerate(self._find_all(roots, ".//POWER_SUPPLY")):
            label = ps.get("LABEL", f"Power Supply {i + 1}")
            status = ps.get("STATUS")
            watts_raw = ps.get("POWER") or ps.get("OUTPUT_WATTS")
            p_supplies.append({
                "@odata.id": f"/redfish/v1/Chassis/1/Power#/PowerSupplies/{i}",
                "MemberId": f"PowerSupply{i + 1}",
                "Name": label,
                "LastPowerOutputWatts": int(watts_raw) if watts_raw and watts_raw.isdigit() else UNAVAILABLE,
                "Status": {"State": "Enabled", "Health": self._health_from_status(status)},
                "Oem": {"DataSource": "live"},
            })

        # RIBCL GET_EMBEDDED_HEALTH does not expose per-rail voltages on iLO 2 —
        # not simulated, genuinely not available via this protocol.
        return {
            "@odata.id": "/redfish/v1/Chassis/1/Power",
            "PowerSupplies": p_supplies,
            "Voltages": [],
            "Oem": {"DataSource": "live", "Note": "Per-rail voltage not exposed by iLO 2 RIBCL"},
        }

    # -- Builders: Processors / Memory / Ethernet --------------------------

    def _build_processor_collection(self, roots: list[ET.Element]) -> dict:
        els = self._find_all(roots, ".//PROCESSORS/PROCESSOR")
        return {"Members": [{"@odata.id": f"/redfish/v1/Systems/1/Processors/{i}"} for i in range(len(els))]}

    def _build_processor(self, roots: list[ET.Element], path: str) -> dict | None:
        idx_str = path.split("/")[-1]
        try:
            idx = int(idx_str)
        except ValueError:
            return None
        els = self._find_all(roots, ".//PROCESSORS/PROCESSOR")
        if idx >= len(els):
            return None
        el = els[idx]

        def attr(tag):
            child = el.find(tag)
            return child.get("VALUE").strip() if (child is not None and child.get("VALUE")) else None

        return {
            "@odata.id": path,
            "Name": attr("LABEL") or f"Processor {idx + 1}",
            "Socket": attr("LABEL") or UNAVAILABLE,
            "ProcessorType": "CPU",
            "Model": attr("NAME") or UNAVAILABLE,
            "MaxSpeedMHz": attr("SPEED") or UNAVAILABLE,
            "TotalCores": attr("EXECUTION_TECHNOLOGY") or UNAVAILABLE,
            "Status": {"State": "Enabled", "Health": self._health_from_status(attr("STATUS"))},
            "Oem": {"DataSource": "live"},
        }

    def _build_memory_collection(self, roots: list[ET.Element]) -> dict:
        # iLO 2's GET_EMBEDDED_HEALTH does not reliably expose per-DIMM detail —
        # only a total. Report that as the summary, no per-DIMM members fabricated.
        mem_el = self._find_first(roots, ".//MEMORY/TOTAL_MEMORY_SIZE")
        if mem_el is None:
            return {"Members": [], "Oem": {"DataSource": UNAVAILABLE, "Note": "Per-DIMM detail not exposed by iLO 2 RIBCL"}}
        return {"Members": [], "Oem": {"DataSource": "live", "Note": "Per-DIMM detail not exposed by iLO 2 RIBCL; see Systems/1 MemorySummary for total"}}

    def _build_ethernet_collection(self, roots: list[ET.Element]) -> dict:
        els = self._find_all(roots, ".//NIC_INFORMATION/NIC")
        return {"Members": [{"@odata.id": f"/redfish/v1/Systems/1/EthernetInterfaces/{i}"} for i in range(len(els))]}

    def _build_ethernet_interface(self, roots: list[ET.Element], path: str) -> dict | None:
        idx_str = path.split("/")[-1]
        try:
            idx = int(idx_str)
        except ValueError:
            return None
        els = self._find_all(roots, ".//NIC_INFORMATION/NIC")
        if idx >= len(els):
            return None
        el = els[idx]

        def attr(tag):
            child = el.find(tag)
            return child.get("VALUE").strip() if (child is not None and child.get("VALUE")) else None

        return {
            "@odata.id": path,
            "Name": attr("DESCRIPTION") or f"NIC {idx + 1}",
            "LinkStatus": "LinkUp" if (attr("STATUS") or "").upper() == "OK" else "LinkDown",
            "MACAddress": attr("MAC_ADDRESS") or UNAVAILABLE,
            "Status": {"State": "Enabled", "Health": self._health_from_status(attr("STATUS"))},
            "Oem": {"DataSource": "live"},
        }

    # -- Builders: IML log -------------------------------------------------

    def _build_iml_entries(self, roots: list[ET.Element]) -> dict:
        els = self._find_all(roots, ".//EVENT_LOG/EVENT")
        if not els:
            return {"Members": [], "Oem": {"DataSource": UNAVAILABLE, "Note": "No IML entries returned by GET_EVENT_LOG"}}

        members = []
        for i, ev in enumerate(els):
            members.append({
                "@odata.id": f"/redfish/v1/Systems/1/LogServices/IML/Entries/{i}",
                "Id": str(i + 1),
                "Severity": ev.get("SEVERITY", "Unknown"),
                "Message": ev.get("DESCRIPTION", UNAVAILABLE),
                "Created": ev.get("CREATED", UNAVAILABLE),
            })
        return {"Members": members, "Oem": {"DataSource": "live"}}