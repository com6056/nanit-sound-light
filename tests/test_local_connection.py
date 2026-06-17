"""Tests for the direct-LAN (local) transport.

These exercise the local socket path the integration prefers over the cloud
relay: the deterministic mDNS URL, the trust-all TLS context, the per-device
token fetch, and the prefer-local / fall-back-to-remote send routing. As with
the reconnect tests they run the real client against in-process fake servers on
127.0.0.1 (plaintext ws://), never a real device or the Nanit cloud (the
`block_nanit_network` guard would fail the test if it tried).
"""

from __future__ import annotations

import asyncio
import ssl

import pytest

from tests.test_websocket_reconnect import _FakeNanit, _wait_until

DEVICE = {"speaker_uid": "SPK123", "baby_uid": "baby123"}


def _has_control(nsl, server: _FakeNanit, **fields) -> bool:
    """True if the server received a control Request{settings} matching fields."""
    for raw in server.received:
        msg = nsl.pb2.Message()
        try:
            msg.ParseFromString(raw)
        except Exception:
            continue
        if not (msg.HasField("request") and msg.request.HasField("settings")):
            continue
        settings = msg.request.settings
        if all(getattr(settings, k) == v for k, v in fields.items()):
            return True
    return False


def _local_key(api):
    return api._conn_key("baby123", "local")


def _remote_key(api):
    return api._conn_key("baby123", "remote")


# ---------------------------------------------------------------------------
# Pure helpers (no sockets)
# ---------------------------------------------------------------------------


def test_local_ws_url_is_deterministic_mdns(nsl):
    """Local URL is wss://Nanit-<speaker_uid>.local:442 with NO path."""
    api = nsl.api.SoundLightAPI(session=None)
    assert api._local_ws_url("SPK123") == "wss://Nanit-SPK123.local:442"


def test_insecure_ssl_context_disables_verification(nsl):
    """The LOCAL TLS context accepts any cert + any hostname (matches the app)."""
    ctx = nsl.api.SoundLightAPI._build_insecure_ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_active_key_prefers_local_then_remote(nsl):
    """_active_connection_key returns local when present, else remote, else None."""
    api = nsl.api.SoundLightAPI(session=None)
    assert api._active_connection_key("baby123") is None

    # Fake "open" sockets via a stub that reports not-closed.
    class _Open:
        state = None

    api._is_websocket_closed = lambda ws: ws is None  # treat any obj as open
    api._websockets[_remote_key(api)] = _Open()
    assert api._active_connection_key("baby123") == _remote_key(api)
    api._websockets[_local_key(api)] = _Open()
    assert api._active_connection_key("baby123") == _local_key(api)


def test_active_transport_maps_local_and_cloud(nsl):
    """active_transport() exposes 'local'/'cloud'/None for the connection sensor."""
    api = nsl.api.SoundLightAPI(session=None)
    assert api.active_transport("baby123") is None

    class _Open:
        state = None

    api._is_websocket_closed = lambda ws: ws is None
    api._websockets[_remote_key(api)] = _Open()
    assert api.active_transport("baby123") == "cloud"  # remote transport -> "cloud"
    api._websockets[_local_key(api)] = _Open()
    assert api.active_transport("baby123") == "local"  # local preferred


# ---------------------------------------------------------------------------
# Device-token fetch (fake aiohttp session)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, status=200, payload=None):
        self._status = status
        self._payload = payload or {}
        self.calls: list[tuple] = []

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return _FakeResp(self._status, self._payload)


async def test_device_token_fetch_parses_user_device_token(nsl):
    # Real wire shape (snake_case), confirmed against the live API.
    session = _FakeSession(
        payload={"user_device_token": {"token": "DEVTOK", "expiration": 9_999_999_999}}
    )
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"

    token = await api._ensure_device_token("SPK123")

    assert token == "DEVTOK"
    url, headers = session.calls[-1]
    assert url.endswith("/speakers/SPK123/udtokens")
    assert headers["Authorization"] == "Bearer user-access"  # REST uses Bearer
    # Cached, so a second call does not re-fetch.
    assert await api._ensure_device_token("SPK123") == "DEVTOK"
    assert len(session.calls) == 1


async def test_device_token_fetch_accepts_camelcase_fallback(nsl):
    """Forward-compat: still parse the app's camelCase DTO names if ever returned."""
    session = _FakeSession(
        payload={
            "userDeviceToken": {"token": "DEVTOK", "expirationTime": 9_999_999_999}
        }
    )
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"
    assert await api._ensure_device_token("SPK123") == "DEVTOK"


async def test_device_token_fetch_404_leaves_no_token(nsl):
    session = _FakeSession(status=404)
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"
    assert await api._ensure_device_token("SPK123") is None


async def test_device_token_expiration_in_ms_is_scaled(nsl):
    """A millisecond expirationTime is normalized to epoch seconds."""
    session = _FakeSession(
        payload={"user_device_token": {"token": "T", "expiration": 9_999_999_999_000}}
    )
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"
    await api._ensure_device_token("SPK123")
    _token, expires_at = api._device_tokens["SPK123"]
    assert 9_000_000_000 < expires_at < 10_000_000_000  # seconds, not ms


