"""Constants for the Better Satel INTEGRA integration."""
from __future__ import annotations

DOMAIN = "satel_integra_plus"

CONF_USER_CODE = "user_code"
CONF_CODE_PREFIX = "code_prefix"
CONF_ARM_HOME_MODE = "arm_home_mode"
CONF_ARM_NIGHT_MODE = "arm_night_mode"
CONF_GATE_STATE_ZONES = "gate_state_zones"
CONF_CLIMATE_BINDINGS = "climate_bindings"
CONF_COMFORT_OUTPUT = "comfort_output"
CONF_SKIP_ZONE_PATTERNS = "skip_zone_patterns"

CONF_FORCE_ARM = "force_arm"

DEFAULT_ARM_HOME_MODE = 3
DEFAULT_ARM_NIGHT_MODE = 2
DEFAULT_COMFORT_OUTPUT = 0  # 0 = no comfort/eco preset until configured
DEFAULT_FORCE_ARM = True

# Zone-name patterns that never become entities (fnmatch-style). ASW-* are
# Satel wireless wall-button modules whose inputs are panel-internal wiring,
# not sensors. Extend via the integration options for installation-specific
# logic zones.
DEFAULT_SKIP_ZONE_PATTERNS = ["ASW-*"]

# Bindings the protocol cannot discover are matched by normalized name
# (gate output <-> reed-contact zone, thermostat output <-> temperature
# zone). Anything name-matching misses is supplied via the entry options;
# there are no built-in defaults.
DEFAULT_GATE_STATE_ZONES: dict[int, int] = {}
DEFAULT_CLIMATE_BINDINGS: dict[int, int] = {}

SIGNAL_PANEL_UPDATE = f"{DOMAIN}_update"

STORAGE_VERSION = 1
STORAGE_KEY_DISCOVERY = f"{DOMAIN}_discovery"

SERVICE_REDISCOVER = "rediscover"

PLATFORMS = [
    "alarm_control_panel",
    "binary_sensor",
    "climate",
    "cover",
    "light",
    "sensor",
    "switch",
]
