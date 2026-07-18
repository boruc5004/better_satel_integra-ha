"""Satel INTEGRA Plus — a satel-first Home Assistant integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CLIMATE_BINDINGS,
    CONF_CODE_PREFIX,
    CONF_GATE_STATE_ZONES,
    CONF_SKIP_ZONE_PATTERNS,
    CONF_USER_CODE,
    DEFAULT_CLIMATE_BINDINGS,
    DEFAULT_GATE_STATE_ZONES,
    DEFAULT_SKIP_ZONE_PATTERNS,
    DOMAIN,
    PLATFORMS,
    SERVICE_REDISCOVER,
    STORAGE_KEY_DISCOVERY,
    STORAGE_VERSION,
)
from .mapping import EntityMap, build_entity_map
from .pysatel.monitor import Discovery, SatelHub

_LOGGER = logging.getLogger(__name__)

FIRST_CONNECT_TIMEOUT = 30.0
DISCOVERY_TIMEOUT = 300.0


@dataclass
class SatelRuntime:
    """Objects shared by all platforms of one config entry."""

    hub: SatelHub
    entity_map: EntityMap


# entry.runtime_data holds a SatelRuntime
SatelConfigEntry = ConfigEntry


def _int_key_map(raw: dict | None) -> dict[int, int]:
    return {int(k): int(v) for k, v in (raw or {}).items()}


def _discovery_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(
        hass, STORAGE_VERSION, f"{STORAGE_KEY_DISCOVERY}_{entry.entry_id}"
    )


async def async_setup_entry(hass: HomeAssistant, entry: SatelConfigEntry) -> bool:
    hub = SatelHub(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        user_code=entry.data.get(CONF_USER_CODE, ""),
        code_prefix=entry.data.get(CONF_CODE_PREFIX, ""),
    )

    store = _discovery_store(hass, entry)
    cached = await store.async_load()
    had_cache = bool(cached and cached.get("outputs"))
    if had_cache:
        hub.discovery = Discovery.from_dict(cached)

    await hub.start()
    timeout = FIRST_CONNECT_TIMEOUT if had_cache else DISCOVERY_TIMEOUT
    if not await _wait_available(hub, timeout):
        await hub.stop()
        raise ConfigEntryNotReady(
            f"could not reach the panel at {entry.data[CONF_HOST]} within "
            f"{timeout:.0f}s (is another integration client connected?)"
        )
    if not had_cache:
        await store.async_save(hub.discovery.as_dict())
        _LOGGER.info("discovery cached (%d outputs)", len(hub.discovery.outputs))

    options = entry.options
    entity_map = build_entity_map(
        hub.discovery,
        gate_state_zones=(
            DEFAULT_GATE_STATE_ZONES
            | _int_key_map(options.get(CONF_GATE_STATE_ZONES))
        ),
        climate_bindings=(
            DEFAULT_CLIMATE_BINDINGS
            | _int_key_map(options.get(CONF_CLIMATE_BINDINGS))
        ),
        skip_zone_patterns=options.get(
            CONF_SKIP_ZONE_PATTERNS, DEFAULT_SKIP_ZONE_PATTERNS
        ),
    )
    entry.runtime_data = SatelRuntime(hub=hub, entity_map=entity_map)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _make_stop_listener(hub)
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SatelConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.hub.stop()
    return ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _make_stop_listener(hub: SatelHub):
    async def _stop(_event) -> None:
        await hub.stop()

    return _stop


async def _wait_available(hub: SatelHub, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while not hub.available:
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0.25)
    return True


@callback
def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REDISCOVER):
        return

    async def _rediscover(_call: ServiceCall) -> None:
        """Drop the cached device list and re-enumerate the panel."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await _discovery_store(hass, entry).async_remove()
                entry.runtime_data.hub.discovery = Discovery()
                await hass.config_entries.async_reload(entry.entry_id)

    hass.services.async_register(DOMAIN, SERVICE_REDISCOVER, _rediscover)
