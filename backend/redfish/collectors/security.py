"""
redfish/collectors/security.py
================================
Redfish resources consumed
---------------------------
- Systems/{id}/Bios -> Attributes (BIOS settings, SecureBoot mode, etc.)
- Systems/{id}/SecureBoot (SecureBoot resource: SecureBootEnable, Mode,
  SecureBootCurrentBoot, SecureBootDatabases)
- Managers/{id} -> Links -> SecurityPolicy (if present)
- Systems/{id} -> Boot (BootSourceOverride, UefiTargetBootSourceOverride,
  BootOrder) for boot mode details
- Chassis/{id} -> PhysicalSecurity.IntrusionSensor (already in chassis
  collector but surfaced again here for the Security card)

TPM, Virtualization (VT-x/IOMMU), Lockdown/ManagerLockdown, and
certificate management links are captured from wherever the BMC exposes
them - primarily via BIOS attributes and OEM extensions.
"""
from .common import component, collection_members, unsupported_marker
from database.models import ComponentCategory


def collect(client, server, topology):
    components = []

    for system_uri, links in topology.get("per_system", {}).items():

        # ── SecureBoot resource ─────────────────────────────────────────
        secure_boot_uri = links.get("secure_boot")
        if secure_boot_uri:
            sb = client.get(secure_boot_uri)
            if sb:
                components.append(component(
                    ComponentCategory.SECURITY, sb.get("@odata.id", secure_boot_uri),
                    "Secure Boot", sb,
                ))

        # ── BIOS resource (attributes contain TPM, VT, Lockdown flags) ──
        bios_uri = links.get("bios")
        if bios_uri:
            bios = client.get(bios_uri)
            if bios:
                attrs = bios.get("Attributes") or {}
                # Build a focused security view from BIOS attributes
                security_attrs = {
                    k: v for k, v in attrs.items()
                    if any(kw in k.lower() for kw in (
                        "tpm", "secure", "boot", "vtd", "vt-d", "virtuali", "lockdown",
                        "iscsi", "lom", "exec", "smartcard", "password",
                    ))
                }
                if security_attrs:
                    bios_sec = {"@odata.id": bios_uri + "#security", "Attributes": security_attrs}
                    components.append(component(
                        ComponentCategory.SECURITY,
                        bios_uri + "#security",
                        "BIOS Security Settings",
                        bios_sec,
                    ))

                # Full BIOS as separate component
                components.append(component(
                    ComponentCategory.SECURITY, bios_uri, "BIOS", bios,
                ))

        # ── Boot configuration ──────────────────────────────────────────
        system_body = client.get(system_uri)
        if system_body:
            boot = system_body.get("Boot") or {}
            if boot:
                boot_id = f"{system_uri}#boot"
                components.append(component(
                    ComponentCategory.SECURITY, boot_id, "Boot Configuration",
                    {**boot, "@odata.id": boot_id},
                ))

    # ── Manager lockdown / certificates ─────────────────────────────────
    for manager_uri in topology.get("managers", []):
        mgr = client.get(manager_uri)
        if not mgr:
            continue
        cert_svc = (mgr.get("Links") or {}).get("CertificateService") or \
                   (mgr.get("Oem", {}).get("Dell") or {}).get("CertificateService") or \
                   (mgr.get("Certificates") or {})
        if cert_svc and isinstance(cert_svc, dict):
            cert_uri = cert_svc.get("@odata.id")
            if cert_uri:
                cert_body = client.get(cert_uri)
                if cert_body:
                    components.append(component(
                        ComponentCategory.SECURITY, cert_uri,
                        "Certificates", cert_body,
                    ))

        # Lockdown mode
        # iDRAC 9: Oem.Dell.ManagerLockdownMode (direct key)
        # iDRAC 8: may be under Oem.Dell.iDRACCardService.ManagerLockdownMode
        #          or Oem.Dell.DelliDRACCardService.ManagerLockdownMode
        # HPE iLO: Oem.Hpe.Actions.Oem (kept as-is for HPE compatibility)
        oem = mgr.get("Oem") or {}
        dell_oem = oem.get("Dell") or {}
        lockdown = (
            dell_oem.get("ManagerLockdownMode")
            or (dell_oem.get("iDRACCardService") or {}).get("ManagerLockdownMode")
            or (dell_oem.get("DelliDRACCardService") or {}).get("ManagerLockdownMode")
            or (oem.get("Hpe") or {}).get("Actions", {}).get("Oem", {})
        )
        if lockdown is not None:
            ld_id = f"{manager_uri}#lockdown"
            components.append(component(
                ComponentCategory.SECURITY, ld_id, "Manager Lockdown",
                {"@odata.id": ld_id, "LockdownMode": lockdown},
            ))


    if not components:
        components.append(unsupported_marker(ComponentCategory.SECURITY))

    return components, []
