"""Pure protobuf API for Nanit Sound + Light devices."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
    SOUND_LIGHT_WS_BASE_URL,
)

_LOGGER = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Authentication failed."""


class MfaRequiredError(Exception):
    """MFA code required for authentication."""

    def __init__(self, message: str, mfa_token: str):
        super().__init__(message)
        self.mfa_token = mfa_token


class SoundLightAPI:
    """Pure protobuf API client for Nanit Sound + Light devices."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self._session = session
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._password: str | None = None
        self._websockets: dict[str, websockets.WebSocketServerProtocol] = {}
        self._device_state: dict[str, dict[str, Any]] = {}
        self._message_id = 1
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

                # Only log response details at debug level to avoid exposing tokens
                sanitized_response = (
                    response_text[:200] + "..."
                    if len(response_text) > 200
                    else response_text
                )
                _LOGGER.debug("Login response preview: %s", sanitized_response)

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
                    _LOGGER.debug("MFA required response data: %s", response_data)

                    mfa_token = response_data.get("mfa_token")
                    if mfa_token:
                        _LOGGER.info(
                            "MFA verification required for user: %s",
                            email.split("@")[0] + "@***",
                        )
                        raise MfaRequiredError("MFA code required", mfa_token)

                raise AuthenticationError(
                    f"Login failed: {response.status} - {response_text[:100] + ('...' if len(response_text) > 100 else '')}"
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
                    "🔒 Authentication retry limit reached (%d attempts). "
                    "Waiting %.1f more minutes to prevent MFA spam and protect your account",
                    self._auth_retry_count,
                    remaining_minutes,
                )
                return False
            else:
                # Reset retry count after waiting period
                _LOGGER.info(
                    "🔓 Authentication retry wait period expired - resuming normal authentication"
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
            "⚠️  Authentication attempt %d/%d failed. Next retry allowed in %s",
            self._auth_retry_count,
            self._max_retry_count,
            next_retry_info,
        )

    async def ensure_authenticated(self) -> bool:
        """Ensure we have a valid access token, refreshing if needed."""
        # If we have a valid token that doesn't need refresh, return immediately
        if self._access_token and not self._is_token_expired():
            return True

        # Token is expired or expiring soon, try to refresh
        if self._access_token and self._refresh_token and self._is_token_expired():
            _LOGGER.debug("Token expires soon, attempting refresh...")
            if await self._refresh_auth():
                return True
            # If refresh failed, clear the invalid token
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

    async def get_sound_light_devices(self) -> list[dict[str, Any]]:
        """Get list of Sound + Light devices."""
        if not await self.ensure_authenticated():
            raise AuthenticationError("Authentication failed or not authenticated")

        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with self._session.get(NANIT_BABIES_URL, headers=headers) as response:
            if response.status == 401:
                # Token expired, try to refresh and retry once
                if await self._refresh_auth():
                    headers = {"Authorization": f"Bearer {self._access_token}"}
                    async with self._session.get(
                        NANIT_BABIES_URL, headers=headers
                    ) as retry_response:
                        response = retry_response
                else:
                    raise AuthenticationError("Token expired and refresh failed")

            if response.status == 200:
                babies_data = await response.json()
                sound_light_devices = []

                for baby in babies_data.get("babies", []):
                    # Check if baby has Sound + Light device
                    speaker_data = baby.get("speaker", {})
                    if speaker_data.get("attached_to_speaker") and speaker_data.get(
                        "speaker"
                    ):
                        device_info = {
                            "baby_uid": baby.get("uid"),
                            "baby_name": baby.get("name", "Nanit"),
                            "speaker_uid": speaker_data["speaker"]["uid"],
                            "speaker_name": speaker_data["speaker"]["name"],
                        }
                        sound_light_devices.append(device_info)
                        _LOGGER.info(
                            "Found Sound + Light device: %s (%s)",
                            device_info["speaker_name"],
                            device_info["speaker_uid"],
                        )

                # Store device list for potential reconnections
                self._device_list = sound_light_devices
                return sound_light_devices
            else:
                raise Exception(f"Failed to get devices: {response.status}")

    async def connect_device(self, device_info: dict[str, Any]) -> None:
        """Connect to a Sound + Light device WebSocket."""
        speaker_uid = device_info["speaker_uid"]
        baby_uid = device_info["baby_uid"]

        ws_url = f"{SOUND_LIGHT_WS_BASE_URL}/{speaker_uid}/user_connect/"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            # Create SSL context in executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            ssl_context = await loop.run_in_executor(None, ssl.create_default_context)

            websocket = await websockets.connect(
                ws_url, additional_headers=headers, ssl=ssl_context
            )

            connection_key = f"{baby_uid}_speaker"
            self._websockets[connection_key] = websocket

            # Start message handler
            asyncio.create_task(self._handle_messages(connection_key, websocket))

            # Send immediate ping to get current device state
            await self.send_ping_for_state(baby_uid)

            _LOGGER.info("Connected to Sound + Light device: %s", speaker_uid)

        except Exception as e:
            _LOGGER.error(
                "Failed to connect to Sound + Light device %s: %s", speaker_uid, e
            )

    async def send_control_command(self, baby_uid: str, **kwargs) -> None:
        """Send control command using pure protobuf."""
        # Ensure we have a healthy WebSocket connection
        if not await self.ensure_websocket_connection(baby_uid):
            _LOGGER.error(
                "Cannot send control command - no WebSocket connection for %s", baby_uid
            )
            return

        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)

        try:
            from .sound_light_pb2 import (
                Color,
                Message,
                Request,
                Settings,
                Sound,
            )

            # Create protobuf control request using official APK pattern
            message = Message()
            request = Request()
            settings = Settings()

            # Set request ID
            request.id = 1

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
                color_data.noColor = color_info.get("noColor", False)
                color_data.hue = float(color_info.get("hue", 0.0))
                color_data.saturation = float(color_info.get("saturation", 0.0))
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

            # Set the settings in the request
            request.settings.CopyFrom(settings)

            # Set the request in the message
            message.request.CopyFrom(request)

            # Serialize and send
            message_bytes = message.SerializeToString()
            await websocket.send(message_bytes)

            _LOGGER.debug(
                "Sent protobuf control for %s: %s (hex: %s)",
                baby_uid,
                kwargs,
                message_bytes.hex(),
            )

        except Exception as e:
            _LOGGER.error("Failed to send control command: %s", e)

    async def send_ping_for_state(self, baby_uid: str) -> None:
        """Send comprehensive status request to get device state and sensor data."""
        # Ensure we have a healthy WebSocket connection
        if not await self.ensure_websocket_connection(baby_uid):
            _LOGGER.warning(
                "Cannot send ping request - no WebSocket connection for %s", baby_uid
            )
            return

        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)

        try:
            from .sound_light_pb2 import (
                GetSettings,
                Message,
                Request,
            )

            # Use proven working pattern: all=True + explicit sensor requests
            # This is the only pattern that successfully returns sensor data
            get_settings = GetSettings()
            get_settings.all = True
            get_settings.temperature = True
            get_settings.humidity = True

            # Create Request with GetSettings in field 5
            request = Request()
            request.id = 1
            request.sessionId = (
                "generated_session_id"  # Will be replaced by actual session
            )
            request.getSettings.CopyFrom(get_settings)

            # Create main Message wrapper
            message = Message()
            message.request.CopyFrom(request)

            # Serialize and send
            message_bytes = message.SerializeToString()
            await websocket.send(message_bytes)

            _LOGGER.debug(
                "Sent GetSettings request (working pattern) for %s (hex: %s)",
                baby_uid,
                message_bytes.hex(),
            )

        except Exception as e:
            _LOGGER.error("Failed to send status request: %s", e)

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
        """Check if WebSocket connection is healthy."""
        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)
        return websocket is not None and not self._is_websocket_closed(websocket)

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
                "WebSocket connection closed for %s, will reconnect on next use",
                connection_key,
            )
        except Exception as e:
            _LOGGER.error("Error in message handler for %s: %s", connection_key, e)
        finally:
            # Clean up websocket reference but don't log as error - this is expected during reconnection
            if connection_key in self._websockets:
                del self._websockets[connection_key]
                _LOGGER.debug("Cleaned up WebSocket reference for %s", connection_key)

    async def _process_protobuf_message(
        self, connection_key: str, raw_message: bytes
    ) -> None:
        """Process incoming message using pure protobuf parsing."""
        baby_uid = connection_key.split("_")[0]
        device_state = self._device_state.setdefault(baby_uid, {})

        try:
            # Try parsing as new Message structure first (only for response messages, not deviceData)
            try:
                from .sound_light_pb2 import Message

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
                    _LOGGER.debug("📋 Response fields: %s", response_fields)

                    # Handle status response for sensors - use APK field names
                    if response.HasField("status"):
                        status = response.status
                        _LOGGER.debug("Found Status field in response")
                        _LOGGER.debug(
                            "📋 Status fields: %s",
                            [field.name for field, _ in status.ListFields()],
                        )

                        # Alternative sensor parsing from status (might be different from settings)
                        if status.HasField("temperature"):
                            device_state["temperature"] = status.temperature
                            _LOGGER.debug("Temperature: %.1f°C", status.temperature)
                        if status.HasField("humidity"):
                            device_state["humidity"] = status.humidity
                            _LOGGER.debug("Humidity: %.1f%%", status.humidity)

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

                        # Parse available sounds list from device
                        if settings.HasField("soundList"):
                            sound_list = settings.soundList
                            if sound_list.tracks:
                                available_sounds = ["No sound"] + list(
                                    sound_list.tracks
                                )
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
                        "📋 Request fields: %s",
                        [field.name for field, _ in request.ListFields()],
                    )

                    # Check for Status field for sensor data
                    if request.HasField("status"):
                        status = request.status
                        _LOGGER.debug("Found Status field in external request")
                        _LOGGER.debug(
                            "📋 Status fields: %s",
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

                # Handle other message types (backend, etc.)
                elif message_response.HasField("backend"):
                    _LOGGER.debug("Received backend message for %s", baby_uid)
                    return  # Backend messages don't need processing

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

    def set_mfa_required_callback(self, callback):
        """Set callback function to be called when MFA is required during re-auth."""
        self._mfa_required_callback = callback

    def is_mfa_pending(self) -> bool:
        """Check if MFA authentication is pending."""
        return self._pending_mfa_token is not None

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
        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)
        if not websocket:
            return

        try:
            from .sound_light_pb2 import (
                GetSettings,
                Message,
                Request,
            )
        except ImportError as e:
            if "incompatible Protobuf" in str(e):
                _LOGGER.error(
                    "Protobuf version mismatch detected. This integration was compiled with "
                    "a newer protobuf version than your Home Assistant runtime. "
                    "Please report this issue at: https://github.com/com6056/nanit-sound-light/issues"
                )
            raise e

            # Request saved sounds list (field 7 in GetSettings)
            get_settings = GetSettings()
            get_settings.savedSounds = True  # Request available sounds

            request = Request()
            request.id = 3  # Different ID for sound list request
            request.sessionId = "sounds_session_id"
            request.getSettings.CopyFrom(get_settings)

            message = Message()
            message.request.CopyFrom(request)

            # Serialize and send
            message_bytes = message.SerializeToString()
            await websocket.send(message_bytes)

            _LOGGER.debug(
                "Sent saved sounds request for %s (hex: %s)",
                baby_uid,
                message_bytes.hex(),
            )

        except Exception as e:
            _LOGGER.error("Failed to send sounds request: %s", e)

    async def close(self) -> None:
        """Close all connections and clean up resources."""
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

        # Clear auth data for security
        self.clear_auth_data()
        self._stored_email = None
        self._stored_password = None
