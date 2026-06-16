"""Protobuf contract tests for the Nanit Sound + Light wire format.

Two jobs:

* **Schema lock** — assert the field numbers (proto tags) of the messages we
  send/parse match what the official app v4.68.0 uses. If someone regenerates
  ``sound_light.proto`` and a tag shifts, this fails here instead of silently
  misreading device state at runtime.
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
# Backend readiness frame (app v4.68.0 nanitlite/control): Message.backend=3,
# Backend.device=1, BackendDevice.status=1, DeviceStatus{Disconnected=0,Connected=1}.
MESSAGE_TAGS = {"request": 1, "response": 2, "backend": 3}
BACKEND_TAGS = {"device": 1}
BACKEND_DEVICE_TAGS = {"status": 1}


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


def test_backend_tags_match_app(nsl):
    _assert_tags(nsl.pb2.Message, MESSAGE_TAGS)
    _assert_tags(nsl.pb2.Backend, BACKEND_TAGS)
    _assert_tags(nsl.pb2.BackendDevice, BACKEND_DEVICE_TAGS)
    # Enum values gate the readiness check in api.py (_BACKEND_STATUS_CONNECTED).
    assert nsl.pb2.Disconnected == 0
    assert nsl.pb2.Connected == 1


def test_response_status_tag_matches_app(nsl):
    """Response.status is tag 9 in the app (tag 6 is firmware) — a prior off-by
    mistag would silently drop or misread any Status-carried sensor frame."""
    fields = nsl.pb2.Response.DESCRIPTOR.fields_by_name
    assert fields["status"].number == 9
    # And the fields we actually rely on stay put.
    assert fields["requestId"].number == 1
    assert fields["statusCode"].number == 2
    assert fields["settings"].number == 4


async def test_backend_connected_frame_marks_device_attached(nsl, api):
    """A Backend{Connected} frame attaches; attachment is STICKY thereafter.

    The real device emits bare/Disconnected backend frames periodically while
    fully usable, so a non-Connected frame must NOT flip the device unavailable —
    only a socket drop clears attachment (covered in the reconnect suite).
    """
    pb2 = nsl.pb2

    connected = pb2.Message(
        backend=pb2.Backend(device=pb2.BackendDevice(status=pb2.Connected))
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), connected.SerializeToString()
    )
    assert api.is_device_attached("baby123") is True

    # A later Disconnected/bare frame is ignored — attachment stays sticky.
    disconnected = pb2.Message(
        backend=pb2.Backend(device=pb2.BackendDevice(status=pb2.Disconnected))
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), disconnected.SerializeToString()
    )
    assert api.is_device_attached("baby123") is True


async def test_response_with_requestid_resolves_pending_command(nsl, api):
    """A Response{requestId,statusCode} hands the ack to the awaiting send."""
    import asyncio

    future = asyncio.get_running_loop().create_future()
    api._pending_responses["baby123"] = {42: future}

    message = nsl.pb2.Message(response=nsl.pb2.Response(requestId=42, statusCode=200))
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )

    assert future.done() and future.result() == 200
    # A genuine Response also implies the device is attached.
    assert api.is_device_attached("baby123") is True


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

    await api._process_protobuf_message(api._conn_key("baby123", "remote"), raw)

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
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )

    assert api.get_device_state("baby123")["is_on"] is False
    assert notified == ["baby123"]


def test_diagnostics_tags_match_app(nsl):
    """Battery/wifi/firmware request+response tags (from the app's @ProtoNumber,
    NOT element-index+1 — a prior heuristic pass got these wrong)."""
    pb2 = nsl.pb2
    req = pb2.Request.DESCRIPTOR.fields_by_name
    assert req["network"].number == 2
    assert req["firmware"].number == 3
    assert req["getStatus"].number == 11
    resp = pb2.Response.DESCRIPTOR.fields_by_name
    assert resp["firmware"].number == 6
    assert resp["networkStatus"].number == 8
    assert resp["status"].number == 9
    assert pb2.Status.DESCRIPTOR.fields_by_name["battery"].number == 1
    assert pb2.Battery.DESCRIPTOR.fields_by_name["soc"].number == 1
    assert pb2.Battery.DESCRIPTOR.fields_by_name["isCharging"].number == 2
    ap = pb2.AccessPointInfo.DESCRIPTOR.fields_by_name
    assert (ap["ssid"].number, ap["rssi"].number, ap["primaryChannel"].number) == (
        1,
        5,
        6,
    )
    assert pb2.FirmwareInfo.DESCRIPTOR.fields_by_name["version"].number == 2
    assert pb2.NetworkStatus.DESCRIPTOR.fields_by_name["currentAp"].number == 4
    # StateOfCharge enum buckets.
    assert (pb2.SoCLow, pb2.SoC25, pb2.SoC50, pb2.SoC75, pb2.SoC90) == (0, 1, 2, 3, 4)


async def test_battery_status_parses(nsl, api):
    """Response{status{battery}} → bucketed percent + charging in device_state."""
    pb2 = nsl.pb2
    status = pb2.Status(battery=pb2.Battery(soc=pb2.SoC75, isCharging=True))
    message = pb2.Message(response=pb2.Response(requestId=1, status=status))
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )
    state = api.get_device_state("baby123")
    assert state["battery_percent"] == 75
    assert state["battery_charging"] is True


async def test_battery_not_charging_when_field_absent(nsl, api):
    """Device omits isCharging when unplugged -> we report not-charging, not unknown.

    Also covers the un-plug transition: a prior charging=True must flip back to
    False when a later battery status arrives without the field.
    """
    pb2 = nsl.pb2
    # Start charging.
    s1 = pb2.Message(
        response=pb2.Response(
            requestId=1,
            status=pb2.Status(battery=pb2.Battery(soc=pb2.SoC50, isCharging=True)),
        )
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), s1.SerializeToString()
    )
    assert api.get_device_state("baby123")["battery_charging"] is True
    # Unplug: next status omits isCharging entirely.
    s2 = pb2.Message(
        response=pb2.Response(
            requestId=2, status=pb2.Status(battery=pb2.Battery(soc=pb2.SoC50))
        )
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), s2.SerializeToString()
    )
    assert api.get_device_state("baby123")["battery_charging"] is False


async def test_network_status_parses(nsl, api):
    """Response{networkStatus{currentAp}} → wifi rssi/ssid/channel in device_state."""
    pb2 = nsl.pb2
    ap = pb2.AccessPointInfo(ssid="Nursery", bssid="aa:bb", rssi=-58, primaryChannel=6)
    message = pb2.Message(
        response=pb2.Response(
            requestId=1, networkStatus=pb2.NetworkStatus(currentAp=ap)
        )
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )
    state = api.get_device_state("baby123")
    assert state["wifi_rssi"] == -58
    assert state["wifi_ssid"] == "Nursery"
    assert state["wifi_channel"] == 6


async def test_firmware_version_parses(nsl, api):
    """Response{firmware{version}} → firmware_version in device_state."""
    pb2 = nsl.pb2
    message = pb2.Message(
        response=pb2.Response(requestId=1, firmware=pb2.FirmwareInfo(version="1.2.3"))
    )
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )
    assert api.get_device_state("baby123")["firmware_version"] == "1.2.3"


async def test_sound_list_is_sanitized(nsl, api):
    """Device-supplied track names are clamped + filtered before becoming options.

    Track names arrive from the cloud/device as untrusted strings; the parser
    drops blank/non-printable names and clamps length, then prepends "No sound".
    """
    pb2 = nsl.pb2
    settings = pb2.Settings(
        soundList=pb2.SoundList(
            tracks=[
                "Pink Noise",  # valid
                "x" * 200,  # overlong -> clamped to 64
                "bad\x07bell",  # non-printable -> dropped
                "   ",  # blank -> dropped
            ]
        )
    )
    message = pb2.Message(response=pb2.Response(requestId=1, settings=settings))
    await api._process_protobuf_message(
        api._conn_key("baby123", "remote"), message.SerializeToString()
    )

    options = api.get_device_state("baby123")["available_sounds"]
    assert options[0] == "No sound"
    assert "Pink Noise" in options
    assert any(len(o) == 64 for o in options)  # the overlong name, clamped
    assert "bad\x07bell" not in options
    assert "   " not in options


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
