"""Nanit Sound + Light integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import PROTOBUF_AVAILABLE
from .const import DOMAIN
from .coordinator import NanitSoundLightCoordinator

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
    if not PROTOBUF_AVAILABLE:
        # The generated protobuf module failed to import (e.g. a protobuf
        # runtime version mismatch). Fail clearly rather than running degraded,
        # where every control/parse path silently no-ops.
        _LOGGER.error(
            "protobuf is unavailable - the Nanit Sound + Light integration "
            "cannot function. Check the 'protobuf' Python package version."
        )
        return False

    # Get version from manifest to avoid hardcoding
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        # Use executor to avoid blocking the event loop
        def _load_manifest():
            with open(manifest_path) as f:
                return json.load(f)

        manifest = await hass.async_add_executor_job(_load_manifest)
        version = manifest.get("version", "unknown")
    except Exception:
        version = "unknown"

    _LOGGER.info("🚀 Setting up Nanit Sound + Light integration v%s", version)

    # Initialize coordinator
    coordinator = NanitSoundLightCoordinator(hass, entry)

    # Fetch initial data
    _LOGGER.debug("⚡ Performing initial data refresh...")
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as e:
        # Check if this is an MFA-related issue during initial setup
        if coordinator.api.is_mfa_pending():
            _LOGGER.info(
                "🔐 MFA required during initial setup - please reconfigure integration"
            )
            # Create a repair issue to guide the user
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"mfa_required_{entry.entry_id}",
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="mfa_required_setup",
                data={"entry_id": entry.entry_id},
            )
            return False
        else:
            _LOGGER.error("💥 Failed to setup integration: %s", e)
            return False

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    _LOGGER.debug("📱 Setting up platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("✅ Nanit Sound + Light integration setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Nanit Sound + Light integration")

    # Unload platforms first (so entities are gone), then tear down the socket.
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_close()
        # Clear any pending MFA notification for this entry.
        await hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": f"nanit_mfa_{entry.entry_id}"},
        )

    return unload_ok
