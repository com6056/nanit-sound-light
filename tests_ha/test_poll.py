"""The periodic poll is a light backup for a cloud_push integration: it pings and
uses cached/pushed state without the old 10s-per-device busy-wait.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.nanit_sound_light.coordinator import NanitSoundLightCoordinator


def test_has_usable_state():
    f = NanitSoundLightCoordinator._has_usable_state
    assert f({}) is False
    assert f({"speaker_name": "Nursery"}) is False  # only metadata, no real state
    assert f({"is_on": False}) is True
    assert f({"brightness": 0.0}) is True


async def test_poll_does_not_busy_wait_when_state_cached(hass, coordinator):
    """With state already known, a poll returns promptly (no 3-10s sleeping)."""
    coordinator.api.ensure_authenticated = AsyncMock(return_value=True)
    coordinator.api.get_device_state.return_value = {"is_on": True, "brightness": 0.5}

    start = hass.loop.time()
    data = await coordinator._async_update_data()
    elapsed = hass.loop.time() - start

    assert elapsed < 0.5  # would be ~3-10s with the old busy-wait
    coordinator.api.send_ping_for_state.assert_awaited()
    assert data["devices"]["baby1"]["is_on"] is True
