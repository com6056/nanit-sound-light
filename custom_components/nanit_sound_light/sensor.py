"""Sensor platform for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
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
    """Set up Nanit Sound + Light sensor entities."""
    coordinator: NanitSoundLightCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    for device in coordinator._devices:
        device_uid = device["baby_uid"]

        # Add temperature sensor
        entities.append(
            NanitSoundLightTemperatureSensor(coordinator, device_uid, device)
        )

        # Add humidity sensor
        entities.append(NanitSoundLightHumiditySensor(coordinator, device_uid, device))

    async_add_entities(entities)


class NanitSoundLightTemperatureSensor(NanitSoundLightEntity, SensorEntity):
    """Temperature sensor for Nanit Sound + Light device."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the temperature sensor."""
        super().__init__(
            coordinator, device_uid, device_data, "temperature", "mdi:thermometer"
        )

        # Temperature sensor attributes
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return the temperature value."""
        device_data = self._get_device_data()
        return device_data.get("temperature")


class NanitSoundLightHumiditySensor(NanitSoundLightEntity, SensorEntity):
    """Humidity sensor for Nanit Sound + Light device."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the humidity sensor."""
        super().__init__(
            coordinator, device_uid, device_data, "humidity", "mdi:water-percent"
        )

        # Humidity sensor attributes
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the humidity value."""
        device_data = self._get_device_data()
        return device_data.get("humidity")
