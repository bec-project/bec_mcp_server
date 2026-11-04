"""bec_queue and bec_beamline_states: what is the queue doing, and may scans run?

Both read cached state only: the queue status and history, scan status and scan progress the BEC
services keep in Redis, and the beamline states the state manager publishes. Nothing here asks a
device for a fresh value. ``BeamlineStateManager.show_all`` and ``QueueStorage.describe_queue``
render with rich and are never called; ``show_all`` prints straight to stdout.

Reads with no public client accessor go through the client's own connector, each in one small
``read_*`` helper below so it can be swapped for an accessor in one place. The queue refresh is
the only private access and lives in ``bec.compat``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

from bec_lib.bl_state_manager import BeamlineStateClientBase
from bec_lib.endpoints import MessageEndpoints
from pydantic import Field

from bec_mcp_server.bec import compat
from bec_mcp_server.bec.connection import BecConnection, BecUnavailableError
from bec_mcp_server.bec.digest import age_s
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding, tool_result

if TYPE_CHECKING:  # pragma: no cover
    from bec_lib import messages
    from bec_lib.client import BECClient

#: queued requests listed per queue; the rest are only counted
MAX_QUEUE_ITEMS = 10

#: finished requests the client's queue storage holds after a refresh (newest first)
MAX_HISTORY = 6

#: beamline states listed before the digest switches to a count
MAX_STATES = 50

#: request blocks listed per queue item
_MAX_REQUESTS_PER_ITEM = 3

#: characters kept from a request's arguments, a state's parameters or a state's label
_MAX_TEXT_CHARS = 160

#: lock id the scan server's interlock actor places on the primary queue (bec_server 4.1)
INTERLOCK_LOCK_ID = "ScanInterlockActor"

#: queue-item statuses that mean the head of a queue is being worked on
_ACTIVE_ITEM_STATUSES = {"RUNNING", "PAUSED", "DEFERRED_PAUSE"}

_STATUS_ORDER = ("invalid", "warning", "unknown", "valid")


# ---------------------------------------------------------------------- connector reads
# bec_lib has no accessor for these, so they read the client's own connector. The digests treat
# None as "not available" and still answer.


def read_scan_progress(client: BECClient) -> messages.ProgressMessage | None:
    """Latest progress the scan bundler published; one key, overwritten for every scan."""
    return client.connector.get(MessageEndpoints.scan_progress())


def read_scan_status(client: BECClient, scan_id: str) -> messages.ScanStatusMessage | None:
    """Latest status message of one scan."""
    return client.connector.get(MessageEndpoints.public_scan_info(scan_id))


def read_available_states(client: BECClient) -> list[messages.BeamlineStateConfig] | None:
    """Configured beamline states as the state manager published them."""
    msg = client.connector.get_last(MessageEndpoints.available_beamline_states())
    return msg["data"].states if msg else None


def read_state_message(client: BECClient, name: str) -> messages.BeamlineStateMessage | None:
    """Latest status message of one beamline state, for its timestamp."""
    msg = client.connector.get_last(MessageEndpoints.beamline_state(name))
    return msg["data"] if msg else None


# ---------------------------------------------------------------------- answer shape


def _answer(
    summary: str, findings: list[Finding], data: dict[str, Any], truncated: bool = False
) -> dict[str, Any]:
    ok = all(finding.severity == "info" for finding in findings)
    return tool_result(ok, summary, findings=findings, data=data, truncated=truncated)


def _unreachable(exc: BecUnavailableError) -> dict[str, Any]:
    finding = Finding("error", "bec_unreachable", str(exc), subject="redis", next_tool="mcp_status")
    return tool_result(False, f"BEC is unreachable: {exc}", findings=[finding])


def _clip(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_args(parameter: dict | None) -> str:
    """One line such as ``samx [-5, 5], steps=50, exp_time=0.1`` from a scan request."""
    if not parameter:
        return ""
    args = parameter.get("args")
    if isinstance(args, dict):
        parts = [f"{name} {value}" for name, value in args.items()]
    else:
        parts = [str(value) for value in args or ()]
    kwargs = parameter.get("kwargs") or {}
    parts += [
        f"{key}={value}"
        for key, value in kwargs.items()
        if key != "system_config" and value is not None
    ]
    return _clip(", ".join(parts))


# ---------------------------------------------------------------------- bec_queue


def _device_locks(item: messages.QueueInfoEntry) -> tuple[list[str], list[str]]:
    owned = sorted({dev for block in item.request_blocks for dev in block.owned_device_locks})
    pending = sorted({dev for block in item.request_blocks for dev in block.pending_device_locks})
    return owned, pending


def _queue_item(position: int, item: messages.QueueInfoEntry) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "position": position,
        "status": item.status,
        "queue_id": item.queue_id,
        "requests": [
            {
                "request": block.msg.scan_type,
                "scan_number": block.scan_number,
                "args": _format_args(block.msg.parameter),
            }
            for block in item.request_blocks[:_MAX_REQUESTS_PER_ITEM]
        ],
    }
    if len(item.request_blocks) > _MAX_REQUESTS_PER_ITEM:
        entry["more_requests"] = len(item.request_blocks) - _MAX_REQUESTS_PER_ITEM
    _, pending = _device_locks(item)
    if pending:
        entry["waiting_for_device_locks"] = pending
    return entry


def _queue_entry(scan_queue: messages.ScanQueueStatus) -> dict[str, Any]:
    listed = scan_queue.info[:MAX_QUEUE_ITEMS]
    return {
        "status": scan_queue.status,
        "locks": [{"id": lock.identifier, "reason": lock.reason} for lock in scan_queue.locks],
        "size": len(scan_queue.info),
        "items": [_queue_item(position, item) for position, item in enumerate(listed)],
    }


def _active_item(
    queues: dict[str, messages.ScanQueueStatus],
) -> tuple[str, messages.QueueInfoEntry] | tuple[None, None]:
    for name in sorted(queues, key=lambda name: name != "primary"):
        info = queues[name].info
        if info and info[0].status in _ACTIVE_ITEM_STATUSES:
            return name, info[0]
    return None, None


def _scan_status(client: BECClient, scan_id: str) -> tuple[Any, float | None]:
    """The scan's latest status message and start time, from the client's scan storage first."""
    scan_item = client.queue.scan_storage.find_scan_by_ID(scan_id)
    status_msg = getattr(scan_item, "status_message", None)
    start = getattr(scan_item, "start_time", None)
    if status_msg is None:
        status_msg = read_scan_status(client, scan_id)
    if start is None and status_msg is not None and status_msg.status == "open":
        start = status_msg.timestamp
    return status_msg, start


def _progress(running: dict[str, Any], progress: Any) -> None:
    percent = 100.0 * progress.value / progress.max_value if progress.max_value else None
    running["progress"] = {
        "value": progress.value,
        "max_value": progress.max_value,
        "percent": None if percent is None else round(percent, 1),
        "done": progress.done,
    }
    elapsed = running.get("elapsed_s")
    if elapsed and running.get("scan_status") == "open" and 0 < progress.value < progress.max_value:
        rate = progress.value / elapsed
        running["remaining_s_estimate"] = round((progress.max_value - progress.value) / rate)


def _running(
    client: BECClient, queue_name: str, item: messages.QueueInfoEntry, now: float
) -> dict[str, Any]:
    block = item.active_request_block or (item.request_blocks[0] if item.request_blocks else None)
    running: dict[str, Any] = {"queue": queue_name, "queue_item_status": item.status}
    if block is None:
        return running
    owned, pending = _device_locks(item)
    running.update(
        {
            "request": block.msg.scan_type,
            "is_scan": block.is_scan,
            "scan_number": block.scan_number,
            "scan_id": block.scan_id,
            "args": _format_args(block.msg.parameter),
            "device_locks": {"owned": owned, "pending": pending},
        }
    )
    if not (block.is_scan and block.scan_id):
        return running

    status_msg, start = _scan_status(client, block.scan_id)
    running["scan_status"] = status_msg.status if status_msg else None
    running["num_points"] = status_msg.num_points if status_msg else None
    running["elapsed_s"] = age_s(start, now)
    progress = read_scan_progress(client)
    # the progress key still holds the previous scan's value until the bundler's first update
    if progress is not None and progress.metadata.get("scan_id") == block.scan_id:
        _progress(running, progress)
    else:
        running["progress"] = None
    return running


def _history_entry(msg: messages.ScanQueueHistoryMessage) -> dict[str, Any]:
    info = msg.info
    return {
        "status": msg.status,
        "reason": info.reason,
        "queue": msg.queue,
        "requests": [block.msg.scan_type for block in info.request_blocks],
        "scan_numbers": [number for number in info.scan_number if number is not None],
        "queue_id": msg.queue_id,
    }


def _request_label(running: dict[str, Any]) -> str:
    text = str(running.get("request") or "a request")
    if running.get("scan_number") is not None:
        text += f" #{running['scan_number']}"
    return text


def _describe_running(running: dict[str, Any]) -> str:
    details = []
    if running.get("scan_status"):
        details.append(f"scan {running['scan_status']}")
    progress = running.get("progress")
    if progress:
        points = f"{progress['value']:g}/{progress['max_value']:g}"
        if progress["percent"] is not None:
            points += f", {progress['percent']:g}%"
        details.append(points)
    if running.get("device_locks", {}).get("pending"):
        details.append("waiting for device locks")
    return _request_label(running) + (f" ({'; '.join(details)})" if details else "")


def _queue_findings(name: str, scan_queue: messages.ScanQueueStatus) -> list[Finding]:
    findings = []
    for lock in scan_queue.locks:
        if lock.identifier == INTERLOCK_LOCK_ID:
            findings.append(
                Finding(
                    "error",
                    "queue_locked_by_interlock",
                    f"The {name} queue is locked by the scan interlock ({lock.reason}); nothing "
                    "new starts until the watched beamline states recover",
                    subject=name,
                    next_tool="bec_beamline_states",
                )
            )
        else:
            findings.append(
                Finding(
                    "warning",
                    "queue_locked",
                    f"The {name} queue is locked by {lock.identifier}: {lock.reason}",
                    subject=name,
                )
            )
    if scan_queue.status == "PAUSED":
        waiting = len(scan_queue.info)
        findings.append(
            Finding(
                "warning" if waiting else "info",
                "queue_paused",
                f"The {name} queue is paused with {waiting} request(s) in it; nothing runs until "
                "it is resumed",
                subject=name,
            )
        )
    return findings


def _running_findings(running: dict[str, Any] | None) -> list[Finding]:
    if running is None:
        return []
    findings = []
    label = _request_label(running)
    pending = running.get("device_locks", {}).get("pending")
    if pending:
        findings.append(
            Finding(
                "warning",
                "device_locks_pending",
                f"{label} waits for device locks on {', '.join(pending)}",
                subject=label,
            )
        )
    if running.get("scan_status") == "paused":
        findings.append(Finding("warning", "scan_paused", f"Scan {label} is paused", subject=label))
    return findings


def _history_findings(past: list[dict[str, Any]]) -> list[Finding]:
    if not past or past[0]["status"] == "COMPLETED":
        return []
    last = past[0]
    scans = ", ".join(f"#{number}" for number in last["scan_numbers"]) or "no scan number"
    reason = f" (reason: {last['reason']})" if last["reason"] else ""
    return [
        Finding(
            "warning",
            "last_request_not_completed",
            f"The last finished request ({', '.join(last['requests'])}, {scans}) ended "
            f"{last['status']}{reason}",
            subject=last["queue_id"],
            next_tool="bec_logs",
        )
    ]


def _queue_summary(
    queue_data: dict[str, Any], running: dict[str, Any] | None, past: list[dict[str, Any]]
) -> str:
    parts = []
    for name, entry in queue_data.items():
        if name != "primary" and not entry["size"] and entry["status"] == "RUNNING":
            continue
        text = f"{name} queue {entry['status']}"
        if running and running["queue"] == name:
            text += f", running {_describe_running(running)}"
            if entry["size"] > 1:
                text += f", {entry['size'] - 1} more queued"
        elif entry["size"]:
            text += f", {entry['size']} request(s) waiting"
        else:
            text += ", nothing queued"
        if entry["locks"]:
            text += f", locked by {', '.join(lock['id'] for lock in entry['locks'])}"
        parts.append(text)
    if past:
        last = past[0]
        scans = "".join(f" #{number}" for number in last["scan_numbers"])
        parts.append(f"last finished: {', '.join(last['requests'])}{scans} {last['status']}")
    summary = "; ".join(parts) + "."
    return summary[0].upper() + summary[1:]


def queue_digest(client: BECClient, history: int = 5) -> dict[str, Any]:
    """Queue status and locks, the running request with progress, what waits, recent history."""
    empty = {"queues": {}, "running": None, "history": []}
    if client.queue is None:
        return _answer("The BEC client has no scan queue yet.", [], empty)

    storage = client.queue.queue_storage
    compat.refresh_queue(storage)
    queues = storage.current_scan_queue or {}
    if not queues:
        return _answer("The scan server has not published a queue yet.", [], empty)

    queue_data = {name: _queue_entry(scan_queue) for name, scan_queue in queues.items()}
    truncated = any(len(scan_queue.info) > MAX_QUEUE_ITEMS for scan_queue in queues.values())
    findings = [
        finding
        for name, scan_queue in queues.items()
        for finding in _queue_findings(name, scan_queue)
    ]

    active_queue, active_item = _active_item(queues)
    running = _running(client, active_queue, active_item, time.time()) if active_item else None
    findings += _running_findings(running)

    history = max(0, min(int(history), MAX_HISTORY))
    past = [_history_entry(msg) for msg in (storage.queue_history or [])[:history]]
    findings += _history_findings(past)

    data = {"queues": queue_data, "running": running, "history": past}
    return _answer(_queue_summary(queue_data, running, past), findings, data, truncated)


def queue(connection: BecConnection, history: int = 5) -> dict[str, Any]:
    """bec_queue: borrow the client and digest the queue."""
    try:
        with connection.use() as client:
            return queue_digest(client, history=history)
    except BecUnavailableError as exc:
        return _unreachable(exc)


# ---------------------------------------------------------------------- bec_beamline_states


def _state_status(manager: Any, name: str) -> tuple[str, str]:
    """Status and label through the state manager's public per-state ``get``."""
    state = getattr(manager, name, None) if manager is not None else None
    if not isinstance(state, BeamlineStateClientBase):
        return "unknown", "not loaded by the client yet"
    current = state.get()
    return str(current["status"]), str(current["label"])


