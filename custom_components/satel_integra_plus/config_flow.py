"""Config flow: host/port/code with a live connection test."""
from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_ARM_HOME_MODE,
    CONF_ARM_NIGHT_MODE,
    CONF_FORCE_ARM,
    CONF_CLIMATE_BINDINGS,
    CONF_CODE_PREFIX,
    CONF_COMFORT_OUTPUT,
    CONF_GATE_STATE_ZONES,
    CONF_SKIP_ZONE_PATTERNS,
    CONF_USER_CODE,
    DEFAULT_ARM_HOME_MODE,
    DEFAULT_ARM_NIGHT_MODE,
    DEFAULT_COMFORT_OUTPUT,
    DEFAULT_FORCE_ARM,
    DEFAULT_SKIP_ZONE_PATTERNS,
    DOMAIN,
)
from .pysatel.client import PanelBusyError, SatelClient
from .pysatel.const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

def _parse_number_map(raw: str) -> dict[str, int]:
    """Parse and shape-check a JSON object of number -> number."""
    obj = json.loads(raw or "{}")
    if not isinstance(obj, dict):
        raise ValueError("expected a JSON object")
    return {str(int(k)): int(v) for k, v in obj.items()}


USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USER_CODE): str,
        vol.Optional(CONF_CODE_PREFIX, default=""): str,
    }
)


class SatelIntegraPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            client = SatelClient(user_input[CONF_HOST], user_input[CONF_PORT])
            try:
                await client.connect()
                model = client.panel.model
            except PanelBusyError:
                errors["base"] = "panel_busy"
            except OSError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unexpected error validating connection")
                errors["base"] = "unknown"
            else:
                await client.close()
                return self.async_create_entry(
                    title=f"{model} ({user_input[CONF_HOST]})", data=user_input
                )
            finally:
                await client.close()
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SatelOptionsFlow":
        return SatelOptionsFlow()


class SatelOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = {
                    CONF_ARM_HOME_MODE: user_input[CONF_ARM_HOME_MODE],
                    CONF_ARM_NIGHT_MODE: user_input[CONF_ARM_NIGHT_MODE],
                    CONF_FORCE_ARM: user_input[CONF_FORCE_ARM],
                    CONF_COMFORT_OUTPUT: user_input[CONF_COMFORT_OUTPUT],
                    CONF_SKIP_ZONE_PATTERNS: [
                        p.strip()
                        for p in user_input[CONF_SKIP_ZONE_PATTERNS].split(",")
                        if p.strip()
                    ],
                    CONF_GATE_STATE_ZONES: _parse_number_map(
                        user_input[CONF_GATE_STATE_ZONES]
                    ),
                    CONF_CLIMATE_BINDINGS: _parse_number_map(
                        user_input[CONF_CLIMATE_BINDINGS]
                    ),
                }
            except (json.JSONDecodeError, ValueError, TypeError):
                errors["base"] = "invalid_json"
            else:
                return self.async_create_entry(title="", data=options)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ARM_HOME_MODE,
                    default=opts.get(CONF_ARM_HOME_MODE, DEFAULT_ARM_HOME_MODE),
                ): vol.In([1, 2, 3]),
                vol.Required(
                    CONF_ARM_NIGHT_MODE,
                    default=opts.get(CONF_ARM_NIGHT_MODE, DEFAULT_ARM_NIGHT_MODE),
                ): vol.In([1, 2, 3]),
                vol.Required(
                    CONF_FORCE_ARM,
                    default=opts.get(CONF_FORCE_ARM, DEFAULT_FORCE_ARM),
                ): bool,
                vol.Required(
                    CONF_COMFORT_OUTPUT,
                    default=opts.get(CONF_COMFORT_OUTPUT, DEFAULT_COMFORT_OUTPUT),
                ): int,
                vol.Required(
                    CONF_SKIP_ZONE_PATTERNS,
                    default=", ".join(
                        opts.get(CONF_SKIP_ZONE_PATTERNS, DEFAULT_SKIP_ZONE_PATTERNS)
                    ),
                ): str,
                vol.Required(
                    CONF_GATE_STATE_ZONES,
                    default=json.dumps(opts.get(CONF_GATE_STATE_ZONES, {})),
                ): str,
                vol.Required(
                    CONF_CLIMATE_BINDINGS,
                    default=json.dumps(opts.get(CONF_CLIMATE_BINDINGS, {})),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
