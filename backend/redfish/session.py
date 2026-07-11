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


def format_httpx_exception(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, httpx.ConnectTimeout):
        return f"Connection timed out (is the BMC online/reachable?): {msg}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"Read timed out (BMC is too slow to respond): {msg}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timeout: {msg}"
    if isinstance(exc, httpx.ConnectError):
        if "SSL" in msg or "certificate" in msg.lower():
            return f"SSL/TLS Error: {msg}"
        return f"Connection refused or unreachable: {msg}"
    if isinstance(exc, httpx.NetworkError):
        return f"Network error: {msg}"
    return str(exc)


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
        self._client: httpx.Client | None = None

    # -- public API -------------------------------------------------------

    def get_http_client(self) -> httpx.Client:
        """Return an httpx.Client pre-configured with valid auth headers,
        authenticating or refreshing first if necessary."""
        self._ensure_valid_session()
        
        with self._lock:
            if self._client is not None:
                return self._client
                
            headers = {"OData-Version": "4.0"}
            if self.uses_basic_auth_fallback:
                auth = (self.username, self.password)
                self._client = httpx.Client(
                    base_url=self.base_url,
                    headers=headers,
                    auth=auth,
                    verify=self.config.REDFISH_VERIFY_TLS,
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                )
            else:
                headers["X-Auth-Token"] = self.token
                self._client = httpx.Client(
                    base_url=self.base_url,
                    headers=headers,
                    verify=self.config.REDFISH_VERIFY_TLS,
                    timeout=self.config.REDFISH_HTTP_TIMEOUT,
                    follow_redirects=True,
                )
            return self._client

    def invalidate(self):
        """Force the next call to re-authenticate (e.g. after a 401)."""
        with self._lock:
            self.token = None
            self.expires_at = None
            if self._client is not None:
                self._client.close()
                self._client = None

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
                follow_redirects=True,
            ) as client:
                client.delete(self.session_uri)
        except httpx.HTTPError as exc:
            logger.warning("Logout failed for %s: %s", self.base_url, exc)
        finally:
            self.token = None
            self.session_uri = None
            self.expires_at = None
            with self._lock:
                if self._client is not None:
                    self._client.close()
                    self._client = None

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
        sessions_url = SESSION_SERVICE_PATH
        try:
            with httpx.Client(
                base_url=self.base_url,
                verify=self.config.REDFISH_VERIFY_TLS,
                timeout=self.config.REDFISH_HTTP_TIMEOUT,
                follow_redirects=True,
            ) as client:
                # Attempt to dynamically discover the Sessions URL from the ServiceRoot
                try:
                    root_resp = client.get("/redfish/v1/")
                    if root_resp.status_code == 200:
                        root_data = root_resp.json()
                        session_service_url = root_data.get("SessionService", {}).get("@odata.id")
                        if session_service_url:
                            svc_resp = client.get(session_service_url)
                            if svc_resp.status_code == 200:
                                svc_data = svc_resp.json()
                                sessions_url = svc_data.get("Sessions", {}).get("@odata.id", sessions_url)
                except Exception as e:
                    logger.debug("Failed to dynamically discover SessionService URL: %s", e)

                resp = client.post(sessions_url, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            formatted = format_httpx_exception(exc)
            raise RedfishUnreachableError(f"Cannot reach {self.base_url}: {formatted}") from exc

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
                follow_redirects=True,
            ) as client:
                resp = client.get("/redfish/v1/")
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
            formatted = format_httpx_exception(exc)
            raise RedfishUnreachableError(formatted) from exc
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
