"""Auth/setup robustness: the coordinator surfaces the right failure type, and
reauth re-prompts for the password instead of depending on a live coordinator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit_sound_light.api import (
    AuthenticationError,
    MfaRequiredError,
)
from custom_components.nanit_sound_light.const import CONF_MFA_CODE, DOMAIN


async def test_update_raises_auth_failed_when_reauth_needed(hass, coordinator):
    coordinator.api.ensure_authenticated = AsyncMock(return_value=False)
    coordinator.api.needs_reauth = MagicMock(return_value=True)
    coordinator.data = None
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_returns_cached_on_transient_auth_blip(hass, coordinator):
    coordinator.api.ensure_authenticated = AsyncMock(return_value=False)
    coordinator.api.needs_reauth = MagicMock(return_value=False)
    cached = {"devices": {"baby1": {"is_on": True}}}
    coordinator.data = cached
    assert await coordinator._async_update_data() == cached


async def test_update_raises_not_ready_when_transient_and_no_cache(hass, coordinator):
    coordinator.api.ensure_authenticated = AsyncMock(return_value=False)
    coordinator.api.needs_reauth = MagicMock(return_value=False)
    coordinator.data = None
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_reauth_reprompts_password_and_updates_token(hass):
    """Reauth re-authenticates with a fresh password and stores the new token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_EMAIL: "test@example.com", "refresh_token": "old"},
    )
    entry.add_to_hass(hass)

    fake_api = MagicMock()
    fake_api.authenticate = AsyncMock()
    fake_api._refresh_token = "new-token"

    with (
        patch(
            "custom_components.nanit_sound_light.config_flow.SoundLightAPI",
            return_value=fake_api,
        ),
        patch(
            "custom_components.nanit_sound_light.async_setup_entry",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "newpass"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "new-token"


async def test_reauth_handles_mfa(hass):
    """A reauth that triggers MFA proceeds to the MFA step and completes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_EMAIL: "test@example.com", "refresh_token": "old"},
    )
    entry.add_to_hass(hass)

    fake_api = MagicMock()
    fake_api.authenticate = AsyncMock(side_effect=MfaRequiredError("mfa", "mfa-token"))
    fake_api.complete_mfa_authentication = AsyncMock()
    fake_api._refresh_token = "new-token"

    with (
        patch(
            "custom_components.nanit_sound_light.config_flow.SoundLightAPI",
            return_value=fake_api,
        ),
        patch(
            "custom_components.nanit_sound_light.async_setup_entry",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "newpass"}
        )
        assert result["step_id"] == "reauth_mfa"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MFA_CODE: "123456"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    fake_api.complete_mfa_authentication.assert_awaited_once()


async def test_reauth_invalid_password_shows_error(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_EMAIL: "test@example.com"},
    )
    entry.add_to_hass(hass)

    fake_api = MagicMock()
    fake_api.authenticate = AsyncMock(side_effect=AuthenticationError("bad"))

    with patch(
        "custom_components.nanit_sound_light.config_flow.SoundLightAPI",
        return_value=fake_api,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong"}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert result["step_id"] == "reauth_confirm"
