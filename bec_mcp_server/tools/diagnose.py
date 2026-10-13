"""bec_diagnose and bec_device_diagnose: deterministic findings about the beamline or one device.

bec_diagnose answers "what is wrong?" in one call. It merges the findings of the tools that own
each area (bec_status for services, bec_queue and bec_beamline_states, bec_scan_history and the
alarms of bec_logs) with the rules in ``diagnose_rules`` for what no other tool reports: the
devices across the beamline and recent log errors. Findings come back ordered by severity, then
area, each naming the tool to call next.

bec_device_diagnose runs the per-device rules: disabled, failed initialization, moving, stale or
missing readback, outside its limits, config resets, and errors or alarms naming the device.

Everything is read from cache through the server's one BECClient: readbacks, limits and moving
status are what the device server last published. A source that cannot be read becomes a
finding of its own, so one broken read never hides the rest of the diagnosis.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

import redis.exceptions
from bec_lib.device import Positioner
from bec_lib.endpoints import MessageEndpoints
from pydantic import Field

from bec_mcp_server.bec import compat
from bec_mcp_server.bec.connection import BecConnection, BecUnavailableError
from bec_mcp_server.bec.digest import jsonable
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding
from bec_mcp_server.tools import diagnose_rules as rules
from bec_mcp_server.tools import diagnose_state as state
from bec_mcp_server.tools import logs, queue_states, scans, status

#: finished scans bec_diagnose checks for ones that did not close cleanly
SCAN_HISTORY_DEPTH = 5

#: queue history bec_diagnose checks; only the last finished request yields a finding
QUEUE_HISTORY_DEPTH = 1

#: config-history entries checked for resets; the device server keeps the last 50
CONFIG_HISTORY_DEPTH = 10

DEFAULT_WINDOW_MINUTES = 60
MAX_WINDOW_MINUTES = 24 * 60
MAX_FINDINGS = 20

#: queue-item statuses that mean the head of a queue is being worked on
_ACTIVE_ITEM_STATUSES = {"RUNNING", "PAUSED", "DEFERRED_PAUSE"}

#: Redis failures end the whole call (BecConnection marks the client broken); anything else only
#: makes one source unavailable
_REDIS_FAILURES = (
    BecUnavailableError,
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    redis.exceptions.BusyLoadingError,
)

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

T = TypeVar("T")


# ---------------------------------------------------------------------------
# reads through the client's connector; bec_lib has no public accessor for these
# ---------------------------------------------------------------------------


def read_device_readback(client: Any, name: str) -> Any:
    """The readback the device server last published for ``name`` (what read(cached=True) uses)."""
    return client.connector.get(MessageEndpoints.device_readback(name))


def read_device_status(client: Any, name: str) -> Any:
    """The moving status the device server publishes from a positioner's motor_is_moving."""
    return client.connector.get(MessageEndpoints.device_status(name))


def read_init_progress(client: Any) -> Any:
    """The device server's latest initialization progress; one key for all devices, 1 h expiry."""
    return client.connector.get(MessageEndpoints.device_initialization_progress())


def read_config_history(client: Any, count: int = CONFIG_HISTORY_DEPTH) -> list[Any]:
    """Sessions the device server dropped after a failed or cancelled load, newest first."""
    return client.connector.lrange(MessageEndpoints.device_config_history(), 0, count - 1) or []


# ---------------------------------------------------------------------------
# BEC messages to snapshot entries
# ---------------------------------------------------------------------------


def _safe(getter: Callable[[], T], default: T | None = None) -> T | None:
    try:
        return getter()
    except _REDIS_FAILURES:
        raise
    except Exception:  # pylint: disable=broad-except
        return default


def _status_name(message: Any) -> str | None:
    value = getattr(message, "status", None)
    if value is None:
        return None
    return getattr(value, "name", None) or str(jsonable(value))


def services_from(client: Any) -> list[state.ServiceState]:
    """Every service publishing a status, through the public ``client.service_status``."""
    statuses = dict(client.service_status)
    return [state.ServiceState(name, _status_name(message)) for name, message in statuses.items()]