# ---------------------------------------------------------------------------
# mDNS resolver injection (HA-in-container can't resolve .local via libc)
# ---------------------------------------------------------------------------


async def test_resolver_substitutes_ip_into_local_url(nsl, monkeypatch):
    """When a resolver is injected, local connects to wss://<resolved-ip>:442."""
    api = nsl.api.SoundLightAPI(session=None)
    api._device_tokens["SPK123"] = ("dev-tok", None)

    async def resolver(speaker_uid):
        assert speaker_uid == "SPK123"
        return "10.0.0.5"

    api.set_local_host_resolver(resolver)

    captured = {}

    async def fake_connect(url, **_kw):
        captured["url"] = url
        raise RuntimeError("stop after capture")  # short-circuit before handler

    monkeypatch.setattr(nsl.api.websockets, "connect", fake_connect)
    await api._connect_transport(DEVICE, "local")

    assert captured["url"] == "wss://10.0.0.5:442"


async def test_resolver_failure_stays_on_relay(nsl, monkeypatch):
    """If the resolver can't find the device, local connect is skipped entirely."""
    api = nsl.api.SoundLightAPI(session=None)
    api._device_tokens["SPK123"] = ("dev-tok", None)

    async def resolver(_host):
        return None

    api.set_local_host_resolver(resolver)

    called = {"connect": False}

    async def fake_connect(_url, **_kw):
        called["connect"] = True
        raise RuntimeError("should not be reached")

    monkeypatch.setattr(nsl.api.websockets, "connect", fake_connect)
    await api._connect_transport(DEVICE, "local")

    assert called["connect"] is False
    assert not api._transport_connected(_local_key(api))


# ---------------------------------------------------------------------------
# Prefer-local / failover routing (two fake servers)
# ---------------------------------------------------------------------------


async def _connect_both(nsl, monkeypatch, *, local_enabled=True):
    """Start local+remote fakes, wire the client to them, return (api, local, remote)."""
    local = _FakeNanit(nsl.pb2)
    remote = _FakeNanit(nsl.pb2)
    await local.start()
    await remote.start()
    monkeypatch.setattr(
        nsl.api, "SOUND_LIGHT_WS_BASE_URL", f"ws://127.0.0.1:{remote.port}"
    )
    api = nsl.api.SoundLightAPI(session=None, local_enabled=local_enabled)
    api._access_token = "test-token"
    api._device_list = [DEVICE]
    # Preload the device token so local is eligible without a cloud fetch
    # (session is None, so _ensure_device_token returns the cached value).
    api._device_tokens["SPK123"] = ("dev-tok", None)
    monkeypatch.setattr(
        api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}"
    )
    return api, local, remote


async def test_prefers_local_for_sends(nsl, monkeypatch):
    api, local, remote = await _connect_both(nsl, monkeypatch)

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    await api.send_control_command("baby123", is_on=True)

    assert _has_control(nsl, local, isOn=True)
    assert not _has_control(nsl, remote, isOn=True)

    await api.close()
    await local.stop()
    await remote.stop()


async def test_falls_back_to_remote_when_local_down(nsl, monkeypatch):
    api, local, remote = await _connect_both(nsl, monkeypatch)

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    # Local server goes away entirely, so the client should route sends to remote.
    await local.stop()
    await _wait_until(lambda: not api._transport_connected(_local_key(api)))

    await api.send_control_command("baby123", is_on=False)
    assert _has_control(nsl, remote, isOn=False)

    await api.close()
    await remote.stop()


async def test_resends_on_surviving_transport_when_inflight_socket_drops(
    nsl, monkeypatch
):
    """A command whose socket drops mid-flight re-sends on the other transport.

    Covers the redundant-drop re-send path in _transact. The reconnect suite's
    drop test does not exercise it because that test runs with reconnects disabled
    and no surviving transport, so the command there is meant to fail. Here both
    transports are up, the in-flight (local) socket dies, and the command must
    land on remote and succeed without a rollback.
    """
    api, local, remote = await _connect_both(nsl, monkeypatch)

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))

    # Local is preferred. Let it accept the send but never ack, so the command
    # sits in flight on local while remote keeps acking normally.
    local._maybe_ack = lambda *_a, **_k: asyncio.sleep(0)

    send = asyncio.ensure_future(api.send_control_command("baby123", is_on=True))
    await _wait_until(lambda: api._inflight_conn_key.get("baby123") == _local_key(api))

    # Drop the local socket mid-flight while remote stays up.
    await local.stop()

    # The command re-sends on remote and returns without raising (no rollback).
    await asyncio.wait_for(send, timeout=5)
    assert _has_control(nsl, remote, isOn=True)

    await api.close()
    await remote.stop()


