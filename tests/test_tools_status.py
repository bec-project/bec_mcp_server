import json
import time

import anyio
import pytest
from bec_lib import messages
from bec_lib.messages import BECStatus
from mcp.client import Client

from bec_mcp_server.app import build_app
from bec_mcp_server.settings import Settings
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


def test_other_bec_major_release_is_an_info_finding(make_connection):
    statuses = _healthy_statuses(**{name: _status(name, bec_lib="3.165.1") for name in CORE})
    result = status_tools.status(_connection(make_connection, statuses))
    # BEC itself is healthy; the note is about this server
    assert result["ok"] is True
    assert result["summary"].startswith("BEC is healthy")
    assert result["findings"][0].startswith("Info: This MCP server uses bec_lib 4.")
    assert "3.165.1" in result["findings"][0]


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


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_tools_are_registered_read_only_in_diagnose_mode(make_connection):
    app = build_app(Settings(), connection=_connection(make_connection, _healthy_statuses()))

    async def list_tools():
        async with Client(app.server, mode="legacy") as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(list_tools)}
    assert set(tools) == {"mcp_status", "bec_ping", "bec_status"}
    assert all(tool.annotations.read_only_hint for tool in tools.values())


@pytest.mark.parametrize("tool", ["bec_ping", "bec_status"])
def test_tools_answer_through_mcp_without_stdout(make_connection, call_tool, stdout_guard, tool):
    class Noisy(dict):
        # bec_lib helpers print; the stdio transport must never see it
        def __iter__(self):
            print("Waiting for services...")
            return super().__iter__()

        def keys(self):
            print("Waiting for services...")
            return super().keys()

    connection = _connection(make_connection, Noisy(_healthy_statuses()))
    app = build_app(Settings(), connection=connection)
    with stdout_guard():
        payload = _payload(call_tool(app.server, tool))
    assert payload["ok"] is True


def test_bec_down_answers_within_the_deadline(make_connection, call_tool):
    app = build_app(Settings(tool_timeout=5), connection=make_connection(swallow_failure=True))
    started = time.monotonic()
    payload = _payload(call_tool(app.server, "bec_status"))
    assert time.monotonic() - started < 5
    assert payload["ok"] is False
    assert payload["findings"][0].startswith("Error: Could not connect")
    assert payload["data"]["findings"][0]["next"] == {"tool": "mcp_status", "args": {}}


def test_mcp_status_reports_the_client_age(make_connection, call_tool, fake_clock):
    connection = make_connection(clock=fake_clock)
    app = build_app(Settings(), connection=connection)
    assert _payload(call_tool(app.server, "mcp_status"))["data"]["bec"]["state"] == "not_connected"

    call_tool(app.server, "bec_ping")
    fake_clock.now += 42
    bec = _payload(call_tool(app.server, "mcp_status"))["data"]["bec"]
    assert bec["state"] == "connected"
    assert bec["client_age_s"] == 42.0
    assert bec["client_starts"] == 1
