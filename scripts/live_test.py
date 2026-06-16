#!/usr/bin/env python3
"""Manual live test harness — drive the real api against a real device, no HA.

A fast way to validate api.py changes against an actual Sound + Light without a
full Home Assistant deploy. It loads the integration's api/const/protobuf as a
standalone package (no Home Assistant import), authenticates, connects (local +
cloud), and prints the connection status and current state for each device.

SAFE BY DEFAULT: it only connects and reads. Set NANIT_SEND_TEST=1 to also run a
gentle light off→on demo (no volume/sound changes).

Environment:
  NANIT_REFRESH_TOKEN   Nanit refresh token (preferred). OR:
  NANIT_EMAIL / NANIT_PASSWORD   account login (no MFA support — use a refresh
                                 token if your account has MFA enabled).
  NANIT_DEVICE_IP       optional. The speaker's LAN IP (e.g. 192.168.1.50). If
                        set, used directly for the local socket; otherwise the
                        local path relies on the OS resolving "Nanit-<uid>.local"
                        (works on hosts with mDNS/nss-mdns; HA itself resolves it
                        via its bundled zeroconf instead).
  NANIT_SEND_TEST       set to "1" to run the gentle light demo.

Usage:
  NANIT_REFRESH_TOKEN=... NANIT_DEVICE_IP=192.168.1.50 python scripts/live_test.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys
import time
import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# Load api/const/sound_light_pb2 as a synthetic package (no Home Assistant import).
_COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nanit_sound_light"
)
_PKG = "nanit_live"
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_COMPONENT)]
sys.modules[_PKG] = _pkg
for _name in ("const", "sound_light_pb2", "api"):
    _spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{_name}", _COMPONENT / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"{_PKG}.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
api_mod = sys.modules[f"{_PKG}.api"]
logging.getLogger(f"{_PKG}.api").setLevel(logging.DEBUG)


async def _authenticate(api) -> None:
    rt = os.environ.get("NANIT_REFRESH_TOKEN")
    if rt:
        api._refresh_token = rt
    email = os.environ.get("NANIT_EMAIL")
    password = os.environ.get("NANIT_PASSWORD")
    if email and password and not rt:
        await api.authenticate(email, password)
    if not await api.ensure_authenticated():
        raise SystemExit(
            "auth failed — set NANIT_REFRESH_TOKEN (or NANIT_EMAIL/NANIT_PASSWORD)"
        )


async def main() -> None:
    import aiohttp

    device_ip = os.environ.get("NANIT_DEVICE_IP")
    send_test = os.environ.get("NANIT_SEND_TEST") == "1"

    async with aiohttp.ClientSession() as session:
        api = api_mod.SoundLightAPI(session, local_enabled=True)
        if device_ip:

            async def resolver(_speaker_uid: str) -> str:
                return device_ip

            api.set_local_host_resolver(resolver)

        await _authenticate(api)
        devices = await api.get_sound_light_devices()
        if not devices:
            raise SystemExit("no Sound + Light devices on this account")

        for dev in devices:
            baby = dev["baby_uid"]
            print(f"\n=== {dev['speaker_name']} ({dev['speaker_uid']}) ===")
            await api.connect_device(dev)
            for _ in range(60):
                await asyncio.sleep(0.1)
                if api.is_device_attached(baby):
                    break
            local_up = api._transport_connected(api._conn_key(baby, "local"))
            remote_up = api._transport_connected(api._conn_key(baby, "remote"))
            print(
                f"attached={api.is_device_attached(baby)} local={local_up} "
                f"cloud={remote_up} active={api.active_transport(baby)}"
            )
            await asyncio.sleep(2)
            state = api.get_device_state(baby)
            keys = (
                "is_on",
                "brightness",
                "hue",
                "saturation",
                "no_color",
                "current_sound",
                "volume",
                "temperature",
                "humidity",
            )
            print("state:", {k: state.get(k) for k in keys})

            if send_test:
                print("--- gentle light off→on demo ---")
                for label, kw in (
                    ("light off", {"color": {"noColor": True}}),
                    (
                        "light on",
                        {
                            "is_on": True,
                            "brightness": 1.0,
                            "color": {"noColor": False, "hue": 0.1, "saturation": 0.7},
                        },
                    ),
                ):
                    t = time.monotonic()
                    try:
                        await api.send_control_command(baby, **kw)
                        print(
                            f"  [{label}] ok {time.monotonic() - t:.2f}s "
                            f"via {api.active_transport(baby)}"
                        )
                    except Exception as e:  # noqa: BLE001
                        print(f"  [{label}] FAILED {time.monotonic() - t:.2f}s: {e!r}")
                    await asyncio.sleep(1.5)

        await api.close()
        print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
