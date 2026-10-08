"""bec_diagnose and bec_device_diagnose against real BEC 4 services.

With --start-servers a separate process loads bec_lib's test device config, disables samy and
narrows samx's soft limits so that samx sits outside them, and both tools have to say so (a
separate process because BECClient is a per-process singleton). Against services that are already
running (BEC_MCP_E2E=1) nothing is changed: the tests only read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest
from bec_lib.device import DeviceBase

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import diagnose as diagnose_tools

#: the device server needs a while to build the simulated devices and apply the changes
SETUP_WAIT_S = 180

#: the config updates reach the cache within seconds
SEEN_WAIT_S = 60

_BREAK_TWO_DEVICES = textwrap.dedent("""
    import sys
    from bec_lib.client import BECClient
    from bec_lib.redis_connector import RedisConnector
    from bec_lib.service_config import ServiceConfig

    client = BECClient(ServiceConfig(sys.argv[1]), RedisConnector, forced=True, wait_for_server=True)
    client.start()
    try:
        client.config.update_session_with_file(sys.argv[2], force=True)
        devices = client.device_manager.devices
        devices.samy.enabled = False
        # the simulated samx starts at 0, so it now reads below its low limit
        devices.samx.limits = [1, 2]
    finally:
        client.shutdown()
    """)


@pytest.fixture
def connection(
    request, bec_servers, bec_services_config_file_path, bec_test_config_file_path, tmp_path
):
    if request.config.getoption("--start-servers", default=False):
        subprocess.run(
            [
                sys.executable,
                "-c",
                _BREAK_TWO_DEVICES,
                str(bec_services_config_file_path),
                str(bec_test_config_file_path),
            ],
            check=True,
            timeout=SETUP_WAIT_S,
        )
    connection = BecConnection(
        BecConnectionConfig(
            service_config_path=str(bec_services_config_file_path), log_dir=str(tmp_path / "logs")
        )
    )
    yield connection
    connection.shutdown()


def _codes(result):
    return [finding["code"] for finding in result["data"].get("findings", [])]


def _wait_for(connection, func, *args, expected):
    deadline = time.monotonic() + SEEN_WAIT_S
    while True:
        result = func(connection, *args)
        if expected <= set(_codes(result)) or time.monotonic() > deadline:
            return result
        time.sleep(1)


def test_disabled_device_and_narrowed_limits_are_findings(request, connection):
    if not request.config.getoption("--start-servers", default=False):
        pytest.skip("changing devices writes to BEC; only on services started for this test")

    beamline = _wait_for(
        connection, diagnose_tools.diagnose, expected={"devices.disabled", "devices.outside_limits"}
    )
    samx = diagnose_tools.device_diagnose(connection, "samx")
    samy = diagnose_tools.device_diagnose(connection, "samy")

    assert {"devices.disabled", "devices.outside_limits"} <= set(_codes(beamline)), beamline
    assert any("samx (0 not in [1, 2])" in line for line in beamline["findings"]), beamline
    assert any("disabled: samy" in line for line in beamline["findings"]), beamline
    assert "device.outside_limits" in _codes(samx), samx
    assert _codes(samy)[0] == "device.disabled", samy
    json.dumps([beamline, samx, samy], allow_nan=False)


def test_diagnose_answers_through_mcp_without_rpc(connection, call_tool, stdout_guard, monkeypatch):
    sent = []
    monkeypatch.setattr(DeviceBase, "_run_rpc_call", lambda *args, **kwargs: sent.append(args))
    app = build_app(Settings(), connection=connection)

    with stdout_guard():
        results = [
            call_tool(app.server, "bec_diagnose"),
            call_tool(app.server, "bec_device_diagnose"),
            call_tool(app.server, "bec_device_diagnose", {"device": "samx"}),
        ]

    for result in results:
        assert not result.is_error, result.content[0].text
        payload = json.loads(result.content[0].text)
        assert set(payload) == {"ok", "summary", "findings", "data", "truncated"}
        assert "diagnose.incomplete" not in _codes(payload), payload
        twins = payload["data"].get("findings", [])
        assert len(twins) == len(payload["findings"])
    assert not sent, "a diagnose tool tried to call a device over RPC"
    app.close()
