"""
diagnostics/dell.py
=====================
Dell iDRAC Tech Support Report (TSR) export.

Redfish OEM action (confirmed against Dell's iDRAC9 Redfish API Guide
and the dell/iDRAC-Redfish-Scripting reference scripts):

    POST /redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellLCService/Actions/DellLCService.ExportTechSupportReport

The report is written by iDRAC to a configured CIFS share. The monitoring
process must be able to read that same share through DELL_TSR_STORAGE_PATH;
the storage may be mounted by any filesystem mechanism.
"""
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .base import DiagnosticsAdapter, DiagnosticsResult
from .exceptions import DiagnosticsError

logger = logging.getLogger(__name__)

_TSR_ACTION = "/redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellLCService/Actions/DellLCService.ExportTechSupportReport"
_FAILED_STATES = {"Failed", "CompletedWithErrors"}
_REQUIRED_CIFS_ENVIRONMENT = (
    "DELL_TSR_CIFS_IP_ADDRESS",
    "DELL_TSR_CIFS_SHARE_NAME",
    "DELL_TSR_CIFS_USERNAME",
    "DELL_TSR_CIFS_PASSWORD",
    "DELL_TSR_STORAGE_PATH",
)
_DEFAULT_DATA_SELECTORS = ("HWData",)
_STORAGE_FILE_WAIT_SECONDS = 30
_STORAGE_FILE_POLL_SECONDS = 1


class DellDiagnosticsAdapter(DiagnosticsAdapter):
    name = "dell"

    def download_support_bundle(self, client, server, progress_cb=None):
        logger.info("[dell] starting TSR export for server %s", server.id)
        settings = self._load_cifs_settings()
        filename = self._filename_for(server)

        payload = {
            "ShareType": "CIFS",
            "IPAddress": settings["ip_address"],
            "ShareName": settings["share_name"],
            "UserName": settings["username"],
            "Password": settings["password"],
            "DataSelectorArrayIn": settings["data_selectors"],
        }

        resp = client.post_for_response(_TSR_ACTION, json_body=payload)
        if resp is None or resp.status_code not in (200, 202):
            status = getattr(resp, "status_code", "no response (404)")
            response_text = getattr(resp, "text", "")
            extended_info = None
            if resp is not None:
                try:
                    response_body = resp.json()
                    if isinstance(response_body, dict):
                        extended_info = (
                            response_body.get("@Message.ExtendedInfo")
                            or response_body.get("error", {}).get("@Message.ExtendedInfo")
                        )
                except ValueError:
                    pass
            masked_payload = {**payload, "Password": "***"}
            logger.error(
                "[dell] ExportTechSupportReport failed: status=%s payload=%s "
                "response.text=%s @Message.ExtendedInfo=%s",
                status, masked_payload, response_text, extended_info,
            )
            raise DiagnosticsError(
                f"Dell TSR export failed to start (status {status}). "
                f"Confirm ExportTechSupportReport is supported on this iDRAC firmware."
            )

        job_uri = self._job_uri_from_response(resp)
        if not job_uri:
            raise DiagnosticsError(
                "Dell TSR export accepted but no job reference was returned - "
                "cannot poll for completion."
            )

        # Location may be the job monitor URI directly, or something like
        # /redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/Jobs/JID_1234
        job_id_match = re.search(r"/Jobs/([^/]+)$", job_uri)
        poll_uri = job_uri if job_id_match else (
            f"/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/Jobs/{job_uri.rstrip('/').split('/')[-1]}"
        )

        self._poll_job(
            client,
            poll_uri,
            is_done=lambda body: body.get("Oem", {}).get("Dell", {}).get("JobState") == "Completed",
            is_failed=lambda body: body.get("Oem", {}).get("Dell", {}).get("JobState") in _FAILED_STATES,
            progress_of=lambda body: body.get("Oem", {}).get("Dell", {}).get("PercentComplete"),
            progress_cb=progress_cb,
        )

        storage_file = self._wait_for_storage_file(settings["storage_path"], filename)
        logger.info("[dell] TSR job completed, reading %s", storage_file)
        try:
            content = storage_file.read_bytes()
        except OSError as exc:
            raise DiagnosticsError(f"Dell TSR export could not read {storage_file}: {exc}") from exc

        return DiagnosticsResult(filename=filename, content=content, content_type="application/zip")

    @staticmethod
    def _load_cifs_settings():
        missing = [name for name in _REQUIRED_CIFS_ENVIRONMENT if not os.environ.get(name)]
        if missing:
            raise DiagnosticsError(
                "Dell TSR CIFS configuration is incomplete; set " + ", ".join(missing)
            )

        selectors = tuple(
            value.strip()
            for value in os.environ.get("DELL_TSR_DATA_SELECTORS", "").split(",")
            if value.strip()
        ) or _DEFAULT_DATA_SELECTORS

        return {
            "ip_address": os.environ["DELL_TSR_CIFS_IP_ADDRESS"],
            "share_name": os.environ["DELL_TSR_CIFS_SHARE_NAME"],
            "username": os.environ["DELL_TSR_CIFS_USERNAME"],
            "password": os.environ["DELL_TSR_CIFS_PASSWORD"],
            "storage_path": Path(os.environ["DELL_TSR_STORAGE_PATH"]),
            "data_selectors": list(selectors),
        }

    @staticmethod
    def _filename_for(server) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{server.id}-{timestamp}.zip"

    @staticmethod
    def _job_uri_from_response(resp):
        location = resp.headers.get("Location")
        if location:
            return location

        try:
            body = resp.json()
        except ValueError:
            return None

        job = body.get("Job") if isinstance(body, dict) else None
        if isinstance(job, dict):
            return job.get("@odata.id") or job.get("Id")
        return job if isinstance(job, str) else None

    @staticmethod
    def _wait_for_storage_file(storage_path: Path, filename: str) -> Path:
        output_path = storage_path / filename
        deadline = time.monotonic() + _STORAGE_FILE_WAIT_SECONDS
        while time.monotonic() < deadline:
            if output_path.is_file():
                return output_path
            time.sleep(_STORAGE_FILE_POLL_SECONDS)

        raise DiagnosticsError(
            f"Dell TSR job completed but {output_path} was not found. "
            "Verify DELL_TSR_STORAGE_PATH exposes the configured share root."
        )
