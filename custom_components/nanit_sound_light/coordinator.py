"""Data update coordinator for Nanit Sound + Light integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SoundLightAPI, AuthenticationError
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
        self._devices: List[Dict[str, Any]] = []
        self._device_states: Dict[str, Dict[str, Any]] = {}
        self._last_colors: Dict[str, Dict[str, Any]] = (
            {}
        )  # Remember last color for each device

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via library."""
        try:
            # Authenticate if needed
            if not self.api._access_token:
                email = self.config_entry.data[CONF_EMAIL]
                password = self.config_entry.data[CONF_PASSWORD]
                refresh_token = self.config_entry.data.get("refresh_token")

                # Use refresh token first if available (like working implementation)
                await self.api.authenticate(email, password, refresh_token)

            # Get device list if needed
            if not self._devices:
                self._devices = await self.api.get_sound_light_devices()

                # Set up real-time state change callback
                self.api.set_state_change_callback(self._on_device_state_change)

                # Connect to all devices
                for device in self._devices:
                    await self.api.connect_device(device)
                    _LOGGER.info("Connected to device: %s", device["speaker_name"])

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
                                    "Got device state for %s after %.1fs: power=%s, brightness=%.3f, volume=%.3f, sound=%s",
                                    baby_uid,
                                    (attempt + 1) * 0.5,
                                    raw_state.get("is_on", False),
                                    raw_state.get("brightness", 0),
                                    raw_state.get("volume", 0),
                                    raw_state.get("current_sound", "None"),
                                )
                                break

                        # Also check for increasing message IDs (indicates device is responding)
                        if current_state and current_state.get("message_id"):
                            if attempt > 0:  # Give it at least one attempt
                                raw_state = current_state
                                _LOGGER.info(
                                    "Got device response for %s after %.1fs (messageId=%s)",
                                    baby_uid,
                                    (attempt + 1) * 0.5,
                                    current_state.get("message_id"),
                                )
                                break
                    else:
                        _LOGGER.warning(
                            "No device response received for %s after 10s, device may be offline",
                            baby_uid,
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
                        "Updated device %s state: brightness=%.3f, volume=%.3f, power=%s, sound=%s",
                        device["speaker_name"],
                        parsed_state.get("brightness", 0.0),
                        parsed_state.get("volume", 0.0),
                        parsed_state.get("is_on", False),
                        parsed_state.get("current_sound", "None"),
                    )

                except Exception as e:
                    _LOGGER.error("Failed to update device %s: %s", baby_uid, e)

            return {"devices": self._device_states}

        except AuthenticationError as e:
            raise UpdateFailed(f"Authentication failed: {e}")
        except Exception as e:
            raise UpdateFailed(f"Error communicating with API: {e}")

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
            _LOGGER.debug("Sending control command for %s: %s", baby_uid, kwargs)

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
            _LOGGER.error("Failed to send control command: %s", e)
            raise

    def get_last_color(self, baby_uid: str) -> Dict[str, Any] | None:
        """Get the last known good color for a device."""
        return self._last_colors.get(baby_uid)

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
