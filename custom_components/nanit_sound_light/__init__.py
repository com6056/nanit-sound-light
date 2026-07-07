"""Nanit Sound + Light integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import PROTOBUF_AVAILABLE
from .const import DOMAIN
from .coordinator import NanitSoundLightCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,  # Temperature, humidity, battery %, WiFi, firmware
    Platform.BINARY_SENSOR,  # Battery charging
    Platform.LIGHT,  # Brightness and color control
    Platform.NUMBER,  # Volume control
    Platform.SWITCH,  # Power control
    Platform.SELECT,  # Sound selection
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nanit Sound + Light from a config entry."""
    if not PROTOBUF_AVAILABLE:
        # The generated protobuf module failed to import (e.g. a protobuf
        # runtime version mismatch). Fail clearly rather than running degraded,
        # where every control/parse path silently no-ops.
        _LOGGER.error(
            "protobuf is unavailable. The Nanit Sound + Light integration "
            "cannot function. Check the 'protobuf' Python package version."
        )
        return False

    coordinator = NanitSoundLightCoordinator(hass, entry)
    # Raises ConfigEntryNotReady on a transient failure (HA retries) or
    # ConfigEntryAuthFailed when the user must re-authenticate (HA opens the
    # reauth flow). See the coordinator's _async_update_data.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload when options change (e.g. toggling the local-connection preference)
    # so the coordinator rebuilds the api with the new setting.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Nanit Sound + Light integration")

    # Unload platforms first (so entities are gone), then tear down the socket.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_close()

    return unload_ok
