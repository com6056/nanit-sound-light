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
import http
import logging

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

    def __init__(
        self,
        pb2,
        *,
        status_code: int = 200,
        send_backend: bool = True,
        reject_status: int | None = None,
        reject_first: int = 0,
        reject_always: bool = False,
    ):
        self._pb2 = pb2
        self._status_code = status_code
        self._send_backend = send_backend
        # Handshake rejection (for the auth-reject backoff tests). `reject_status`
        # is the HTTP status returned by `process_request`; `reject_first` rejects
        # that many handshakes then accepts; `reject_always` rejects every one.
        self._reject_status = reject_status
        self._reject_first = reject_first
        self._reject_always = reject_always
        self.handshakes = 0
        self.received: list[bytes] = []
        self.connections: list = []
        self.auth_headers: list[str | None] = []
        self._server = None
        self.port = 0

    async def _process_request(self, connection, request):
        """Reject the handshake with an HTTP status, or return None to accept."""
        self.auth_headers.append(request.headers.get("Authorization"))
        if self._reject_status is None:
            return None
        self.handshakes += 1
        if self._reject_always or self.handshakes <= self._reject_first:
            return connection.respond(
                http.HTTPStatus(self._reject_status), "rejected\n"
            )
        return None

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

        self._server = await websockets.serve(
            handler, "127.0.0.1", 0, process_request=self._process_request
        )
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


async def test_persistent_remote_auth_reject_quiets_logs(nsl, monkeypatch, caplog):
    """A relay that keeps rejecting the handshake (401/403/404) is logged loudly
    only for the first few attempts, then one WARNING, then debug, so a wedged
    device can't flood the log with one ERROR per retry."""
    server = await _serve(nsl, monkeypatch, reject_status=403, reject_always=True)
    api = nsl.api.SoundLightAPI(session=None)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    api._access_token = "test-token"
    api._device_list = [DEVICE]
    key = api._conn_key("baby123", "remote")

    threshold = nsl.api.AUTH_REJECT_BACKOFF_THRESHOLD
    with caplog.at_level(logging.DEBUG):
        for _ in range(threshold + 3):
            await api._connect_transport(DEVICE, "remote")

    # The first `threshold` attempts each hit the relay and were rejected; the
    # threshold attempt armed the cooldown, so the remaining calls short-circuit
    # before the handshake. The counter and the handshake count stop climbing.
    assert api._auth_reject_counts[key] == threshold
    assert server.handshakes == threshold
    api_errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name.endswith(".api")
    ]
    api_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith(".api")
    ]
    # Loud ERROR for the first (threshold - 1) attempts, then a single WARNING at
    # the threshold. So ERROR lines are bounded, not one per attempt.
    assert len(api_errors) == threshold - 1
    assert len(api_warnings) == 1

    await api.close()
    await server.stop()


async def test_persistent_auth_reject_escalates_reconnect_interval(nsl, monkeypatch):
    """Once consecutive auth rejections cross the threshold, the reconnect loop
    switches from the fast app-matching backoff to the long, quiet interval."""
    api = nsl.api.SoundLightAPI(session=None)
    api._device_list = [DEVICE]

    async def fake_connect_transport(device_info, transport):
        # Simulate _handle_auth_reject's effect without real sockets: the
        # transport never connects and the auth-reject counter climbs.
        ck = api._conn_key(device_info["baby_uid"], transport)
        api._auth_reject_counts[ck] = api._auth_reject_counts.get(ck, 0) + 1

    monkeypatch.setattr(api, "_connect_transport", fake_connect_transport)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        delays.append(delay)
        # Stop after the long interval has been used a couple of times.
        if delays.count(nsl.api.AUTH_REJECT_RETRY_INTERVAL) >= 2:
            api._closing = True
        await real_sleep(0)

    monkeypatch.setattr(nsl.api.asyncio, "sleep", fake_sleep)

    await api._reconnect_with_backoff("baby123", "remote")

    assert delays[0] != nsl.api.AUTH_REJECT_RETRY_INTERVAL  # started on fast backoff
    assert nsl.api.AUTH_REJECT_RETRY_INTERVAL in delays  # escalated to the long one
    assert delays[-1] == nsl.api.AUTH_REJECT_RETRY_INTERVAL


