"""Binary sensor platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up Nanit Sound + Light binary sensor entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = [
        NanitSoundLightChargingSensor(coordinator, device["baby_uid"], device)
        for device in coordinator._devices
    ]
    async_add_entities(entities)


class NanitSoundLightChargingSensor(NanitSoundLightEntity, BinarySensorEntity):
    """Battery charging indicator."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the charging binary sensor."""
        super().__init__(
            coordinator, device_uid, device_data, "charging", "mdi:power-plug"
        )
        self._attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def is_on(self) -> bool | None:
        """Return True when the device reports it's charging."""
        return self._get_device_data().get("battery_charging")
