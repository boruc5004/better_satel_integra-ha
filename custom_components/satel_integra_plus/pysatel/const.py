"""Protocol constants for the Satel INTEGRA integration protocol.

Source: Satel "INT-RS / ETHM-1 integration protocol" (rev 2015-03-19).
"""
from __future__ import annotations

from enum import IntEnum

DEFAULT_PORT = 7094

# ---------------------------------------------------------------- commands
class Cmd(IntEnum):
    # state reads (bitmask answers; * = supports extended 32-byte form)
    ZONES_VIOLATION = 0x00          # *
    ZONES_TAMPER = 0x01             # *
    ZONES_ALARM = 0x02              # *
    ZONES_TAMPER_ALARM = 0x03       # *
    ZONES_ALARM_MEMORY = 0x04       # *
    ZONES_TAMPER_ALARM_MEMORY = 0x05  # *
    ZONES_BYPASS = 0x06             # *
    ZONES_NO_VIOLATION_TROUBLE = 0x07  # *
    ZONES_LONG_VIOLATION_TROUBLE = 0x08  # *
    PARTITIONS_ARMED_SUPPRESSED = 0x09
    PARTITIONS_ARMED = 0x0A
    PARTITIONS_ARMED_MODE_2 = 0x0B
    PARTITIONS_ARMED_MODE_3 = 0x0C
    PARTITIONS_FIRST_CODE = 0x0D
    PARTITIONS_ENTRY_TIME = 0x0E
    PARTITIONS_EXIT_TIME_LONG = 0x0F
    PARTITIONS_EXIT_TIME_SHORT = 0x10
    PARTITIONS_TEMP_BLOCKED = 0x11
    PARTITIONS_BLOCKED_GUARD = 0x12
    PARTITIONS_ALARM = 0x13
    PARTITIONS_FIRE_ALARM = 0x14
    PARTITIONS_ALARM_MEMORY = 0x15
    PARTITIONS_FIRE_ALARM_MEMORY = 0x16
    OUTPUTS_STATE = 0x17            # *
    DOORS_OPENED = 0x18
    DOORS_OPENED_LONG = 0x19
    RTC_AND_STATUS = 0x1A
    PARTITIONS_VIOLATED_ZONES = 0x25
    ZONES_ISOLATE = 0x26            # *
    PARTITIONS_VERIFIED_ALARMS = 0x27
    ZONES_MASKED = 0x28             # *
    ZONES_MASKED_MEMORY = 0x29      # *
    PARTITIONS_ARMED_MODE_1 = 0x2A
    PARTITIONS_WARNING_ALARMS = 0x2B

    # module / panel info
    MODULE_VERSION = 0x7C
    ZONE_TEMPERATURE = 0x7D
    INTEGRA_VERSION = 0x7E
    NEW_DATA = 0x7F

    # control (8-byte user code prefix)
    ARM_MODE_0 = 0x80
    ARM_MODE_1 = 0x81
    ARM_MODE_2 = 0x82
    ARM_MODE_3 = 0x83
    DISARM = 0x84
    CLEAR_ALARM = 0x85
    ZONES_BYPASS_SET = 0x86
    ZONES_BYPASS_UNSET = 0x87
    OUTPUTS_ON = 0x88
    OUTPUTS_OFF = 0x89
    OPEN_DOOR = 0x8A
    FORCE_ARM_MODE_0 = 0xA0
    FORCE_ARM_MODE_1 = 0xA1
    FORCE_ARM_MODE_2 = 0xA2
    FORCE_ARM_MODE_3 = 0xA3
    OUTPUTS_SWITCH = 0x91

    READ_SELF_INFO = 0xE0
    READ_DEVICE_INFO = 0xEE
    RESULT = 0xEF


# Control commands whose answer is a 0xEF result frame (everything else
# echoes its own command byte). Used for response correlation.
RESULT_ANSWERED_CMDS = frozenset(range(0x80, 0x92)) | frozenset(range(0xA0, 0xA4))

