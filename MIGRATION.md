# Migrating to ha-nanit

Nanit Sound + Light has merged into
[ha-nanit](https://github.com/wealthystudent/ha-nanit) (see
[issue #1](https://github.com/com6056/nanit-sound-light/issues/1)). As of
ha-nanit v1.12.0, everything this integration does is in there: the same
transport underneath (local connection with mDNS discovery, cloud relay
fallback, command coalescing), the same entities, and the same sensors.
ha-nanit works with or without a Nanit camera on the account, so a
standalone Sound + Light is fully supported.

Home Assistant cannot hand entities from one integration to another, so
the move is a remove and re-add. Plan for about ten minutes.

## Steps

1. **Write down your current entity ids.** Open Settings, then Devices &
   Services, then Nanit Sound + Light, and click the device. Automations,
   scenes, scripts, dashboards, and long-term statistics all reference
   these ids.
2. **Remove the Nanit Sound + Light integration.** On the same page, open
   the three-dot menu on the config entry and delete it.
3. **Install ha-nanit** through HACS and add the "Nanit" integration from
   Settings, then Devices & Services. Sign in with the same Nanit account
   (email, password, and the MFA code if your account uses one).
4. **Reconnect your automations.** Either update each automation, scene,
   and dashboard to the new entity ids from the table below, or rename the
   new entities back to your old ids (open the entity, click the cog, edit
   "Entity ID"). Renaming is less work and also keeps your history and
   long-term statistics attached, since Home Assistant keys both by entity
   id.

## Entity id mapping

Defaults shown for a speaker named "Nursery" in the Nanit app, with the
baby also named "Nursery". Your names may differ, the pattern is what
matters: the old ids were `<speaker name>_<entity>`, the new ones are
`<baby name>_sound_light_<entity>`.

| Entity          | Old (nanit-sound-light)          | New (ha-nanit)                                    |
| --------------- | -------------------------------- | ------------------------------------------------- |
| Light           | `light.nursery_light`            | `light.nursery_sound_light_light`                 |
| Power switch    | `switch.nursery_power`           | `switch.nursery_sound_light_power`                |
| Sound selector  | `select.nursery_sound`           | `select.nursery_sound_light_sound_track`          |
| Volume          | `number.nursery_volume`          | `number.nursery_sound_light_volume`               |
| Temperature     | `sensor.nursery_temperature`     | `sensor.nursery_sound_light_temperature`          |
| Humidity        | `sensor.nursery_humidity`        | `sensor.nursery_sound_light_humidity`             |
| Battery         | `sensor.nursery_battery`         | `sensor.nursery_sound_light_battery`              |
| Charging        | `binary_sensor.nursery_charging` | `binary_sensor.nursery_sound_light_charging`      |
| WiFi signal     | `sensor.nursery_signal_strength` | `sensor.nursery_sound_light_wifi_signal_strength` |
| Firmware        | `sensor.nursery_firmware`        | `sensor.nursery_sound_light_firmware`             |
| Connection type | `sensor.nursery_connection_type` | `sensor.nursery_sound_light_connection_mode`      |

New entities you gain with ha-nanit: a dedicated sound on/off switch
(`switch.nursery_sound_light_sound`, so the selector no longer doubles as
the off switch) and a connectivity diagnostic sensor. If you also own a
Nanit camera, all the camera features arrive with the same config entry.

## Notes

- **Manual speaker IP**: if you had a fixed IP configured here, set it
  again in ha-nanit under the integration's Configure dialog. With mDNS
  discovery you probably don't need it at all.
- **The speaker allows one local client at a time.** Remove this
  integration before adding ha-nanit (the steps above already do this).
  If both run at once, the second one falls back to the cloud relay until
  the local slot frees up.
- **The WiFi signal sensor is disabled by default** in ha-nanit. Enable it
  from the entity registry if you used it.
- Something not covered here: open an issue on
  [ha-nanit](https://github.com/wealthystudent/ha-nanit/issues), which is
  where Sound + Light support is maintained now.

Thanks for running Nanit Sound + Light, and see you over in ha-nanit!
