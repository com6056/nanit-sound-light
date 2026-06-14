"""Base entity for Nanit Sound + Light devices."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import NanitSoundLightCoordinator

_LOGGER = logging.getLogger(__name__)


class NanitSoundLightEntity(CoordinatorEntity):
    """Base entity for Nanit Sound + Light devices."""

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
        entity_type: str,
        icon: str,
        display_name: str | None = None,
    ) -> None:
        """Initialize the base entity.

        ``entity_type`` drives the (stable) unique_id; ``display_name`` overrides
        the human-facing suffix when it differs from the type (e.g. the wifi
        sensor's unique_id stays ``…_wifi`` but it shows as "Signal strength",
        the Home Assistant device-class convention).
        """
        super().__init__(coordinator)
        self._device_uid = device_uid
        self._device_data = device_data

        # Set up entity attributes
        self._attr_unique_id = f"{device_uid}_{entity_type}"
        label = display_name if display_name is not None else entity_type.title()
        self._attr_name = f"{device_data.get('speaker_name', 'Sound + Light')} {label}"
        self._attr_icon = icon

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device_info = {
            "identifiers": {(DOMAIN, self._device_uid)},
            "name": self._device_data.get("speaker_name", "Sound + Light"),
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }

        return device_info

    def _get_device_data(self) -> dict[str, Any]:
        """Get current device data from coordinator."""
        if self.coordinator.data and "devices" in self.coordinator.data:
            return self.coordinator.data["devices"].get(self._device_uid, {})
        return {}

    @property
    def available(self) -> bool:
        """Return True only when the device is actually reachable.

        Gated on the live websocket AND the backend readiness frame (not just
        cached coordinator data) so that on a cloud/socket outage — or while the
        relay is up but the physical device is still detached behind it — the
        entities go unavailable instead of showing the last-known state as if it
        were live. A brief reconnect blip can flip this for a second or two,
        which is the honest signal for a baby device.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self._device_uid in self.coordinator.data.get("devices", {})
            and self.coordinator.api.is_websocket_connected(self._device_uid)
            and self.coordinator.api.is_device_attached(self._device_uid)
        )

    def _log_error(self, action: str, error: Exception) -> None:
        """Log error with consistent format."""
        _LOGGER.error("Failed to %s for %s: %s", action, self._device_uid, error)
