"""Shared test fixtures for the Nanit Sound + Light integration.

Two hard rules these fixtures enforce:

1. **Never touch the real device or the Nanit cloud.** `block_nanit_network`
   (autouse) makes any DNS lookup for a `*.nanit.com` host raise, so a regression
   that accidentally opens a real socket fails loudly instead of poking the device
   while a baby is asleep. Tests talk only to in-process fakes.

2. **No Home Assistant import for the lean layers.** `api.py` depends only on
   aiohttp / websockets / protobuf, so we load it (plus `const` and
   `sound_light_pb2`) as a synthetic package and skip the real package
   ``__init__.py`` (which imports Home Assistant). Coordinator/entity tests that
   genuinely need ``hass`` should use ``pytest-homeassistant-custom-component``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import socket
import sys
import types
from types import SimpleNamespace

import pytest

_COMPONENT_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nanit_sound_light"
)
_SYNTH_PKG = "nanit_sl_under_test"


def _load_api_package() -> SimpleNamespace:
    """Load api/const/sound_light_pb2 under a synthetic package (no HA import)."""
    if _SYNTH_PKG not in sys.modules:
        pkg = types.ModuleType(_SYNTH_PKG)
        pkg.__path__ = [str(_COMPONENT_DIR)]
        sys.modules[_SYNTH_PKG] = pkg
        # Order matters: api.py does `from .const import ...` /
        # `from .sound_light_pb2 import ...`, so those must already be registered.
        for name in ("const", "sound_light_pb2", "api"):
            spec = importlib.util.spec_from_file_location(
                f"{_SYNTH_PKG}.{name}", _COMPONENT_DIR / f"{name}.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"{_SYNTH_PKG}.{name}"] = module
            spec.loader.exec_module(module)
    return SimpleNamespace(
        api=sys.modules[f"{_SYNTH_PKG}.api"],
        pb2=sys.modules[f"{_SYNTH_PKG}.sound_light_pb2"],
    )


@pytest.fixture(scope="session")
def nsl() -> SimpleNamespace:
    """The Nanit api module + its protobuf module, loaded without Home Assistant."""
    return _load_api_package()


@pytest.fixture
def api(nsl: SimpleNamespace):
    """A fresh, offline SoundLightAPI instance (no aiohttp session needed for parsing)."""
    return nsl.api.SoundLightAPI(session=None)


@pytest.fixture(autouse=True)
def block_nanit_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to resolve a Nanit host. Tests must use in-process fakes."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and "nanit.com" in host:
            raise AssertionError(
                f"Test tried to reach the real Nanit network ({host}). "
                "Use a fake/in-process server instead."
            )
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
