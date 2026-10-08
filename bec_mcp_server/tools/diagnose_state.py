"""What the diagnose rules read: a snapshot of cached BEC state, filled by tools/diagnose.py.

Plain data only, so the rules in diagnose_rules.py run against fake state in tests exactly as
they run against a live BEC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServiceState:
    """A service's published status; ``status`` is a BECStatus name such as ``"RUNNING"``."""

    name: str
    status: str | None


@dataclass(frozen=True)
class InitProgress:
    """The device server's latest initialization progress message (one key for all devices)."""

    device: str
    finished: bool
    success: bool
    index: int
    total: int


@dataclass(frozen=True)
class DeviceState:
    """Cached state of one device.

    ``readback_time`` is None when no readback is cached, ``limits`` is None for devices without
    soft limits, and ``moving`` is None when the device publishes no moving status.
    """

    name: str
    enabled: bool | None = None
    is_positioner: bool = False
    readback: Any = None
    readback_time: float | None = None
    limits: tuple[float, float] | None = None
    moving: bool | None = None


@dataclass(frozen=True)
class ConfigReset:
    """One device-config history entry: the session the device server dropped after a failed load."""

    timestamp: float | None
    devices: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueueState:
    """One scan queue: its status and whether it runs or holds a request."""

    name: str
    status: str | None
    pending: int = 0
    running: bool = False


@dataclass(frozen=True)
class LogRecord:
    """One record from the log stream."""

    level: str
    level_no: int
    message: str
    service: str | None = None
    timestamp: float | None = None


@dataclass(frozen=True)
class AlarmRecord:
    """One alarm; ``severity`` is ``"WARNING"``, ``"MINOR"`` or ``"MAJOR"``."""

    severity: str
    message: str
    exception_type: str | None = None
    devices: tuple[str, ...] = ()
    timestamp: float | None = None


@dataclass
class Snapshot:
    """Cached BEC state the rules read.

    A source left as None was not collected and its rules are skipped; a source that failed is
    named in ``unavailable`` instead. ``device_names`` lists every configured device, while
    ``devices`` holds the state of the devices that were collected. ``logs`` and ``alarms`` are
    oldest first.
    """

    now: float
    services: list[ServiceState] | None = None
    device_names: list[str] | None = None
    devices: dict[str, DeviceState] = field(default_factory=dict)
    init_progress: InitProgress | None = None
    config_resets: list[ConfigReset] = field(default_factory=list)
    queues: list[QueueState] | None = None
    logs: list[LogRecord] | None = None
    alarms: list[AlarmRecord] | None = None
    unavailable: dict[str, str] = field(default_factory=dict)
    window_s: float = 3600.0
    stale_after_s: float = 3600.0
