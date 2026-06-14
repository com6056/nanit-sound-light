"""Light entity behavior that needs the real coordinator (coalesce + optimistic).

The device-facing api is mocked; these assert what the light *commands*.
"""

from __future__ import annotations

import asyncio

from custom_components.nanit_sound_light.coordinator import COMMAND_COALESCE_DELAY
from custom_components.nanit_sound_light.light import NanitSoundLightLight

FLUSH_WAIT = COMMAND_COALESCE_DELAY + 0.1


def _light(coordinator):
    return NanitSoundLightLight(
        coordinator, "baby1", coordinator.data["devices"]["baby1"]
    )


async def test_light_off_sends_bare_no_color(hass, coordinator):
    """Light OFF disables only color (sound keeps playing) — no isOn change."""
    light = _light(coordinator)
    await light.async_turn_off()
    await asyncio.sleep(FLUSH_WAIT)

    coordinator.api.send_control_command.assert_awaited_once()
    _, kwargs = coordinator.api.send_control_command.call_args
    assert kwargs["color"] == {"noColor": True}
    assert "is_on" not in kwargs  # power untouched -> white noise keeps playing


async def test_turn_on_restores_device_color_when_no_stored_color(hass, coordinator):
    """After a restart cleared _last_colors, turning the light on must still
    clear no_color (using the device's retained hue/sat) — not be a no-op."""
    # Device is off + light disabled (no_color) but still holds a hue/sat.
    coordinator.data["devices"]["baby1"].update(
        {"is_on": False, "no_color": True, "hue": 0.5, "saturation": 0.8}
    )
    assert coordinator.get_last_color("baby1") is None  # nothing remembered

    light = _light(coordinator)
    await light.async_turn_on()  # no brightness, no hs_color
    await asyncio.sleep(FLUSH_WAIT)

    coordinator.api.send_control_command.assert_awaited_once()
    _, kwargs = coordinator.api.send_control_command.call_args
    assert kwargs["is_on"] is True
    # The fix: color is re-enabled from the device's retained hue/sat.
    assert kwargs["color"]["noColor"] is False
    assert kwargs["color"]["hue"] == 0.5
    assert kwargs["color"]["saturation"] == 0.8