# Commands that answer 16 bytes normally / 32 bytes in extended (I256 Plus) form.
EXTENDED_STATE_CMDS = frozenset({
    Cmd.ZONES_VIOLATION, Cmd.ZONES_TAMPER, Cmd.ZONES_ALARM, Cmd.ZONES_TAMPER_ALARM,
    Cmd.ZONES_ALARM_MEMORY, Cmd.ZONES_TAMPER_ALARM_MEMORY, Cmd.ZONES_BYPASS,
    Cmd.ZONES_NO_VIOLATION_TROUBLE, Cmd.ZONES_LONG_VIOLATION_TROUBLE,
    Cmd.OUTPUTS_STATE, Cmd.ZONES_ISOLATE, Cmd.ZONES_MASKED, Cmd.ZONES_MASKED_MEMORY,
})

# Result codes (answer to control commands / errors)
RESULT_OK = 0x00
RESULT_ACCEPTED = 0xFF  # command accepted for processing => success
RESULT_USER_CODE_NOT_FOUND = 0x01
RESULT_NO_ACCESS = 0x02
RESULT_CANNOT_ARM_USE_FORCE = 0x11
RESULT_CANNOT_ARM = 0x12

RESULT_MESSAGES = {
    0x00: "OK",
    0x01: "requesting user code not found",
    0x02: "no access",
    0x03: "selected user does not exist",
    0x04: "selected user already exists",
    0x05: "wrong code or code already exists",
    0x06: "telephone code already exists",
    0x07: "changed code is the same",
    0x08: "other error",
    0x11: "cannot arm, but can use force arm",
    0x12: "cannot arm",
    0xFF: "command accepted",
}

# INTEGRA panel types (0x7E answer byte 0)
INTEGRA_TYPES = {
    0: "INTEGRA 24",
    1: "INTEGRA 32",
    2: "INTEGRA 64",
    3: "INTEGRA 128",
    4: "INTEGRA 128-WRL SIM300",
    132: "INTEGRA 128-WRL LEON",
    66: "INTEGRA 64 Plus",
    67: "INTEGRA 128 Plus",
    72: "INTEGRA 256 Plus",
}
INTEGRA_256_PLUS = 72

# 0xEE device types
class DeviceType(IntEnum):
    PARTITION = 0
    ZONE = 1
    USER = 2
    EXPANDER = 3
    OUTPUT = 4
    ZONE_WITH_PARTITION = 5
    TIMER = 6
    TELEPHONE = 7
    OBJECT = 15
    PARTITION_WITH_OBJECT = 16


# Output functions (DLOADX numbering) that this integration understands.
OUTPUT_FUNC_UNUSED = 0
OUTPUT_FUNC_MONO = 24            # "Przełącznik MONO" (timed pulse)
OUTPUT_FUNC_BI = 25              # "Przełącznik BI" (bistable switch)
OUTPUT_FUNC_ROLLER_UP = 105      # "Roleta w górę"
OUTPUT_FUNC_ROLLER_DOWN = 106    # "Roleta w dół"
OUTPUT_FUNC_THERMOSTAT = 120     # "Termostat" (INTEGRA fw 1.19+)

CONTROLLABLE_OUTPUT_FUNCS = frozenset({
    OUTPUT_FUNC_MONO, OUTPUT_FUNC_BI,
    OUTPUT_FUNC_ROLLER_UP, OUTPUT_FUNC_ROLLER_DOWN,
})

# Zone reaction types of interest (DLOADX numbering)
ZONE_REACTION_24H_WATER_LEAK = 52
ZONE_REACTION_24H_FIRE_SMOKE = 33
ZONE_REACTION_24H_LOW_TEMPERATURE = 56
ZONE_REACTION_NO_ALARM_ACTION = 47

# Temperature reading
TEMP_UNDETERMINED = 0xFFFF


def decode_temperature(raw: int) -> float | None:
    """Decode a 0x7D 16-bit temperature; None when undetermined."""
    if raw == TEMP_UNDETERMINED:
        return None
    return (raw - 110) / 2.0


def encode_user_code(code: str, prefix: str = "") -> bytes:
    """Pack prefix+code digits into the 8-byte BCD field (0xF-padded)."""
    digits = prefix + code
    if not digits.isdigit():
        raise ValueError("user code must contain only digits")
    if len(digits) > 16:
        raise ValueError("prefix + code longer than 16 digits")
    nibbles = [int(d) for d in digits] + [0xF] * (16 - len(digits))
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, 16, 2))
