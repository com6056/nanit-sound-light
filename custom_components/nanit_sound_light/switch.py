"""Switch platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up Nanit Sound + Light switch entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Create from the device list (like sensor/binary_sensor), not from
    # coordinator.data: a device whose first poll body failed would be missing
    # from data and would never get its control entities.
    entities = [
        NanitSoundLightSwitch(coordinator, device["baby_uid"], device)
        for device in coordinator._devices
    ]
    async_add_entities(entities)


class NanitSoundLightSwitch(NanitSoundLightEntity, SwitchEntity):
    """Switch entity for Nanit Sound + Light power control."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the switch."""
        super().__init__(
            coordinator,
            device_uid,
            device_data,
            "power",
            "mdi:speaker-wireless",
            name="Power",
        )

    @property
    def is_on(self) -> bool:
        """Return true if the device is powered on, regardless of light state.

        The device can be powered on with the light off (brightness 0), so this
        tracks device power only. The light entity owns light on/off.
        """
        return self._get_device_data().get("is_on", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the device power, leaving the light state unchanged."""
        await self.coordinator.async_send_control_command(self._device_uid, is_on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the device power (the whole device, not just the light)."""
        await self.coordinator.async_send_control_command(self._device_uid, is_on=False)
