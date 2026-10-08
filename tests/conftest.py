"""Shared helpers: call tools through a real MCP session, fail on writes to stdout, and a fake
BECClient that BecConnection can start, use and shut down."""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import anyio
import pytest
from mcp.client import Client

from bec_mcp_server.bec import connection as connection_module
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig


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

class FakeConnector:
    def __init__(self, *, alive: bool = True, username: str = "default") -> None:
        self.alive = alive
        self._username = username
        self.retry_enabled: list[bool] = []

    def ping(self) -> bool:
        if isinstance(self.alive, BaseException):
            raise self.alive
        return self.alive

    @property
    def username(self) -> str:
        return self._username

    def set_retry_enabled(self, enabled: bool) -> None:
        self.retry_enabled.append(enabled)


class FakeClient:
    """Mimics the BECClient surface BecConnection relies on."""

    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        swallow_failure: bool = False,
        username: str = "default",
        start_gate: threading.Event | None = None,
        service_status: dict | None = None,
        services_metric: dict | None = None,
        service_name: str = "BECMCPClient/0123456789abcdef",
        on_start: Any = None,
    ) -> None:
        self.start_error = start_error
        self.swallow_failure = swallow_failure
        self.start_gate = start_gate
        self.on_start = on_start
        self.connector = FakeConnector(username=username)
        self.device_manager: Any = None
        self._service_status = service_status or {}
        self._services_metric = services_metric or {}
        self._service_name = service_name
        self.shutdown_calls: list[float | None] = []
        self.started = False

    def start(self) -> None:
        if self.start_gate is not None:
            self.start_gate.wait(10)
        if self.on_start is not None:
            self.on_start(self)
        if self.start_error is not None:
            raise self.start_error
        if self.swallow_failure:
            # BECClient.start() catches redis ConnectionError, shuts down and returns
            self.connector.alive = False
            return
        self.device_manager = object()
        self.started = True

    def shutdown(self, per_thread_timeout_s: float | None = None) -> None:
        self.shutdown_calls.append(per_thread_timeout_s)

    @property
    def service_status(self) -> dict:
        return self._service_status


class ClientFactory:
    """Records every client BecConnection creates; ``make`` builds the next one."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.client_kwargs = client_kwargs
        self.clients: list[FakeClient] = []
        self.service_configs: list[Any] = []
        self.connector_classes: list[type] = []

    def __call__(self, service_config: Any, connector_cls: type) -> FakeClient:
        self.service_configs.append(service_config)
        self.connector_classes.append(connector_cls)
        client = FakeClient(**self.client_kwargs)
        self.clients.append(client)
        return client


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _isolated_bec_environment(monkeypatch, tmp_path):
    """No test may pick up the developer's BEC config or write logs to their home."""
    for var in (
        "BEC_SERVICE_CONFIG",
        "BEC_REDIS_HOST",
        "BEC_REDIS_PORT",
        "BEC_REDIS_USERNAME",
        "BEC_REDIS_PASSWORD",
        "BEC_MCP_ACL_FILE",
        "BEC_MCP_SERVICE_CONFIG",
        "BEC_MCP_LOG_DIR",
        "BEC_MCP_CLIENT_MAX_AGE",
        "BEC_MCP_REDIS_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(connection_module, "DEFAULT_LOG_DIR", tmp_path / "logs")


@pytest.fixture
def make_connection():
    """Build a BecConnection on fake clients: make_connection(config=..., **client_kwargs)."""
    created: list[BecConnection] = []

    def _make(config: BecConnectionConfig | None = None, clock=None, **client_kwargs):
        factory = ClientFactory(**client_kwargs)
        kwargs = {"client_factory": factory}
        if clock is not None:
            kwargs["clock"] = clock
        connection = BecConnection(config or BecConnectionConfig(host="localhost"), **kwargs)
        connection.factory = factory  # type: ignore[attr-defined]
        created.append(connection)
        return connection

    yield _make
    for connection in created:
        connection.shutdown()
