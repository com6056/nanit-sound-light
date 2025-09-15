"""Config flow for Nanit Sound + Light integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SoundLightAPI, AuthenticationError, MfaRequiredError
from .const import DOMAIN, CONF_MFA_CODE

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_MFA_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MFA_CODE): str,
    }
)


class NanitSoundLightConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanit Sound + Light."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str | None = None
        self._password: str | None = None
        self._mfa_token: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            try:
                # Use shared session from Home Assistant
                session = async_get_clientsession(self.hass)
                api = SoundLightAPI(session)

                # Try to authenticate
                await api.authenticate(self._email, self._password)

                # If we get here, authentication succeeded without MFA
                devices = await api.get_sound_light_devices()

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    # Store refresh token if we got one
                    data = {
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                    }
                    if api._refresh_token:
                        data["refresh_token"] = api._refresh_token

                    return self.async_create_entry(
                        title=f"Nanit Sound + Light ({self._email})",
                        data=data,
                    )

            except MfaRequiredError as mfa_error:
                # Store MFA token and proceed to MFA step
                self._mfa_token = mfa_error.mfa_token
                return await self.async_step_mfa()

            except AuthenticationError as e:
                _LOGGER.error("Authentication failed: %s", e)
                errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.error("Unexpected error: %s", e)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle MFA verification step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]

            try:
                # Use shared session from Home Assistant
                session = async_get_clientsession(self.hass)
                api = SoundLightAPI(session)

                # Complete MFA authentication
                await api.complete_mfa_authentication(
                    self._email, self._password, self._mfa_token, mfa_code
                )

                # MFA successful, get devices
                devices = await api.get_sound_light_devices()

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    # Store refresh token like working implementation
                    data = {
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                    }
                    if api._refresh_token:
                        data["refresh_token"] = api._refresh_token

                    return self.async_create_entry(
                        title=f"Nanit Sound + Light ({self._email})",
                        data=data,
                    )

            except AuthenticationError as e:
                _LOGGER.error("MFA verification failed: %s", e)
                errors["base"] = "invalid_mfa"
            except Exception as e:
                _LOGGER.error("Unexpected error during MFA: %s", e)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )
