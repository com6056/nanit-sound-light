"""Data update coordinator for Nanit Sound + Light integration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthenticationError, SoundLightAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class NanitSoundLightCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Nanit Sound + Light API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=30
            ),  # 30 seconds - faster polling as backup for WebSocket events
        )
        self.config_entry = config_entry
        # Use shared session from Home Assistant
        session = async_get_clientsession(hass)
        self.api = SoundLightAPI(session)

        # Validate configuration
        if not self.validate_config():
            raise ValueError("Invalid configuration data")

        # Initialize API with stored credentials for automatic re-authentication
        email = self.config_entry.data[CONF_EMAIL]
        password = self.config_entry.data[CONF_PASSWORD]
        refresh_token = self.config_entry.data.get("refresh_token")

        # Store credentials in API for potential re-authentication
        self.api._stored_email = email
        self.api._stored_password = password
        if refresh_token:
            self.api._refresh_token = refresh_token

        # Set up token update callback
        self.api.set_token_update_callback(self.update_stored_refresh_token)

        # Set up MFA callback immediately - we need this for token refresh scenarios
        self.api.set_mfa_required_callback(self._trigger_mfa_reauth)

        self._devices: list[dict[str, Any]] = []
        self._device_states: dict[str, dict[str, Any]] = {}
        self._last_colors: dict[
            str, dict[str, Any]
        ] = {}  # Remember last color for each device

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library."""
        start_time = time.time()
        try:
            # Ensure authentication is valid (will refresh or re-authenticate if needed)
            if not await self.api.ensure_authenticated():
                # If ensure_authenticated returns False, it means we're in a retry backoff
                # or MFA is pending - don't raise an exception immediately
                if self.api.is_mfa_pending():
                    _LOGGER.info(
                        "🔐 MFA authentication pending - integration paused until user completes verification"
                    )
                else:
                    _LOGGER.warning(
                        "⚠️ Authentication unavailable (rate limited or failed) - using cached data"
                    )

                if hasattr(self, "data") and self.data:
                    return self.data

                # During initial setup or when no cached data exists, we need to handle MFA differently
                # If MFA is pending, we should return a minimal state to prevent constant retries
                if self.api.is_mfa_pending():
                    # Return a minimal state that indicates MFA is needed
                    return {"mfa_required": True, "devices": {}}

                raise UpdateFailed("Authentication failed and no cached data available")

            # Get device list if needed
            if not self._devices:
                self._devices = await self.api.get_sound_light_devices()

                # Set up real-time state change callback
                self.api.set_state_change_callback(self._on_device_state_change)

                # Connect to all devices
                for device in self._devices:
                    await self.api.connect_device(device)
                    _LOGGER.info(
                        "🔗 Connected to device: %s (%s)",
                        device["speaker_name"],
                        device["speaker_uid"][:8] + "...",
                    )

                    # Also request available sounds after connection
                    baby_uid = device["baby_uid"]
                    await self.api.send_saved_sounds_request(baby_uid)

            # Update device states by sending ping commands
            for device in self._devices:
                baby_uid = device["baby_uid"]
                try:
                    # Send ping to request current state and wait for automatic deviceData stream
                    _LOGGER.debug("Requesting initial state via ping for %s", baby_uid)
                    await self._ping_device_for_state(baby_uid)

                    # Wait longer for deviceData messages (device might be off or slow to respond)
                    _LOGGER.debug(
                        "Waiting for deviceData messages to populate state..."
                    )
                    raw_state = {}

                    for attempt in range(20):  # 20 attempts * 0.5s = 10s max
                        await asyncio.sleep(0.5)
                        current_state = self.api.get_device_state(baby_uid)

                        # Check if we have ANY meaningful state (even if device is off)
                        if current_state:
                            # Accept any state that has been updated from deviceData parsing
                            # Even if device is off, we should get real "off" state vs. uninitialized defaults
                            state_keys = [
                                "brightness",
                                "volume",
                                "current_sound",
                                "hue",
                                "is_on",
                                "message_id",
                            ]
                            if any(
                                k in current_state and current_state[k] is not None
                                for k in state_keys
                            ):
                                raw_state = current_state
                                _LOGGER.info(
                                    "📊 Device %s state acquired in %.1fs: power=%s, brightness=%.1f%%, volume=%.1f%%, sound='%s'",
                                    device["speaker_name"],
                                    (attempt + 1) * 0.5,
                                    "ON" if raw_state.get("is_on", False) else "OFF",
                                    raw_state.get("brightness", 0) * 100,
                                    raw_state.get("volume", 0) * 100,
                                    raw_state.get("current_sound", "None")[:20]
                                    + (
                                        "..."
                                        if len(raw_state.get("current_sound", "")) > 20
                                        else ""
                                    ),
                                )
                                break

                        # Also check for increasing message IDs (indicates device is responding)
                        if current_state and current_state.get("message_id"):
                            if attempt > 0:  # Give it at least one attempt
                                raw_state = current_state
                                _LOGGER.info(
                                    "📡 Device %s responding (message_id=%s, attempt=%.1fs)",
                                    device["speaker_name"],
                                    current_state.get("message_id"),
                                    (attempt + 1) * 0.5,
                                )
                                break
                    else:
                        _LOGGER.warning(
                            "⚠️ Device %s unresponsive after 10s - may be offline or in sleep mode",
                            device["speaker_name"],
                        )
                        raw_state = {}

                    # Get parsed state from protobuf API (already parsed)
                    parsed_state = raw_state.copy() if raw_state else {}

                    # Store last good color when device provides color data (not noColor=true)
                    no_color = parsed_state.get("no_color", False)
                    if (
                        not no_color
                        and "hue" in parsed_state
                        and "saturation" in parsed_state
                    ):
                        last_color = {
                            "hue": parsed_state["hue"],
                            "saturation": parsed_state["saturation"],
                            "brightness": parsed_state.get("brightness", 1.0),
                        }
                        self._last_colors[baby_uid] = last_color
                        _LOGGER.debug(
                            "Stored last color for %s: %s", baby_uid, last_color
                        )

                    self._device_states[baby_uid] = {
                        **device,
                        **parsed_state,
                        "last_update": self.hass.loop.time(),
                    }

                    _LOGGER.debug(
                        "✅ Updated %s: brightness=%.1f%%, volume=%.1f%%, power=%s, sound='%s'",
                        device["speaker_name"],
                        parsed_state.get("brightness", 0.0) * 100,
                        parsed_state.get("volume", 0.0) * 100,
                        "ON" if parsed_state.get("is_on", False) else "OFF",
                        parsed_state.get("current_sound", "None")[:15]
                        + (
                            "..."
                            if len(parsed_state.get("current_sound", "")) > 15
                            else ""
                        ),
                    )

                except Exception as e:
                    error_type = type(e).__name__
                    _LOGGER.error(
                        "❌ Failed to update device %s (%s): %s",
                        device["speaker_name"],
                        error_type,
                        e,
                    )

            update_duration = time.time() - start_time
            _LOGGER.debug(
                "📈 Update cycle completed in %.2fs for %d devices",
                update_duration,
                len(self._devices),
            )
            return {"devices": self._device_states}

        except AuthenticationError as e:
            raise UpdateFailed(f"Authentication failed: {e}")
        except Exception as e:
            raise UpdateFailed(f"Error communicating with API: {e}")

    async def update_stored_refresh_token(self, new_refresh_token: str) -> None:
        """Update the stored refresh token in the config entry."""
        if new_refresh_token != self.config_entry.data.get("refresh_token"):
            try:
                new_data = dict(self.config_entry.data)
                new_data["refresh_token"] = new_refresh_token
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                _LOGGER.debug("Updated stored refresh token")
            except Exception as e:
                _LOGGER.warning("Failed to update stored refresh token: %s", e)

    def validate_config(self) -> bool:
        """Validate that we have required configuration data."""
        required_fields = [CONF_EMAIL, CONF_PASSWORD]
        for field in required_fields:
            if field not in self.config_entry.data or not self.config_entry.data[field]:
                _LOGGER.error("Missing required configuration field: %s", field)
                return False
        return True

    async def _trigger_mfa_reauth(self) -> None:
        """Trigger MFA re-authentication flow via Home Assistant."""
        _LOGGER.info(
            "🔐 MFA re-authentication required - creating user notification and reauth flow"
        )

        # Create a persistent notification to inform the user
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "message": "Your Nanit Sound + Light integration requires MFA verification to continue. Please complete the authentication flow.",
                "title": "Nanit Authentication Required",
                "notification_id": f"nanit_mfa_{self.config_entry.entry_id}",
            },
        )

        # Create a reauth flow to prompt user for MFA
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "reauth", "entry_id": self.config_entry.entry_id},
                data={},
            )
        )

    async def _ping_device_for_state(self, baby_uid: str) -> None:
        """Send ping command to get current device state using protobuf."""
        try:
            await self.api.send_ping_for_state(baby_uid)
            # Wait briefly for response
            await asyncio.sleep(1)
        except Exception as e:
            _LOGGER.debug("Ping failed for %s: %s", baby_uid, e)

    async def async_send_control_command(self, baby_uid: str, **kwargs) -> None:
        """Send control command to device using protobuf."""
        try:
            _LOGGER.debug(
                "🎮 Sending command to %s: %s",
                (
                    self._devices[0]["speaker_name"]
                    if self._devices
                    else baby_uid[:8] + "..."
                ),
                (
                    {k: v for k, v in kwargs.items() if k != "color"}
                    if "color" in kwargs
                    else kwargs
                ),
            )

            await self.api.send_control_command(baby_uid, **kwargs)

            # Apply pending command immediately to coordinator data for instant UI feedback
            if "devices" in self.data and baby_uid in self.data["devices"]:
                device_data = self.data["devices"][baby_uid]

                _LOGGER.debug("Applying immediate feedback for %s", baby_uid)

                for key, value in kwargs.items():
                    if key == "sound":
                        device_data["current_sound"] = value
                    elif key == "is_on":
                        device_data["is_on"] = value
                    elif key == "brightness":
                        device_data["brightness"] = value
                    elif key == "volume":
                        device_data["volume"] = value
                    elif key == "color":
                        # Update all color-related fields for proper light state
                        if "noColor" in value:
                            device_data["no_color"] = value["noColor"]
                        if "hue" in value:
                            device_data["hue"] = value["hue"]
                        if "saturation" in value:
                            device_data["saturation"] = value["saturation"]
                        if "brightness" in value:
                            device_data["brightness"] = value["brightness"]

                # Immediately notify entities
                self.async_update_listeners()

            # Trigger state update after command (for device confirmation)
            await self._ping_device_for_state(baby_uid)

        except Exception as e:
            error_type = type(e).__name__
            _LOGGER.error(
                "❌ Control command failed for %s (%s): %s",
                baby_uid[:8] + "...",
                error_type,
                e,
            )
            raise

    def get_last_color(self, baby_uid: str) -> dict[str, Any] | None:
        """Get the last known good color for a device."""
        return self._last_colors.get(baby_uid)

    def save_last_color(self, baby_uid: str, color_dict: dict[str, Any]) -> None:
        """Save a user-chosen color as the last color to restore later."""
        if not color_dict.get("noColor", True):  # Only save when color is enabled
            last_color = {
                "hue": color_dict["hue"],
                "saturation": color_dict["saturation"],
                "brightness": color_dict.get("brightness", 1.0),
            }
            self._last_colors[baby_uid] = last_color
            _LOGGER.debug("Saved last color for %s: %s", baby_uid, last_color)

    async def _on_device_state_change(self, baby_uid: str) -> None:
        """Handle real-time device state changes from WebSocket."""
        _LOGGER.debug("Real-time state change detected for device %s", baby_uid)

        # Simply trigger a coordinator refresh - WebSocket updates are fast enough
        # No need for complex immediate state management since responses are sub-second
        try:
            await self.async_request_refresh()
        except Exception as e:
            _LOGGER.debug("Failed to refresh after state change: %s", e)

    async def async_close(self) -> None:
        """Close the coordinator."""
        if self.api:
            await self.api.close()
