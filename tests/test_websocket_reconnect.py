"""WebSocket reconnect / liveness tests for the flakiness fix (Bug A).

These run the real client against an in-process fake Nanit server on
127.0.0.1 (plaintext ws://, which is why `connect_device` only builds a TLS
context for wss://). No real device, no Nanit cloud. The `block_nanit_network`
guard would fail the test if it tried.

Covered:
* the reconnect backoff schedule matches the official app (0/2/5/7),
* a control command actually reaches the socket,
* when the server drops the connection, the client reconnects on its own
  instead of waiting for the next 30s poll.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

DEVICE = {"speaker_uid": "SPK123", "baby_uid": "baby123"}


async def _wait_until(predicate, timeout=3.0, interval=0.02):
    async def loop():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(loop(), timeout)


class _FakeNanit:
    """Minimal ws server that records frames and tracks live connections.

    Behaves like the real relay enough to exercise the protocol rework:
    * on connect it sends a `Message{backend{device{status: Connected}}}`
      frame (the readiness gate the client now waits for before sending),
    * for each control `Request{settings}` it replies with a
      `Response{requestId, statusCode: 200}` so the client's await-ack
      transaction completes (set `status_code` to simulate a rejection).
    """

    def __init__(self, pb2, *, status_code: int = 200, send_backend: bool = True):
        self._pb2 = pb2
        self._status_code = status_code
        self._send_backend = send_backend
        self.received: list[bytes] = []
        self.connections: list = []
        self._server = None
        self.port = 0

    async def start(self):
        async def handler(ws, *_args):
            self.connections.append(ws)
            if self._send_backend:
                backend = self._pb2.Message(
                    backend=self._pb2.Backend(
                        device=self._pb2.BackendDevice(status=self._pb2.Connected)
                    )
                )
                await ws.send(backend.SerializeToString())
            try:
                async for msg in ws:
                    self.received.append(msg)
                    await self._maybe_ack(ws, msg)
            except Exception:
                pass

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _maybe_ack(self, ws, raw: bytes) -> None:
        """Ack any request the way the device does (Response by requestId).

        Echoes settings back on a control request. For query requests
        (getSettings/getStatus/network/firmware) just returns the status code so
        the client's await resolves promptly.
        """
        msg = self._pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            return
        if not msg.HasField("request"):
            return
        response = self._pb2.Response(
            requestId=msg.request.id, statusCode=self._status_code
        )
        if msg.request.HasField("settings"):
            response.settings.CopyFrom(msg.request.settings)
        await ws.send(self._pb2.Message(response=response).SerializeToString())

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
async def fake_nanit(nsl, monkeypatch):
    server = _FakeNanit(nsl.pb2)
    await server.start()
    monkeypatch.setattr(
        nsl.api, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{server.port}"
    )
    yield server
    await server.stop()


def test_reconnect_backoff_matches_app(nsl):
    backoff = nsl.api._reconnect_backoff
    assert [backoff(r) for r in (0, 1, 3, 4, 10, 11, 50)] == [0, 2, 2, 5, 5, 7, 7]


async def test_connect_and_send_reaches_socket(nsl, fake_nanit):
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    assert api.is_websocket_connected("baby123")

    await api.send_control_command("baby123", is_on=True)

    def got_control():
        for raw in fake_nanit.received:
            msg = nsl.pb2.Message()
            try:
                msg.ParseFromString(raw)
            except Exception:
                continue
            if msg.HasField("request") and msg.request.settings.isOn:
                return True
        return False

    await _wait_until(got_control)
    await api.close()


async def test_reconnects_after_server_drop(nsl, fake_nanit):
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    await _wait_until(lambda: len(fake_nanit.connections) == 1)
    assert api.is_websocket_connected("baby123")

    # Server drops the connection, so the client should reconnect on its own.
    await fake_nanit.connections[0].close()

    await _wait_until(lambda: len(fake_nanit.connections) == 2)
    await _wait_until(lambda: api.is_websocket_connected("baby123"))

    await api.close()


async def test_send_raises_when_unreachable(nsl, monkeypatch):
    """A control command to a device with no socket must raise, not no-op silently."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]
    # No server running and no device info match -> ensure_websocket_connection fails.
    monkeypatch.setattr(nsl.api, "SOUND_LIGHT_WS_BASE_URL", "ws://127.0.0.1:1")

    with pytest.raises(ConnectionError):
        await api.send_control_command("baby123", is_on=True)

    await api.close()


