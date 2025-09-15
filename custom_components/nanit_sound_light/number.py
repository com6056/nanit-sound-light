"""Number platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up Nanit Sound + Light number entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []

    # Create volume control entity for each device
    if coordinator.data and "devices" in coordinator.data:
        for device_uid, device_data in coordinator.data["devices"].items():
            entities.append(NanitSoundLightVolume(coordinator, device_uid, device_data))

    async_add_entities(entities)


class NanitSoundLightVolume(NanitSoundLightEntity, NumberEntity):
    """Number entity for Nanit Sound + Light volume control."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the volume control."""
        super().__init__(
            coordinator, device_uid, device_data, "volume", "mdi:volume-high"
        )
        self._attr_mode = NumberMode.SLIDER
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self) -> float | None:
        """Return the current volume percentage."""
        device_data = self._get_device_data()
        volume_float = device_data.get("volume", 0.0)
        # Round to 1 decimal place to avoid long float precision issues
        return round(volume_float * 100, 1)  # Convert 0.0-1.0 to 0-100

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.data is not None

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume percentage."""
        try:
            volume_float = value / 100.0  # Convert 0-100 to 0.0-1.0
            await self.coordinator.async_send_control_command(
                self._device_uid, volume=volume_float
            )
        except Exception as e:
            self._log_error("set volume", e)
