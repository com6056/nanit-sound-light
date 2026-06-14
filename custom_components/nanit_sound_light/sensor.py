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
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
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

        # Diagnostics: battery %, WiFi signal, firmware version.
        entities.append(NanitSoundLightBatterySensor(coordinator, device_uid, device))
        entities.append(NanitSoundLightWifiSensor(coordinator, device_uid, device))
        entities.append(NanitSoundLightFirmwareSensor(coordinator, device_uid, device))

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


class NanitSoundLightBatterySensor(NanitSoundLightEntity, SensorEntity):
    """Battery charge sensor (coarse 5-bucket state-of-charge → percent)."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator, device_uid, device_data, "battery", "mdi:battery")
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the battery charge percentage (bucketed)."""
        return self._get_device_data().get("battery_percent")


class NanitSoundLightWifiSensor(NanitSoundLightEntity, SensorEntity):
    """WiFi signal-strength sensor (diagnostic); SSID/BSSID/channel as attrs."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the WiFi sensor."""
        # unique_id stays "…_wifi" (stable); display name is the HA device-class
        # convention "Signal strength" (matches how core/most integrations name it).
        super().__init__(
            coordinator,
            device_uid,
            device_data,
            "wifi",
            "mdi:wifi",
            display_name="Signal strength",
        )
        self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
        self._attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the WiFi RSSI in dBm."""
        return self._get_device_data().get("wifi_rssi")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return SSID / BSSID / channel as attributes."""
        device_data = self._get_device_data()
        return {
            "ssid": device_data.get("wifi_ssid"),
            "bssid": device_data.get("wifi_bssid"),
            "channel": device_data.get("wifi_channel"),
        }


class NanitSoundLightFirmwareSensor(NanitSoundLightEntity, SensorEntity):
    """Firmware version sensor (diagnostic)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the firmware sensor."""
        super().__init__(coordinator, device_uid, device_data, "firmware", "mdi:chip")

    @property
    def native_value(self) -> str | None:
        """Return the installed firmware version."""
        return self._get_device_data().get("firmware_version")