def _format_parameters(parameters: dict) -> str:
    shown = {key: value for key, value in parameters.items() if key != "name" and value is not None}
    return _clip(", ".join(f"{key}={value}" for key, value in shown.items()))


def _primary_locks(client: BECClient) -> list[str] | None:
    if client.queue is None:
        return None
    storage = client.queue.queue_storage
    compat.refresh_queue(storage)
    primary = (storage.current_scan_queue or {}).get("primary")
    return None if primary is None else [lock.identifier for lock in primary.locks]


def _interlock(client: BECClient, statuses: dict[str, str]) -> dict[str, Any] | None:
    actors = getattr(client, "builtin_actors", None)
    hli = getattr(actors, "scan_interlock", None) if actors is not None else None
    if hli is None:
        return None
    watched = {
        name: [str(status) for status in accepted] for name, accepted in hli.states_watched.items()
    }
    tripped = sorted(
        name for name, accepted in watched.items() if statuses.get(name, "unknown") not in accepted
    )
    locks = _primary_locks(client)
    return {
        "enabled": bool(hli.enabled),
        "trigger": str(hli.trigger_setting),
        "watched": watched,
        "tripped_by": tripped,
        "primary_queue_locked": None if locks is None else INTERLOCK_LOCK_ID in locks,
    }


