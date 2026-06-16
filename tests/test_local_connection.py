"""Tests for the direct-LAN (local) transport.

These exercise the local socket path the integration prefers over the cloud
relay: the deterministic mDNS URL, the trust-all TLS context, the per-device
token fetch, and the prefer-local / fall-back-to-remote send routing. As with
the reconnect tests they run the real client against in-process fake servers on
127.0.0.1 (plaintext ws://) — never a real device or the Nanit cloud (the
``block_nanit_network`` guard would fail the test if it tried).
"""

from __future__ import annotations

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

    # Local server goes away entirely; the client should route sends to remote.
    await local.stop()
    await _wait_until(lambda: not api._transport_connected(_local_key(api)))

    await api.send_control_command("baby123", is_on=False)
    assert _has_control(nsl, remote, isOn=False)

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


async def test_local_disabled_connects_remote_only(nsl, monkeypatch):
    api, local, remote = await _connect_both(nsl, monkeypatch, local_enabled=False)
    # If local were attempted this would raise; with local disabled it must not be.
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
