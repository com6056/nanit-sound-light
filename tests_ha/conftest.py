"""Fixtures for the Home-Assistant-fixture test suite.

These tests run the real coordinator/entities against a `hass` event loop
(provided by pytest-homeassistant-custom-component) with a mocked SoundLightAPI,
so the coalescing/pin-guard/rollback/availability logic actually executes — but
no network and no real device (the api is a mock, and a socket guard fails any
accidental real connection).
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit_sound_light.const import DOMAIN
from custom_components.nanit_sound_light.coordinator import NanitSoundLightCoordinator


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Required by pytest-homeassistant-custom-component to load this integration."""
    yield


@pytest.fixture(autouse=True)
def block_nanit_network(monkeypatch):
    """Fail any attempt to resolve a Nanit host — tests use the mocked api only."""
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if isinstance(host, str) and "nanit.com" in host:
            raise AssertionError(f"Test tried to reach the real Nanit network: {host}")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


def _make_api() -> MagicMock:
    api = MagicMock()
    api.send_control_command = AsyncMock()
    api.send_ping_for_state = AsyncMock()
    api.get_device_state = MagicMock(return_value={})
    api.is_websocket_connected = MagicMock(return_value=True)
    api.close = AsyncMock()
    return api


@pytest.fixture
async def coordinator(hass):
    """A coordinator wired to a mocked api, pre-populated with one device."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_EMAIL: "test@example.com"})
    entry.add_to_hass(hass)

    api = _make_api()
    with patch(
        "custom_components.nanit_sound_light.coordinator.SoundLightAPI",
        return_value=api,
    ):
        coord = NanitSoundLightCoordinator(hass, entry)

    coord.api = api
    # Avoid the real 1s ping sleep inside the flush.
    coord._ping_device_for_state = AsyncMock()
    coord._devices = [{"speaker_name": "Nursery", "baby_uid": "baby1"}]
    coord.data = {
        "devices": {
            "baby1": {
                "speaker_name": "Nursery",
                "is_on": False,
                "volume": 0.0,
                "brightness": 0.0,
                "current_sound": "No sound",
                "no_color": True,
            }
        }
    }
    coord.last_update_success = True
    return coord