async def test_diagnostics_requests_reach_socket_with_markers(nsl, fake_nanit):
    """Battery/wifi/firmware queries serialize the right request bodies/markers."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api.is_device_attached("baby123"))

    await api.send_status_request("baby123")
    await api.send_network_request("baby123")
    await api.send_firmware_request("baby123")

    def _decoded():
        out = []
        for raw in fake_nanit.received:
            msg = nsl.pb2.Message()
            try:
                msg.ParseFromString(raw)
            except Exception:
                continue
            if msg.HasField("request"):
                out.append(msg.request)
        return out

    await _wait_until(
        lambda: any(r.HasField("getStatus") and r.getStatus.all for r in _decoded())
    )
    await _wait_until(
        lambda: any(
            r.HasField("network") and r.network.HasField("getStatus")
            for r in _decoded()
        )
    )
    await _wait_until(
        lambda: any(
            r.HasField("firmware") and r.firmware.HasField("info") for r in _decoded()
        )
    )
    await api.close()


async def test_backend_connected_marks_device_attached(nsl, fake_nanit):
    """The relay's backend Connected frame flips is_device_attached (the gate)."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    assert api.is_device_attached("baby123") is False
    await api.connect_device(DEVICE)
    await _wait_until(lambda: api.is_device_attached("baby123"))
    await api.close()


async def test_control_command_awaits_ack_then_returns(nsl, fake_nanit):
    """A command resolves only once the matching Response (requestId) arrives."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    # Returns without raising because the fake server acks with statusCode 200.
    await api.send_control_command("baby123", is_on=True)
    await api.close()


async def _serve(nsl, monkeypatch, **kwargs):
    server = _FakeNanit(nsl.pb2, **kwargs)
    await server.start()
    monkeypatch.setattr(
        nsl.api, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{server.port}"
    )
    return server


async def test_control_command_rejection_raises(nsl, monkeypatch):
    """A non-2xx ack from the device surfaces as an error (so the UI rolls back)."""
    server = await _serve(nsl, monkeypatch, status_code=500)
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    with pytest.raises(ConnectionError):
        await api.send_control_command("baby123", is_on=True)

    await api.close()
    await server.stop()


async def test_slow_ack_is_accepted_without_resend(nsl, monkeypatch):
    """A slow/absent ack on a LIVE socket does NOT raise and does NOT re-send.

    The device is busy, not gone. Re-sending piles duplicates onto an already
    overloaded device (which wedges it). The command is accepted optimistically,
    and exactly one control frame reaches the wire.
    """
    monkeypatch.setattr(nsl.api, "COMMAND_ACK_TIMEOUT", 0.3)
    # Server attaches (so the gate passes) but never acks a control request.
    server = await _serve(nsl, monkeypatch, send_backend=True)
    server._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)  # swallow, never reply
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api.is_device_attached("baby123"))
    # Returns without raising despite the missing ack.
    await api.send_control_command("baby123", is_on=True)

    # Exactly one control frame on the wire, no duplicate re-send.
    controls = 0
    for raw in server.received:
        msg = nsl.pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            continue
        if msg.HasField("request") and msg.request.HasField("settings"):
            controls += 1
    assert controls == 1

    await api.close()
    await server.stop()


async def test_command_sends_best_effort_when_no_backend_frame(nsl, monkeypatch):
    """If the relay never sends a Connected frame, the gate falls back to a
    best-effort send (a missed/renamed frame must not brick control)."""
    monkeypatch.setattr(nsl.api, "DEVICE_ATTACH_TIMEOUT", 0.3)
    # Server never sends the backend frame, but still acks control requests.
    server = await _serve(nsl, monkeypatch, send_backend=False)
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    assert api.is_device_attached("baby123") is False  # never got a backend frame
    # Sends anyway after the short attach timeout, and the ack resolves it.
    await api.send_control_command("baby123", is_on=True)
    # The ack (a Response) also defensively inferred attachment.
    assert api.is_device_attached("baby123") is True

    await api.close()
    await server.stop()


async def test_inflight_command_fails_fast_on_socket_drop(nsl, monkeypatch):
    """A command awaiting an ack is failed promptly when the socket drops,
    instead of waiting out the full ack timeout."""
    monkeypatch.setattr(nsl.api, "COMMAND_ACK_TIMEOUT", 30)  # long, so the drop wins
    server = await _serve(nsl, monkeypatch, send_backend=True)
    server._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)  # never acks
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api.is_device_attached("baby123"))

    send_task = asyncio.ensure_future(api.send_control_command("baby123", is_on=True))
    await _wait_until(lambda: bool(api._pending_responses.get("baby123")))

    # Stop reconnects first (so the dropped socket isn't immediately re-dialed
    # mid-teardown), then drop the connection while the command awaits its ack.
    api._closing = True
    await server.connections[0].close()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(send_task, timeout=5)  # well under the 30s ack timeout

    await api.close()
    await server.stop()
