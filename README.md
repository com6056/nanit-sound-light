# Nanit Sound + Light Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]

Control your Nanit Sound + Light from Home Assistant: the light, the sound
machine, power, and the device's sensors. It does not control Nanit cameras.

## Disclaimer

This is an unofficial, community-built integration. It is not affiliated with,
authorized, maintained, sponsored, or endorsed by Nanit or any of its affiliates.
"Nanit" and the Nanit logo are trademarks of their respective owner and are used
here only to identify the hardware this project works with.

The integration talks to the device using a protocol that was independently
reverse-engineered for interoperability. It can break at any time if Nanit changes
its apps, firmware, or services, and it comes with no warranty (see
[LICENSE](LICENSE)). Use at your own risk.

## Features

- **Local connection with automatic cloud fallback.** When Home Assistant and the
  device are on the same network, control goes straight to the device over the LAN,
  which is faster and more reliable than the cloud. If the device is not reachable
  locally, it falls back to the Nanit cloud on its own. There is a toggle in the
  integration's Configure screen (on by default).
- **Light**: brightness and color.
- **Sound**: volume and sound selection (whatever your device offers).
- **Power**: turn the whole device on and off.
- **Environment**: temperature and humidity.
- **Device health**: battery level, charging status, WiFi signal strength (with
  SSID, BSSID, and channel), firmware version, and which connection is currently in
  use (local or cloud).
- **Real-time updates** over a push WebSocket, so state changes show up without
  polling lag.
- **Secure sign-in**: MFA is supported, tokens refresh automatically, and Home
  Assistant prompts you to re-authenticate if a session can no longer be renewed.
  Your password is never written to disk.

## Installation

### HACS (recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. Add this repository as a custom repository (HACS, the three-dot menu, Custom
   repositories): `https://github.com/com6056/nanit-sound-light`, category
   Integration.
3. Install "Nanit Sound + Light".
4. Restart Home Assistant.
5. Go to Settings, Devices & Services, Add Integration, and search for "Nanit
   Sound + Light".

### Manual

1. Copy `custom_components/nanit_sound_light/` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration from Settings, Devices & Services as above.

## Configuration

Setup walks you through it:

1. Enter your Nanit email and password.
2. If your account uses MFA, enter the code Nanit emails you.
3. Your Sound + Light devices are discovered automatically.

A Nanit account can only be added once. If a saved session can no longer be
refreshed, Home Assistant asks for your password again (and MFA if needed) rather
than making you delete and re-add the integration.

After setup, the integration's Configure button has one option, "Use local (LAN)
connection when available." It is on by default and falls back to the cloud
automatically, so most people can leave it alone. Turn it off if your network
cannot resolve the device locally and you would rather skip the local attempt.

## Entities

Each device shows up with these entities:

| Entity        | Name            | What it does                                                    |
| ------------- | --------------- | --------------------------------------------------------------- |
| Light         | (device name)   | Brightness and color (HS)                                       |
| Switch        | Power           | Whole-device power                                              |
| Number        | Volume          | Volume, 0 to 100%                                               |
| Select        | Sound           | Sound selection from the device's list                          |
| Sensor        | Temperature     | Ambient temperature                                             |
| Sensor        | Humidity        | Ambient humidity                                                |
| Sensor        | Battery         | Battery charge level (coarse)                                   |
| Binary sensor | Charging        | Whether the device is charging                                  |
| Sensor        | Signal strength | WiFi RSSI, with SSID/BSSID/channel (diagnostic, off by default) |
| Sensor        | Firmware        | Installed firmware version (diagnostic)                         |
| Sensor        | Connection type | Local or Cloud, the transport currently in use (diagnostic)     |

Turning the light off dims it to zero while leaving the device powered, so white
noise keeps playing. The Power switch controls the whole device.

Entities go unavailable when the device cannot be reached, instead of showing the
last-known values as if they were live.

## Troubleshooting

To capture logs for a problem, add this to `configuration.yaml`, restart, and
reproduce the issue:

```yaml
logger:
  logs:
    custom_components.nanit_sound_light: debug
```

Remove it once you have what you need. Debug logging is verbose and is not meant
to be left on.

| Problem               | What to try                                                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Authentication failed | Re-enter your password when Home Assistant prompts for reauth.                                                           |
| Invalid MFA code      | Use the latest code from your email (not SMS).                                                                           |
| No devices found      | Confirm the device is paired and online in the Nanit app.                                                                |
| Entity unavailable    | Usually a brief network drop. It reconnects on its own.                                                                  |
| Stuck on cloud        | The local path needs HA and the device on the same network with mDNS working. It always works over the cloud regardless. |

When filing an issue, include debug logs, your Home Assistant version, and the
steps to reproduce.

## Contributing

Pull requests and issues are welcome. The test suites run in throwaway containers
and never touch a real device:

```bash
./tests/run.sh        # offline api tests
./tests_ha/run.sh     # Home Assistant fixture tests
./ci.sh               # ruff, prettier, protobuf check, hassfest
```

## Credits

- Original Nanit integration by [@indiefan](https://github.com/indiefan):
  [home_assistant_nanit](https://github.com/indiefan/home_assistant_nanit).
- The Sound + Light protocol was reverse-engineered separately for this project.

This builds on the original integration's groundwork while focusing on the Sound +
Light rather than the cameras.

---

[releases-shield]: https://img.shields.io/github/release/com6056/nanit-sound-light.svg?style=for-the-badge
[releases]: https://github.com/com6056/nanit-sound-light/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/com6056/nanit-sound-light.svg?style=for-the-badge
[commits]: https://github.com/com6056/nanit-sound-light/commits/main
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/com6056/nanit-sound-light.svg?style=for-the-badge