def main_reading(message: Any, name: str) -> tuple[Any, float | None]:
    """The device's own signal (named like the device) from a readback message, else the first."""
    signals = getattr(message, "signals", None)
    if not isinstance(signals, dict) or not signals:
        return None, None
    entry = signals[name] if name in signals else signals[sorted(signals)[0]]
    if not isinstance(entry, dict):
        return jsonable(entry), None
    timestamp = entry.get("timestamp")
    valid = isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
    return jsonable(entry.get("value")), float(timestamp) if valid else None


def _limits(device: Any) -> tuple[float, float] | None:
    """The soft limits as published (BEC reports [0, 0] when none are set)."""
    try:
        low, high = (float(jsonable(value)) for value in _safe(lambda: device.limits))
    except (TypeError, ValueError):
        return None
    return low, high


def device_state(client: Any, name: str, device: Any, *, read_values: bool) -> state.DeviceState:
    """One device's cached state; ``read_values`` adds readback, limits and moving status."""
    enabled = _safe(lambda: bool(device.enabled))
    positioner = isinstance(device, Positioner)
    if not read_values:
        return state.DeviceState(name, enabled=enabled, is_positioner=positioner)
    readback, timestamp = main_reading(read_device_readback(client, name), name)
    moving = getattr(read_device_status(client, name), "status", None)
    return state.DeviceState(
        name,
        enabled=enabled,
        is_positioner=positioner,
        readback=readback,
        readback_time=timestamp,
        limits=_limits(device) if positioner else None,
        moving=bool(moving) if isinstance(moving, (int, float)) else None,
    )


def init_progress_from(message: Any) -> state.InitProgress | None:
    """The device server's latest initialization progress message as a snapshot entry."""
    if message is None:
        return None
    return state.InitProgress(
        device=str(message.device),
        finished=bool(message.finished),
        success=bool(message.success),
        index=int(message.index),
        total=int(message.total),
    )


def config_resets_from(messages: list[Any]) -> list[state.ConfigReset]:
    """Config-history entries (newest first, as stored) as resets, oldest first."""
    resets = []
    for message in reversed(messages):
        removed_at = (getattr(message, "metadata", None) or {}).get("removed_at")
        resource = getattr(message, "resource", None)
        entries = resource if isinstance(resource, list) else []
        names = tuple(
            str(entry["name"]) for entry in entries if isinstance(entry, dict) and entry.get("name")
        )
        timestamp = float(removed_at) if isinstance(removed_at, (int, float)) else None
        resets.append(state.ConfigReset(timestamp, names))
    return resets


def queues_from(client: Any) -> list[state.QueueState]:
    """Each scan queue's status and whether it runs or holds a request."""
    queue = getattr(client, "queue", None)
    if queue is None:
        raise RuntimeError("the BEC client has no scan queue")
    compat.refresh_queue(queue.queue_storage)
    queues = []
    for name, scan_queue in sorted(dict(queue.queue_storage.current_scan_queue or {}).items()):
        info = list(scan_queue.info or [])
        running = bool(info) and info[0].status in _ACTIVE_ITEM_STATUSES
        status_value = scan_queue.status
        queues.append(
            state.QueueState(
                name,
                str(status_value) if status_value is not None else None,
                pending=len(info) - (1 if running else 0),
                running=running,
            )
        )
    return queues


def logs_and_alarms_from(
    messages: list[Any],
) -> tuple[list[state.LogRecord], list[state.AlarmRecord]]:
    """Split the log stream into plain records and the alarms ``raise_alarm`` logged.

    Clients echo the alarms they receive; the raiser's own line already counts them, so echoes
    are dropped.
    """
    records: list[state.LogRecord] = []
    alarms: list[state.AlarmRecord] = []
    for message in messages:
        record = logs.parse_log_message(message)
        if record is None:
            continue
        alarm = logs.parse_alarm_line(record.message)
        if alarm is None:
            records.append(
                state.LogRecord(
                    record.level, record.level_no, record.message, record.service, record.timestamp
                )
            )
        elif not alarm.echo:
            alarms.append(
                state.AlarmRecord(
                    alarm.severity,
                    alarm.message,
                    alarm.exception_type,
                    tuple(_NAME_RE.findall(alarm.device or "")),
                    record.timestamp,
                )
            )
    return records, alarms


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def _gather(snapshot: state.Snapshot, source: str, read: Callable[[], T]) -> T | None:
    """Run one read; on failure name the source in ``snapshot.unavailable`` and go on."""
    try:
        return read()
    except _REDIS_FAILURES:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        snapshot.unavailable[source] = f"{type(exc).__name__}: {exc}"
        return None