async def test_executor_shutdown_during_connect_is_quiet(nsl, monkeypatch, caplog):
    """A reconnect racing HA's executor teardown (restart/stop) must not log
    ERROR or count as a transient failure. Observed in production as 'Executor
    shutdown has been called' noise during HA restarts."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]
    # wss:// so the connect path needs the executor-built TLS context.
    monkeypatch.setattr(nsl.api, "SOUND_LIGHT_WS_BASE_URL", "wss://127.0.0.1:1")

    loop = asyncio.get_running_loop()

    def shutdown_executor(*_a, **_k):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(loop, "run_in_executor", shutdown_executor)

    with caplog.at_level(logging.DEBUG):
        await api._connect_transport(DEVICE, "remote")

    loud = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.endswith(".api")
    ]
    assert not loud
    assert api._transient_fail_counts == {}  # not treated as a device failure

    await api.close()


async def test_close_waits_for_connection_tasks(nsl, fake_nanit):
    """close() awaits its cancelled handler/reconnect tasks, so nothing from
    the old instance is still unwinding when a reload builds the next one."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "test-token"
    api._device_list = [DEVICE]

    await api.connect_device(DEVICE)
    handler_tasks = list(api._handler_tasks.values())
    assert handler_tasks

    await api.close()
    assert all(task.done() for task in handler_tasks)


async def test_hard_expired_token_refreshes_before_remote_connect(
    nsl, fake_nanit, monkeypatch
):
    """A remote connect holding a hard-expired access token refreshes it first
    instead of handshaking into a guaranteed 401 (which would count toward the
    auth-reject backoff and could cool the transport down for minutes)."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "stale-token"
    api._token_expires_at = 1.0  # long past its exp
    api._device_list = [DEVICE]

    async def fake_ensure():
        api._access_token = "fresh-token"
        api._token_expires_at = nsl.api.time.time() + 3600
        return True

    monkeypatch.setattr(api, "ensure_authenticated", fake_ensure)

    await api.connect_device(DEVICE)
    assert api.is_websocket_connected("baby123")
    # The handshake presented the refreshed token, not the stale one.
    assert fake_nanit.auth_headers[-1] == "token fresh-token"

    await api.close()


async def test_buffer_window_token_connects_without_refresh(
    nsl, fake_nanit, monkeypatch
):
    """A token merely inside the pre-expiry refresh buffer (still VALID) is
    used as-is. The poll rotates it; a transient refresh failure here must not
    block a connect that would have succeeded with the current token."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = "still-valid"
    api._token_expires_at = nsl.api.time.time() + 60  # inside the 300s buffer
    api._device_list = [DEVICE]

    called = {"ensure": False}

    async def fake_ensure():
        called["ensure"] = True
        return True

    monkeypatch.setattr(api, "ensure_authenticated", fake_ensure)

    await api.connect_device(DEVICE)
    assert api.is_websocket_connected("baby123")
    assert called["ensure"] is False  # no pre-connect refresh for a valid token
    assert fake_nanit.auth_headers[-1] == "token still-valid"

    await api.close()


async def test_repeated_transient_remote_failures_quiet_logs(nsl, monkeypatch, caplog):
    """A remote transport failing transiently (refused/outage) logs ERROR only
    for the first few attempts, then one WARNING, then debug. Only the log
    level is throttled. Every call below still attempts a real connect
    (unlike the auth-reject cooldown, which short-circuits attempts)."""
    api = nsl.api.SoundLightAPI(session=None)
    api._schedule_reconnect = lambda *_a, **_k: None  # drive attempts explicitly
    api._access_token = "test-token"
    api._device_list = [DEVICE]
    # Nothing listens here: every connect attempt fails fast (refused).
    monkeypatch.setattr(nsl.api, "SOUND_LIGHT_WS_BASE_URL", "ws://127.0.0.1:1")
    key = api._conn_key("baby123", "remote")

    threshold = nsl.api.TRANSIENT_FAIL_LOG_THRESHOLD
    attempts = threshold + 3
    with caplog.at_level(logging.DEBUG):
        for _ in range(attempts):
            await api._connect_transport(DEVICE, "remote")

    assert api._transient_fail_counts[key] == attempts  # no attempt was skipped
    api_errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name.endswith(".api")
    ]
    api_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith(".api")
    ]
    assert len(api_errors) == threshold - 1
    assert len(api_warnings) == 1

    await api.close()


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
