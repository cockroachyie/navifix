"""
redfish/session.py
===================
Implements DMTF Redfish Session-based authentication
(POST /redfish/v1/SessionService/Sessions).

Responsibilities
----------------
- Create a session against a BMC and capture the X-Auth-Token + the
  session's own @odata.id (Location header) so it can be explicitly
  deleted (logged out) later.
- Persist sessions in the database (RedfishSessionRecord) so multiple
  poller workers can share a session for the same server instead of each
  opening a fresh one (BMCs enforce a low max-concurrent-session limit,
  often 4-6).
- Detect an expired/invalidated session (HTTP 401 on a resource request)
  and transparently re-authenticate.
- Fall back to HTTP Basic Auth only if SessionService is unavailable
  (some minimal BMC implementations only support Basic).

This module intentionally knows nothing about *what* resources exist on a
BMC - that is discovery.py's job. It only knows how to get and keep a
valid, authenticated HTTP client.
"""
import logging
import threading
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

SESSION_SERVICE_PATH = "/redfish/v1/SessionService/Sessions"
DEFAULT_ASSUMED_SESSION_TTL_MINUTES = 30  # most BMCs default to 30 min idle timeout


class RedfishAuthError(Exception):
    """Raised when credentials are rejected (HTTP 401/403 on session create)."""


class RedfishUnreachableError(Exception):
    """Raised when the BMC cannot be reached at all (network/TLS/timeout)."""


class RedfishSession:
    """
    Holds live authentication state for a single BMC. One instance per
    server, cached by SessionManager. Thread-safe: multiple poller threads
    for different servers each get their own instance, but a given
    instance's token refresh is locked so we don't double-authenticate.
    """

    def __init__(self, base_url: str, username: str, password: str, config, server_id: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.config = config
        self.server_id = server_id

        self.token: str | None = None
        self.session_uri: str | None = None
        self.expires_at: datetime | None = None
        self.uses_basic_auth_fallback = False

        self._lock = threading.Lock()

    # -- public API -------------------------------------------------------

    def get_http_client(self) -> httpx.Client:
        """Return an httpx.Client pre-configured with valid auth headers,
        authenticating or refreshing first if necessary."""
        self._ensure_valid_session()
        headers = {"OData-Version": "4.0"}
        if self.uses_basic_auth_fallback:
            auth = (self.username, self.password)
            return httpx.Client(
                base_url=self.base_url,
                headers=headers,
                auth=auth,
                verify=self.config.REDFISH_VERIFY_TLS,
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
            )
        headers["X-Auth-Token"] = self.token
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            verify=self.config.REDFISH_VERIFY_TLS,
            timeout=self.config.REDFISH_HTTP_TIMEOUT,
        )

    def invalidate(self):
        """Force the next call to re-authenticate (e.g. after a 401)."""
        with self._lock:
            self.token = None
            self.expires_at = None

    def logout(self):
        """Explicitly DELETE the session on the BMC to free the session slot."""
        if self.uses_basic_auth_fallback or not self.token or not self.session_uri:
            return
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers={"X-Auth-Token": self.token},
                verify=self.config.REDFISH_VERIFY_TLS,
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
            ) as client:
                client.delete(self.session_uri)
        except httpx.HTTPError as exc:
            logger.warning("Logout failed for %s: %s", self.base_url, exc)
        finally:
            self.token = None
            self.session_uri = None
            self.expires_at = None

    # -- internals ----------------------------------------------------------

    def _ensure_valid_session(self):
        with self._lock:
            if self.uses_basic_auth_fallback:
                return
            if self.token and self.expires_at and datetime.utcnow() < (
                self.expires_at - self.config.REDFISH_SESSION_REFRESH_MARGIN
            ):
                return
            self._authenticate()

    def _authenticate(self):
        """POST credentials to SessionService/Sessions and capture the token."""
        payload = {"UserName": self.username, "Password": self.password}
        try:
            with httpx.Client(
                base_url=self.base_url,
                verify=self.config.REDFISH_VERIFY_TLS,
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
            ) as client:
                resp = client.post(SESSION_SERVICE_PATH, json=payload)
        except httpx.ConnectError as exc:
            raise RedfishUnreachableError(f"Cannot reach {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise RedfishUnreachableError(f"Timeout reaching {self.base_url}: {exc}") from exc

        if resp.status_code == 404:
            # SessionService not implemented - fall back to HTTP Basic Auth.
            logger.info("SessionService not found on %s, falling back to Basic Auth", self.base_url)
            self.uses_basic_auth_fallback = True
            self._verify_basic_auth()
            return

        if resp.status_code in (401, 403):
            raise RedfishAuthError(f"Authentication rejected by {self.base_url}")

        if resp.status_code not in (200, 201):
            raise RedfishUnreachableError(
                f"Unexpected status {resp.status_code} creating session on {self.base_url}"
            )

        token = resp.headers.get("X-Auth-Token")
        location = resp.headers.get("Location") or resp.json().get("@odata.id")
        if not token:
            raise RedfishAuthError(f"No X-Auth-Token returned by {self.base_url}")

        self.token = token
        self.session_uri = self._normalize_location(location)
        self.expires_at = datetime.utcnow() + timedelta(minutes=DEFAULT_ASSUMED_SESSION_TTL_MINUTES)
        logger.info("Authenticated session for %s at %s", self.base_url, self.session_uri)

    def _verify_basic_auth(self):
        """Sanity-check that Basic Auth actually works before we commit to it."""
        try:
            with httpx.Client(
                base_url=self.base_url,
                auth=(self.username, self.password),
                verify=self.config.REDFISH_VERIFY_TLS,
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
            ) as client:
                resp = client.get("/redfish/v1/")
        except httpx.HTTPError as exc:
            raise RedfishUnreachableError(str(exc)) from exc
        if resp.status_code in (401, 403):
            raise RedfishAuthError(f"Basic auth rejected by {self.base_url}")
        self.expires_at = None  # basic auth never expires

    def _normalize_location(self, location: str | None) -> str | None:
        if not location:
            return None
        if location.startswith("http"):
            # Strip scheme+host, keep path only.
            from urllib.parse import urlparse
            return urlparse(location).path
        return location


class SessionManager:
    """Process-wide cache of RedfishSession objects, one per server, so
    poller threads reuse authenticated sessions instead of re-logging in
    on every 30-second poll cycle."""

    def __init__(self, config):
        self.config = config
        self._sessions: dict[str, RedfishSession] = {}
        self._lock = threading.Lock()

    def get_session(self, server_id: str, base_url: str, username: str, password: str) -> RedfishSession:
        with self._lock:
            existing = self._sessions.get(server_id)
            if existing and existing.base_url == base_url.rstrip("/"):
                # If credentials have changed (e.g. updated in the UI), we must
                # completely invalidate the old session state, tokens, and basic
                # auth fallback flags so we start fresh.
                if existing.username != username or existing.password != password:
                    existing.logout()
                    self._sessions.pop(server_id, None)
                    existing = None
                else:
                    return existing
            
            session = RedfishSession(base_url, username, password, self.config, server_id)
            self._sessions[server_id] = session
            return session

    def drop_session(self, server_id: str):
        with self._lock:
            session = self._sessions.pop(server_id, None)
        if session:
            session.logout()
