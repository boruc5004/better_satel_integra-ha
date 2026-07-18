"""Self-contained Satel INTEGRA integration-protocol library (no HA imports)."""
from .client import (
    CommandRefusedError,
    NotConnectedError,
    PanelBusyError,
    ResponseTimeoutError,
    SatelClient,
    SatelError,
)
from .monitor import Discovery, OutputInfo, PartitionInfo, SatelHub, ZoneInfo

__all__ = [
    "CommandRefusedError",
    "Discovery",
    "NotConnectedError",
    "OutputInfo",
    "PanelBusyError",
    "PartitionInfo",
    "ResponseTimeoutError",
    "SatelClient",
    "SatelError",
    "SatelHub",
    "ZoneInfo",
]
