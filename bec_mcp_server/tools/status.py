"""bec_ping and bec_status: is BEC reachable, and are its services healthy?

Both read only what the client already has: a Redis PING, the public ``client.service_status``
and the service metrics BECService caches next to it. Neither starts anything on the BEC side.
When BEC cannot be reached they answer ``ok: false`` with the reason instead of raising, because
"BEC is down" is the answer to the question, not a tool failure.
"""

from __future__ import annotations

import time
from collections import Counter
from importlib import metadata
from typing import Any

from bec_mcp_server.bec import compat
from bec_mcp_server.bec.connection import BecConnection, BecUnavailableError
from bec_mcp_server.bec.digest import age_s, jsonable, short_service_name
from bec_mcp_server.tools import READ_ONLY_TOOL, BecCall, Finding, tool_result

#: services every healthy BEC instance runs, in the order a reader expects them. Unique services
#: publish their status under the bare class name; matching is on the normalized name so that
#: older spellings (FileWriter) still count.
CORE_SERVICES = ("DeviceServer", "ScanServer", "ScanBundler", "FileWriterManager")
_CORE_KEYS = {"DeviceServer": "deviceserver", "ScanServer": "scanserver"}
_CORE_KEYS |= {"ScanBundler": "scanbundler", "FileWriterManager": "filewriter"}

#: BUSY and IDLE are normal service states, not failures
_HEALTHY_STATUSES = {"RUNNING", "BUSY", "IDLE"}

#: entries listed per group before the rest is only counted
_MAX_LISTED = 20


def _normalized(name: str) -> str:
    return name.split("/", 1)[0].lower().replace("_", "").replace(" ", "")


def _core_name(service_name: str) -> str | None:
    normalized = _normalized(service_name)
    for core, key in _CORE_KEYS.items():
        if key in normalized:
            return core
    return None


#: processes that serve a person rather than the beamline: IPython, the GUI, an editor's
#: language server, MCP servers
_CLIENT_MARKERS = ("client", "becwidgets", "pylsp")


def _is_client(service_name: str) -> bool:
    normalized = _normalized(service_name)
    return any(marker in normalized for marker in _CLIENT_MARKERS)


def _status_name(message: Any) -> str:
    value = getattr(message, "status", None)
    return getattr(value, "name", None) or str(jsonable(value))


def _service_entry(
    name: str, message: Any, metrics: dict[str, Any], own_name: str | None, now: float
) -> dict[str, Any]:
    info = getattr(message, "info", None)
    if isinstance(info, dict):
        hostname, user, published = info.get("hostname"), info.get("user"), info.get("timestamp")
        versions = info.get("versions") or {}
    else:
        hostname = getattr(info, "hostname", None)
        user = getattr(info, "user", None)
        published = getattr(info, "timestamp", None)
        versions = getattr(info, "versions", None)
    bec_lib_version = (
        versions.get("bec_lib")
        if isinstance(versions, dict)
        else getattr(versions, "bec_lib", None)
    )
    entry: dict[str, Any] = {
        "name": short_service_name(name),
        "status": _status_name(message),
        "host": hostname,
        "user": user,
        "bec_lib": bec_lib_version,
        "status_age_s": age_s(published, now),
    }
    metric_message = metrics.get(name)
    values = getattr(metric_message, "metrics", None)
    if isinstance(values, dict):
        entry["cpu_percent"] = jsonable(values.get("cpu_percent"))
        memory = values.get("memory_in_mb")
        entry["memory_mb"] = round(float(memory), 1) if isinstance(memory, (int, float)) else None
        entry["threads"] = values.get("num_threads")
        entry["uptime_s"] = age_s(values.get("create_time"), now)
    if own_name is not None and name == own_name:
        entry["this_server"] = True
    return {key: val for key, val in entry.items() if val is not None}


def _redis_digest(connection: BecConnection, client: Any) -> dict[str, Any]:
    start = time.perf_counter()
    answered = bool(client.connector.ping())
    round_trip_ms = (time.perf_counter() - start) * 1000
    digest: dict[str, Any] = {
        "address": connection.redis_target,
        "answered": answered,
        "round_trip_ms": round(round_trip_ms, 2),
    }
    try:
        digest["account"] = str(client.connector.username)
    except Exception:  # pylint: disable=broad-except
        pass  # ACL WHOAMI is informational; a missing grant must not fail the health check
    return digest


def _unreachable(connection: BecConnection, exc: BecUnavailableError) -> dict[str, Any]:
    return tool_result(
        False,
        f"BEC is unreachable: {exc}",
        findings=[
            Finding("error", "bec_unreachable", str(exc), subject="redis", next_tool="mcp_status")
        ],
        data={"redis": {"address": connection.redis_target, "answered": False}},
    )


def ping(connection: BecConnection) -> dict[str, Any]:
    """Redis round trip through the BEC client, and how many services publish a status."""
    try:
        with connection.use() as client:
            redis = _redis_digest(connection, client)
            names = list(client.service_status)
    except BecUnavailableError as exc:
        return _unreachable(connection, exc)
    clients = sum(1 for name in names if _is_client(name))
    services = len(names) - clients
    redis["services_publishing"] = services
    redis["clients_publishing"] = clients
    return tool_result(
        True,
        f"Redis at {redis['address']} answered in {redis['round_trip_ms']:.1f} ms; "
        f"{services} services and {clients} clients publish a status.",
        data={"redis": redis},
    )


