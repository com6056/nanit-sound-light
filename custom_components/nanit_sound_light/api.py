"""Pure protobuf API for Nanit Sound + Light devices."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import ssl
import time
from typing import Any

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosedError

from .const import (
    NANIT_API_BASE,
    NANIT_AUTH_URL,
    NANIT_BABIES_URL,
    NANIT_DEVICE_TOKEN_URL_TEMPLATE,
    SOUND_LIGHT_LOCAL_MDNS_PREFIX,
    SOUND_LIGHT_LOCAL_WS_PORT,
    SOUND_LIGHT_WS_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)

_REDACTED_RESPONSE_KEYS = frozenset(
    {"access_token", "refresh_token", "mfa_token", "password"}
)


def _redact_response_for_log(body: str | dict) -> str:
    """Return a debug-safe representation of an auth response.

    The Nanit /login endpoint returns access_token / refresh_token /
    mfa_token in the response body — logging the raw body or even a
    truncated preview leaks bearer tokens to anyone who pastes their HA
    debug log into a GitHub issue. This decodes the body as JSON,
    replaces sensitive values with "***", and returns the redacted form
    for logging. Falls back to a length-only description for non-JSON
    bodies.
    """
    try:
        data = body if isinstance(body, dict) else json.loads(body)
    except (ValueError, TypeError):
        length = len(body) if isinstance(body, (str, bytes)) else len(str(body))
        return f"<{length} bytes, not JSON>"
    if not isinstance(data, dict):
        return f"<{type(data).__name__}, not a JSON object>"
    redacted = {
        k: ("***" if k in _REDACTED_RESPONSE_KEYS and v else v) for k, v in data.items()
    }
    try:
        return json.dumps(redacted)
    except (TypeError, ValueError):
        return f"<dict with {len(redacted)} keys, not serializable>"


# WebSocket liveness. The device relies on WebSocket protocol-level ping/pong
# for keepalive; it sends no app-level keepalive frame (see CLAUDE.md). We set
# the ping interval explicitly rather than leaning on library defaults.
WS_PING_INTERVAL = 20  # seconds
WS_PING_TIMEOUT = 20  # seconds — drop a half-open socket instead of wedging
WS_CLOSE_TIMEOUT = 5  # seconds

# Command transaction model, mirroring the official app's SocketRequestManager:
# send ONE Request, then await the Response whose requestId matches (one in
# flight, drain each response). The app uses a 10s ack timeout. Fire-and-forget
# sends with undrained responses degrade the device's transaction state until it
# wedges (needs a power cycle) — this is the fix for that.
COMMAND_ACK_TIMEOUT = 10  # seconds to await a matching Response

# We do NOT re-send a command on a slow/absent ack. A timed-out ack on a LIVE
# socket means the device is busy, not gone — and re-sending piles duplicate
# commands onto an already-overloaded device, which is exactly what makes it
# stop responding for ~30s and then flush the whole backlog at once. The official
# app never retries either (one in flight, await ack, done). So a slow ack is
# accepted optimistically (the pin holds the UI; the device pushes real state when
# it catches up; the 30s poll reconciles). Only an actual socket DROP fails the
# command (→ rollback); an explicit non-2xx rejection also fails it.

# device.status enum value from Backend.device (Disconnected=0, Connected=1).
# The app derives "remote route is live" solely from this and sends nothing
# until Connected; sending into a still-Disconnected relay is what caused our
# command latency. We wait up to this long for the Connected frame before a
# command, then send best-effort (a missed/changed backend frame must not brick
# control — see send_control_command).
_BACKEND_STATUS_CONNECTED = 1
DEVICE_ATTACH_TIMEOUT = 10  # seconds to wait for backend Connected before a send

# Battery state-of-charge is a coarse 5-bucket enum (StateOfCharge); map each to
# a representative percentage. SoCLow has no number in its name → ~10% (low).
_SOC_TO_PERCENT = {0: 10, 1: 25, 2: 50, 3: 75, 4: 90}

# Reconnect backoff, mirroring the official app's
# RemoteControlSocketCandidate.getNextRetryTime: 0s, then 2s, 5s, capped at 7s.
# Replaces the old "reconnect lazily on the next 30s poll" behaviour.


def _reconnect_backoff(retries: int) -> int:
    """Seconds before REMOTE reconnect attempt number ``retries`` (0-indexed)."""
    if retries < 1:
        return 0
    if retries < 4:
        return 2
    if retries < 11:
        return 5
    return 7


# The LOCAL socket backs off more slowly than remote — mirrors the app's
# LocalControlSocketCandidate (0, 3, 10, 60, then 90s cap). Local failures are
# non-fatal (remote covers control meanwhile), so a slack schedule avoids
# hammering a `.local` name that may not resolve on this host at all.
_LOCAL_BACKOFF_SCHEDULE = (0, 3, 10, 60, 90)


def _local_reconnect_backoff(retries: int) -> int:
    """Seconds before LOCAL reconnect attempt number ``retries`` (0-indexed)."""
    idx = min(max(retries, 0), len(_LOCAL_BACKOFF_SCHEDULE) - 1)
    return _LOCAL_BACKOFF_SCHEDULE[idx]


# Transports per device, in send-preference order: try LOCAL first (fast, direct
# LAN), fall back to the REMOTE cloud relay. The app keeps both open on the same
# network and prefers local for sends (ControlSocketDecision / priority AP>LOCAL>
# REMOTE); we mirror local>remote.
TRANSPORT_LOCAL = "local"
TRANSPORT_REMOTE = "remote"
_TRANSPORTS = (TRANSPORT_LOCAL, TRANSPORT_REMOTE)
# Separator for the per-(device, transport) websocket key. "::" can't appear in a
# Nanit uid, so split is unambiguous.
_KEY_SEP = "::"


# Import protobuf classes at module level to avoid blocking async operations
try:
    from .sound_light_pb2 import (
        Color,
        GetSettings,
        Message,
        Request,
        Settings,
        Sound,
    )

    PROTOBUF_AVAILABLE = True
except ImportError as e:
    _LOGGER.error("Failed to import protobuf classes: %s", e)

    # Create dummy classes to prevent import errors
    class Color:
        pass

    class GetSettings:
        pass

    class Message:
        pass

    class Request:
        pass

    class Settings:
        pass

    class Sound:
        pass

    PROTOBUF_AVAILABLE = False


class CommandTimeoutError(ConnectionError):
    """A control command was sent but not acked within COMMAND_ACK_TIMEOUT.

    A ConnectionError subclass (so existing handlers still catch it), but
    distinct so the sender can retry an ack-timeout without retrying an explicit
    device rejection.
    """


class AuthenticationError(Exception):
    """Authentication failed."""


class MfaRequiredError(Exception):
    """MFA code required for authentication."""

    def __init__(self, message: str, mfa_token: str):
        super().__init__(message)
        self.mfa_token = mfa_token


class SoundLightAPI:
    """Pure protobuf API client for Nanit Sound + Light devices."""

    def __init__(
        self, session: aiohttp.ClientSession, *, local_enabled: bool = True
    ) -> None:
        """Initialize the API client.

        ``local_enabled`` turns on the direct-LAN path (preferred for sends,
        with the cloud relay as fallback). It is best-effort: if the device
        token can't be fetched or the ``.local`` name doesn't resolve, the
        client silently stays on the relay.
        """
        self._session = session
        self._local_enabled = local_enabled
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._password: str | None = None
        # Keyed by f"{baby_uid}{_KEY_SEP}{transport}" — a device can have BOTH a
        # local and a remote socket open at once (the app does). Device-level
        # state (attachment, pending acks, send lock, sessionId) stays keyed by
        # baby_uid and is shared across a device's transports.
        self._websockets: dict[str, websockets.WebSocketServerProtocol] = {}
        # Per-speaker local device token: speaker_uid -> (token, expires_at|None).
        # Distinct from the user access token; only the LOCAL socket uses it.
        self._device_tokens: dict[str, tuple[str, float | None]] = {}
        # Optional async resolver: speaker_uid -> LAN IPv4 (or None). Injected by
        # the coordinator (HA's zeroconf), because a HA install in a container
        # usually can't resolve `.local` via libc (no nss-mdns); it finds the
        # device's mDNS service by uid and returns its IP. When unset we fall back
        # to handing the deterministic `.local` name to the OS resolver (works on
        # HA OS / hosts with nss-mdns). Signature: async (speaker_uid) -> str|None.
        self._local_host_resolver = None
        # Which transport a device's one in-flight command went out on, so a
        # redundant socket dropping doesn't fail a command acked on the other.
        self._inflight_conn_key: dict[str, str] = {}
        self._device_state: dict[str, dict[str, Any]] = {}
        # Mirrors the app's AtomicInteger(0): first _next_message_id() returns 1.
        self._message_id = 0
        self._state_change_callback = None  # Callback for real-time updates
        self._last_auth_failure = None  # Track last auth failure time
        self._auth_retry_count = 0  # Track consecutive auth failures
        self._max_retry_count = 3  # Max retries before requiring manual intervention
        self._token_update_callback = None  # Callback for token updates
        self._stored_email: str | None = None
        self._stored_password: str | None = None
        self._pending_mfa_token: str | None = None  # Store MFA token when needed
        self._mfa_required_callback = None  # Callback when MFA is required
        self._device_list: list[
            dict[str, Any]
        ] = []  # Store device info for reconnection
        self._token_expires_at: float | None = None  # Token expiration timestamp
        self._token_refresh_buffer = 300  # Refresh token 5 minutes before expiration

        # Connection lifecycle. `_closing` stops the reconnect loop on shutdown;
        # `_connect_locks` serialises connects per (device, transport) so a
        # proactive reconnect, a lazy `ensure_websocket_connection`, and the 30s
        # poll can't open duplicate sockets; `_reconnect_tasks` tracks the running
        # backoff loop per (device, transport). All three dicts are keyed by
        # connection key (`baby_uid{_KEY_SEP}transport`).
        self._closing = False
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        # Strong refs to the per-connection message-handler tasks. asyncio only
        # holds a weak reference to a bare create_task() result, so without this
        # the handler could be garbage-collected mid-run and silently stop
        # delivering device pushes.
        self._handler_tasks: dict[str, asyncio.Task] = {}

        # Backend readiness gate. The device's first frame after a remote connect
        # is Message{backend} reporting whether the physical device is attached
        # behind the relay; we must not send until it's Connected (else commands
        # stall = latency). `_device_attached` is the latched bool, `_attached_events`
        # lets a pending send await the Connected transition. Attachment is
        # STICKY: set by a Connected backend frame or any real traffic, and
        # cleared only on a socket drop — NOT by the bare/Disconnected backend
        # frames the device emits periodically while fully usable.
        self._device_attached: dict[str, bool] = {}
        self._attached_events: dict[str, asyncio.Event] = {}

        # One command in flight per device + request/response correlation. A
        # send registers a future keyed by its message id; the message handler
        # resolves it when the matching Response arrives (drain each response).
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._pending_responses: dict[str, dict[int, asyncio.Future]] = {}

        # Random per-connection sessionId, mirroring the app (a per-launch
        # SecureRandom token). The device tolerates a null sessionId in
        # responses, but stamping a fresh one per socket matches the app.
        self._session_ids: dict[str, str] = {}

    def has_stored_credentials(self) -> bool:
        """Check if we have stored credentials for re-authentication."""
        return (
            self._stored_email is not None
            and self._stored_password is not None
            and len(self._stored_email.strip()) > 0
            and len(self._stored_password.strip()) > 0
        )

    def _extract_token_expiration(self, token: str) -> float | None:
        """Extract expiration time from JWT token."""
        if not token:
            return None

        try:
            # JWT tokens have 3 parts separated by dots
            parts = token.split(".")
            if len(parts) != 3:
                _LOGGER.debug(
                    "Token is not a JWT (doesn't have 3 parts), assuming no expiration info available"
                )
                return None

            # Decode the payload (second part)
            payload = parts[1]
            # Add padding if needed for base64 decoding
            payload += "=" * (4 - len(payload) % 4)

            try:
                decoded = base64.urlsafe_b64decode(payload)
                payload_data = json.loads(decoded.decode("utf-8"))

                # JWT standard 'exp' field contains expiration timestamp
                exp = payload_data.get("exp")
                if exp:
                    exp_time = float(exp)
                    current_time = time.time()
                    expires_in_minutes = (exp_time - current_time) / 60
                    _LOGGER.debug(
                        "JWT token expires in %.1f minutes (exp=%d)",
                        expires_in_minutes,
                        exp,
                    )
                    return exp_time
                else:
                    _LOGGER.debug("JWT token has no 'exp' field")
                    return None

            except (
                base64.binascii.Error,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as e:
                _LOGGER.debug("Failed to decode JWT payload: %s", e)
                return None

        except Exception as e:
            _LOGGER.debug("Failed to extract token expiration: %s", e)

        return None

    def _is_token_expired(self) -> bool:
        """Check if access token is expired or needs refresh soon."""
        if not self._access_token or not self._token_expires_at:
            return True

        # Check if token expires within the buffer time (5 minutes by default)
        current_time = time.time()
        expires_soon = current_time >= (
            self._token_expires_at - self._token_refresh_buffer
        )

        if expires_soon:
            _LOGGER.debug(
                "Token expires in %.1f minutes, will refresh",
                (self._token_expires_at - current_time) / 60,
            )

        return expires_soon

    async def authenticate(
        self, email: str, password: str, refresh_token: str | None = None
    ) -> None:
        """Authenticate with Nanit API (try refresh token first if available)."""
        # Store credentials for potential re-authentication
        self._stored_email = email
        self._stored_password = password

        if refresh_token:
            self._refresh_token = refresh_token
            # Try to use existing refresh token first
            if await self._refresh_auth():
                return

        try:
            # Store password for potential MFA verification
            self._password = password

            # Initial authentication (let user choose MFA method if needed)
            auth_data = {"email": email, "password": password, "channel": "email"}
            headers = {"Content-Type": "application/json", "nanit-api-version": "1"}

            _LOGGER.debug("Initial auth request to: %s", NANIT_AUTH_URL)
            _LOGGER.debug(
                "Auth data: %s",
                {
                    **auth_data,
                    "password": "***",
                    "mfa_token": "***" if "mfa_token" in auth_data else None,
                },
            )

            async with self._session.post(
                NANIT_AUTH_URL, json=auth_data, headers=headers
            ) as response:
                response_text = await response.text()
                _LOGGER.debug(
                    "Login response: status=%d, length=%d bytes",
                    response.status,
                    len(response_text),
                )
                _LOGGER.debug(
                    "Login response (redacted): %s",
                    _redact_response_for_log(response_text),
                )

                if response.status == 201:
                    # Successful login without MFA
                    response_data = await response.json()
                    self._access_token = response_data.get("access_token")

                    # Extract token expiration
                    if self._access_token:
                        self._token_expires_at = self._extract_token_expiration(
                            self._access_token
                        )
                        if self._token_expires_at:
                            expires_in_minutes = (
                                self._token_expires_at - time.time()
                            ) / 60
                            _LOGGER.debug(
                                "Access token expires in %.1f minutes",
                                expires_in_minutes,
                            )
                    new_refresh_token = response_data.get("refresh_token")
                    if new_refresh_token:
                        self._refresh_token = new_refresh_token
                        # Notify coordinator of token update
                        if self._token_update_callback:
                            try:
                                await self._token_update_callback(new_refresh_token)
                            except Exception as e:
                                _LOGGER.debug("Token update callback failed: %s", e)

                    if self._access_token:
                        # Reset auth failure tracking on success
                        self._last_auth_failure = None
                        self._auth_retry_count = 0
                        _LOGGER.info(
                            "Authentication successful for user: %s",
                            email.split("@")[0] + "@***",
                        )
                        return {"success": True}

                elif response.status in [200, 482]:
                    # MFA required - 482 is the actual MFA status code
                    response_data = await response.json()
                    _LOGGER.debug(
                        "MFA required response (redacted): %s",
                        _redact_response_for_log(response_data),
                    )

                    mfa_token = response_data.get("mfa_token")
                    if mfa_token:
                        _LOGGER.info(
                            "MFA verification required for user: %s",
                            email.split("@")[0] + "@***",
                        )
                        raise MfaRequiredError("MFA code required", mfa_token)

                raise AuthenticationError(
                    f"Login failed: status={response.status}, "
                    f"body={_redact_response_for_log(response_text)}"
                )

        except MfaRequiredError:
            # Re-raise MFA errors as-is (don't count as auth failure for retry purposes)
            raise
        except (
            aiohttp.ClientError,
            aiohttp.ServerTimeoutError,
            asyncio.TimeoutError,
        ) as e:
            error_type = type(e).__name__
            _LOGGER.error(
                "Network error during authentication (%s): %s - Check internet connection and Nanit server status",
                error_type,
                e,
            )
            raise AuthenticationError(f"Network error during login: {e}")
        except Exception as e:
            error_type = type(e).__name__
            _LOGGER.error("Unexpected authentication error (%s): %s", error_type, e)
            raise AuthenticationError(f"Login failed: {e}")

    async def complete_mfa_authentication(
        self, email: str, password: str, mfa_token: str, mfa_code: str
    ) -> None:
        """Complete MFA authentication with the provided code."""
        try:
            # Clean up the MFA code by removing quotes and whitespace
            mfa_code = mfa_code.strip()
            if mfa_code.startswith('"') and mfa_code.endswith('"'):
                mfa_code = mfa_code[1:-1]

            # Send MFA code to verify authentication
            mfa_data = {
                "email": email,
                "password": password,
                "mfa_token": mfa_token,
                "mfa_code": mfa_code,
                "channel": "email",
            }
            headers = {"Content-Type": "application/json", "nanit-api-version": "1"}

            _LOGGER.debug(
                "MFA verification request for user: %s", email.split("@")[0] + "@***"
            )

            async with self._session.post(
                NANIT_AUTH_URL, json=mfa_data, headers=headers
            ) as response:
                _LOGGER.debug(
                    "MFA response: status=%d, success=%s",
                    response.status,
                    response.status == 201,
                )

                if response.status == 201:
                    response_data = await response.json()
                    self._access_token = response_data.get("access_token")

                    # Extract token expiration
                    if self._access_token:
                        self._token_expires_at = self._extract_token_expiration(
                            self._access_token
                        )
                        if self._token_expires_at:
                            expires_in_minutes = (
                                self._token_expires_at - time.time()
                            ) / 60
                            _LOGGER.debug(
                                "Access token expires in %.1f minutes",
                                expires_in_minutes,
                            )

                    new_refresh_token = response_data.get("refresh_token")
                    if new_refresh_token:
                        self._refresh_token = new_refresh_token
                        # Notify coordinator of token update
                        if self._token_update_callback:
                            try:
                                await self._token_update_callback(new_refresh_token)
                            except Exception as e:
                                _LOGGER.debug("Token update callback failed: %s", e)

                    if not self._access_token:
                        raise AuthenticationError(
                            "No access token received after MFA verification"
                        )

                    # Reset auth failure tracking on successful MFA
                    self._last_auth_failure = None
                    self._auth_retry_count = 0
                    _LOGGER.info(
                        "MFA verification successful for user: %s",
                        email.split("@")[0] + "@***",
                    )
                else:
                    error_msg = f"MFA verification failed: {response.status}"
                    if response.status == 401:
                        error_msg += " - Invalid MFA code provided"
                    elif response.status >= 500:
                        error_msg += " - Server error, please try again"
                    raise AuthenticationError(error_msg)

        except (
            aiohttp.ClientError,
            aiohttp.ServerTimeoutError,
            asyncio.TimeoutError,
        ) as e:
            error_type = type(e).__name__
            _LOGGER.error(
                "Network error during MFA verification (%s): %s", error_type, e
            )
            raise AuthenticationError(f"Network error during MFA verification: {e}")
        except Exception as e:
            error_type = type(e).__name__
            _LOGGER.error("MFA verification failed (%s): %s", error_type, e)
            raise AuthenticationError(f"MFA verification failed: {e}")

    async def _refresh_auth(self) -> bool:
        """Refresh authentication token using stored refresh token."""
        if not self._refresh_token:
            _LOGGER.debug("No refresh token available for token refresh")
            return False

        refresh_data = {"refresh_token": self._refresh_token}

        try:
            refresh_url = f"{NANIT_API_BASE}/tokens/refresh"
            _LOGGER.debug("Attempting token refresh")
            async with self._session.post(refresh_url, json=refresh_data) as response:
                if response.status == 200:
                    data = await response.json()
                    self._access_token = data.get("access_token")

                    # Extract token expiration
                    if self._access_token:
                        self._token_expires_at = self._extract_token_expiration(
                            self._access_token
                        )
                        if self._token_expires_at:
                            expires_in_minutes = (
                                self._token_expires_at - time.time()
                            ) / 60
                            _LOGGER.debug(
                                "Refreshed token expires in %.1f minutes",
                                expires_in_minutes,
                            )

                    new_refresh_token = data.get("refresh_token")
                    if new_refresh_token:
                        self._refresh_token = new_refresh_token
                        # Notify coordinator of token update
                        if self._token_update_callback:
                            try:
                                await self._token_update_callback(new_refresh_token)
                            except Exception as e:
                                _LOGGER.debug("Token update callback failed: %s", e)
                    # Reset auth failure tracking on successful refresh
                    self._last_auth_failure = None
                    self._auth_retry_count = 0
                    _LOGGER.info("Token refresh successful - authentication renewed")
                    return True
                elif response.status == 404:
                    _LOGGER.info("Refresh token expired - re-authentication required")
                    # Clear expired tokens
                    self._refresh_token = None
                    self._access_token = None
                elif response.status == 401:
                    _LOGGER.warning(
                        "Refresh token invalid - re-authentication required"
                    )
                    self._refresh_token = None
                    self._access_token = None
                else:
                    _LOGGER.warning(
                        "Token refresh failed with status: %d - will retry with full auth",
                        response.status,
                    )
        except (
            aiohttp.ClientError,
            aiohttp.ServerTimeoutError,
            asyncio.TimeoutError,
        ) as e:
            _LOGGER.debug("Network error during token refresh: %s", e)
        except Exception as e:
            _LOGGER.debug("Token refresh failed: %s", e)

        return False

    def _should_attempt_auth(self) -> bool:
        """Check if we should attempt authentication based on retry limits and timing."""
        # If we haven't failed recently, allow auth attempt
        if self._last_auth_failure is None:
            return True

        # Calculate time since last failure
        time_since_failure = time.time() - self._last_auth_failure

        # If we've hit max retries, require a longer wait period (30 minutes)
        if self._auth_retry_count >= self._max_retry_count:
            if time_since_failure < 1800:  # 30 minutes
                remaining_minutes = (1800 - time_since_failure) / 60
                _LOGGER.warning(
                    "Authentication retry limit reached (%d attempts). "
                    "Waiting %.1f more minutes to prevent MFA spam and protect your account",
                    self._auth_retry_count,
                    remaining_minutes,
                )
                return False
            else:
                # Reset retry count after waiting period
                _LOGGER.info(
                    "Authentication retry wait period expired - resuming normal authentication"
                )
                self._auth_retry_count = 0
                self._last_auth_failure = None
                return True

        # Exponential backoff for earlier retries (30s, 2min, 5min)
        min_wait_times = [30, 120, 300]  # seconds
        if self._auth_retry_count > 0:
            min_wait = min_wait_times[
                min(self._auth_retry_count - 1, len(min_wait_times) - 1)
            ]
            if time_since_failure < min_wait:
                _LOGGER.debug(
                    "Authentication backoff active. Wait %.1f more seconds",
                    min_wait - time_since_failure,
                )
                return False

        return True

    def _record_auth_failure(self) -> None:
        """Record an authentication failure for rate limiting."""
        self._last_auth_failure = time.time()
        self._auth_retry_count += 1

        next_retry_info = (
            "30 minutes"
            if self._auth_retry_count >= self._max_retry_count
            else f"{[30, 120, 300][min(self._auth_retry_count - 1, 2)]} seconds"
        )

        _LOGGER.warning(
            " Authentication attempt %d/%d failed. Next retry allowed in %s",
            self._auth_retry_count,
            self._max_retry_count,
            next_retry_info,
        )

    async def ensure_authenticated(self) -> bool:
        """Ensure we have a valid access token, refreshing if needed."""
        # If we have a valid token that doesn't need refresh, return immediately
        if self._access_token and not self._is_token_expired():
            return True

        # Use the refresh token whenever we lack a usable access token. This
        # covers a fresh startup that carries only the stored refresh token (no
        # access token, and — since we no longer persist the password — no
        # credentials to fall back on), as well as an access token that's
        # expiring. Without this, startup never used the refresh token and the
        # entry got stuck on "Authentication temporarily unavailable".
        if self._refresh_token and (not self._access_token or self._is_token_expired()):
            _LOGGER.debug("Refreshing access token via refresh token...")
            if await self._refresh_auth():
                return True
            # Refresh failed. If the token was rejected, _refresh_auth cleared
            # it (→ needs_reauth → reauth). Otherwise it was transient.
            self._access_token = None
            self._token_expires_at = None

        # If we don't have a valid token and should not attempt auth, return False
        if not self._access_token and not self._should_attempt_auth():
            return False

        # If we have no token but stored credentials, try to re-authenticate
        if not self._access_token and self.has_stored_credentials():
            try:
                await self.authenticate(
                    self._stored_email, self._stored_password, self._refresh_token
                )
                return self._access_token is not None
            except MfaRequiredError as mfa_error:
                # Store MFA token and notify coordinator to trigger repair flow
                self._pending_mfa_token = mfa_error.mfa_token
                _LOGGER.info("MFA required for re-authentication")
                if self._mfa_required_callback:
                    try:
                        await self._mfa_required_callback()
                    except Exception as e:
                        _LOGGER.debug("MFA required callback failed: %s", e)
                return False
            except AuthenticationError as e:
                self._record_auth_failure()
                _LOGGER.error("Re-authentication failed: %s", e)
                return False

        return self._access_token is not None

    async def _fetch_babies_with_retry(self) -> dict[str, Any]:
        """Fetch the babies endpoint, refreshing the access token once on 401.

        Returns the parsed JSON body. Each `response.json()` call lives
        inside its own `async with` so we never await a buffered body
        after the context manager has released the underlying connection.
        """
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with self._session.get(NANIT_BABIES_URL, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            if response.status == 401:
                # Try to refresh and retry once.
                if not await self._refresh_auth():
                    raise AuthenticationError("Token expired and refresh failed")
            else:
                raise Exception(f"Failed to get devices: {response.status}")

        # Refresh succeeded; retry with the new access token.
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with self._session.get(
            NANIT_BABIES_URL, headers=headers
        ) as retry_response:
            if retry_response.status == 200:
                return await retry_response.json()
            raise Exception(
                f"Failed to get devices after refresh: {retry_response.status}"
            )

    async def get_sound_light_devices(self) -> list[dict[str, Any]]:
        """Get list of Sound + Light devices."""
        if not await self.ensure_authenticated():
            raise AuthenticationError("Authentication failed or not authenticated")

        babies_data = await self._fetch_babies_with_retry()
        sound_light_devices = []

        for baby in babies_data.get("babies", []):
            # Check if baby has Sound + Light device
            speaker_data = baby.get("speaker", {})
            if speaker_data.get("attached_to_speaker") and speaker_data.get("speaker"):
                device_info = {
                    "baby_uid": baby.get("uid"),
                    "baby_name": baby.get("name", "Nanit"),
                    "speaker_uid": speaker_data["speaker"]["uid"],
                    "speaker_name": speaker_data["speaker"]["name"],
                }
                sound_light_devices.append(device_info)
                _LOGGER.debug(
                    "Found Sound + Light device: %s (%s)",
                    device_info["speaker_name"],
                    device_info["speaker_uid"],
                )

        # Store device list for potential reconnections
        self._device_list = sound_light_devices
        return sound_light_devices

    @staticmethod
    def _conn_key(baby_uid: str, transport: str) -> str:
        """The websocket dict key for one device/transport pair."""
        return f"{baby_uid}{_KEY_SEP}{transport}"

    @staticmethod
    def _split_conn_key(connection_key: str) -> tuple[str, str]:
        """Inverse of ``_conn_key``: (baby_uid, transport)."""
        baby_uid, _, transport = connection_key.rpartition(_KEY_SEP)
        return baby_uid, transport

    def _local_ws_url(self, speaker_uid: str) -> str:
        """Direct-LAN websocket URL for a speaker (deterministic mDNS name).

        ``wss://Nanit-<speaker_uid>.local:442`` — no path (the relay's
        ``/<uid>/user_connect/`` path is remote-only). Reverse-engineered from
        the app's ConnectivityRouteLocalMDNS.
        """
        host = f"{SOUND_LIGHT_LOCAL_MDNS_PREFIX}{speaker_uid}.local"
        return f"wss://{host}:{SOUND_LIGHT_LOCAL_WS_PORT}"

    @staticmethod
    def _build_insecure_ssl_context() -> ssl.SSLContext:
        """A trust-all TLS context for the LOCAL device socket.

        The speaker presents a self-signed cert on the LAN and the official app
        accepts ANY cert and ANY hostname for it (its local OkHttp client uses an
        empty TrustManager + always-true HostnameVerifier). We match that: there
        is no public CA to verify against and no cert to pin. Only ever used for
        the on-LAN device — the cloud relay keeps full verification.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _fetch_device_token(self, speaker_uid: str) -> None:
        """Fetch + cache the per-device token used for LOCAL socket auth.

        ``GET /speakers/{uid}/udtokens`` (Bearer user token) →
        ``{"userDeviceToken": {"token", "expirationTime"}}``. Best-effort: any
        failure (endpoint shape, 404, network) just leaves the device without a
        local token, so the integration stays on the remote relay.
        """
        if self._session is None or not self._access_token:
            return
        url = NANIT_DEVICE_TOKEN_URL_TEMPLATE.format(speaker_uid=speaker_uid)
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "nanit-api-version": "1",
        }
        try:
            async with self._session.get(url, headers=headers) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Device-token fetch for %s returned %d; staying on relay",
                        speaker_uid,
                        response.status,
                    )
                    return
                data = await response.json()
            # The wire JSON is snake_case (like /login: access_token, refresh_token)
            # even though the app's decompiled DTO fields are camelCase. Accept the
            # real snake_case keys, with camelCase as a forward-compat fallback.
            udt = data.get("user_device_token") or data.get("userDeviceToken") or {}
            token = udt.get("token")
            if not token:
                _LOGGER.debug("Device-token response for %s had no token", speaker_uid)
                return
            expires_at: float | None = None
            exp = udt.get("expiration") or udt.get("expirationTime")
            if exp:
                expires_at = float(exp)
                # expirationTime's unit is unconfirmed; treat a > year-2286 value
                # as milliseconds. Mis-scaling only affects refresh cadence.
                if expires_at > 1e12:
                    expires_at /= 1000.0
            self._device_tokens[speaker_uid] = (token, expires_at)
            _LOGGER.debug("Cached local device token for %s", speaker_uid)
        except Exception as e:  # noqa: BLE001 — local is best-effort
            _LOGGER.debug("Device-token fetch failed for %s: %s", speaker_uid, e)

    async def _ensure_device_token(self, speaker_uid: str) -> str | None:
        """Return a usable local device token, fetching/refreshing if needed."""
        cached = self._device_tokens.get(speaker_uid)
        if cached is not None:
            token, expires_at = cached
            if expires_at is None or time.time() < expires_at - 60:
                return token
        await self._fetch_device_token(speaker_uid)
        cached = self._device_tokens.get(speaker_uid)
        return cached[0] if cached else None

    def _transport_connected(self, connection_key: str) -> bool:
        """True if the socket for this exact (device, transport) is open."""
        websocket = self._websockets.get(connection_key)
        return websocket is not None and not self._is_websocket_closed(websocket)

    def _any_transport_connected(self, baby_uid: str) -> bool:
        """True if ANY of a device's transports has a live socket."""
        return any(
            self._transport_connected(self._conn_key(baby_uid, t)) for t in _TRANSPORTS
        )

    def _active_connection_key(self, baby_uid: str) -> str | None:
        """The connection key to SEND on, preferring local over remote."""
        for transport in _TRANSPORTS:  # local first
            key = self._conn_key(baby_uid, transport)
            if self._transport_connected(key):
                return key
        return None

    def active_transport(self, baby_uid: str) -> str | None:
        """User-facing label for the transport sends currently route over.

        ``"local"`` (direct LAN), ``"cloud"`` (the relay), or ``None`` when the
        device is unreachable. Backs the Connection Type diagnostic sensor.
        """
        key = self._active_connection_key(baby_uid)
        if key is None:
            return None
        _baby, transport = self._split_conn_key(key)
        return "local" if transport == TRANSPORT_LOCAL else "cloud"

    async def _connect_transport(
        self, device_info: dict[str, Any], transport: str
    ) -> None:
        """Open one socket for a device on a given transport (idempotent).

        Local failures are logged at debug and swallowed (the relay covers
        control); remote failures stay at error. Device-level attachment is NOT
        reset here — it is sticky (set by frames, cleared only when all of a
        device's transports drop), so bringing up a second transport never
        re-gates an already-working device.
        """
        speaker_uid = device_info["speaker_uid"]
        baby_uid = device_info["baby_uid"]
        connection_key = self._conn_key(baby_uid, transport)

        lock = self._connect_locks.setdefault(connection_key, asyncio.Lock())
        async with lock:
            if self._transport_connected(connection_key):
                return  # connected while we waited for the lock

            if transport == TRANSPORT_REMOTE:
                ws_url = f"{SOUND_LIGHT_WS_BASE_URL}/{speaker_uid}/user_connect/"
                token = self._access_token
            else:  # local
                ws_url = self._local_ws_url(speaker_uid)
                # Resolve the device's LAN IP if a resolver is injected
                # (HA-in-container can't do `.local` via libc). The resolver
                # (HA zeroconf) finds the device by uid in its mDNS cache and
                # returns an IP. On HA OS (no resolver) we hand the deterministic
                # `.local` name straight to the OS resolver instead.
                if self._local_host_resolver is not None:
                    ip = await self._local_host_resolver(speaker_uid)
                    if not ip:
                        _LOGGER.debug(
                            "Local mDNS resolve failed for %s; staying on relay",
                            speaker_uid,
                        )
                        return
                    ws_url = f"wss://{ip}:{SOUND_LIGHT_LOCAL_WS_PORT}"
                token = await self._ensure_device_token(speaker_uid)
            if not token:
                # No usable token (no access token, or local token unavailable).
                return

            # The WebSocket handshake uses the `token` auth scheme, NOT `Bearer`
            # — verified in the app (WebSocketClient sends `Authorization: token
            # <token>`), for BOTH local and remote. The remote token is the user
            # access token; the local token is the per-device token.
            headers = {"Authorization": f"token {token}"}

            try:
                # TLS context only for wss:// (plaintext ws:// is used by tests
                # against an in-process fake). The local device uses a trust-all
                # context (self-signed cert, app accepts any); remote uses full
                # verification. Build it off the event loop.
                ssl_context = None
                if ws_url.startswith("wss://"):
                    loop = asyncio.get_event_loop()
                    builder = (
                        self._build_insecure_ssl_context
                        if transport == TRANSPORT_LOCAL
                        else ssl.create_default_context
                    )
                    ssl_context = await loop.run_in_executor(None, builder)

                websocket = await websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ssl=ssl_context,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=WS_CLOSE_TIMEOUT,
                )

                self._websockets[connection_key] = websocket

                # Start message handler, keeping a strong reference so it can't
                # be garbage-collected mid-run; drop the ref when it finishes.
                task = asyncio.create_task(
                    self._handle_messages(connection_key, websocket)
                )
                self._handler_tasks[connection_key] = task
                task.add_done_callback(
                    lambda _t, key=connection_key: self._handler_tasks.pop(key, None)
                )

                # Don't send anything yet. The app sends nothing on open and
                # waits for the backend Connected frame (remote) / first Response
                # (local) before sending. The coordinator poll and
                # send_saved_sounds_request both wait for attachment.
                _LOGGER.debug(
                    "Connected to Sound + Light device %s via %s",
                    speaker_uid,
                    transport,
                )

            except Exception as e:
                log = _LOGGER.debug if transport == TRANSPORT_LOCAL else _LOGGER.error
                log(
                    "Failed to connect to Sound + Light device %s via %s: %s",
                    speaker_uid,
                    transport,
                    e,
                )

    async def connect_device(self, device_info: dict[str, Any]) -> None:
        """Connect a device's transports: remote always, local when enabled.

        Both can be open simultaneously (the app does this on-LAN); sends prefer
        local. Local is best-effort and never blocks remote — a local failure is
        swallowed inside ``_connect_transport``.
        """
        await self._connect_transport(device_info, TRANSPORT_REMOTE)
        if self._local_enabled:
            await self._connect_transport(device_info, TRANSPORT_LOCAL)

    def _eligible_transports(self) -> tuple[str, ...]:
        """Transports we should (re)connect: remote always, local if enabled."""
        if self._local_enabled:
            return _TRANSPORTS
        return (TRANSPORT_REMOTE,)

    def _schedule_reconnect(self, baby_uid: str, transport: str | None = None) -> None:
        """Start backoff reconnect loop(s) for a device.

        ``transport`` reconnects just that one (used when a specific socket
        drops); ``None`` schedules every eligible transport (used when a send
        finds no live socket at all).
        """
        if self._closing:
            return
        transports = (
            (transport,) if transport is not None else self._eligible_transports()
        )
        for t in transports:
            connection_key = self._conn_key(baby_uid, t)
            task = self._reconnect_tasks.get(connection_key)
            if task is not None and not task.done():
                continue
            self._reconnect_tasks[connection_key] = asyncio.create_task(
                self._reconnect_with_backoff(baby_uid, t)
            )

    async def _reconnect_with_backoff(self, baby_uid: str, transport: str) -> None:
        """Reconnect one dropped transport, backing off like the official app.

        Per-transport so a dead local socket can't stall remote reconnection
        (and vice-versa). Local uses the slower local schedule and gives up if no
        device token is available — a later full connect (driven by the poll)
        retries it.
        """
        device_info = next(
            (d for d in self._device_list if d.get("baby_uid") == baby_uid), None
        )
        if device_info is None:
            return

        connection_key = self._conn_key(baby_uid, transport)
        backoff = (
            _local_reconnect_backoff
            if transport == TRANSPORT_LOCAL
            else _reconnect_backoff
        )
        retries = 0
        while not self._closing and not self._transport_connected(connection_key):
            delay = backoff(retries)
            if delay:
                await asyncio.sleep(delay)
            if self._closing:
                return
            _LOGGER.debug(
                "Reconnecting to %s via %s (attempt %d)",
                baby_uid,
                transport,
                retries + 1,
            )
            await self._connect_transport(device_info, transport)
            retries += 1

        if self._transport_connected(connection_key):
            _LOGGER.debug(
                "Reconnected to %s via %s after %d attempt(s)",
                baby_uid,
                transport,
                retries,
            )

    def _next_message_id(self) -> int:
        """Return a unique, monotonically increasing control-message id.

        The official app stamps every control request with an incrementing id
        (an AtomicInteger) and correlates responses by it. We previously sent
        ``id=1`` on every command, so concurrent commands — e.g. a Home
        Assistant scene touching power + sound + volume + light at once — were
        indistinguishable and their out-of-order responses could clobber each
        other's state. asyncio is single-threaded, so a plain increment is
        race-free here.
        """
        self._message_id += 1
        return self._message_id

    def _session_id(self, baby_uid: str) -> str:
        """Return this connection's random sessionId, creating one if needed."""
        sid = self._session_ids.get(baby_uid)
        if sid is None:
            # ~50 random bits as an opaque token (the app uses BigInteger(50,
            # SecureRandom).toString(32); the device treats this as opaque and
            # tolerates a null sessionId, so the exact radix doesn't matter).
            sid = format(secrets.randbits(50), "x")
            self._session_ids[baby_uid] = sid
        return sid

    def _attached_event(self, baby_uid: str) -> asyncio.Event:
        """The per-device 'backend Connected' event (created lazily)."""
        return self._attached_events.setdefault(baby_uid, asyncio.Event())

    def is_device_attached(self, baby_uid: str) -> bool:
        """True once the relay reported the physical device as attached.

        Gates entity availability and command sends: a socket can be open while
        the device behind the relay is still Disconnected, in which case sending
        only produces latency. Set from the Backend frame's device.status, and
        also inferred from any genuine Response/settings traffic (if the device
        is answering, it's clearly attached) so a missed/renamed backend frame
        can't wedge us permanently.
        """
        return self._device_attached.get(baby_uid, False)

    def _mark_attached(self, baby_uid: str) -> None:
        """Latch the device as attached and wake anyone waiting to send."""
        self._device_attached[baby_uid] = True
        self._attached_event(baby_uid).set()

    def _mark_detached(self, baby_uid: str) -> None:
        """Clear attachment (socket dropped or backend reported Disconnected)."""
        self._device_attached[baby_uid] = False
        event = self._attached_events.get(baby_uid)
        if event is not None:
            event.clear()

    async def wait_for_device_attached(
        self, baby_uid: str, timeout: float | None = None
    ) -> bool:
        """Wait up to ``timeout`` for the backend Connected frame.

        Returns True once attached, False on timeout. Callers decide what to do
        on False (commands send best-effort; the poll just warns). ``timeout``
        defaults to ``DEVICE_ATTACH_TIMEOUT`` read at call time (so tests can
        monkeypatch the module constant).
        """
        if timeout is None:
            timeout = DEVICE_ATTACH_TIMEOUT
        if self.is_device_attached(baby_uid):
            return True
        try:
            await asyncio.wait_for(self._attached_event(baby_uid).wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _resolve_pending_response(self, baby_uid: str, response) -> None:
        """Resolve the awaiting send (if any) for an inbound Response by requestId.

        Mirrors the app's correlation: a Response carries the requestId of the
        Request it answers; we hand its statusCode to the matching send so it
        can confirm success (2xx) or surface a rejection. Unmatched Responses
        (e.g. our fire-and-forget GetSettings poll) simply have no waiter.
        """
        if not response.HasField("requestId"):
            return
        request_id = response.requestId
        status_code = response.statusCode if response.HasField("statusCode") else 200
        future = self._pending_responses.get(baby_uid, {}).get(request_id)
        if future is not None and not future.done():
            future.set_result(status_code)

    def _fail_pending_responses(self, baby_uid: str, error: Exception) -> None:
        """Fail all in-flight sends for a device (socket dropped before ack)."""
        for future in self._pending_responses.pop(baby_uid, {}).values():
            if not future.done():
                future.set_exception(error)

    def build_control_message(
        self, session_id: str | None = None, **kwargs
    ) -> tuple[bytes, int]:
        """Build a serialized control Message from the given fields.

        Returns ``(message_bytes, message_id)``. Every provided field is packed
        into a SINGLE ``Settings`` message, so a coalesced multi-field command
        (the app's "apply a preset" pattern) is one atomic write rather than
        several racing writes. Pure and synchronous with no websocket, so it is
        unit-testable offline.
        """
        message = Message()
        request = Request()
        settings = Settings()

        message_id = self._next_message_id()
        request.id = message_id
        if session_id is not None:
            request.sessionId = session_id

        # Set control parameters
        if "is_on" in kwargs:
            settings.isOn = kwargs["is_on"]
        if "brightness" in kwargs:
            settings.brightness = float(kwargs["brightness"])
        if "volume" in kwargs:
            settings.volume = float(kwargs["volume"])
        if "color" in kwargs:
            color_info = kwargs["color"]
            color_data = Color()
            # Only set the color sub-fields actually provided. A light-off
            # command sends a bare {noColor: true} and must NOT carry hue=0/
            # saturation=0, which would clobber the device's stored color (the
            # last-color restore relies on it surviving an off/on cycle).
            if "noColor" in color_info:
                color_data.noColor = color_info["noColor"]
            if "hue" in color_info:
                color_data.hue = float(color_info["hue"])
            if "saturation" in color_info:
                color_data.saturation = float(color_info["saturation"])
            # Note: brightness is sent separately in Settings.brightness, not in Color
            settings.color.CopyFrom(color_data)

            # Set brightness separately in Settings (matches official APK pattern)
            if "brightness" in color_info:
                settings.brightness = float(color_info["brightness"])
        if "sound" in kwargs:
            sound_option = kwargs["sound"]
            sound_data = Sound()
            if sound_option == "No sound":
                sound_data.noSound = True
                sound_data.track = ""  # Empty track when no sound
            else:
                sound_data.noSound = False
                sound_data.track = str(sound_option)
            settings.sound.CopyFrom(sound_data)

        # Set the settings in the request, and the request in the message
        request.settings.CopyFrom(settings)
        message.request.CopyFrom(request)

        return message.SerializeToString(), message_id

    async def send_control_command(self, baby_uid: str, **kwargs) -> None:
        """Send one control command and await the device's ack, like the app.

        Mirrors the official app's transaction model (SocketRequestManager): one
        Request in flight per device, await the Response whose ``requestId``
        matches (10s). One send, no retry — the app never re-sends, and re-sending
        on a slow ack piles duplicates onto a busy device and wedges it. A slow/
        absent ack on a LIVE socket is accepted optimistically (device busy, not
        gone — the pin holds the UI, the device pushes real state when it catches
        up). A socket drop or an explicit non-2xx rejection raises so the
        coordinator rolls back the optimistic UI.
        """
        # Ensure we have a healthy WebSocket connection. Raise (rather than
        # silently return) so the caller's failure surfaces instead of the
        # command appearing to succeed while nothing reached the device, and
        # kick a reconnect.
        if not await self.ensure_websocket_connection(baby_uid):
            self._schedule_reconnect(baby_uid)
            raise ConnectionError(
                f"No WebSocket connection to send control command for {baby_uid}"
            )

        # Check if protobuf classes are available
        if not PROTOBUF_AVAILABLE:
            _LOGGER.error(
                "Protobuf classes not available - cannot send control command"
            )
            return

        # Readiness gate: the relay can be up while the physical device is still
        # Disconnected behind it, in which case a command just stalls. Wait for
        # the backend Connected frame, but fall back to a best-effort send if it
        # never arrives — a missed/renamed backend frame must not brick control
        # (the ack-await below still surfaces a genuine failure).
        if not await self.wait_for_device_attached(baby_uid):
            _LOGGER.warning(
                "Device %s not confirmed attached (no backend Connected frame); "
                "sending command best-effort",
                baby_uid,
            )

        message_bytes, message_id = self.build_control_message(
            session_id=self._session_id(baby_uid), **kwargs
        )
        _LOGGER.debug(
            "Sending protobuf control for %s (id=%s): %s (hex: %s)",
            baby_uid,
            message_id,
            kwargs,
            message_bytes.hex(),
        )
        try:
            await self._transact(baby_uid, message_bytes, message_id)
            _LOGGER.debug("Control command id=%s on %s acked", message_id, baby_uid)
        except CommandTimeoutError:
            # Slow/absent ack but the socket is alive: the device is busy, not
            # gone. Do NOT re-send (duplicates overload it) and do NOT roll back —
            # accept optimistically; the device applies + pushes state when it
            # drains, and the 30s poll reconciles if it never landed.
            _LOGGER.warning(
                "No prompt ack for %s command id=%s (device busy); "
                "not re-sending, keeping optimistic state",
                baby_uid,
                message_id,
            )

    async def _transact(
        self, baby_uid: str, message_bytes: bytes, message_id: int
    ) -> int:
        """Send one CONTROL command under the per-device lock and await its ack.

        One command in flight per device (the app's model): the lock is held
        until the matching Response (by requestId) arrives or the ack times out.
        Raises ``CommandTimeoutError`` on a slow/absent ack (caller accepts it
        optimistically), and ``ConnectionError`` on a socket drop or non-2xx
        rejection (caller rolls back the optimistic UI). (Polls/diagnostics use
        ``_send_no_wait`` instead — they don't await, so a slow read can't stall a
        command.) Returns the 2xx status code.
        """
        lock = self._send_locks.setdefault(baby_uid, asyncio.Lock())
        async with lock:
            # Pick the transport to send on under the lock (prefer local).
            connection_key = self._active_connection_key(baby_uid)
            websocket = self._websockets.get(connection_key) if connection_key else None
            if websocket is None or self._is_websocket_closed(websocket):
                self._schedule_reconnect(baby_uid)
                raise ConnectionError(
                    f"WebSocket closed before sending request for {baby_uid}"
                )

            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending_responses.setdefault(baby_uid, {})[message_id] = future
            # Record which transport this in-flight command went out on so a
            # redundant socket dropping doesn't fail a command the OTHER socket
            # is about to ack (see _handle_messages teardown).
            self._inflight_conn_key[baby_uid] = connection_key
            try:
                await websocket.send(message_bytes)
                try:
                    status_code = await asyncio.wait_for(
                        future, timeout=COMMAND_ACK_TIMEOUT
                    )
                except asyncio.TimeoutError as e:
                    # Don't reconnect: the socket is alive, the device is just slow
                    # (a genuine drop fails the future via the handler instead).
                    # Reconnecting here was pointless churn during a device stall.
                    raise CommandTimeoutError(
                        f"No ack for command id={message_id} on {baby_uid} "
                        f"within {COMMAND_ACK_TIMEOUT}s"
                    ) from e

                if not (200 <= status_code < 300):
                    raise ConnectionError(
                        f"Device rejected command id={message_id} on {baby_uid}: "
                        f"status {status_code}"
                    )
                return status_code
            finally:
                self._pending_responses.get(baby_uid, {}).pop(message_id, None)
                self._inflight_conn_key.pop(baby_uid, None)

    async def _send_no_wait(self, baby_uid: str, message_bytes: bytes) -> None:
        """Send a best-effort request under the per-device lock, WITHOUT awaiting
        an ack.

        Used for polls/diagnostics. The device's Response is still drained and
        parsed by the message handler — we just don't hold the lock waiting for
        it. Awaiting poll acks (the previous behaviour) serialized them with
        control commands and, when the device was slow to ack a big GetSettings,
        held the lock for seconds and stalled/timed-out the user's toggles. One
        command still stays in flight (control commands DO await their ack); a
        read overlapping a write is fine because every Response is drained.
        """
        lock = self._send_locks.setdefault(baby_uid, asyncio.Lock())
        async with lock:
            connection_key = self._active_connection_key(baby_uid)
            websocket = self._websockets.get(connection_key) if connection_key else None
            if websocket is None or self._is_websocket_closed(websocket):
                self._schedule_reconnect(baby_uid)
                return
            await websocket.send(message_bytes)

    async def send_ping_for_state(self, baby_uid: str) -> None:
        """Send comprehensive status request to get device state and sensor data."""
        # Ensure we have a healthy WebSocket connection. Unlike a control
        # command this is a best-effort poll, so don't raise — just warn and let
        # the reconnect loop bring the socket back.
        if not await self.ensure_websocket_connection(baby_uid):
            _LOGGER.warning(
                "Cannot send ping request - no WebSocket connection for %s", baby_uid
            )
            self._schedule_reconnect(baby_uid)
            return

        # Wait (best-effort) for the device to attach before polling state — a
        # GetSettings into a still-Disconnected relay just stalls. Don't raise;
        # if it never attaches we let the reconnect/poll cycle retry.
        if not await self.wait_for_device_attached(baby_uid):
            _LOGGER.debug(
                "Skipping state ping for %s — device not attached yet", baby_uid
            )
            return

        try:
            if not PROTOBUF_AVAILABLE:
                _LOGGER.error("Protobuf not available for sending ping state request")
                return

            # Use proven working pattern: all=True + explicit sensor requests
            # This is the only pattern that successfully returns sensor data
            get_settings = GetSettings()
            get_settings.all = True
            get_settings.temperature = True
            get_settings.humidity = True

            # Create Request with GetSettings in field 5. A unique id (not a
            # hardcoded 1) keeps it from colliding with control-command ids in
            # the response-correlation map.
            request = Request()
            message_id = self._next_message_id()
            request.id = message_id
            request.sessionId = self._session_id(baby_uid)
            request.getSettings.CopyFrom(get_settings)

            # Create main Message wrapper
            message = Message()
            message.request.CopyFrom(request)
            message_bytes = message.SerializeToString()

            # Best-effort fire-and-forget: the response is drained + parsed by
            # the handler. Don't hold the lock awaiting its ack (that stalled
            # user commands behind slow GetSettings responses).
            await self._send_no_wait(baby_uid, message_bytes)

            _LOGGER.debug(
                "Sent GetSettings request (working pattern) for %s (hex: %s)",
                baby_uid,
                message_bytes.hex(),
            )

        except Exception as e:
            _LOGGER.error("Failed to send status request: %s", e)

    async def _send_query(self, baby_uid: str, mutate_request) -> None:
        """Build a diagnostics Request via ``mutate_request`` and send best-effort.

        Battery/wifi/firmware ride their own query request types (GetStatus /
        Network / Firmware), not the GetSettings poll. Fire-and-forget: the
        response is drained + parsed by the handler; a device that doesn't answer
        just leaves those sensors unknown. We don't await the ack so a poll can't
        stall user commands.
        """
        if not await self.ensure_websocket_connection(baby_uid):
            self._schedule_reconnect(baby_uid)
            return
        if not await self.wait_for_device_attached(baby_uid, timeout=2.0):
            return
        if not PROTOBUF_AVAILABLE:
            return

        request = Request()
        request.id = self._next_message_id()
        request.sessionId = self._session_id(baby_uid)
        mutate_request(request)
        message = Message()
        message.request.CopyFrom(request)
        try:
            await self._send_no_wait(baby_uid, message.SerializeToString())
        except Exception as e:
            _LOGGER.debug("Diagnostics query failed for %s: %s", baby_uid, e)

    async def send_status_request(self, baby_uid: str) -> None:
        """Poll battery (+ temp/humidity) via GetStatus(all=true)."""

        def _mutate(request) -> None:
            request.getStatus.all = True

        await self._send_query(baby_uid, _mutate)

    async def send_network_request(self, baby_uid: str) -> None:
        """Poll the current WiFi access point via Network{getStatus}."""

        def _mutate(request) -> None:
            request.network.getStatus.SetInParent()  # present-but-empty marker

        await self._send_query(baby_uid, _mutate)

    async def send_firmware_request(self, baby_uid: str) -> None:
        """Fetch the firmware version via Firmware{info}."""

        def _mutate(request) -> None:
            request.firmware.info.SetInParent()  # present-but-empty marker

        await self._send_query(baby_uid, _mutate)

    def _is_websocket_closed(self, websocket) -> bool:
        """Check if websocket is closed, handling different websocket library versions."""
        if websocket is None:
            return True

        try:
            # Try the standard method first
            if hasattr(websocket, "closed"):
                return websocket.closed

            # For newer websockets library versions, check state
            if hasattr(websocket, "state"):
                from websockets.protocol import State

                return websocket.state in (State.CLOSED, State.CLOSING)

            # Fallback: assume connection is open if we can't determine
            return False
        except Exception:
            # If we can't determine the state, assume it's closed for safety
            return True

    def is_websocket_connected(self, baby_uid: str) -> bool:
        """True if the device is reachable on ANY transport (local or remote)."""
        return self._any_transport_connected(baby_uid)

    async def ensure_websocket_connection(self, baby_uid: str) -> bool:
        """Ensure WebSocket connection is available and healthy."""
        if self.is_websocket_connected(baby_uid):
            return True

        _LOGGER.info(
            "WebSocket connection needed for %s, attempting to connect...", baby_uid
        )

        # Find the device info for connection
        device_info = None
        for device in self._device_list:
            if device.get("baby_uid") == baby_uid:
                device_info = device
                break

        if not device_info:
            _LOGGER.error("No device info found for WebSocket connection: %s", baby_uid)
            return False

        try:
            await self.connect_device(device_info)
            return self.is_websocket_connected(baby_uid)
        except Exception as e:
            _LOGGER.error(
                "Failed to establish WebSocket connection for %s: %s", baby_uid, e
            )
            return False

    async def _handle_messages(
        self, connection_key: str, websocket: websockets.WebSocketServerProtocol
    ) -> None:
        """Handle incoming WebSocket messages."""
        try:
            async for raw_message in websocket:
                try:
                    if isinstance(raw_message, bytes):
                        _LOGGER.debug(
                            "Received %d bytes on %s", len(raw_message), connection_key
                        )
                        await self._process_protobuf_message(
                            connection_key, raw_message
                        )
                    elif isinstance(raw_message, str):
                        _LOGGER.debug("Received text message: %s", raw_message)

                except Exception as e:
                    _LOGGER.error(
                        "Error processing message on %s: %s", connection_key, e
                    )

        except ConnectionClosedError:
            _LOGGER.warning(
                "WebSocket connection closed for %s, reconnecting", connection_key
            )
        except Exception as e:
            _LOGGER.error("Error in message handler for %s: %s", connection_key, e)
        finally:
            # Only clean up if the stored socket is still *this* one — a proactive
            # reconnect may have already replaced it under the same key.
            if self._websockets.get(connection_key) is websocket:
                del self._websockets[connection_key]
                _LOGGER.debug("Cleaned up WebSocket reference for %s", connection_key)
                baby_uid, transport = self._split_conn_key(connection_key)
                err = ConnectionError("WebSocket closed before ack")
                if not self._any_transport_connected(baby_uid):
                    # Last transport gone: device is no longer reachable, so it's
                    # detached and any in-flight ack will never come — fail it now
                    # so the caller rolls back instead of waiting out the timeout.
                    self._mark_detached(baby_uid)
                    self._fail_pending_responses(baby_uid, err)
                elif self._inflight_conn_key.get(baby_uid) == connection_key:
                    # A redundant socket dropped while the OTHER is still up, and
                    # the in-flight command went out on THIS one — fail it so it
                    # re-sends over the surviving transport. A command in flight on
                    # the surviving socket is left alone (it'll still get acked).
                    self._fail_pending_responses(baby_uid, err)
                # Proactively reconnect just this transport.
                if not self._closing:
                    self._schedule_reconnect(baby_uid, transport)

    @staticmethod
    def _parse_battery(device_state: dict[str, Any], battery) -> None:
        """Parse a Status.Battery into device_state (percent + charging)."""
        if battery.HasField("soc"):
            device_state["battery_percent"] = _SOC_TO_PERCENT.get(battery.soc)
            _LOGGER.debug("Battery soc bucket=%s", battery.soc)
        # The device OMITS isCharging when it isn't charging (proto2 drops the
        # default-false field), so an absent field inside a battery status means
        # "not charging" — not unknown, and not still-charging from a prior frame.
        # Set it on every battery status so unplugging flips it back to off.
        charging = battery.isCharging if battery.HasField("isCharging") else False
        device_state["battery_charging"] = charging
        _LOGGER.debug("Battery charging=%s", charging)

    @staticmethod
    def _parse_network(device_state: dict[str, Any], network_status) -> None:
        """Parse a NetworkStatus.currentAp into device_state (wifi diagnostics)."""
        if not network_status.HasField("currentAp"):
            return
        ap = network_status.currentAp
        if ap.HasField("rssi"):
            device_state["wifi_rssi"] = ap.rssi
        if ap.HasField("ssid"):
            device_state["wifi_ssid"] = ap.ssid
        if ap.HasField("bssid"):
            device_state["wifi_bssid"] = ap.bssid
        if ap.HasField("primaryChannel"):
            device_state["wifi_channel"] = ap.primaryChannel

    async def _process_protobuf_message(
        self, connection_key: str, raw_message: bytes
    ) -> None:
        """Process incoming message using pure protobuf parsing."""
        baby_uid, _transport = self._split_conn_key(connection_key)
        device_state = self._device_state.setdefault(baby_uid, {})

        try:
            # Try parsing as new Message structure first (only for response messages, not deviceData)
            try:
                if not PROTOBUF_AVAILABLE:
                    _LOGGER.error("Protobuf not available for processing message")
                    return

                message_response = Message()
                message_response.ParseFromString(raw_message)

                _LOGGER.debug("Successfully parsed as Message for %s", baby_uid)
                _LOGGER.debug(
                    "Message fields: %s",
                    [field.name for field, _ in message_response.ListFields()],
                )

                # Handle response messages (responses to our requests)
                if message_response.HasField("response"):
                    response = message_response.response
                    response_fields = [field.name for field, _ in response.ListFields()]
                    _LOGGER.debug("Response fields: %s", response_fields)

                    # A Response means the relay round-tripped to the physical
                    # device, so it's attached (sticky — see the backend branch).
                    self._mark_attached(baby_uid)
                    self._resolve_pending_response(baby_uid, response)

                    # Handle status response for sensors - use APK field names
                    if response.HasField("status"):
                        status = response.status
                        _LOGGER.debug("Found Status field in response")
                        _LOGGER.debug(
                            "Status fields: %s",
                            [field.name for field, _ in status.ListFields()],
                        )

                        # Alternative sensor parsing from status (might be different from settings)
                        if status.HasField("temperature"):
                            device_state["temperature"] = status.temperature
                            _LOGGER.debug("Temperature: %.1f°C", status.temperature)
                        if status.HasField("humidity"):
                            device_state["humidity"] = status.humidity
                            _LOGGER.debug("Humidity: %.1f%%", status.humidity)
                        # Battery (from GetStatus) — coarse 5-bucket SoC + charging.
                        if status.HasField("battery"):
                            self._parse_battery(device_state, status.battery)

                    # WiFi readback (from Network{getStatus}).
                    if response.HasField("networkStatus"):
                        self._parse_network(device_state, response.networkStatus)

                    # Firmware version readback (from Firmware{info}).
                    if response.HasField("firmware") and response.firmware.HasField(
                        "version"
                    ):
                        device_state["firmware_version"] = response.firmware.version
                        _LOGGER.debug(
                            "Firmware version for %s: %s",
                            baby_uid,
                            response.firmware.version,
                        )

                    # Handle settings response (device state) - use APK field names
                    if response.HasField("settings"):
                        settings = response.settings
                        if settings.HasField("brightness"):
                            device_state["brightness"] = settings.brightness
                            _LOGGER.debug(
                                "Parsed brightness from settings: %.3f",
                                settings.brightness,
                            )
                        if settings.HasField("volume"):
                            device_state["volume"] = settings.volume
                            _LOGGER.debug(
                                "Parsed volume from settings: %.3f", settings.volume
                            )
                        if settings.HasField("isOn"):
                            device_state["is_on"] = settings.isOn
                            _LOGGER.debug(
                                "Parsed power state from settings: %s", settings.isOn
                            )
                        if settings.HasField("sound"):
                            sound = settings.sound
                            if sound.HasField("noSound") and sound.noSound:
                                device_state["current_sound"] = "No sound"
                            elif sound.HasField("track"):
                                device_state["current_sound"] = sound.track
                        if settings.HasField("color"):
                            color = settings.color

                            # Handle noColor field
                            if color.HasField("noColor"):
                                device_state["no_color"] = color.noColor
                            else:
                                # If device sends hue/saturation without noColor field, assume color is enabled
                                if color.HasField("hue") or color.HasField(
                                    "saturation"
                                ):
                                    device_state["no_color"] = False

                            if color.HasField("hue"):
                                device_state["hue"] = color.hue
                            if color.HasField("saturation"):
                                device_state["saturation"] = color.saturation
                        else:
                            # Don't override existing color state - device doesn't return color info
                            pass

                        # Parse available sounds list from device. Track
                        # names come from the cloud/device as untrusted
                        # strings — clamp length and require printable chars
                        # before exposing as HA select-entity options.
                        if settings.HasField("soundList"):
                            sound_list = settings.soundList
                            if sound_list.tracks:
                                clean_tracks = [
                                    t[:64]
                                    for t in sound_list.tracks
                                    if t and t.isprintable() and t.strip()
                                ]
                                available_sounds = ["No sound"] + clean_tracks
                                device_state["available_sounds"] = available_sounds
                                _LOGGER.info(
                                    "Received dynamic sound list for %s: %s",
                                    baby_uid,
                                    available_sounds,
                                )

                        # Parse temperature and humidity sensors with test result logging
                        temp_received = settings.HasField("temperature")
                        humidity_received = settings.HasField("humidity")

                        if temp_received:
                            device_state["temperature"] = settings.temperature
                            _LOGGER.debug("Temperature: %.1f°C", settings.temperature)

                        if humidity_received:
                            device_state["humidity"] = settings.humidity
                            _LOGGER.debug("Humidity: %.1f%%", settings.humidity)

                        # Log test results to determine if explicit requests are needed
                        _LOGGER.debug(
                            "Sensor data received: temp=%s, humidity=%s",
                            "yes" if temp_received else "no",
                            "yes" if humidity_received else "no",
                        )

                    return  # Successfully parsed as Message response

                # Handle request messages (external changes from device/app)
                elif message_response.HasField("request"):
                    request = message_response.request
                    _LOGGER.debug(
                        "Processing Message request (external change) for %s", baby_uid
                    )
                    _LOGGER.debug(
                        "Request fields: %s",
                        [field.name for field, _ in request.ListFields()],
                    )

                    # Check for Status field for sensor data
                    if request.HasField("status"):
                        status = request.status
                        _LOGGER.debug("Found Status field in external request")
                        _LOGGER.debug(
                            "Status fields: %s",
                            [field.name for field, _ in status.ListFields()],
                        )

                        if status.HasField("temperature"):
                            device_state["temperature"] = status.temperature
                            _LOGGER.debug(
                                "External temperature: %.1f°C", status.temperature
                            )
                        if status.HasField("humidity"):
                            device_state["humidity"] = status.humidity
                            _LOGGER.debug("External humidity: %.1f%%", status.humidity)

                    # Parse external changes from request.settings field
                    if request.HasField("settings"):
                        settings = request.settings
                        _LOGGER.debug("Found settings in external request message")

                        # Parse external state changes including battery data
                        if settings.HasField("brightness"):
                            device_state["brightness"] = settings.brightness
                            _LOGGER.debug(
                                "External change - brightness: %.3f",
                                settings.brightness,
                            )

                        if settings.HasField("volume"):
                            device_state["volume"] = settings.volume
                            _LOGGER.debug(
                                "External change - volume: %.3f", settings.volume
                            )
                        if settings.HasField("isOn"):
                            device_state["is_on"] = settings.isOn
                            _LOGGER.debug("External change - power: %s", settings.isOn)
                        if settings.HasField("sound"):
                            sound = settings.sound
                            if sound.HasField("noSound") and sound.noSound:
                                device_state["current_sound"] = "No sound"
                                _LOGGER.debug("External change - sound: No sound")
                            elif sound.HasField("track"):
                                device_state["current_sound"] = sound.track
                                _LOGGER.debug(
                                    "External change - sound: %s", sound.track
                                )
                        if settings.HasField("color"):
                            color = settings.color

                            # Handle noColor field
                            if color.HasField("noColor"):
                                device_state["no_color"] = color.noColor
                            else:
                                # If device sends hue/saturation without noColor field, assume color is enabled
                                if color.HasField("hue") or color.HasField(
                                    "saturation"
                                ):
                                    device_state["no_color"] = False

                            if color.HasField("hue"):
                                device_state["hue"] = color.hue
                            if color.HasField("saturation"):
                                device_state["saturation"] = color.saturation
                        else:
                            # Don't override existing color state - device doesn't return color info
                            pass

                        # Trigger callback for external changes
                        if self._state_change_callback:
                            _LOGGER.debug("Triggering callback for external change")
                            try:
                                await self._state_change_callback(baby_uid)
                            except Exception as callback_error:
                                _LOGGER.debug(
                                    "External change callback failed: %s",
                                    callback_error,
                                )

                    return  # Successfully parsed as Message request

                # Backend readiness frame. The relay reports whether the physical
                # device is attached behind it; gate availability + sends on it.
                elif message_response.HasField("backend"):
                    backend = message_response.backend
                    status = None
                    if backend.HasField("device") and backend.device.HasField("status"):
                        status = backend.device.status
                    if status == _BACKEND_STATUS_CONNECTED:
                        _LOGGER.debug(
                            "Backend: device %s attached (Connected)", baby_uid
                        )
                        self._mark_attached(baby_uid)
                    else:
                        # The real device sends bare/Disconnected backend frames
                        # PERIODICALLY while fully usable (it keeps acking commands
                        # and pushing state). Treating those as a hard detach made
                        # the entity flap to unavailable and blocked sends. So a
                        # non-Connected backend frame is NOT a detach: attachment
                        # is sticky once established (by a Connected frame or any
                        # real traffic) and only cleared on a socket drop.
                        _LOGGER.debug(
                            "Backend: device %s sent non-Connected status=%s "
                            "(ignored; attachment stays sticky)",
                            baby_uid,
                            status,
                        )
                    return

                # If message parsed as Message but has unknown structure, fall through to legacy parsing

            except Exception as e:
                _LOGGER.warning("Failed to parse message for %s: %s", baby_uid, e)
                return

        except Exception as e:
            _LOGGER.warning("Protobuf parsing failed for %s: %s", baby_uid, e)
            _LOGGER.debug("Message hex: %s", raw_message.hex())

    def get_device_state(self, baby_uid: str) -> dict[str, Any]:
        """Get current state for a device."""
        return self._device_state.get(baby_uid, {})

    def set_state_change_callback(self, callback):
        """Set callback function to be called when device state changes via WebSocket."""
        self._state_change_callback = callback

    def set_token_update_callback(self, callback):
        """Set callback function to be called when tokens are updated."""
        self._token_update_callback = callback

    def set_local_host_resolver(self, resolver) -> None:
        """Inject an async resolver: speaker_uid -> LAN IPv4 (or None).

        The coordinator wires this to Home Assistant's zeroconf so the LAN path
        works even when the container's libc resolver can't do mDNS. ``None``
        falls back to the OS resolver. Signature: async (speaker_uid) -> str|None.
        """
        self._local_host_resolver = resolver

    def set_mfa_required_callback(self, callback):
        """Set callback function to be called when MFA is required during re-auth."""
        self._mfa_required_callback = callback

    def is_mfa_pending(self) -> bool:
        """Check if MFA authentication is pending."""
        return self._pending_mfa_token is not None

    def needs_reauth(self) -> bool:
        """True when recovery requires the user to re-authenticate.

        Either MFA is pending, or we hold no usable token (the refresh token
        was rejected or never issued) and have no stored password to silently
        re-authenticate with. A transient network error during refresh leaves
        the refresh token in place, so this stays False and the caller can keep
        using cached data instead of forcing a reauth.
        """
        if self._pending_mfa_token is not None:
            return True
        return (
            self._access_token is None
            and self._refresh_token is None
            and not self.has_stored_credentials()
        )

    async def complete_pending_mfa(self, mfa_code: str) -> bool:
        """Complete pending MFA authentication."""
        if not self._pending_mfa_token:
            _LOGGER.error("No pending MFA authentication")
            return False

        if not self.has_stored_credentials():
            _LOGGER.error("No stored credentials for MFA completion")
            return False

        try:
            await self.complete_mfa_authentication(
                self._stored_email,
                self._stored_password,
                self._pending_mfa_token,
                mfa_code,
            )
            # Clear pending MFA state on success
            self._pending_mfa_token = None
            # Reset auth failure tracking on successful MFA
            self._last_auth_failure = None
            self._auth_retry_count = 0
            return True
        except AuthenticationError as e:
            _LOGGER.error("Pending MFA completion failed: %s", e)
            # Don't clear pending state on failure - allow retry
            return False

    def clear_auth_data(self) -> None:
        """Clear sensitive authentication data."""
        self._access_token = None
        self._refresh_token = None
        self._password = None
        self._pending_mfa_token = None
        self._token_expires_at = None  # Clear token expiration tracking
        # Keep stored email/password for re-auth, but clear temp password and MFA state
        # Only clear if explicitly called (not during normal refresh)

    async def send_saved_sounds_request(self, baby_uid: str) -> None:
        """Request available sound list from device."""
        if not self.is_websocket_connected(baby_uid):
            return

        # Best-effort with a SHORT attach wait: this is non-critical (the
        # all=True state ping also returns the sound list), so don't let it
        # stack a full DEVICE_ATTACH_TIMEOUT on top of the ping's wait and risk
        # blowing the coordinator's first-refresh timeout on multi-device setups.
        if not await self.wait_for_device_attached(baby_uid, timeout=2.0):
            _LOGGER.debug(
                "Skipping saved-sounds request for %s — device not attached yet",
                baby_uid,
            )
            return

        try:
            if not PROTOBUF_AVAILABLE:
                _LOGGER.error("Protobuf not available for sending saved sounds request")
                return

            # Request saved sounds list (field 7 in GetSettings)
            get_settings = GetSettings()
            get_settings.savedSounds = True  # Request available sounds

            # Unique id (not a hardcoded 3, which collided with control-command
            # ids) so the response map stays unambiguous.
            request = Request()
            message_id = self._next_message_id()
            request.id = message_id
            request.sessionId = self._session_id(baby_uid)
            request.getSettings.CopyFrom(get_settings)

            message = Message()
            message.request.CopyFrom(request)
            message_bytes = message.SerializeToString()

            # Best-effort fire-and-forget (response drained by the handler).
            await self._send_no_wait(baby_uid, message_bytes)

            _LOGGER.debug(
                "Sent saved sounds request for %s (hex: %s)",
                baby_uid,
                message_bytes.hex(),
            )

        except Exception as e:
            _LOGGER.error("Failed to send sounds request: %s", e)

    async def close(self) -> None:
        """Close all connections and clean up resources."""
        # Stop reconnecting before tearing sockets down, else the handler's
        # teardown would immediately schedule a fresh reconnect loop.
        self._closing = True
        for task in self._reconnect_tasks.values():
            task.cancel()
        self._reconnect_tasks.clear()
        for task in self._handler_tasks.values():
            task.cancel()
        self._handler_tasks.clear()

        # Fail any in-flight command waiters and drop readiness/session state so
        # a send racing the shutdown returns instead of hanging on its ack.
        for baby_uid in list(self._pending_responses):
            self._fail_pending_responses(baby_uid, ConnectionError("API shutting down"))
        self._inflight_conn_key.clear()
        self._device_attached.clear()
        self._attached_events.clear()
        self._session_ids.clear()

        # Close all websockets
        websocket_close_tasks = []
        for connection_key, websocket in list(self._websockets.items()):
            try:
                if not self._is_websocket_closed(websocket):
                    websocket_close_tasks.append(websocket.close())
            except Exception as e:
                _LOGGER.debug(
                    "Error preparing websocket close for %s: %s", connection_key, e
                )

        # Wait for all websockets to close with timeout
        if websocket_close_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*websocket_close_tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Websocket close timeout - some connections may not have closed gracefully"
                )
            except Exception as e:
                _LOGGER.debug("Error during websocket cleanup: %s", e)

        # Clear websocket references
        self._websockets.clear()

        # Clear device state
        self._device_state.clear()
        # Drop cached local device tokens (re-fetched on next connect).
        self._device_tokens.clear()

        # Clear auth data for security
        self.clear_auth_data()
        self._stored_email = None
        self._stored_password = None
