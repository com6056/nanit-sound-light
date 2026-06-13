"""WebSocket reconnect / liveness tests for the flakiness fix (Bug A).

These run the real client against an in-process fake Nanit server on
127.0.0.1 (plaintext ws://, which is why ``connect_device`` only builds a TLS
context for wss://). No real device, no Nanit cloud — the ``block_nanit_network``
guard would fail the test if it tried.

Covered:
* the reconnect backoff schedule matches the official app (0/2/5/7);
* a control command actually reaches the socket;
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
    """Minimal ws server that records frames and tracks live connections."""

    def __init__(self):
        self.received: list[bytes] = []
        self.connections: list = []
        self._server = None
        self.port = 0

    async def start(self):
        async def handler(ws, *_args):
            self.connections.append(ws)
            try:
                async for msg in ws:
                    self.received.append(msg)
            except Exception:
                pass

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
async def fake_nanit(nsl, monkeypatch):
    server = _FakeNanit()
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

    # Server drops the connection — the client should reconnect on its own.
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
