"""api.ensure_authenticated bootstrap behavior.

Regression guard for the startup-auth bug: after we stopped persisting the
password, a fresh start carries only the stored refresh token (no access token,
no credentials). ensure_authenticated must use that refresh token. Earlier its
refresh branch was gated on an existing access token, so startup never refreshed
and the entry got stuck on "Authentication temporarily unavailable".
"""

from __future__ import annotations


async def test_ensure_authenticated_bootstraps_from_refresh_token(nsl):
    """Only a refresh token present → ensure_authenticated must refresh."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = None
    api._refresh_token = "stored-refresh-token"
    api._stored_password = None  # not persisted anymore

    calls = {"refresh": 0}

    async def fake_refresh():
        calls["refresh"] += 1
        api._access_token = "fresh-access-token"
        return True

    api._refresh_auth = fake_refresh

    assert await api.ensure_authenticated() is True
    assert calls["refresh"] == 1
    # And this is NOT a reauth situation. We have a working token now.
    assert api.needs_reauth() is False


async def test_rejected_refresh_token_becomes_reauth(nsl):
    """If the refresh token is rejected (cleared), needs_reauth() flips True."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = None
    api._refresh_token = "rejected-token"
    api._stored_password = None

    async def fake_refresh():
        # _refresh_auth clears the token on a 401/404 rejection.
        api._refresh_token = None
        return False

    api._refresh_auth = fake_refresh

    assert await api.ensure_authenticated() is False
    assert api.needs_reauth() is True


async def test_transient_refresh_failure_is_not_reauth(nsl):
    """A transient refresh failure keeps the token → not a reauth situation."""
    api = nsl.api.SoundLightAPI(session=None)
    api._access_token = None
    api._refresh_token = "still-valid-token"
    api._stored_password = None

    async def fake_refresh():
        # Network blip: token kept, just returns False.
        return False

    api._refresh_auth = fake_refresh

    assert await api.ensure_authenticated() is False
    assert api.needs_reauth() is False  # token still present → transient, will retry
