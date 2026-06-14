"""Diagnostic entities (battery / wifi / firmware) read coordinator state."""

from __future__ import annotations

from custom_components.nanit_sound_light.binary_sensor import (
    NanitSoundLightChargingSensor,
)
from custom_components.nanit_sound_light.sensor import (
    NanitSoundLightBatterySensor,
    NanitSoundLightFirmwareSensor,
    NanitSoundLightWifiSensor,
)


async def test_battery_and_charging(hass, coordinator):
    coordinator.data["devices"]["baby1"].update(
        {"battery_percent": 75, "battery_charging": True}
    )
    battery = NanitSoundLightBatterySensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    charging = NanitSoundLightChargingSensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    assert battery.native_value == 75
    assert charging.is_on is True


async def test_wifi_and_firmware(hass, coordinator):
    coordinator.data["devices"]["baby1"].update(
        {
            "wifi_rssi": -58,
            "wifi_ssid": "Nursery",
            "wifi_channel": 6,
            "firmware_version": "1.2.3",
        }
    )
    wifi = NanitSoundLightWifiSensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    firmware = NanitSoundLightFirmwareSensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    assert wifi.native_value == -58
    assert wifi.extra_state_attributes["ssid"] == "Nursery"
    assert wifi.extra_state_attributes["channel"] == 6
    assert firmware.native_value == "1.2.3"


async def test_diagnostics_unknown_when_not_reported(hass, coordinator):
    """Before any GetStatus/Network/Firmware reply, values read as None (unknown)."""
    battery = NanitSoundLightBatterySensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    wifi = NanitSoundLightWifiSensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    firmware = NanitSoundLightFirmwareSensor(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )
    assert battery.native_value is None
    assert wifi.native_value is None
    assert firmware.native_value is None
