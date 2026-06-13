"""Protobuf contract tests for the Nanit Sound + Light wire format.

Two jobs:

* **Schema lock** — assert the field numbers (proto tags) of the messages we
  send/parse match what the official app v4.68.0 uses (see
  ``the reverse-engineering notes``). If someone regenerates ``sound_light.proto``
  and a tag shifts, this fails here instead of silently misreading device state
  at runtime — the same failure mode a similar contract test
  guards against elsewhere.
* **Parse round-trip** — build a device→app message with known bytes and assert
  ``SoundLightAPI._process_protobuf_message`` decodes it into the expected
  ``_device_state``.

All offline: pure protobuf bytes, no socket, no Home Assistant.
"""

from __future__ import annotations

import pytest

# Field number → name, confirmed against Nanit app v4.68.0 (nanitlite/control).
# The first 8 Settings fields are unchanged since the v4.0.6 reverse-engineering;
# v4.68.0 only *appends* favorites(9)..cryDetection(15), which protobuf skips.
SETTINGS_TAGS = {
    "brightness": 1,
    "color": 2,
    "volume": 3,
    "sound": 4,
    "isOn": 5,
    "soundList": 6,  # == app "savedSounds"
    "temperature": 7,
    "humidity": 8,
}
COLOR_TAGS = {"noColor": 1, "hue": 2, "saturation": 3}
SOUND_TAGS = {"noSound": 1, "track": 2}


def _assert_tags(message_cls, expected: dict[str, int]) -> None:
    fields = message_cls.DESCRIPTOR.fields_by_name
    for name, tag in expected.items():
        assert name in fields, (
            f"{message_cls.DESCRIPTOR.name}.{name} missing from proto"
        )
        assert fields[name].number == tag, (
            f"{message_cls.DESCRIPTOR.name}.{name} tag drifted: "
            f"expected {tag}, got {fields[name].number}"
        )


def test_settings_tags_match_app(nsl):
    _assert_tags(nsl.pb2.Settings, SETTINGS_TAGS)


def test_color_tags_match_app(nsl):
    _assert_tags(nsl.pb2.Color, COLOR_TAGS)


def test_sound_tags_match_app(nsl):
    _assert_tags(nsl.pb2.Sound, SOUND_TAGS)


async def test_response_settings_parses_into_device_state(nsl, api):
    """A device→app Response{settings} updates _device_state correctly."""
    pb2 = nsl.pb2
    settings = pb2.Settings(
        isOn=True,
        brightness=0.5,
        volume=0.8,
        sound=pb2.Sound(noSound=False, track="Pink Noise"),
        color=pb2.Color(noColor=True),
    )
    message = pb2.Message(response=pb2.Response(requestId=7, settings=settings))
    raw = message.SerializeToString()

    await api._process_protobuf_message("baby123_speaker", raw)

    state = api.get_device_state("baby123")
    assert state["is_on"] is True
    assert state["brightness"] == pytest.approx(0.5)
    assert state["volume"] == pytest.approx(0.8)
    assert state["current_sound"] == "Pink Noise"
    assert state["no_color"] is True


async def test_external_request_change_triggers_callback(nsl, api):
    """A device→app Request{settings} (external change) updates state and notifies."""
    pb2 = nsl.pb2
    notified: list[str] = []

    async def on_change(baby_uid: str) -> None:
        notified.append(baby_uid)

    api.set_state_change_callback(on_change)

    message = pb2.Message(request=pb2.Request(id=1, settings=pb2.Settings(isOn=False)))
    await api._process_protobuf_message("baby123_speaker", message.SerializeToString())

    assert api.get_device_state("baby123")["is_on"] is False
    assert notified == ["baby123"]


def test_additive_v468_fields_are_ignored_not_errors(nsl, api):
    """Unknown higher-tag fields (app v4.68 favorites/routines/etc) must parse cleanly.

    We can't build them (our proto doesn't declare them), but we can prove a
    Settings carrying an unknown high tag still decodes the fields we do know.
    """
    pb2 = nsl.pb2
    settings = pb2.Settings(isOn=True, brightness=1.0)
    raw = settings.SerializeToString()
    # Append a bogus field at tag 12 (varint) — simulates a new app-only field.
    raw += bytes([(12 << 3) | 0, 0x01])
    reparsed = pb2.Settings()
    reparsed.ParseFromString(raw)  # must not raise
    assert reparsed.isOn is True
    assert reparsed.brightness == pytest.approx(1.0)
