"""Coordinator behavior under a real hass loop: coalescing, pin-guard, rollback.

These exercise the logic that the offline suite can't (it needs the event loop's
call_later / task scheduling). The device-facing api is mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from custom_components.nanit_sound_light.coordinator import COMMAND_COALESCE_DELAY

FLUSH_WAIT = COMMAND_COALESCE_DELAY + 0.1


async def test_concurrent_commands_coalesce_into_one_send(hass, coordinator):
    """A scene's separate entity commands collapse into ONE combined send."""
    await coordinator.async_send_control_command("baby1", is_on=True)
    await coordinator.async_send_control_command("baby1", volume=0.5)
    await coordinator.async_send_control_command("baby1", sound="Pink Noise")

    await asyncio.sleep(FLUSH_WAIT)

    coordinator.api.send_control_command.assert_awaited_once()
    args, kwargs = coordinator.api.send_control_command.call_args
    assert args[0] == "baby1"
    assert kwargs == {"is_on": True, "volume": 0.5, "sound": "Pink Noise"}


async def test_optimistic_state_is_applied_immediately(hass, coordinator):
    await coordinator.async_send_control_command("baby1", is_on=True)
    # Applied before the flush even runs.
    assert coordinator.data["devices"]["baby1"]["is_on"] is True


async def test_pin_guard_suppresses_stale_echo_then_releases(hass, coordinator):
    """A stale 'off' echo can't flap a just-commanded 'on', and real changes resume."""
    await coordinator.async_send_control_command("baby1", is_on=True)
    assert coordinator.data["devices"]["baby1"]["is_on"] is True

    # Stale echo (device briefly still reports the pre-command value) is suppressed.
    coordinator.api.get_device_state.return_value = {"is_on": False}
    await coordinator._on_device_state_change("baby1")
    assert coordinator.data["devices"]["baby1"]["is_on"] is True

    # Device confirms our value -> pin releases.
    coordinator.api.get_device_state.return_value = {"is_on": True}
    await coordinator._on_device_state_change("baby1")

    # A genuine later external change now flows through.
    coordinator.api.get_device_state.return_value = {"is_on": False}
    await coordinator._on_device_state_change("baby1")
    assert coordinator.data["devices"]["baby1"]["is_on"] is False


async def test_pin_guard_releases_after_window(hass, coordinator):
    """Once the pin window lapses, the device's value wins even if it differs."""
    await coordinator.async_send_control_command("baby1", is_on=True)

    # Force the pin to look expired.
    coordinator._pinned_fields["baby1"]["is_on"] = (True, hass.loop.time() - 1)

    coordinator.api.get_device_state.return_value = {"is_on": False}
    await coordinator._on_device_state_change("baby1")
    assert coordinator.data["devices"]["baby1"]["is_on"] is False


async def test_failed_send_rolls_back_optimistic_state(hass, coordinator):
    """A send failure must not leave the UI showing a state the device rejected."""
    coordinator.api.send_control_command = AsyncMock(
        side_effect=ConnectionError("socket down")
    )

    await coordinator.async_send_control_command("baby1", is_on=True)
    # Optimistic 'on' shown immediately...
    assert coordinator.data["devices"]["baby1"]["is_on"] is True

    await asyncio.sleep(FLUSH_WAIT)

    # ...then rolled back to the pre-command value after the send fails.
    assert coordinator.data["devices"]["baby1"]["is_on"] is False


async def test_successful_send_clears_rollback_snapshot(hass, coordinator):
    await coordinator.async_send_control_command("baby1", is_on=True)
    await asyncio.sleep(FLUSH_WAIT)
    coordinator.api.send_control_command.assert_awaited_once()
    assert "baby1" not in coordinator._rollback_snapshot
