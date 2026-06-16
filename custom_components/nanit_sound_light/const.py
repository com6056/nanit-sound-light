"""Constants for Nanit Sound + Light integration."""

DOMAIN = "nanit_sound_light"
MANUFACTURER = "Nanit"
MODEL = "Sound + Light"

# Configuration constants
CONF_MFA_CODE = "mfa_code"

# API endpoints (discovered from APK analysis)
NANIT_API_BASE = "https://api.nanit.com"
NANIT_AUTH_URL = f"{NANIT_API_BASE}/login"
NANIT_BABIES_URL = f"{NANIT_API_BASE}/babies"

# Sound + Light WebSocket (discovered from APK analysis)
SOUND_LIGHT_WS_BASE_URL = "wss://remote.nanit.com/speakers"

# LOCAL (LAN) connection (reverse-engineered from the official app). On the same
# network the app talks to the speaker directly over the LAN at a deterministic
# mDNS hostname, bypassing the laggy cloud relay; it falls back to the relay when
# off-network.
# The host is "<prefix><speaker_uid>.local" and the socket is wss:// on port 442
# with NO path (unlike the relay's /<uid>/user_connect/). Local TLS is trust-all
# on the device side, and local auth uses a per-device token (see below), NOT the
# user access token the relay uses.
SOUND_LIGHT_LOCAL_MDNS_PREFIX = "Nanit-"
SOUND_LIGHT_LOCAL_WS_PORT = 442

# Per-speaker device-token endpoint. The local socket authenticates with this
# token (Authorization: token <device_token>), obtained with the user's Bearer
# access token. The exact API version prefix before /speakers is unconfirmed from
# the (obfuscated) decompile — this matches our other endpoints (no prefix, plus
# the nanit-api-version header). Confirm against a live request; a 404 here just
# leaves the integration on the remote relay.
NANIT_DEVICE_TOKEN_URL_TEMPLATE = NANIT_API_BASE + "/speakers/{speaker_uid}/udtokens"
