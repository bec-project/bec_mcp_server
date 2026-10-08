"""bec_ping and bec_status against real BEC 4 services."""

from __future__ import annotations

import json
import threading
import time
from importlib import metadata

import pytest

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import status as status_tools

#: services start in parallel and need a few seconds to publish their first status
STARTUP_WAIT_S = 90


@pytest.fixture
def connection(bec_servers, bec_services_config_file_path, tmp_path):
    connection = BecConnection(
        BecConnectionConfig(
            service_config_path=str(bec_services_config_file_path), log_dir=str(tmp_path / "logs")
        )
    )
    yield connection
    connection.shutdown()


def _wait_until_healthy(connection):
    """Wait for every core service's status, and for its metrics, which follow within seconds."""
    deadline = time.monotonic() + STARTUP_WAIT_S
    while True:
        result = status_tools.status(connection)
        core = result["data"].get("core_services", [])
        if result["ok"] and all("memory_mb" in entry for entry in core):
            return result
        if time.monotonic() > deadline:
            return result
        time.sleep(1)


def test_bec_status_reports_the_running_services(connection, bec_redis_host_port):
    result = _wait_until_healthy(connection)

    assert result["ok"] is True, result
    host, port = bec_redis_host_port
    assert result["data"]["redis"]["address"] == f"{host}:{port}"
    core = {entry["name"]: entry for entry in result["data"]["core_services"]}
    assert set(core) == set(status_tools.CORE_SERVICES)
    for entry in core.values():
        assert entry["status"] in {"RUNNING", "BUSY", "IDLE"}
        assert entry["bec_lib"].startswith("4.")
        assert entry["memory_mb"] > 0
    assert any(entry.get("this_server") for entry in result["data"]["clients"]["listed"])
    json.dumps(result, allow_nan=False)

    ping = status_tools.ping(connection)
    assert ping["ok"] is True
    assert ping["data"]["redis"]["services_publishing"] >= len(status_tools.CORE_SERVICES)


def test_tools_answer_through_mcp_and_the_client_shuts_down(connection, call_tool):
    assert _wait_until_healthy(connection)["ok"]
    app = build_app(Settings(), connection=connection)

    for tool in ("bec_ping", "bec_status", "mcp_status"):
        result = call_tool(app.server, tool)
        assert not result.is_error, result.content[0].text
        assert json.loads(result.content[0].text)["ok"] is True, tool

    state = json.loads(call_tool(app.server, "mcp_status").content[0].text)["data"]["bec"]
    assert state["state"] == "connected"
    assert state["client_starts"] == 1

    app.close()
    assert connection.state()["state"] == "closed"
    time.sleep(1)
    leftovers = [
        thread.name
        for thread in threading.enumerate()
        if not thread.daemon and "BECMCPClient" in thread.name and thread.is_alive()
    ]
    assert leftovers == []


def test_installed_bec_lib_is_bec_4():
    assert metadata.version("bec_lib").startswith("4.")
