"""Entity availability: reflects the live socket, not just cached data."""

from __future__ import annotations

from custom_components.nanit_sound_light.switch import NanitSoundLightSwitch


def _switch(coordinator):
    return NanitSoundLightSwitch(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )


async def test_available_when_socket_connected(hass, coordinator):
    coordinator.api.is_websocket_connected.return_value = True
    assert _switch(coordinator).available is True


async def test_unavailable_when_socket_down(hass, coordinator):
    """A cloud/socket outage makes entities unavailable instead of stale-but-'live'."""
    coordinator.api.is_websocket_connected.return_value = False
    assert _switch(coordinator).available is False


async def test_unavailable_when_last_update_failed(hass, coordinator):
    coordinator.api.is_websocket_connected.return_value = True
    coordinator.last_update_success = False
    assert _switch(coordinator).available is False


async def test_unavailable_when_device_missing_from_data(hass, coordinator):
    coordinator.api.is_websocket_connected.return_value = True
    sw = _switch(coordinator)  # build while the device exists...
    coordinator.data = {"devices": {}}  # ...then it drops out of the data
    assert sw.available is False


async def test_switch_is_on_reflects_device_state(hass, coordinator):
    sw = _switch(coordinator)
    assert sw.is_on is False
    coordinator.data["devices"]["baby1"]["is_on"] = True
    assert sw.is_on is True
