"""HA-free checks for the roller start staggering option."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.satel_integra_plus.const import (
    CONF_ROLLER_START_DELAY,
    DEFAULT_ROLLER_START_DELAY,
    MAX_ROLLER_START_DELAY,
)

ROOT = Path(__file__).parent.parent
COMPONENT = ROOT / "custom_components" / "satel_integra_plus"


def test_roller_start_delay_constants_are_backward_compatible() -> None:
    assert CONF_ROLLER_START_DELAY == "roller_start_delay"
    assert DEFAULT_ROLLER_START_DELAY == 0.0
    assert MAX_ROLLER_START_DELAY == 10.0


def test_all_option_translations_include_roller_start_delay() -> None:
    expected = {
        "strings.json": (
            "Delay between individual roller starts in seconds "
            "(0 disables staggering)"
        ),
        "translations/en.json": (
            "Delay between individual roller starts in seconds "
            "(0 disables staggering)"
        ),
        "translations/pl.json": (
            "Opóźnienie między startami poszczególnych rolet w sekundach "
            "(0 wyłącza sekwencję)"
        ),
    }
    for relative_path, label in expected.items():
        data = json.loads((COMPONENT / relative_path).read_text(encoding="utf-8"))
        assert (
            data["options"]["step"]["init"]["data"][
                CONF_ROLLER_START_DELAY
            ]
            == label
        )


def test_options_flow_preserves_and_validates_roller_start_delay() -> None:
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    assert (
        "CONF_ROLLER_START_DELAY: user_input[CONF_ROLLER_START_DELAY]"
        in source
    )
    assert "CONF_ROLLER_START_DELAY," in source
    assert (
        "vol.Required(\n"
        "                    CONF_ROLLER_START_DELAY,\n"
        "                    default=opts.get(\n"
        "                        CONF_ROLLER_START_DELAY,\n"
        "                        DEFAULT_ROLLER_START_DELAY,\n"
        "                    ),\n"
        "                ): vol.All("
        in source
    )
    assert "vol.Coerce(float)" in source
    assert "vol.Range(min=0.0, max=MAX_ROLLER_START_DELAY)" in source