def _state_finding(state: dict[str, Any]) -> Finding:
    device = state.get("device")
    return Finding(
        "error" if state["status"] == "invalid" else "warning",
        f"state_{state['status']}",
        f"Beamline state {state['name']} is {state['status']}: {state['label']}",
        subject=state["name"],
        next_tool="bec_device_read" if device else None,
        next_args={"device": device} if device else None,
    )


def _interlock_findings(
    states: list[dict[str, Any]], interlock: dict[str, Any] | None
) -> list[Finding]:
    if interlock is None:
        return []
    findings = []
    configured = {state["name"] for state in states}
    for name in sorted(set(interlock["watched"]) - configured):
        findings.append(
            Finding(
                "warning",
                "interlock_watches_unknown_state",
                f"The scan interlock watches {name}, which is not a configured beamline state, "
                "so its status stays unknown",
                subject=name,
            )
        )
    if interlock["enabled"] and interlock["tripped_by"]:
        locked = "; the primary queue is locked" if interlock["primary_queue_locked"] else ""
        findings.append(
            Finding(
                "error",
                "interlock_tripped",
                f"The scan interlock ({interlock['trigger']}) is tripped by "
                f"{', '.join(interlock['tripped_by'])}{locked}. New requests wait until these "
                "states are back to an accepted status",
                subject="scan_interlock",
                next_tool="bec_queue",
            )
        )
    elif interlock["primary_queue_locked"]:
        findings.append(
            Finding(
                "warning",
                "interlock_lock_without_trip",
                "The primary queue still carries the scan interlock lock although no watched "
                "state is out of range now",
                subject="scan_interlock",
                next_tool="bec_queue",
            )
        )
    return findings


