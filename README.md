# Nanit Sound + Light for Home Assistant

A Home Assistant custom integration for **Nanit Sound + Light devices only**.

## Features ✅ **ALL WORKING & TESTED**

- **✅ Power Control** - Turn Sound + Light device on/off (confirmed working)
- **✅ Brightness Control** - LED brightness adjustment (0-100%, confirmed working)
- **✅ Color Control** - HSB color adjustment (hue, saturation, brightness, confirmed working)
- **✅ Volume Control** - Sound volume adjustment (0-100%, confirmed working)  
- **✅ Sound Selection** - Choose from 11 built-in sounds (confirmed working with "Lullaby", "White Noise", etc.)
- **✅ Multi-Factor Authentication** - Full MFA support for Nanit accounts
- **✅ Temperature & Humidity Sensors** - Environmental monitoring

## Installation

### HACS (Recommended)

This integration is available through HACS (Home Assistant Community Store).

1. Install HACS if you haven't already
2. Add this repository to HACS as a custom repository:
   - Go to HACS → Integrations → ⋮ → Custom repositories
   - Add repository URL: `https://github.com/com6056/nanit-sound-light`
   - Category: Integration
3. Install "Nanit Sound + Light" from HACS
4. Restart Home Assistant
5. Go to Settings → Devices & Services → Add Integration
6. Search for "Nanit Sound + Light" and configure with your Nanit credentials

### Manual Installation

1. Copy the `custom_components/nanit_sound_light` folder to your Home Assistant `custom_components` directory
2. Restart Home Assistant
3. Add the integration through the UI

## Configuration

1. **Email & Password**: Your Nanit account credentials
2. **MFA Support**: If you have multi-factor authentication enabled, you'll be prompted for the verification code
3. **Device Discovery**: The integration will automatically find your Sound + Light devices

## Supported Devices

- **Nanit Sound + Light** - All models with environmental sensors

## Why Sound + Light Only?

This integration focuses exclusively on Sound + Light devices to provide:
- **Reliable protobuf communication** with `wss://remote.nanit.com/speakers`
- **Complete device control** - all major functions working and tested
- **Real-time state synchronization** via configuration response parsing  
- **Clean architecture** without camera complexity
- **Pure protobuf implementation** - no manual hex building

## ✅ **Verified Working Status**

All controls have been **tested and confirmed working** on actual hardware:
- **Power ON/OFF**: Device responds immediately ✅
- **Brightness 0-100%**: Light visibly changes ✅  
- **Volume 0-100%**: Sound volume audibly changes ✅
- **Sound Selection**: Successfully changed to "Lullaby", "White Noise" ✅
- **Color Control**: Light visibly changed to red color ✅

## Troubleshooting

### **Enable Debug Logging**

Add this to your Home Assistant `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.nanit_sound_light: debug
```

Then restart Home Assistant and check the logs.

### **Key Debug Log Patterns**

#### **🔗 Connection Issues:**
```
INFO - Setting up Nanit Sound + Light integration
INFO - Connected to device: Sound and Light  
INFO - Authentication successful
```

#### **🎛️ Control Commands:**
```
DEBUG - Sent protobuf control for {device}: {'brightness': 0.5} (hex: ...)
DEBUG - Parsed brightness: 0.500
DEBUG - Updated device {name} state: brightness=0.500, volume=0.400, power=True
```

#### **📡 WebSocket Communication:**
```
INFO - Connected to Sound + Light device: L151AMN2434018
DEBUG - Received 49 bytes on {device}_speaker
DEBUG - Successfully parsed protobuf response for {device}
DEBUG - Response fields: ['configData']
```

#### **❌ Common Issues:**
- **"Authentication failed"** → Check credentials, MFA code
- **"No Sound + Light devices found"** → Check device is paired in Nanit app
- **"No WebSocket connection"** → Check network, device online status
- **"Protobuf parsing failed"** → New message format, check hex dump

### **Troubleshooting Steps**

1. **Enable debug logging** (see above)
2. **Restart Home Assistant** to apply logging changes
3. **Check logs** in Settings → System → Logs
4. **Look for specific error patterns** above
5. **Report issues** with debug logs showing connection and parsing details

## Credits

- **Original Nanit integration**: [@indiefan](https://github.com/indiefan) - [home_assistant_nanit](https://github.com/indiefan/home_assistant_nanit)
- **Authentication flow**: Based on working implementation from original integration
- **WebSocket patterns**: Learned from original Nanit camera WebSocket implementation
- **Sound + Light protobuf**: Reverse-engineered from APK analysis and hex dump analysis
- **APK analysis techniques**: Inspired by original developer notes and reverse engineering approach

This integration builds upon the foundational work of the original Nanit integration while focusing specifically on Sound + Light devices.