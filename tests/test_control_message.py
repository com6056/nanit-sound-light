"""Regression tests for the command-race fix (Bug B).

The Sound + Light exposes one physical device as several Home Assistant
entities (switch/light/select/number). A scene toggles them at once, which used
to send four racing protobuf messages whose out-of-order responses clobbered
each other, leaving the device off after a "turn on" scene.

The fix has two parts:
* `build_control_message` packs every field into ONE `Settings` message, so
  a coalesced multi-field command is a single atomic write.
* Each message carries a unique, incrementing id (the app correlates responses
  by it, and we used to hardcode `id=1`).

These tests cover the message layer (offline, no Home Assistant). The
coordinator's time-based coalescing is covered by the HA-fixture layer.
"""

from __future__ import annotations

import pytest


def _decode(nsl, raw: bytes):
    msg = nsl.pb2.Message()
    msg.ParseFromString(raw)
    return msg


def test_combined_command_is_one_settings_message(nsl, api):
    """A scene-like multi-field command becomes a single Settings, all fields set."""
    raw, _ = api.build_control_message(
        is_on=True,
        sound="Pink Noise",
        volume=1.0,
        color={"noColor": True},
    )
    msg = _decode(nsl, raw)

    assert msg.HasField("request")
    settings = msg.request.settings
    # Every field rides in the one message, no per-field racing writes.
    assert settings.isOn is True
    assert settings.volume == pytest.approx(1.0)
    assert settings.sound.track == "Pink Noise"
    assert settings.sound.noSound is False
    assert settings.color.noColor is True


def test_power_on_survives_alongside_other_fields(nsl, api):
    """Regression for the on→off flap: isOn=True can't be clobbered within one msg."""
    raw, _ = api.build_control_message(is_on=True, volume=0.5, sound="Pink Noise")
    settings = _decode(nsl, raw).request.settings
    assert settings.isOn is True  # the field a partial 'volume' write used to stomp


def test_message_ids_increment_and_are_unique(api):
    """Each control message gets a fresh id so responses can be correlated."""
    _, id1 = api.build_control_message(is_on=True)
    _, id2 = api.build_control_message(volume=0.5)
    _, id3 = api.build_control_message(sound="No sound")
    ids = [id1, id2, id3]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3  # all distinct (previously every id was 1)


def test_no_sound_sets_no_sound_flag(nsl, api):
    raw, _ = api.build_control_message(sound="No sound")
    sound = _decode(nsl, raw).request.settings.sound
    assert sound.noSound is True
    assert sound.track == ""


def test_light_off_sends_bare_no_color(nsl, api):
    """Light OFF is a clean color{noColor:true} with no stray hue/sat/brightness.

    The old encoding sent noColor + hue:0 + sat:0 + brightness:1.0, which both
    muddied the on/off mechanism and clobbered the device's stored color. We now
    omit any color sub-field that wasn't provided so the last color survives an
    off/on cycle.
    """
    raw, _ = api.build_control_message(color={"noColor": True})
    settings = _decode(nsl, raw).request.settings
    assert settings.color.noColor is True
    assert not settings.color.HasField("hue")
    assert not settings.color.HasField("saturation")
    assert not settings.HasField("brightness")
    # And it must NOT touch the power primitive (sound keeps playing).
    assert not settings.HasField("isOn")


def test_session_id_is_stamped_when_provided(nsl, api):
    raw, _ = api.build_control_message(session_id="abc123", is_on=True)
    assert _decode(nsl, raw).request.sessionId == "abc123"
