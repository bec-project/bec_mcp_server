"""Tool modules and what they share: annotations, the call context and the answer shape.

A BEC tool module holds plain functions that take the connection and return a digest, plus a
``register(server, connection, call)`` that exposes them as MCP tools. ``call(func, *args)`` runs
a blocking function on the BEC worker pool under the per-call deadline; the server's own module,
``health``, gets the whole :class:`ToolContext` instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Literal, Protocol

from mcp_types import ToolAnnotations

from bec_mcp_server.docs.search import DocsIndex
from bec_mcp_server.runtime import Runtime
from bec_mcp_server.settings import Settings

#: every v1 tool reads cached state; MCP clients use this to skip permission prompts
READ_ONLY_TOOL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class Connection(Protocol):
    """What the tools need from the BEC connection."""

    def connect(self) -> None:
        """Start the client now instead of on the first tool call."""

    def shutdown(self) -> None:
        """Stop the client and its threads; called once the transport has ended."""

    def state(self) -> dict[str, Any]:
        """Connection state for mcp_status.

        Must not block or start the client: mcp_status runs on the event loop and has to answer
        while BEC is down or every worker slot is busy.
        """


#: what BEC tool modules get to run blocking work: ``await call(func, *args, **kwargs)``
BecCall = Callable[..., Awaitable[Any]]


@dataclass
class ToolContext:
    """Everything a tool module needs to register and run its tools."""

    settings: Settings
    runtime: Runtime
    connection: Connection | None
    #: the docs search index; None when the mode has no learn tier
    docs: DocsIndex | None = None

    async def bec_call(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run blocking BEC work on the BEC pool, bounded by the per-call deadline.

        This is the ``call`` that BEC tool modules receive. Expected failures (``ConnectionError``,
        ``LookupError``, ``ValueError``, ``TimeoutError``) reach the agent with their message.
        """
        return await self.runtime.call("bec", func, *args, **kwargs)


Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    """One deterministic conclusion a tool drew, with the call that checks it further.

    Tools return findings in two forms: a sentence a small model can read out, and a
    structured twin that bec_diagnose aggregates across tools.
    """

    severity: Severity
    code: str
    message: str
    subject: str | None = None
    next_tool: str | None = None
    next_args: dict[str, Any] | None = None

    def text(self) -> str:
        """``<Severity>: <sentence>. Next: <tool>(<args>).``"""
        message = self.message.rstrip()
        if not message.endswith((".", "!", "?")):
            message += "."
        text = f"{self.severity.capitalize()}: {message}"
        if self.next_tool is not None:
            args = ", ".join(
                f"{key}={json.dumps(value)}" for key, value in (self.next_args or {}).items()
            )
            text += f" Next: {self.next_tool}({args})."
        return text

    def as_dict(self) -> dict[str, Any]:
        """The structured twin, stored under data["findings"]."""
        return {
            "severity": self.severity,
            "code": self.code,
            "subject": self.subject,
            "next": (
                None
                if self.next_tool is None
                else {"tool": self.next_tool, "args": dict(self.next_args or {})}
            ),
        }


def tool_result(
    ok: bool,
    summary: str,
    *,
    findings: Iterable[Finding | str] = (),
    data: dict[str, Any] | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """The answer shape every tool returns, so a model always finds the same keys.

    Top-level ``findings`` is always a list of sentences; :class:`Finding` objects also land in
    ``data["findings"]`` as their structured twin.
    """
    findings = list(findings)
    data = dict(data or {})
    structured = [finding.as_dict() for finding in findings if isinstance(finding, Finding)]
    if structured:
        data["findings"] = structured
    return {
        "ok": ok,
        "summary": summary,
        "findings": [
            finding.text() if isinstance(finding, Finding) else finding for finding in findings
        ],
        "data": data,
        "truncated": truncated,
    }
