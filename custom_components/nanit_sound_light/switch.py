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

    entities = []

    # Create switch entity for each device
    if coordinator.data and "devices" in coordinator.data:
        for device_uid, device_data in coordinator.data["devices"].items():
            entities.append(NanitSoundLightSwitch(coordinator, device_uid, device_data))

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
            coordinator, device_uid, device_data, "power", "mdi:speaker-wireless"
        )

    @property
    def is_on(self) -> bool:
        """Return true if the device is powered on (regardless of light state)."""
        device_data = self._get_device_data()
        # Switch represents device power state only (device can be on with light off via noColor)
        return device_data.get("is_on", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if self.coordinator.data and "devices" in self.coordinator.data:
            device_data = self.coordinator.data["devices"].get(self._device_uid, {})
            return {
                "brightness": device_data.get("brightness", 0.0),
                "volume": device_data.get("volume", 0.0),
                "current_sound": device_data.get("current_sound"),
                "speaker_uid": self._device_data.get("speaker_uid"),
                "baby_uid": self._device_data.get("baby_uid"),
            }
        return {}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the Sound + Light device (power only, don't change light state)."""
        try:
            # Only power on the device - don't change light/color state
            # If light was off (noColor=true), it should stay off when device powers on
            await self.coordinator.async_send_control_command(
                self._device_uid, is_on=True
            )
        except Exception as e:
            self._log_error("turn on device", e)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the Sound + Light device completely (device power off)."""
        try:
            # Power off the entire device (different from light entity which just turns off light)
            await self.coordinator.async_send_control_command(
                self._device_uid, is_on=False
            )
        except Exception as e:
            self._log_error("turn off device", e)
