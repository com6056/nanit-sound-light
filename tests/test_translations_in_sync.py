"""Guard: strings.json and translations/en.json must stay identical.

`strings.json` is the developer/hassfest source; `translations/en.json` is what
Home Assistant actually loads at runtime for a custom integration (entity-state
translations like the Connection Type sensor's Local/Cloud labels come from here,
NOT from strings.json). They must match or the UI silently shows raw values.
"""

from __future__ import annotations

import json
import pathlib

_COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "nanit_sound_light"
)


def test_strings_and_en_translation_match() -> None:
    strings = json.loads((_COMPONENT / "strings.json").read_text())
    en = json.loads((_COMPONENT / "translations" / "en.json").read_text())
    assert strings == en, (
        "strings.json and translations/en.json have drifted — keep them identical "
        "(translations/en.json is the runtime copy)."
    )
