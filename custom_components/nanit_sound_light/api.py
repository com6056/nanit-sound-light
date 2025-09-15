"""Pure protobuf API for Nanit Sound + Light devices."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any, Dict, List, Optional

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
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._password: Optional[str] = None
        self._websockets: Dict[str, websockets.WebSocketServerProtocol] = {}
        self._device_state: Dict[str, Dict[str, Any]] = {}
        self._message_id = 1
        self._state_change_callback = None  # Callback for real-time updates

    async def authenticate(
        self, email: str, password: str, refresh_token: Optional[str] = None
    ) -> None:
        """Authenticate with Nanit API (try refresh token first like working implementation)."""
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
            _LOGGER.debug("Auth data: %s", {**auth_data, "password": "***"})

            async with self._session.post(
                NANIT_AUTH_URL, json=auth_data, headers=headers
            ) as response:
                response_text = await response.text()
                _LOGGER.debug(f"Login response: {response.status} - {response_text}")

                if response.status == 201:
                    # Successful login without MFA
                    response_data = await response.json()
                    self._access_token = response_data.get("access_token")
                    self._refresh_token = response_data.get("refresh_token")

                    if self._access_token:
                        _LOGGER.info("Authentication successful")
                        return {"success": True}

                elif response.status in [200, 482]:
                    # MFA required - 482 is the actual MFA status code
                    response_data = await response.json()
                    _LOGGER.debug("MFA required response data: %s", response_data)

                    mfa_token = response_data.get("mfa_token")
                    if mfa_token:
                        _LOGGER.info("MFA verification required")
                        raise MfaRequiredError("MFA code required", mfa_token)

                raise AuthenticationError(
                    f"Login failed: {response.status} - {response_text}"
                )

        except MfaRequiredError:
            # Re-raise MFA errors as-is
            raise
        except Exception as e:
            _LOGGER.error("Authentication failed: %s", e)
            raise AuthenticationError(f"Login failed: {e}")

    async def complete_mfa_authentication(
        self, email: str, password: str, mfa_token: str, mfa_code: str
    ) -> None:
        """Complete MFA authentication with the provided code (exact match to working implementation)."""
        try:
            # Clean up the MFA code exactly like working implementation
            mfa_code = mfa_code.strip()
            if mfa_code.startswith('"') and mfa_code.endswith('"'):
                mfa_code = mfa_code[1:-1]

            # Send MFA code to verify - exact same as working implementation
            mfa_data = {
                "email": email,
                "password": password,
                "mfa_token": mfa_token,
                "mfa_code": mfa_code,
                "channel": "email",
            }
            headers = {"Content-Type": "application/json", "nanit-api-version": "1"}

            _LOGGER.debug(
                "MFA verification request data: %s", {**mfa_data, "password": "***"}
            )

            async with self._session.post(
                NANIT_AUTH_URL, json=mfa_data, headers=headers
            ) as response:
                response_text = await response.text()
                _LOGGER.debug(f"MFA response: {response.status} - {response_text}")

                if response.status == 201:
                    response_data = await response.json()
                    self._access_token = response_data.get("access_token")
                    self._refresh_token = response_data.get("refresh_token")

                    if not self._access_token:
                        raise AuthenticationError(
                            "No access token received after MFA verification"
                        )

                    _LOGGER.info("MFA verification successful")
                else:
                    raise AuthenticationError(
                        f"MFA verification failed: {response.status} - {response_text}"
                    )

        except Exception as e:
            _LOGGER.error("MFA verification failed: %s", e)
            raise AuthenticationError(f"MFA verification failed: {e}")

    async def _refresh_auth(self) -> bool:
        """Refresh authentication token like working implementation."""
        if not self._refresh_token:
            return False

        refresh_data = {"refresh_token": self._refresh_token}

        try:
            refresh_url = f"{NANIT_API_BASE}/tokens/refresh"
            async with self._session.post(refresh_url, json=refresh_data) as response:
                if response.status == 200:
                    data = await response.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get(
                        "refresh_token"
                    )  # Update refresh token too
                    _LOGGER.info("Token refresh successful")
                    return True
                elif response.status == 404:
                    _LOGGER.debug("Refresh token expired, need to re-login")
        except Exception as e:
            _LOGGER.debug("Token refresh failed: %s", e)

        return False

    async def get_sound_light_devices(self) -> List[Dict[str, Any]]:
        """Get list of Sound + Light devices."""
        if not self._access_token:
            raise AuthenticationError("Not authenticated")

        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with self._session.get(NANIT_BABIES_URL, headers=headers) as response:
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

                return sound_light_devices
            else:
                raise Exception(f"Failed to get devices: {response.status}")

    async def connect_device(self, device_info: Dict[str, Any]) -> None:
        """Connect to a Sound + Light device WebSocket."""
        speaker_uid = device_info["speaker_uid"]
        baby_uid = device_info["baby_uid"]

        ws_url = f"{SOUND_LIGHT_WS_BASE_URL}/{speaker_uid}/user_connect/"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            # Create SSL context in executor to avoid blocking the event loop
            import asyncio

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
        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)
        if not websocket:
            _LOGGER.error("No WebSocket connection for %s", baby_uid)
            return

        try:
            from .sound_light_pb2 import (
                Message,
                Request,
                Settings,
                Color,
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
        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)
        if not websocket:
            return

        try:
            from .sound_light_pb2 import (
                Message,
                Request,
                GetSettings,
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
            _LOGGER.debug("WebSocket connection closed for %s", connection_key)
        except Exception as e:
            _LOGGER.error("Error in message handler for %s: %s", connection_key, e)
        finally:
            if connection_key in self._websockets:
                del self._websockets[connection_key]

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

    def get_device_state(self, baby_uid: str) -> Dict[str, Any]:
        """Get current state for a device."""
        return self._device_state.get(baby_uid, {})

    def set_state_change_callback(self, callback):
        """Set callback function to be called when device state changes via WebSocket."""
        self._state_change_callback = callback

    async def send_saved_sounds_request(self, baby_uid: str) -> None:
        """Request available sound list from device."""
        connection_key = f"{baby_uid}_speaker"
        websocket = self._websockets.get(connection_key)
        if not websocket:
            return

        try:
            from .sound_light_pb2 import (
                Message,
                Request,
                GetSettings,
            )

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
        """Close all connections."""
        for websocket in list(self._websockets.values()):
            try:
                await websocket.close()
            except Exception as e:
                _LOGGER.debug("Error closing websocket: %s", e)

        self._websockets.clear()