def _states_summary(
    states: list[dict[str, Any]], total: int, interlock: dict[str, Any] | None
) -> str:
    if states:
        counts: dict[str, int] = {}
        for state in states:
            counts[state["status"]] = counts.get(state["status"], 0) + 1
        ordered = sorted(counts, key=lambda s: _STATUS_ORDER.index(s) if s in _STATUS_ORDER else 0)
        summary = f"{total} beamline state(s): {', '.join(f'{counts[s]} {s}' for s in ordered)}."
    else:
        summary = "No beamline states are configured."
    if interlock is None:
        return summary + " Scan interlock not available."
    if not interlock["enabled"]:
        return summary + " Scan interlock disabled."
    summary += (
        f" Scan interlock enabled ({interlock['trigger']}), watching "
        f"{len(interlock['watched'])} state(s)"
    )
    if interlock["tripped_by"]:
        summary += f", tripped by {', '.join(interlock['tripped_by'])}"
    if interlock["primary_queue_locked"]:
        summary += "; primary queue locked"
    return summary + "."


def _state_entry(
    client: BECClient, manager: Any, config: messages.BeamlineStateConfig, now: float
) -> dict[str, Any]:
    status, label = _state_status(manager, config.name)
    msg = read_state_message(client, config.name)
    state = {
        "name": config.name,
        "type": config.state_type,
        "status": status,
        "label": _clip(label),
        "age_s": age_s(msg.timestamp if msg else None, now),
        "parameters": _format_parameters(config.parameters),
    }
    if isinstance(config.parameters.get("device"), str):
        state["device"] = config.parameters["device"]
    return state


