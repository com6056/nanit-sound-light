"""Light platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .coordinator import NanitSoundLightCoordinator
from .entity import NanitSoundLightEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Nanit Sound + Light light entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []

    # Create light entity for each device
    if coordinator.data and "devices" in coordinator.data:
        for device_uid, device_data in coordinator.data["devices"].items():
            entities.append(NanitSoundLightLight(coordinator, device_uid, device_data))

    async_add_entities(entities)


class NanitSoundLightLight(NanitSoundLightEntity, LightEntity):
    """Light entity for Nanit Sound + Light brightness and color control."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator, device_uid, device_data, "light", "mdi:lightbulb")

        # Light-specific attributes
        self._attr_supported_color_modes = {ColorMode.HS}
        self._attr_color_mode = ColorMode.HS

    @property
    def is_on(self) -> bool:
        """Return true if the light is on."""
        device_data = self._get_device_data()
        device_power_on = device_data.get("is_on", False)
        has_brightness = device_data.get("brightness", 0.0) > 0
        no_color_enabled = device_data.get("no_color", False)

        # Calculate light state
        is_light_on = device_power_on and has_brightness and not no_color_enabled

        # Add comprehensive logging for debugging
        _LOGGER.debug(
            "💡 Light state for %s: device_power=%s, brightness=%.2f, no_color=%s → light_on=%s",
            self._device_uid,
            device_power_on,
            device_data.get("brightness", 0.0),
            no_color_enabled,
            is_light_on,
        )

        return is_light_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness of the light."""
        device_data = self._get_device_data()
        brightness_float = device_data.get("brightness", 0.0)
        return int(brightness_float * 255)  # Convert 0.0-1.0 to 0-255

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the HS color of the light."""
        device_data = self._get_device_data()
        hue_normalized = device_data.get("hue", 0.0)
        saturation_normalized = device_data.get("saturation", 0.0)
        # Convert device values to HA format
        hue_degrees = hue_normalized * 360.0  # Convert 0.0-1.0 to 0-360 degrees
        saturation_percent = saturation_normalized * 100.0  # Convert 0.0-1.0 to 0-100
        return (hue_degrees, saturation_percent)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        device_data = self._get_device_data()
        return {
            "volume": device_data.get("volume", 0.0),
            "current_sound": device_data.get("current_sound"),
            "brightness_percent": device_data.get("brightness", 0.0) * 100,
            "device_hue": device_data.get("hue", 0.0),
            "device_saturation": device_data.get("saturation", 0.0),
            "no_color": device_data.get("no_color", False),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light with brightness and color."""
        _LOGGER.debug("Light turn ON requested for %s", self._device_uid)

        control_params = {"is_on": True}

        # Check if device is currently off and automatically set "No sound"
        device_data = self._get_device_data()
        is_currently_on = device_data.get("is_on", False)
        if not is_currently_on:
            # Device is off, set "No sound" to avoid unwanted audio
            control_params["sound"] = "No sound"
            _LOGGER.debug(
                "Device %s is off, automatically setting 'No sound' when turning on light",
                self._device_uid,
            )

        # Handle brightness
        if "brightness" in kwargs:
            brightness = kwargs["brightness"]
            brightness_float = brightness / 255.0  # Convert 0-255 to 0.0-1.0
            control_params["brightness"] = brightness_float

        # Handle color - always prioritize last stored color when light is off
        device_data = self._get_device_data()
        current_no_color = device_data.get("no_color", False)
        device_is_on = device_data.get("is_on", False)

        # If light is currently off or has no color, restore color
        if current_no_color or not device_is_on:
            last_color = self.coordinator.get_last_color(self._device_uid)
            if last_color and "hs_color" not in kwargs:
                # Use stored last color when just turning on (no explicit color from HA)
                color_dict = {
                    "noColor": False,
                    "hue": last_color["hue"],
                    "saturation": last_color["saturation"],
                    "brightness": control_params.get(
                        "brightness", last_color["brightness"]
                    ),
                }
                _LOGGER.debug(
                    "Restoring last color for %s: hue=%.3f, sat=%.3f",
                    self._device_uid,
                    last_color["hue"],
                    last_color["saturation"],
                )
                control_params["color"] = color_dict
            elif "hs_color" in kwargs:
                # Use HA UI color (either no stored color OR user explicitly set color)
                hue, saturation = kwargs["hs_color"]
                color_dict = {
                    "noColor": False,
                    "hue": float(hue) / 360.0,
                    "saturation": saturation / 100.0,
                    "brightness": control_params.get("brightness", 1.0),
                }
                _LOGGER.debug(
                    "Using HA UI color for %s: hue=%.3f, sat=%.3f",
                    self._device_uid,
                    color_dict["hue"],
                    color_dict["saturation"],
                )
                control_params["color"] = color_dict
                # Save this as last color for future restoration
                self.coordinator.save_last_color(self._device_uid, color_dict)
            # Remove the default warm white fallback - if no color info, don't set color
        elif "hs_color" in kwargs:
            # Device is on and user set explicit color - check if it's different from current
            current_hue = device_data.get("hue", 0.0) * 360.0  # Convert to degrees
            current_sat = (
                device_data.get("saturation", 0.0) * 100.0
            )  # Convert to percent
            hue, saturation = kwargs["hs_color"]

            # Consider colors "different" if they differ by more than 5 degrees/5 percent
            hue_diff = abs(current_hue - hue)
            sat_diff = abs(current_sat - saturation)

            if hue_diff > 5.0 or sat_diff > 5.0:
                # Significant color change - use new color
                color_dict = {
                    "noColor": False,
                    "hue": float(hue) / 360.0,
                    "saturation": saturation / 100.0,
                    "brightness": control_params.get("brightness", 1.0),
                }
                _LOGGER.debug("Color changed for %s", self._device_uid)
                control_params["color"] = color_dict
                # Save this as last color for future restoration
                self.coordinator.save_last_color(self._device_uid, color_dict)
            else:
                # Color is essentially the same - just a power toggle, don't set color
                _LOGGER.debug("Color unchanged for %s, not setting", self._device_uid)

        try:
            _LOGGER.debug("Sending light ON command for %s", self._device_uid)
            await self.coordinator.async_send_control_command(
                self._device_uid, **control_params
            )
        except Exception as e:
            _LOGGER.error("❌ Failed to turn on light for %s: %s", self._device_uid, e)
            self._log_error("turn on light", e)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the light using noColor instead of brightness=0."""
        _LOGGER.debug("Light turn OFF requested for %s", self._device_uid)
        try:
            # Use noColor=True to turn off light (better than brightness=0)
            color_dict = {
                "noColor": True,
                "hue": 0.0,
                "saturation": 0.0,
                "brightness": 1.0,  # Keep brightness high, noColor disables it
            }
            _LOGGER.debug("Sending light OFF command for %s", self._device_uid)
            await self.coordinator.async_send_control_command(
                self._device_uid, color=color_dict
            )
        except Exception as e:
            _LOGGER.error("❌ Failed to turn off light for %s: %s", self._device_uid, e)
            self._log_error("turn off light", e)