async def test_device_still_available_while_one_transport_down(nsl, monkeypatch):
    """is_websocket_connected stays True if ANY transport is up (availability)."""
    api, local, remote = await _connect_both(nsl, monkeypatch)

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api._transport_connected(_local_key(api)))
    assert api.is_websocket_connected("baby123")

    await local.stop()
    await _wait_until(lambda: not api._transport_connected(_local_key(api)))
    # Remote still up -> device still reachable, still attached (sticky).
    assert api.is_websocket_connected("baby123")
    assert api.is_device_attached("baby123")

    await api.close()
    await remote.stop()


async def test_local_403_invalidates_device_token_and_refetches(nsl, monkeypatch):
    """A local handshake rejected with 403 drops the cached device token so the
    next attempt refetches a fresh one.

    The device rotates the per-device token server-side, and it can rotate before
    our cached copy's clock expiry, so without invalidation-on-403 we would keep
    presenting a stale token and loop on 403 forever. Here the first handshake is
    rejected, which must invalidate the cache; the second refetches a fresh token
    and connects.
    """
    local = _FakeNanit(nsl.pb2, reject_status=403, reject_first=1)
    await local.start()

    session = _FakeSession(
        payload={"user_device_token": {"token": "FRESH", "expiration": 9_999_999_999}}
    )
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"
    api._device_list = [DEVICE]
    # A stale cached token with no clock expiry: _ensure_device_token would keep
    # serving it indefinitely without the invalidation fix.
    api._device_tokens["SPK123"] = ("STALE", None)
    monkeypatch.setattr(
        api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}"
    )
    local_key = _local_key(api)

    # First attempt presents the stale token and is rejected 403.
    await api._connect_transport(DEVICE, "local")
    assert not api._transport_connected(local_key)
    assert "SPK123" not in api._device_tokens  # token invalidated on 403
    assert api._auth_reject_counts[local_key] == 1  # rejection counted
    assert session.calls == []  # the stale cached token was used, no refetch yet

    # Second attempt refetches a fresh token and connects (server now accepts).
    await api._connect_transport(DEVICE, "local")
    await _wait_until(lambda: api._transport_connected(local_key))
    assert session.calls  # refetched via /udtokens after invalidation
    assert api._device_tokens["SPK123"][0] == "FRESH"
    assert local_key not in api._auth_reject_counts  # reset on a clean connect

    await api.close()
    await local.stop()


async def test_local_auth_reject_cooldown_stops_udtokens_refetch(nsl, monkeypatch):
    """Once local auth rejections cross the threshold, further connect attempts
    are skipped during the cooldown, so a wedged device stops refetching /udtokens.

    This covers the poll-path gap: the 30s coordinator poll drives
    ensure_websocket_connection -> connect_device -> _connect_transport, which is
    NOT the reconnect loop, so without a time-based gate it would keep hitting the
    cloud token endpoint every cycle. The cooldown short-circuits the connect
    attempt itself, before the token fetch and the handshake.
    """
    local = _FakeNanit(nsl.pb2, reject_status=403, reject_always=True)
    await local.start()
    session = _FakeSession(
        payload={"user_device_token": {"token": "T", "expiration": 9_999_999_999}}
    )
    api = nsl.api.SoundLightAPI(session=session)
    api._access_token = "user-access"
    api._device_list = [DEVICE]
    monkeypatch.setattr(
        api, "_local_ws_url", lambda _uid: f"ws://127.0.0.1:{local.port}"
    )
    local_key = _local_key(api)
    threshold = nsl.api.AUTH_REJECT_BACKOFF_THRESHOLD

    # Drive attempts up to the threshold. Each rejected attempt refetches the
    # token once (the 403 invalidated the cache), so the cloud is hit each time.
    for _ in range(threshold):
        await api._connect_transport(DEVICE, "local")
    assert api._auth_reject_counts[local_key] == threshold
    assert local_key in api._auth_reject_until  # cooldown armed
    udtoken_calls = len(session.calls)
    handshakes = local.handshakes

    # Further attempts during the cooldown short-circuit: no new /udtokens fetch,
    # no new handshake. This is the poll hammering the wedged device.
    for _ in range(5):
        await api._connect_transport(DEVICE, "local")
    assert len(session.calls) == udtoken_calls  # cloud not hit again
    assert local.handshakes == handshakes  # no further connect attempts

    # When the cooldown elapses the gate reopens and a connect is attempted again
    # (proving it is time-based, not a permanent lockout).
    api._auth_reject_until[local_key] = 0
    await api._connect_transport(DEVICE, "local")
    assert local.handshakes == handshakes + 1

    await api.close()
    await local.stop()


async def test_local_disabled_connects_remote_only(nsl, monkeypatch):
    api, local, remote = await _connect_both(nsl, monkeypatch, local_enabled=False)
    # If local were attempted this would raise. With local disabled it must not be.
    monkeypatch.setattr(
        api,
        "_local_ws_url",
        lambda _uid: pytest.fail("local must not be attempted when disabled"),
    )

    await api.connect_device(DEVICE)
    await _wait_until(lambda: api._transport_connected(_remote_key(api)))
    assert not api._transport_connected(_local_key(api))

    await api.close()
    await local.stop()
    await remote.stop()
