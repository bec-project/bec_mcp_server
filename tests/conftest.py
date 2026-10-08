"""Shared helpers: call tools through a real MCP session, and fail on writes to stdout."""

from __future__ import annotations

import contextlib
from typing import Any

import anyio
import pytest
from mcp.client import Client


def _session_call(server: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    async def run():
        # legacy mode runs the initialize handshake, like Claude Code and other stdio clients
        async with Client(server, mode="legacy") as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(run)


@pytest.fixture
def call_tool():
    """Call one tool on an MCPServer through a full in-memory MCP session."""
    return _session_call


@pytest.fixture
def stdout_guard(capfd):
    """Context manager that fails when anything reaches stdout inside it.

    In the stdio transport stdout is the protocol channel, so one stray print (bec_lib helpers
    print with rich) corrupts the session. capfd sees both sys.stdout and file descriptor 1,
    including writes from worker threads.
    """

    @contextlib.contextmanager
    def guard():
        capfd.readouterr()
        yield
        out = capfd.readouterr().out
        assert not out, f"wrote to stdout, which corrupts the stdio transport: {out!r}"

    return guard
