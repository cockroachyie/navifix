"""
diagnostics/base.py
=====================
Shared contract + shared plumbing for every vendor's support-bundle
adapter. Adapters stay thin: they know their own endpoint names and
payload shapes, but delegate polling/download mechanics here so
behavior (timeouts, retries, logging) is consistent across vendors.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .exceptions import DiagnosticsError, DiagnosticsTimeout

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_JOB_TIMEOUT_SECONDS = 20 * 60  # 20 minutes


@dataclass
class DiagnosticsResult:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class DiagnosticsAdapter:
    """Every vendor adapter implements this one method. `progress_cb`,
    if given, is called with a 0-100 int (or None if indeterminate)."""

    name = "generic"

    def download_support_bundle(
        self,
        client,
        server,
        progress_cb: Optional[Callable[[Optional[int]], None]] = None,
    ) -> DiagnosticsResult:
        raise NotImplementedError

    def _poll_job(
        self,
        client,
        job_uri: str,
        is_done: Callable[[dict], bool],
        is_failed: Callable[[dict], bool],
        progress_of: Callable[[dict], Optional[int]] = lambda body: None,
        progress_cb: Optional[Callable[[Optional[int]], None]] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> dict:
        start = time.monotonic()
        last_body = None
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                logger.error(
                    "[%s] job at %s did not complete within %ss (last body: %s)",
                    self.name, job_uri, timeout, last_body,
                )
                raise DiagnosticsTimeout(
                    f"{self.name} diagnostics job timed out after {timeout}s"
                )

            body = client.get(job_uri)
            if body is None:
                raise DiagnosticsError(
                    f"{self.name}: lost track of job at {job_uri} while polling"
                )
            last_body = body

            if progress_cb:
                progress_cb(progress_of(body))

            if is_failed(body):
                logger.error("[%s] job at %s reported failure: %s", self.name, job_uri, body)
                raise DiagnosticsError(f"{self.name} diagnostics job failed: {body}")

            if is_done(body):
                logger.info("[%s] job at %s completed after %.0fs", self.name, job_uri, elapsed)
                return body

            time.sleep(poll_interval)

    def _download_binary(self, client, uri: str) -> bytes:
        content = client.get_binary(uri)
        if content is None:
            raise DiagnosticsError(f"{self.name}: failed to download bundle from {uri}")
        return content
    