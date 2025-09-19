# Nanit Sound + Light Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]

_Control your Nanit Sound + Light devices directly from Home Assistant._

**This integration focuses exclusively on Nanit Sound + Light devices** and provides complete control over lighting, sound, and environmental monitoring.

## Features

- 💡 **Light Control** - Full brightness and color adjustment
- 🔊 **Sound Control** - Volume and sound selection with 11+ built-in options
- ⚡ **Power Management** - Complete device on/off control
- 🌡️ **Environmental Sensors** - Temperature and humidity monitoring
- 🔐 **Secure Authentication** - Full MFA support with automatic token refresh
- 🔄 **Real-time Updates** - Instant state synchronization

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

The integration will guide you through the setup process:

1. **Account Credentials** - Enter your Nanit email and password
2. **MFA (if enabled)** - Enter the verification code sent to your email
3. **Device Discovery** - Your Sound + Light devices will be automatically discovered

If your session expires, the integration will automatically handle re-authentication, including MFA if required.

## Supported Entities

| Entity Type | Description |
|-------------|-------------|
| **Light** | Brightness (0-100%) and color control (HSB) |
| **Switch** | Device power on/off |
| **Number** | Volume control (0-100%) |
| **Select** | Sound selection (No sound + 11 built-in sounds) |
| **Sensor** | Temperature and humidity monitoring |

## Why Sound + Light Only?

This integration is specifically designed for Nanit Sound + Light devices to provide:

- **Reliable Communication** - Direct protobuf protocol with Nanit's speaker WebSocket service
- **Complete Control** - All device functions fully implemented and tested
- **Real-time Updates** - Instant state changes without polling delays
- **Clean Architecture** - Focused scope without camera complexity

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

| Issue | Solution |
|-------|----------|
| Authentication failed | Verify credentials; delete and re-add integration if needed |
| Invalid MFA code | Use the 4-digit code from email (not SMS) |
| No devices found | Ensure device is paired and online in the Nanit app |
| Connection timeout | Check network connectivity and device status |

### Getting Help

When reporting issues, please include:
- Debug logs showing the error
- Home Assistant version
- Steps to reproduce the issue

## Contributions

Contributions are welcome! Please feel free to submit a Pull Request.

## Credits

- **Original Nanit integration**: [@indiefan](https://github.com/indiefan) - [home_assistant_nanit](https://github.com/indiefan/home_assistant_nanit)
- **Sound + Light protocol**: Reverse-engineered from APK analysis

This integration builds upon the foundational work of the original Nanit integration while focusing specifically on Sound + Light devices.

---

[releases-shield]: https://img.shields.io/github/release/com6056/nanit-sound-light.svg?style=for-the-badge
[releases]: https://github.com/com6056/nanit-sound-light/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/com6056/nanit-sound-light.svg?style=for-the-badge
[commits]: https://github.com/com6056/nanit-sound-light/commits/main
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/com6056/nanit-sound-light.svg?style=for-the-badge