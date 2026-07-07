"""Select platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
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
    """Set up Nanit Sound + Light select entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Create from the device list (like sensor/binary_sensor), not from
    # coordinator.data: a device whose first poll body failed would be missing
    # from data and would never get its control entities.
    entities = [
        NanitSoundLightSoundSelect(coordinator, device["baby_uid"], device)
        for device in coordinator._devices
    ]
    async_add_entities(entities)


class NanitSoundLightSoundSelect(NanitSoundLightEntity, SelectEntity):
    """Select entity for Nanit Sound + Light sound selection."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the sound selector."""
        super().__init__(
            coordinator,
            device_uid,
            device_data,
            "sound",
            "mdi:music-note",
            name="Sound",
        )
        # Options are set dynamically from the device response.

    @property
    def options(self) -> list[str]:
        """Return dynamic sound options from device."""
        device_data = self._get_device_data()
        device_sounds = device_data.get("available_sounds")
        if device_sounds:
            return device_sounds
        return []  # Empty list if no sounds available from device

    @property
    def current_option(self) -> str | None:
        """Return the current sound selection."""
        device_data = self._get_device_data()
        current_sound = device_data.get("current_sound")

        # No sound list from the device yet: report unknown rather than an
        # option that isn't in the (empty) options list.
        available_options = self.options
        if not available_options:
            return None

        # Handle valid sounds in our dynamic list
        if current_sound in available_options:
            return current_sound

        # Handle None or unknown sounds, default to "No sound"
        # This provides better UX than showing empty selection
        if current_sound is None or current_sound == "":
            return "No sound"

        return None

    async def async_select_option(self, option: str) -> None:
        """Select a sound option."""
        if option not in self.options:
            _LOGGER.warning("Unknown sound option: %s", option)
            return
        await self.coordinator.async_send_control_command(
            self._device_uid, sound=option
        )
        _LOGGER.debug("Selected sound '%s' for device %s", option, self._device_uid)
