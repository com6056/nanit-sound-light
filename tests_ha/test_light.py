"""Light entity behavior that needs the real coordinator (coalesce + optimistic).

The device-facing api is mocked. These assert what the light *commands*.
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


async def test_light_off_dims_to_zero_keeping_power(hass, coordinator):
    """Light OFF = brightness 0 (noColor doesn't darken the light). Power/sound
    untouched so white noise keeps playing."""
    light = _light(coordinator)
    await light.async_turn_off()
    await asyncio.sleep(FLUSH_WAIT)

    coordinator.api.send_control_command.assert_awaited_once()
    _, kwargs = coordinator.api.send_control_command.call_args
    assert kwargs == {"brightness": 0.0}  # only brightness, power/sound untouched


async def test_app_side_light_off_reads_as_off(hass, coordinator):
    """noColor:true is the Nanit app's "Light off": it darkens the lamp while
    RETAINING brightness underneath (on-device 2026-07-11). is_on must gate on
    no_color too, or an app-side "Light off" shows as still on in HA."""
    coordinator.data["devices"]["baby1"].update(
        {"is_on": True, "brightness": 0.8, "no_color": True}
    )
    assert _light(coordinator).is_on is False

    # Color re-enabled -> emitting again.
    coordinator.data["devices"]["baby1"]["no_color"] = False
    assert _light(coordinator).is_on is True


async def test_turn_on_restores_device_color_when_no_stored_color(hass, coordinator):
    """After a restart cleared _last_colors, turning the light on must still
    clear no_color (using the device's retained hue/sat), not be a no-op."""
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
