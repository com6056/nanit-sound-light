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
