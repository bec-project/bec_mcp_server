import asyncio
import json
import time

import anyio
import pytest
from bec_lib import messages
from bec_lib.messages import BECStatus
from mcp import Client
from mcp.server.mcpserver import MCPServer

from bec_mcp_server.tools import status as status_tools

OWN_NAME = "BECMCPClient/0123456789abcdef"


def _versions(bec_lib="4.1.6"):
    return messages.ServiceVersions(
        bec_lib=bec_lib,
        bec_server=bec_lib,
        bec_ipython_client=bec_lib,
        bec_widgets="3.39.0",
        ophyd_devices="1.49.0",
    )


def _status(name, status=BECStatus.RUNNING, host="x10da-bec-01", bec_lib="4.1.6"):
    return messages.StatusMessage(
        name=name,
        status=status,
        info=messages.ServiceInfo(
            user="e12345", hostname=host, timestamp=time.time() - 2, versions=_versions(bec_lib)
        ),
    )


def _metric(name, memory=321.04, cpu=1.5):
    return messages.ServiceMetricMessage(
        name=name,
        metrics={
            "cpu_percent": cpu,
            "memory_in_mb": memory,
            "num_threads": 12,
            "create_time": time.time() - 3600,
            "pid": 4242,
        },
    )


CORE = ("DeviceServer", "ScanServer", "ScanBundler", "FileWriterManager")


def _healthy_statuses(**overrides):
    statuses = {name: _status(name) for name in CORE}
    statuses["SciHub"] = _status("SciHub")
    statuses["BECIPythonClient/aaaaaaaa-1111"] = _status("BECIPythonClient/aaaaaaaa-1111")
    statuses[OWN_NAME] = _status(OWN_NAME)
    statuses.update(overrides)
    return statuses


def _connection(make_connection, statuses, metrics=None):
    return make_connection(
        service_status=statuses,
        services_metric=metrics if metrics is not None else {},
        service_name=OWN_NAME,
    )


def test_healthy_bec(make_connection):
    statuses = _healthy_statuses(ScanServer=_status("ScanServer", BECStatus.BUSY))
    metrics = {"DeviceServer": _metric("DeviceServer")}
    result = status_tools.status(_connection(make_connection, statuses, metrics))

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["summary"].startswith("BEC is healthy: all 4 core services are running")
    core = {entry["name"]: entry for entry in result["data"]["core_services"]}
    assert list(core) == list(CORE)
    assert core["ScanServer"]["status"] == "BUSY"
    device_server = core["DeviceServer"]
    assert device_server["host"] == "x10da-bec-01"
    assert device_server["bec_lib"] == "4.1.6"
    assert device_server["memory_mb"] == 321.0
    assert device_server["cpu_percent"] == 1.5
    assert 3590 < device_server["uptime_s"] < 3700
    assert [entry["name"] for entry in result["data"]["other_services"]] == ["SciHub"]
    clients = result["data"]["clients"]
    assert clients["count"] == 2
    assert clients["by_type"] == {"BECIPythonClient": 1, "BECMCPClient": 1}
    own = next(entry for entry in clients["listed"] if entry.get("this_server"))
    assert own["name"] == "BECMCPClient/01234567"
    assert result["data"]["redis"]["answered"] is True
    json.dumps(result, allow_nan=False)


def test_missing_core_service_is_a_finding(make_connection):
    statuses = _healthy_statuses()
    del statuses["ScanBundler"]
    result = status_tools.status(_connection(make_connection, statuses))

    assert result["ok"] is False
    assert result["summary"].startswith("BEC needs attention: ScanBundler is not publishing")
    assert {"name": "ScanBundler", "status": "MISSING"} in result["data"]["core_services"]


def test_no_core_services_means_bec_is_down(make_connection):
    statuses = {OWN_NAME: _status(OWN_NAME)}
    result = status_tools.status(_connection(make_connection, statuses))

    assert result["ok"] is False
    assert result["summary"].startswith("BEC is down")
    assert len(result["findings"]) == 4


@pytest.mark.parametrize(
    "status, expected", [(BECStatus.ERROR, "reports status ERROR"), (BECStatus.RUNNING, None)]
)
def test_core_service_error_is_a_finding(make_connection, status, expected):
    statuses = _healthy_statuses(DeviceServer=_status("DeviceServer", status))
    result = status_tools.status(_connection(make_connection, statuses))
    assert result["ok"] is (expected is None)
    if expected:
        assert expected in result["findings"][0]


def test_unknown_status_is_a_finding(make_connection):
    statuses = _healthy_statuses()
    statuses["DeviceServer"] = statuses["DeviceServer"].model_copy(update={"status": 99})
    result = status_tools.status(_connection(make_connection, statuses))
    assert result["ok"] is False
    assert "unknown status" in result["findings"][0]


def test_mixed_core_versions_are_a_finding(make_connection):
    statuses = _healthy_statuses(ScanServer=_status("ScanServer", bec_lib="4.1.4"))
    result = status_tools.status(_connection(make_connection, statuses))
    assert result["ok"] is False
    assert "ScanServer 4.1.4" in result["findings"][0]


def test_long_client_lists_are_capped(make_connection):
    statuses = _healthy_statuses()
    for index in range(30):
        name = f"BECGuiClient/{index:08d}-x"
        statuses[name] = _status(name)
    result = status_tools.status(_connection(make_connection, statuses))
    assert result["truncated"] is True
    assert result["data"]["clients"]["count"] == 32
    assert len(result["data"]["clients"]["listed"]) == 20


def test_unreachable_bec_is_an_answer_not_an_exception(make_connection):
    connection = make_connection(swallow_failure=True)
    for result in (status_tools.status(connection), status_tools.ping(connection)):
        assert result["ok"] is False
        assert result["summary"].startswith("BEC is unreachable: Could not connect")
        assert result["data"]["redis"]["answered"] is False


def test_ping_counts_services_and_clients(make_connection):
    result = status_tools.ping(_connection(make_connection, _healthy_statuses()))
    assert result["ok"] is True
    redis = result["data"]["redis"]
    assert redis["services_publishing"] == 5
    assert redis["clients_publishing"] == 2
    assert redis["account"] == "default"
    assert "answered in" in result["summary"]


# ---------------------------------------------------------------------------- through MCP


def _server(connection):
    server = MCPServer("bec-test")

    async def call(func, *args):
        return await anyio.to_thread.run_sync(lambda: func(*args))

    status_tools.register(server, connection, call)
    return server


async def _call(server, name):
    async with Client(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool(name, {})
    return tools, result


def test_tools_are_read_only_and_answer_through_mcp(make_connection):
    connection = _connection(make_connection, _healthy_statuses())
    tools, result = asyncio.run(_call(_server(connection), "bec_status"))

    assert {tool.name for tool in tools.tools} == {"bec_ping", "bec_status"}
    assert all(tool.annotations.read_only_hint for tool in tools.tools)
    assert result.is_error is False
    assert result.structured_content["ok"] is True


def test_no_stdout_during_a_tool_call(make_connection, capsys):
    class Noisy(dict):
        def __iter__(self):
            print("Waiting for services...")
            return super().__iter__()

        def keys(self):
            print("Waiting for services...")
            return super().keys()

    connection = _connection(make_connection, Noisy(_healthy_statuses()))
    asyncio.run(_call(_server(connection), "bec_ping"))
    asyncio.run(_call(_server(connection), "bec_status"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Waiting for services" in captured.err