def beamline_states_digest(client: BECClient) -> dict[str, Any]:
    """Each configured beamline state with status, label and age, plus the scan interlock."""
    manager = getattr(client, "beamline_states", None)
    configs = read_available_states(client) or []
    now = time.time()

    states = [_state_entry(client, manager, config, now) for config in configs[:MAX_STATES]]
    interlock = _interlock(client, {state["name"]: state["status"] for state in states})

    findings = []
    if manager is not None and not getattr(manager, "ready", True):
        findings.append(
            Finding("info", "states_not_loaded", "The client has not loaded beamline states yet")
        )
    findings += [_state_finding(state) for state in states if state["status"] != "valid"]
    findings += _interlock_findings(states, interlock)

    data = {"states": states, "interlock": interlock}
    summary = _states_summary(states, len(configs), interlock)
    return _answer(summary, findings, data, truncated=len(configs) > MAX_STATES)


def beamline_states(connection: BecConnection) -> dict[str, Any]:
    """bec_beamline_states: borrow the client and digest states and interlock."""
    try:
        with connection.use() as client:
            return beamline_states_digest(client)
    except BecUnavailableError as exc:
        return _unreachable(exc)


# ---------------------------------------------------------------------- MCP registration


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_queue and bec_beamline_states on ``server``; ``call`` offloads the reads."""

    @server.tool(
        name="bec_queue",
        description="What the scan queue is doing: queue status and locks, the running scan with "
        "its progress, what is waiting, and the last finished requests.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_queue(
        history: Annotated[
            int, Field(description="Finished requests to include, newest first (0 to 6)")
        ] = 5,
    ) -> dict:
        return await call(queue, connection, history)

    @server.tool(
        name="bec_beamline_states",
        description="Beamline states (valid, warning, invalid or unknown) with labels and age, and "
        "whether the scan interlock watches them and has locked the queue.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_beamline_states() -> dict:
        return await call(beamline_states, connection)
