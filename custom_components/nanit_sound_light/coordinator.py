"""Data update coordinator for Nanit Sound + Light integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (  # noqa: F401  # CONF_PASSWORD kept for legacy-entry migration
    CONF_EMAIL,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthenticationError, SoundLightAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# How long to gather rapid-fire commands before flushing them as one combined
# message. A HA scene fires its member entities within the same event-loop tick,
# so a short window collapses power + sound + volume + light into one write.
COMMAND_COALESCE_DELAY = 0.15  # seconds

# After a command we "pin" the fields it set for a short window so a stale
# device echo can't flap them back (the device, or a racing confirmation ping,
# can briefly report the pre-command value). A pin is released early the moment
# the device confirms our value, so a genuine later external change isn't
# blocked. The id stamped on each message is NOT used for correlation by either
# the device or this integration, so this time-based guard — not the id — is
# what actually prevents the "turn on → flaps off" race.
COMMAND_PIN_SECONDS = 3.0

# This is a cloud_push integration: real-time state arrives over the websocket
# (_on_device_state_change). The periodic poll is only a backup nudge, so it
# never busy-waits — it waits briefly only when a device has no state yet (first
# poll, or after a reconnect lost it), capped by these.
INITIAL_STATE_ATTEMPTS = 6  # × interval ≈ 3s max
INITIAL_STATE_INTERVAL = 0.5  # seconds


class NanitSoundLightCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Nanit Sound + Light API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=30
            ),  # 30 seconds - faster polling as backup for WebSocket events
        )
        self.config_entry = config_entry
        # Use shared session from Home Assistant
        session = async_get_clientsession(hass)
        self.api = SoundLightAPI(session)

        # Validate configuration
        if not self.validate_config():
            raise ValueError("Invalid configuration data")

        # Migrate legacy entries that have CONF_PASSWORD persisted on disk
        # (pre-2026-05 versions stored it for silent re-auth). Strip it so
        # the on-disk .storage/core.config_entries no longer holds the
        # plaintext password.
        if CONF_PASSWORD in self.config_entry.data:
            new_data = {
                k: v for k, v in self.config_entry.data.items() if k != CONF_PASSWORD
            }
            hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            _LOGGER.info(
                "Removed legacy plaintext password from config entry data "
                "(refresh_token is sufficient for re-authentication)"
            )

        # Initialize API with the email + refresh_token only. Password lives
        # in memory only after a successful authenticate() call within this
        # session — it is never loaded from disk. If HA restarts and the
        # refresh token has been rejected, the integration will require the
        # user to remove + re-add it (rare; refresh tokens are long-lived
        # and rotated on every successful login).
        email = self.config_entry.data[CONF_EMAIL]
        refresh_token = self.config_entry.data.get("refresh_token")

        self.api._stored_email = email
        if refresh_token:
            self.api._refresh_token = refresh_token

        # Persist rotated refresh tokens back to the config entry.
        self.api.set_token_update_callback(self.update_stored_refresh_token)
        # Reauth is driven by ConfigEntryAuthFailed from _async_update_data (HA
        # opens the reauth flow), so no manual MFA callback is registered here.

        self._devices: list[dict[str, Any]] = []
        self._device_states: dict[str, dict[str, Any]] = {}
        self._last_colors: dict[
            str, dict[str, Any]
        ] = {}  # Remember last color for each device

        # Command coalescing: a HA scene applies the Sound + Light's power,
        # sound, volume and light as four separate entities, which fire nearly
        # simultaneously. Sending four racing protobuf messages let their
        # out-of-order responses clobber each other (device ended up off after
        # a "turn on" scene). We instead accumulate fields arriving within a
        # short window and flush them as ONE combined Settings message — the
        # same "apply a preset" pattern the official app uses.
        self._pending_commands: dict[str, dict[str, Any]] = {}
        self._flush_handles: dict[str, asyncio.TimerHandle] = {}
        # Per device: {device_field: (commanded_value, expiry_loop_time)} — see
        # COMMAND_PIN_SECONDS. And a snapshot of pre-command values so a failed
        # send can be rolled back instead of leaving the UI showing a state the
        # device never accepted.
        self._pinned_fields: dict[str, dict[str, tuple[Any, float]]] = {}
        self._rollback_snapshot: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _has_usable_state(state: dict[str, Any]) -> bool:
        """True once the device has reported real state (not just defaults)."""
        keys = ("brightness", "volume", "current_sound", "hue", "is_on")
        return bool(state) and any(state.get(k) is not None for k in keys)

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library."""
        try:
            # Ensure authentication is valid (will refresh the token if needed).
            if not await self.api.ensure_authenticated():
                if self.api.needs_reauth():
                    # The user must act (MFA, or a rejected refresh token with no
                    # stored password). Raising ConfigEntryAuthFailed makes HA
                    # start the reauth flow.
                    raise ConfigEntryAuthFailed(
                        "Re-authentication required for Nanit Sound + Light"
                    )
                # Otherwise it's transient (network blip or auth backoff): keep
                # serving cached data, or report not-ready if we have none yet.
                if self.data:
                    _LOGGER.warning(
                        "Authentication temporarily unavailable - using cached data"
                    )
                    return self.data
                raise UpdateFailed("Authentication temporarily unavailable")

            # Get device list if needed
            if not self._devices:
                self._devices = await self.api.get_sound_light_devices()

                # Set up real-time state change callback
                self.api.set_state_change_callback(self._on_device_state_change)

                # Connect to all devices
                for device in self._devices:
                    await self.api.connect_device(device)
                    _LOGGER.debug(
                        "Connected to device: %s (%s)",
                        device["speaker_name"],
                        device["speaker_uid"][:8] + "...",
                    )

                    # Also request available sounds after connection
                    baby_uid = device["baby_uid"]
                    await self.api.send_saved_sounds_request(baby_uid)

            # Refresh device state. Nudge each device with a ping; the push
            # callback applies the response. Only wait when we have no state for
            # a device yet — never busy-wait on every poll.
            for device in self._devices:
                baby_uid = device["baby_uid"]
                try:
                    await self.api.send_ping_for_state(baby_uid)

                    if not self._has_usable_state(self.api.get_device_state(baby_uid)):
                        for _ in range(INITIAL_STATE_ATTEMPTS):
                            await asyncio.sleep(INITIAL_STATE_INTERVAL)
                            if self._has_usable_state(
                                self.api.get_device_state(baby_uid)
                            ):
                                break

                    parsed_state = dict(self.api.get_device_state(baby_uid))

                    # Remember the last real color the device reported.
                    if (
                        not parsed_state.get("no_color", False)
                        and "hue" in parsed_state
                        and "saturation" in parsed_state
                    ):
                        self._last_colors[baby_uid] = {
                            "hue": parsed_state["hue"],
                            "saturation": parsed_state["saturation"],
                            "brightness": parsed_state.get("brightness", 1.0),
                        }

                    # Preserve any prior (incl. still-pinned optimistic) state,
                    # then overlay the polled state honoring command pins so a
                    # slow device echo can't undo a just-issued command.
                    merged = {**device, **self._device_states.get(baby_uid, {})}
                    self._merge_device_state(baby_uid, merged, parsed_state)
                    merged["last_update"] = self.hass.loop.time()
                    self._device_states[baby_uid] = merged

                except Exception as e:
                    _LOGGER.error(
                        "Failed to update device %s (%s): %s",
                        device["speaker_name"],
                        type(e).__name__,
                        e,
                    )

            return {"devices": self._device_states}

        except AuthenticationError as e:
            raise ConfigEntryAuthFailed(f"Authentication failed: {e}")
        except (ConfigEntryAuthFailed, UpdateFailed):
            raise
        except Exception as e:
            raise UpdateFailed(f"Error communicating with API: {e}")

    async def update_stored_refresh_token(self, new_refresh_token: str) -> None:
        """Update the stored refresh token in the config entry."""
        if new_refresh_token != self.config_entry.data.get("refresh_token"):
            try:
                new_data = dict(self.config_entry.data)
                new_data["refresh_token"] = new_refresh_token
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                _LOGGER.debug("Updated stored refresh token")
            except Exception as e:
                _LOGGER.warning("Failed to update stored refresh token: %s", e)

    def validate_config(self) -> bool:
        """Validate that we have required configuration data."""
        # Only email is required on disk. The password is no longer
        # persisted — it lives in memory after authenticate() during the
        # session. The refresh_token is optional at validate time because
        # it might not have been issued yet on a fresh entry.
        if (
            CONF_EMAIL not in self.config_entry.data
            or not self.config_entry.data[CONF_EMAIL]
        ):
            _LOGGER.error("Missing required configuration field: %s", CONF_EMAIL)
            return False
        return True

    async def _ping_device_for_state(self, baby_uid: str) -> None:
        """Send ping command to get current device state using protobuf."""
        try:
            await self.api.send_ping_for_state(baby_uid)
            # Wait briefly for response
            await asyncio.sleep(1)
        except Exception as e:
            _LOGGER.debug("Ping failed for %s: %s", baby_uid, e)

    async def async_send_control_command(self, baby_uid: str, **kwargs) -> None:
        """Queue a control command, coalescing concurrent fields into one send.

        Entity services (switch/light/select/number) call this; a scene calls
        several at once. Rather than sending a racing message per field, we
        merge the fields, apply optimistic state for instant UI feedback, and
        schedule a single combined flush.
        """
        _LOGGER.debug(
            "Queuing command for %s: %s",
            (
                self._devices[0]["speaker_name"]
                if self._devices
                else baby_uid[:8] + "..."
            ),
            (
                {k: v for k, v in kwargs.items() if k != "color"}
                if "color" in kwargs
                else kwargs
            ),
        )

        # Merge into any command already pending for this device.
        self._pending_commands.setdefault(baby_uid, {}).update(kwargs)

        # Optimistic UI feedback right away (does not wait for the flush).
        self._apply_optimistic_state(baby_uid, kwargs)

        # (Re)schedule a single flush so a burst collapses into one message.
        handle = self._flush_handles.pop(baby_uid, None)
        if handle is not None:
            handle.cancel()
        self._flush_handles[baby_uid] = self.hass.loop.call_later(
            COMMAND_COALESCE_DELAY,
            lambda: self.hass.async_create_task(self._flush_commands(baby_uid)),
        )

    @staticmethod
    def _command_to_device_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Map a control command's kwargs to the device_data keys it affects."""
        fields: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key == "sound":
                fields["current_sound"] = value
            elif key == "is_on":
                fields["is_on"] = value
            elif key == "brightness":
                fields["brightness"] = value
            elif key == "volume":
                fields["volume"] = value
            elif key == "color":
                if "noColor" in value:
                    fields["no_color"] = value["noColor"]
                if "hue" in value:
                    fields["hue"] = value["hue"]
                if "saturation" in value:
                    fields["saturation"] = value["saturation"]
                if "brightness" in value:
                    fields["brightness"] = value["brightness"]
        return fields

    def _apply_optimistic_state(self, baby_uid: str, kwargs: dict[str, Any]) -> None:
        """Apply a command's fields to coordinator data immediately for snappy UI.

        Also pins each field (so a stale echo can't flap it back) and snapshots
        the prior value (so a failed send can be rolled back).
        """
        if not self.data or baby_uid not in self.data.get("devices", {}):
            return

        device_data = self.data["devices"][baby_uid]
        fields = self._command_to_device_fields(kwargs)
        expiry = self.hass.loop.time() + COMMAND_PIN_SECONDS
        pins = self._pinned_fields.setdefault(baby_uid, {})
        snapshot = self._rollback_snapshot.setdefault(baby_uid, {})

        for key, value in fields.items():
            # Snapshot the pre-command value once per in-flight batch.
            if key not in snapshot:
                snapshot[key] = device_data.get(key)
            device_data[key] = value
            pins[key] = (value, expiry)

        self.async_update_listeners()

    def _merge_device_state(
        self, baby_uid: str, target: dict[str, Any], parsed: dict[str, Any]
    ) -> None:
        """Merge parsed device state into target, honoring active command pins.

        A pinned field is suppressed only while the pin is active AND the
        incoming value contradicts what we commanded; if the device confirms our
        value (or the window lapses) the pin is released so normal updates — and
        genuine external changes — flow again.
        """
        now = self.hass.loop.time()
        pins = self._pinned_fields.get(baby_uid, {})
        for key, value in parsed.items():
            pin = pins.get(key)
            if pin is not None:
                pinned_value, expiry = pin
                if now >= expiry or value == pinned_value:
                    pins.pop(key, None)  # window lapsed or device confirmed
                else:
                    continue  # stale/contradicting echo within window: suppress
            target[key] = value
        if not pins:
            self._pinned_fields.pop(baby_uid, None)

    def _rollback_optimistic_state(self, baby_uid: str) -> None:
        """Undo optimistic state after a failed send so the UI doesn't lie."""
        self._pinned_fields.pop(baby_uid, None)
        snapshot = self._rollback_snapshot.pop(baby_uid, None)
        if not snapshot:
            return
        if self.data and baby_uid in self.data.get("devices", {}):
            device_data = self.data["devices"][baby_uid]
            for key, value in snapshot.items():
                if value is None:
                    device_data.pop(key, None)
                else:
                    device_data[key] = value
            self.async_update_listeners()
        _LOGGER.warning(
            "Rolled back optimistic state for %s after a failed command",
            baby_uid[:8] + "...",
        )

    async def _flush_commands(self, baby_uid: str) -> None:
        """Send all coalesced fields for a device as one combined command."""
        self._flush_handles.pop(baby_uid, None)
        kwargs = self._pending_commands.pop(baby_uid, None)
        if not kwargs:
            return

        try:
            await self.api.send_control_command(baby_uid, **kwargs)
            # One confirmation ping per flush (not one per field).
            await self._ping_device_for_state(baby_uid)
            # Accepted: drop the rollback snapshot (pins expire on their own).
            self._rollback_snapshot.pop(baby_uid, None)
        except Exception as e:
            error_type = type(e).__name__
            _LOGGER.error(
                "Control command failed for %s (%s): %s — rolling back",
                baby_uid[:8] + "...",
                error_type,
                e,
            )
            self._rollback_optimistic_state(baby_uid)

    def get_last_color(self, baby_uid: str) -> dict[str, Any] | None:
        """Get the last known good color for a device."""
        return self._last_colors.get(baby_uid)

    def save_last_color(self, baby_uid: str, color_dict: dict[str, Any]) -> None:
        """Save a user-chosen color as the last color to restore later."""
        if not color_dict.get("noColor", True):  # Only save when color is enabled
            last_color = {
                "hue": color_dict["hue"],
                "saturation": color_dict["saturation"],
                "brightness": color_dict.get("brightness", 1.0),
            }
            self._last_colors[baby_uid] = last_color
            _LOGGER.debug("Saved last color for %s: %s", baby_uid, last_color)

    async def _on_device_state_change(self, baby_uid: str) -> None:
        """Apply a real-time device push directly, without a full re-poll.

        The api layer has already parsed the inbound message into its device
        state, so we merge that into coordinator data and notify listeners.
        The old behaviour fired ``async_request_refresh`` on *every* inbound
        message, and each refresh did a fresh ping with a multi-second wait —
        which amplified the command race instead of settling it.
        """
        _LOGGER.debug("Real-time state change detected for device %s", baby_uid)

        parsed = self.api.get_device_state(baby_uid)
        if not parsed:
            return

        if self.data and baby_uid in self.data.get("devices", {}):
            self._merge_device_state(baby_uid, self.data["devices"][baby_uid], parsed)
            self.async_update_listeners()

    async def async_close(self) -> None:
        """Close the coordinator."""
        # Cancel any pending command flushes so they don't fire after shutdown.
        for handle in self._flush_handles.values():
            handle.cancel()
        self._flush_handles.clear()
        self._pending_commands.clear()
        self._pinned_fields.clear()
        self._rollback_snapshot.clear()

        if self.api:
            await self.api.close()
