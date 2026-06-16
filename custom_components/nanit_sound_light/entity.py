"""Base entity for Nanit Sound + Light devices."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import NanitSoundLightCoordinator


class NanitSoundLightEntity(CoordinatorEntity):
    """Base entity for Nanit Sound + Light devices."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanitSoundLightCoordinator,
        device_uid: str,
        device_data: dict[str, Any],
        entity_type: str,
        icon: str | None = None,
        name: str | None = None,
    ) -> None:
        """Initialize the base entity.

        entity_type drives the stable unique_id. name is the short label that
        Home Assistant pairs with the device name (for example "Power"). Pass None
        to use the device-class default name (Temperature, Battery, and so on),
        for the primary entity that should read as just the device name, or for an
        entity that names itself through a translation_key.
        """
        super().__init__(coordinator)
        self._device_uid = device_uid
        self._device_data = device_data
        self._attr_unique_id = f"{device_uid}_{entity_type}"
        self._attr_name = name
        if icon is not None:
            self._attr_icon = icon

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information, including firmware as sw_version once known."""
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self._device_uid)},
            "name": self._device_data.get("speaker_name", "Sound + Light"),
            "manufacturer": MANUFACTURER,
            "model": MODEL,
        }
        firmware = self._get_device_data().get("firmware_version")
        if firmware:
            info["sw_version"] = firmware
        return info

    def _get_device_data(self) -> dict[str, Any]:
        """Get current device data from the coordinator."""
        if self.coordinator.data and "devices" in self.coordinator.data:
            return self.coordinator.data["devices"].get(self._device_uid, {})
        return {}

    @property
    def available(self) -> bool:
        """Return True only when the device is actually reachable.

        Gated on the live websocket and the readiness latch, not just cached
        coordinator data, so that on a cloud or socket outage (or while the relay
        is up but the device is still detached behind it) the entities go
        unavailable instead of showing the last-known state as if it were live. A
        brief reconnect blip can flip this for a second or two, which is the honest
        signal for a baby device.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self._device_uid in self.coordinator.data.get("devices", {})
            and self.coordinator.api.is_websocket_connected(self._device_uid)
            and self.coordinator.api.is_device_attached(self._device_uid)
        )
