"""Nanit Sound + Light integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,  # Temperature, humidity sensors
    Platform.LIGHT,  # Brightness and color control
    Platform.NUMBER,  # Volume control
    Platform.SWITCH,  # Power control
    Platform.SELECT,  # Sound selection
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nanit Sound + Light from a config entry."""
    _LOGGER.info("Setting up Nanit Sound + Light integration")

    # Initialize coordinator
    from .coordinator import NanitSoundLightCoordinator
    from .const import DOMAIN

    coordinator = NanitSoundLightCoordinator(hass, entry)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Nanit Sound + Light integration")

    from .const import DOMAIN

    # Close coordinator and API connections
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_close()

    # Clear any pending MFA notifications
    hass.components.persistent_notification.async_dismiss(f"nanit_mfa_{entry.entry_id}")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Remove stored data
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