def _resolve(devices: dict[str, Any], name: str) -> str | None:
    name = (name or "").strip()
    if name in devices:
        return name
    folded = {key.casefold(): key for key in devices}
    return folded.get(name.casefold())


def _collect_devices(
    client: Any, snapshot: state.Snapshot, devices: dict[str, Any], device: str | None
) -> None:
    snapshot.device_names = sorted(devices)
    if device is None:
        # every device's enablement, plus the three cached reads of each positioner
        for name, obj in sorted(devices.items()):
            entry = _gather(
                snapshot,
                f"device {name}",
                lambda name=name, obj=obj: device_state(
                    client, name, obj, read_values=isinstance(obj, Positioner)
                ),
            )
            if entry is not None:
                snapshot.devices[name] = entry
        return
    name = _resolve(devices, device)
    if name is not None:
        snapshot.devices[name] = _gather(
            snapshot,
            f"device {name}",
            lambda: device_state(client, name, devices[name], read_values=True),
        ) or state.DeviceState(name)


def collect(
    client: Any,
    *,
    device: str | None = None,
    window_s: float = DEFAULT_WINDOW_MINUTES * 60,
    log_messages: list[Any] | None = None,
    now: float | None = None,
) -> state.Snapshot:
    """Read everything the rules need; for ``device``, only that device's values.

    ``log_messages`` is the already-read log stream; without it the stream is read here.
    """
    snapshot = state.Snapshot(now=time.time() if now is None else now, window_s=window_s)
    snapshot.services = _gather(snapshot, "the services", lambda: services_from(client))
    devices = _gather(snapshot, "the devices", lambda: dict(client.device_manager.devices))
    if devices is not None:
        _collect_devices(client, snapshot, devices, device)
    snapshot.init_progress = _gather(
        snapshot,
        "the device initialization progress",
        lambda: init_progress_from(read_init_progress(client)),
    )
    snapshot.config_resets = (
        _gather(
            snapshot,
            "the device config history",
            lambda: config_resets_from(read_config_history(client)),
        )
        or []
    )
    snapshot.queues = _gather(snapshot, "the scan queue", lambda: queues_from(client))
    if log_messages is None:
        log_messages = _gather(snapshot, "the log stream", lambda: logs.read_log_stream(client))
    if log_messages is not None:
        split = _gather(snapshot, "the log stream", lambda: logs_and_alarms_from(log_messages))
        if split is not None:
            snapshot.logs, snapshot.alarms = split
    return snapshot


def _section(area: str, read: Callable[[], dict[str, Any]]) -> list[rules.Entry]:
    """Another tool's findings as entries; a failure becomes an incomplete-diagnosis finding."""
    try:
        return rules.tool_entries(area, read())
    except _REDIS_FAILURES:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        incomplete = Finding(
            "warning",
            "diagnose.incomplete",
            f"Could not check the {area}: {type(exc).__name__}: {exc}",
            area,
            "mcp_status",
        )
        return rules.finding_entries([incomplete], "diagnose")


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


def _window_minutes(window_minutes: int) -> int:
    return max(1, min(int(window_minutes), MAX_WINDOW_MINUTES))


def _unreachable(exc: BecUnavailableError) -> dict[str, Any]:
    # "BEC is down" is the diagnosis, not a tool failure
    finding = Finding("error", "bec.unreachable", f"BEC is unreachable: {exc}", "BEC", "mcp_status")
    return rules.report(
        rules.finding_entries([finding]), subject="BEC", data={}, max_findings=MAX_FINDINGS
    )


