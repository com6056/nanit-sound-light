# Nanit Sound + Light Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]

_Control your Nanit Sound + Light devices directly from Home Assistant._

**This integration focuses exclusively on Nanit Sound + Light devices** and provides control over lighting, sound, power, and environmental monitoring. (It does not control Nanit cameras.)

## Features

- 💡 **Light control** — brightness and color
- 🔊 **Sound control** — volume and sound selection (the options available on your device)
- ⚡ **Power control** — turn the device on and off
- 🌡️ **Environmental sensors** — temperature and humidity
- 🔄 **Real-time updates** — state changes pushed over a WebSocket, no polling lag
- 🔐 **Secure authentication** — MFA supported, with automatic token refresh and a re-authentication prompt when needed
- 🔁 **Resilient connection** — automatic reconnect with backoff so transient cloud/network drops recover on their own

## Installation

### HACS (Recommended)

1. Ensure that [HACS](https://hacs.xyz/) is installed
2. Add this repository as a custom repository:
   - In HACS, go to "Integrations" → "..." → "Custom repositories"
   - Repository: `https://github.com/com6056/nanit-sound-light`
   - Category: Integration
3. Click "Install" on the "Nanit Sound + Light" integration
4. Restart Home Assistant
5. In the Home Assistant UI, go to "Settings" → "Devices & Services" → "Add Integration" → "Nanit Sound + Light"

### Manual Installation

1. Using the tool of choice, open the directory (folder) for your HA configuration (where you find `configuration.yaml`)
2. If you do not have a `custom_components` directory there, create it
3. In the `custom_components` directory create a new folder called `nanit_sound_light`
4. Download _all_ the files from the `custom_components/nanit_sound_light/` directory in this repository
5. Place the files you downloaded in the new directory you created
6. Restart Home Assistant
7. In the Home Assistant UI, go to "Settings" → "Devices & Services" → "Add Integration" → "Nanit Sound + Light"

## Configuration

The integration guides you through setup:

1. **Account credentials** — enter your Nanit email and password
2. **MFA (if enabled)** — enter the verification code Nanit emails you
3. **Device discovery** — your Sound + Light devices are discovered automatically

The same Nanit account can only be added once. If your saved session can no longer be refreshed, Home Assistant prompts you to re-enter your password (and MFA, if required) — no need to delete and re-add the integration.

## Supported Entities

Each Sound + Light device is exposed as several entities:

| Entity Type | Description                                   |
| ----------- | --------------------------------------------- |
| **Light**   | Brightness (0–100%) and color (HS)            |
| **Switch**  | Device power on/off                           |
| **Number**  | Volume (0–100%)                               |
| **Select**  | Sound selection (the device's available list) |
| **Sensor**  | Temperature and humidity                      |

Entities go **unavailable** when the device can't be reached, rather than showing the last-known values as if they were live.

## Troubleshooting

### Debug Logging

If you encounter issues, enable debug logging by adding this to your `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.nanit_sound_light: debug
```

Then restart Home Assistant and check **Settings** → **System** → **Logs**.

### Common Issues

| Issue                 | Solution                                                         |
| --------------------- | ---------------------------------------------------------------- |
| Authentication failed | Re-enter your password when Home Assistant prompts for reauth    |
| Invalid MFA code      | Use the latest verification code from your email (not SMS)       |
| No devices found      | Ensure the device is paired and online in the Nanit app          |
| Entity unavailable    | Usually a transient cloud/network drop; it reconnects on its own |

### Getting Help

When reporting issues, please include:

- Debug logs showing the error
- Home Assistant version
- Steps to reproduce the issue

## Contributions

Contributions are welcome! Please open an issue or submit a Pull Request. Run the test suites with `./tests/run.sh` and `./tests_ha/run.sh` (both run in a throwaway container and never touch a real device).

## Credits

- **Original Nanit integration**: [@indiefan](https://github.com/indiefan) — [home_assistant_nanit](https://github.com/indiefan/home_assistant_nanit)
- **Sound + Light protocol**: independently reverse-engineered for interoperability

This integration builds on the foundational work of the original Nanit integration while focusing specifically on Sound + Light devices.

---

[releases-shield]: https://img.shields.io/github/release/com6056/nanit-sound-light.svg?style=for-the-badge
[releases]: https://github.com/com6056/nanit-sound-light/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/com6056/nanit-sound-light.svg?style=for-the-badge
[commits]: https://github.com/com6056/nanit-sound-light/commits/main
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/com6056/nanit-sound-light.svg?style=for-the-badge
