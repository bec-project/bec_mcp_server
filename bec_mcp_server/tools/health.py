"""Health of the MCP server itself."""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from importlib import metadata
from typing import Any

from mcp.server.mcpserver import MCPServer

from bec_mcp_server.tools import READ_ONLY_TOOL, Finding, ToolContext, tool_result

#: failed calls listed by mcp_status, newest first
STATUS_FAILURES_SHOWN = 5

#: a failure younger than this is reported as a finding
RECENT_FAILURE_WINDOW_S = 600


def register(server: MCPServer, context: ToolContext) -> None:
    """Register mcp_status."""

    @server.tool(
        description="Report whether this MCP server is up: version, transport, mode, uptime, "
        "BEC connection state and recent failed tool calls.",
        annotations=READ_ONLY_TOOL,
    )
    async def mcp_status() -> dict:
        # runs on the event loop and touches no BEC state, so it answers even when BEC is down
        # or every worker slot is busy
        return server_status(context)


def server_status(context: ToolContext) -> dict[str, Any]:
    """The mcp_status answer, built from in-memory state only."""
    settings, runtime = context.settings, context.runtime
    now = datetime.now(timezone.utc)
    pools = {name: pool.describe() for name, pool in runtime.pools.items()}
    failures = list(runtime.recent_failures)[::-1]
    findings = []
    for name, pool in pools.items():
        if pool["in_flight"] >= pool["capacity"]:
            findings.append(
                Finding(
                    "error",
                    "pool_saturated",
                    f"All {pool['capacity']} {name} call slots are busy, so new calls fail until "
                    "earlier ones return; Redis or a BEC service may be hung",
                    subject=f"{name} pool",
                )
            )
    if failures and (now - failures[0].at).total_seconds() < RECENT_FAILURE_WINDOW_S:
        latest = failures[0]
        findings.append(
            Finding(
                "warning",
                "recent_tool_failure",
                f"{latest.tool} failed {_duration((now - latest.at).total_seconds())} ago: "
                f"{latest.error or latest.outcome}",
                subject=latest.tool,
            )
        )
    # describe() never loads or downloads the index, so this stays safe on the event loop
    docs_index = context.docs.describe() if context.docs is not None else {"state": "off"}
    if "error" in docs_index:
        findings.append(
            Finding("warning", "docs_index_error", docs_index["error"], subject="docs index")
        )
    if "warning" in docs_index:
        findings.append(
            Finding("info", "docs_index_stale", docs_index["warning"], subject="docs index")
        )
    transport: dict[str, Any] = {"kind": settings.transport}
    if settings.transport == "streamable-http":
        transport.update(
            url=settings.http_url,
            auth="bearer token" if settings.token is not None else "none",
            extra_allowed_hosts=list(settings.allowed_hosts),
        )
    data = {
        "server": {"name": "bec-mcp-server", "version": _version("bec-mcp-server")},
        "versions": {
            "mcp_sdk": _version("mcp"),
            "bec_lib": _version("bec_lib"),
            "python": platform.python_version(),
        },
        "transport": transport,
        "mode": settings.mode,
        "started_at": runtime.started_at.isoformat(timespec="seconds"),
        "uptime_s": round(runtime.uptime_s),
        "bec": _bec_state(context),
        "tool_timeout_s": settings.tool_timeout,
        "pools": pools,
        "calls": {"total": runtime.calls_total, "failed": runtime.calls_failed},
        "recent_failures": [record.describe() for record in failures[:STATUS_FAILURES_SHOWN]],
        "docs_index": docs_index,
        "audit_log": "off" if context.settings.audit_log is None else "on",
    }
    summary = (
        f"MCP server {data['server']['version']} is up ({settings.transport}, {settings.mode} "
        f"mode) for {_duration(runtime.uptime_s)}; {runtime.calls_total} tool calls, "
        f"{runtime.calls_failed} failed."
    )
    ok = not any(pool["in_flight"] >= pool["capacity"] for pool in pools.values())
    return tool_result(ok, summary, findings=findings, data=data)


def _bec_state(context: ToolContext) -> dict[str, Any]:
    if context.connection is None:
        return {"state": "not_configured"}
    try:
        return context.connection.state()
    # the status probe must answer even if state() is broken
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return {"state": "unknown", "error": f"{type(exc).__name__}: {exc}"}


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"