def _read_log_stream(connection: Any, client: Any) -> tuple[logs.LogSource, str | None]:
    """The log records once for both the log rules and bec_logs' alarms, or why it failed.

    They come from the log cache while it follows the stream, else from the stream itself.
    """
    try:
        return logs.cached_or_streamed(connection, client), None
    except _REDIS_FAILURES:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        return logs.LogSource([], logs.LOG_STREAM_CAPACITY, cached=False), (
            f"{type(exc).__name__}: {exc}"
        )


def diagnose(
    connection: BecConnection, window_minutes: int = DEFAULT_WINDOW_MINUTES
) -> dict[str, Any]:
    """Ordered findings across services, devices, queue, beamline states, scans and logs."""
    minutes = _window_minutes(window_minutes)
    try:
        services = status.status(connection)
        with connection.use() as client:
            source, log_error = _read_log_stream(connection, client)
            snapshot = collect(client, window_s=minutes * 60.0, log_messages=source.records)
            if log_error is not None:
                snapshot.unavailable["the log stream"] = log_error
            entries = rules.tool_entries("services", services)
            entries += rules.finding_entries(rules.device_overview_findings(snapshot))
            entries += _section(
                "queue", lambda: queue_states.queue_digest(client, QUEUE_HISTORY_DEPTH)
            )
            entries += _section("states", lambda: queue_states.beamline_states_digest(client))
            entries += _section(
                "scans", lambda: scans.scan_history_digest(client, SCAN_HISTORY_DEPTH)
            )
            entries += rules.finding_entries(rules.log_findings(snapshot))
            if log_error is None:
                entries += _section(
                    "logs",
                    lambda: logs.logs_digest(
                        source.records,
                        logs.read_client_alarms(client),
                        minutes=minutes,
                        limit=1,
                        stream_capacity=source.capacity,
                    ),
                )
            entries += rules.finding_entries(rules.unavailable_findings(snapshot))
    except BecUnavailableError as exc:
        return _unreachable(exc)
    return rules.report(
        entries, subject="the beamline", data=rules.overview(snapshot), max_findings=MAX_FINDINGS
    )


def device_diagnose(
    connection: BecConnection, device: str = "", window_minutes: int = DEFAULT_WINDOW_MINUTES
) -> dict[str, Any]:
    """Findings for one device: disabled, failed init, moving, stale, limits, errors, alarms.

    Without a device name it answers for all devices at once, so a bare call still helps.
    """
    device = (device or "").strip()
    window_s = _window_minutes(window_minutes) * 60.0
    try:
        with connection.use() as client:
            cache = getattr(connection, "log_cache", None)
            cached = cache.view().records if cache is not None and cache.following else None
            snapshot = collect(
                client, device=device or None, window_s=window_s, log_messages=cached
            )
    except BecUnavailableError as exc:
        return _unreachable(exc)
    if not device:
        return rules.diagnose_devices(snapshot, max_findings=MAX_FINDINGS)
    name = next(iter(snapshot.devices), device)
    return rules.diagnose_device(snapshot, name, max_findings=MAX_FINDINGS)


# ---------------------------------------------------------------------- MCP registration

WindowMinutes = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_WINDOW_MINUTES,
        description="How far back to look for log errors, alarms and config resets, in minutes.",
    ),
]


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_diagnose and bec_device_diagnose; ``call`` offloads under the deadline."""

    @server.tool(
        name="bec_diagnose",
        description="Ordered findings about what is wrong with the beamline across services, "
        "devices, queue, beamline states, recent scans and logs, each with the tool to call next.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_diagnose(
        window_minutes: WindowMinutes = DEFAULT_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        return await call(diagnose, connection, window_minutes)

    @server.tool(
        name="bec_device_diagnose",
        description="Findings about what is wrong with one device, such as disabled, failed to "
        "initialize, moving, stale readback, outside its limits, or named in recent errors and "
        "alarms.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_device_diagnose(
        device: Annotated[
            str, Field(description="Device name, e.g. samx; leave empty to check all devices.")
        ] = "",
        window_minutes: WindowMinutes = DEFAULT_WINDOW_MINUTES,
    ) -> dict[str, Any]:
        return await call(device_diagnose, connection, device, window_minutes)
