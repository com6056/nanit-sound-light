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
        self._reauth_entry: config_entries.ConfigEntry | None = None

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
                    _LOGGER.warning(
                        "🔍 No Sound + Light devices found for user account"
                    )
                    errors["base"] = "no_devices"
                else:
                    _LOGGER.info(
                        "🎉 Successfully found %d Sound + Light device(s)", len(devices)
                    )
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
                _LOGGER.info("🔐 MFA verification required for user setup")
                return await self.async_step_mfa()

            except AuthenticationError as e:
                _LOGGER.error("🚫 Authentication failed during setup: %s", e)
                errors["base"] = "invalid_auth"
            except Exception as e:
                error_type = type(e).__name__
                _LOGGER.error("💥 Unexpected setup error (%s): %s", error_type, e)
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
                    _LOGGER.warning("🔍 No devices found after MFA verification")
                    errors["base"] = "no_devices"
                else:
                    _LOGGER.info(
                        "🎉 MFA verification successful - found %d device(s)",
                        len(devices),
                    )
                    # Store refresh token for future authentication
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
                _LOGGER.error("🚫 MFA verification failed: %s", e)
                errors["base"] = "invalid_mfa"
            except Exception as e:
                error_type = type(e).__name__
                _LOGGER.error("💥 Unexpected error during MFA (%s): %s", error_type, e)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth flow when MFA is required for re-authentication."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if not self._reauth_entry:
            return self.async_abort(reason="reauth_failed")

        # Get the coordinator to check if MFA is pending
        from .const import DOMAIN

        coordinator = self.hass.data[DOMAIN].get(self._reauth_entry.entry_id)

        if not coordinator or not coordinator.api.is_mfa_pending():
            return self.async_abort(reason="no_mfa_pending")

        # Set the unique ID for this reauth flow to prevent duplicates
        await self.async_set_unique_id(self._reauth_entry.unique_id)
        self._abort_if_unique_id_configured()

        # Show MFA form for reauth
        return await self.async_step_reauth_mfa()

    async def async_step_reauth_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle MFA input during reauth."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]

            # Get coordinator
            from .const import DOMAIN

            coordinator = self.hass.data[DOMAIN].get(self._reauth_entry.entry_id)

            if coordinator:
                try:
                    # Complete pending MFA authentication
                    success = await coordinator.api.complete_pending_mfa(mfa_code)

                    if success:
                        _LOGGER.info(
                            "🎉 MFA re-authentication successful - integration resumed"
                        )
                        # Clear the persistent notification
                        await self.hass.services.async_call(
                            "persistent_notification",
                            "dismiss",
                            {
                                "notification_id": f"nanit_mfa_{self._reauth_entry.entry_id}"
                            },
                        )

                        # Trigger coordinator refresh to resume normal operation
                        await coordinator.async_request_refresh()
                        return self.async_create_entry(
                            title="Reauth successful", data={}
                        )
                    else:
                        _LOGGER.warning(
                            "🚫 MFA re-authentication failed - invalid code"
                        )
                        errors["base"] = "invalid_mfa"

                except Exception as e:
                    _LOGGER.error("💥 Reauth MFA verification failed: %s", e)
                    errors["base"] = "invalid_mfa"
            else:
                errors["base"] = "reauth_failed"

        return self.async_show_form(
            step_id="reauth_mfa",
            data_schema=STEP_MFA_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "email": self._reauth_entry.data.get(CONF_EMAIL, "")
            },
        )