def status(connection: BecConnection) -> dict[str, Any]:
    """Health of BEC's services: core services, their status, version, host, CPU and memory."""
    try:
        with connection.use() as client:
            redis = _redis_digest(connection, client)
            statuses = dict(client.service_status)
            metrics = compat.service_metrics(client)
            own_name = compat.own_service_name(client)
    except BecUnavailableError as exc:
        return _unreachable(connection, exc)

    core, others, clients = _group_services(statuses, metrics, own_name)
    findings = _core_findings(core)
    problems = [finding for finding in findings if finding.severity != "info"]
    findings += _server_version_findings(core)
    summary = _status_summary(core, problems, len(others), len(clients))
    truncated = len(others) > _MAX_LISTED or len(clients) > _MAX_LISTED
    data: dict[str, Any] = {
        "redis": redis,
        "core_services": [
            core.get(name, {"name": name, "status": "MISSING"}) for name in CORE_SERVICES
        ],
        "other_services": others[:_MAX_LISTED],
        "clients": {
            "count": len(clients),
            "by_type": dict(Counter(entry["name"].split("/", 1)[0] for entry in clients)),
            "listed": clients[:_MAX_LISTED],
        },
    }
    if truncated:
        data["note"] = f"lists are capped at {_MAX_LISTED} entries; counts cover everything"
    return tool_result(not problems, summary, findings=findings, data=data, truncated=truncated)


def _server_version_findings(core: dict[str, dict[str, Any]]) -> list[Finding]:
    """Note when this server's bec_lib is a different major release than the services'."""
    try:
        own = metadata.version("bec_lib")
    except metadata.PackageNotFoundError:
        return []
    theirs = sorted({entry["bec_lib"] for entry in core.values() if entry.get("bec_lib")})
    if not theirs or all(version.split(".")[0] == own.split(".")[0] for version in theirs):
        return []
    return [
        Finding(
            "info",
            "server_bec_version_differs",
            f"This MCP server uses bec_lib {own} while the core services run "
            f"{', '.join(theirs)}; answers about devices, scans and the queue may be incomplete",
        )
    ]


def _status_summary(
    core: dict[str, dict[str, Any]], findings: list[Finding], others: int, clients: int
) -> str:
    if not findings:
        return (
            f"BEC is healthy: all {len(CORE_SERVICES)} core services are running "
            f"({others} other services and {clients} clients publish a status)."
        )
    if not core:
        return (
            "BEC is down: Redis answers, but none of the core services "
            f"({', '.join(CORE_SERVICES)}) publish a status."
        )
    return f"BEC needs attention: {findings[0].message}"


def _group_services(
    statuses: dict[str, Any], metrics: dict[str, Any], own_name: str | None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the status entries into core services (by name), other services and clients."""
    now = time.time()
    core: dict[str, dict[str, Any]] = {}
    others: list[dict[str, Any]] = []
    clients: list[dict[str, Any]] = []
    for name, message in sorted(statuses.items()):
        entry = _service_entry(name, message, metrics, own_name, now)
        core_name = _core_name(name)
        if core_name is not None and core_name not in core:
            core[core_name] = entry
        elif _is_client(name):
            clients.append(entry)
        else:
            others.append(entry)
    return core, others, clients


def _core_findings(core: dict[str, dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for name in CORE_SERVICES:
        entry = core.get(name)
        logs = {"service": name, "level": "error"}
        if entry is None:
            findings.append(
                Finding(
                    "error",
                    "core_service_missing",
                    f"{name} is not publishing a status, so it is down or cannot reach Redis; "
                    "device, scan and file data may be stale",
                    subject=name,
                    next_tool="bec_logs",
                    next_args=logs,
                )
            )
        elif entry["status"] == "ERROR":
            findings.append(
                Finding(
                    "error",
                    "core_service_error",
                    f"{name} reports status ERROR",
                    subject=name,
                    next_tool="bec_logs",
                    next_args=logs,
                )
            )
        elif entry["status"] not in _HEALTHY_STATUSES:
            findings.append(
                Finding(
                    "warning",
                    "core_service_status_unknown",
                    f"{name} reports an unknown status {entry['status']!r}, so its health is "
                    "unclear",
                    subject=name,
                )
            )
    versions = {name: entry.get("bec_lib") for name, entry in core.items() if entry.get("bec_lib")}
    if len(set(versions.values())) > 1:
        listed = ", ".join(f"{name} {version}" for name, version in sorted(versions.items()))
        findings.append(
            Finding(
                "warning",
                "core_version_mismatch",
                f"Core services run different BEC versions ({listed}); a service was probably "
                "not restarted after an update",
            )
        )
    return findings


# ---------------------------------------------------------------------- MCP registration


def register(server: Any, connection: BecConnection, call: BecCall) -> None:
    """Expose bec_ping and bec_status on ``server``; ``call`` offloads under the deadline."""

    @server.tool(
        name="bec_ping",
        description="Check that BEC's Redis answers: round trip in ms and how many services "
        "publish a status.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_ping() -> dict[str, Any]:
        return await call(ping, connection)

    @server.tool(
        name="bec_status",
        description="Is BEC healthy? Status of each core service with version, host, CPU and "
        "memory, plus other services and connected clients.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_status() -> dict[str, Any]:
        return await call(status, connection)
