"""bec_devices, bec_device_info and bec_device_read against real BEC 4 services.

With --start-servers the session has no devices yet, so a separate process loads bec_lib's test
device config first (a separate process because BECClient is a per-process singleton). Against
services that are already running (BEC_MCP_E2E=1) nothing is loaded: the tests only read.
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
from bec_mcp_server.bec import compat
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import devices as device_tools

#: the device server needs a while to build and publish the simulated devices
DEVICES_WAIT_S = 120

_LOAD_CONFIG = textwrap.dedent("""
    import sys
    from bec_lib.client import BECClient
    from bec_lib.redis_connector import RedisConnector
    from bec_lib.service_config import ServiceConfig

    client = BECClient(ServiceConfig(sys.argv[1]), RedisConnector, forced=True, wait_for_server=True)
    client.start()
    try:
        client.config.update_session_with_file(sys.argv[2], force=True)
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
                _LOAD_CONFIG,
                str(bec_services_config_file_path),
                str(bec_test_config_file_path),
            ],
            check=True,
            timeout=DEVICES_WAIT_S,
        )
    connection = BecConnection(
        BecConnectionConfig(
            service_config_path=str(bec_services_config_file_path), log_dir=str(tmp_path / "logs")
        )
    )
    yield connection
    connection.shutdown()


def _wait_for_samx(connection):
    deadline = time.monotonic() + DEVICES_WAIT_S
    while True:
        result = connection.run(device_tools.list_devices, prefix="samx")
        if result["data"]["total"] or time.monotonic() > deadline:
            return result
        time.sleep(1)


def test_device_tools_read_cached_state_of_every_device(connection, monkeypatch):
    sent, refused = [], []
    monkeypatch.setattr(DeviceBase, "_run_rpc_call", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr(compat, "_refuse_rpc", lambda *args, **kwargs: refused.append(args))
    assert _wait_for_samx(connection)["data"]["total"] == 1, "samx never appeared"

    listing = connection.run(device_tools.list_devices, limit=device_tools.MAX_DEVICE_LIMIT)
    names = [row["name"] for row in listing["data"]["devices"]]
    assert listing["ok"] is True and "samx" in names
    for name in names:
        info = connection.run(device_tools.device_info, device=name)
        read = connection.run(device_tools.device_read, device=name)
        assert info["ok"] is True and read["ok"] is True, name
        json.dumps([info, read], allow_nan=False)

    samx = connection.run(device_tools.device_read, device="samx")
    assert samx["data"]["limits"] == {"low": -50, "high": 50}
    assert samx["data"]["readback"]["samx"]["age_s"] is not None
    assert not sent and not refused, "a device tool tried to call a device over RPC"


def test_device_tools_answer_through_mcp(connection, call_tool, stdout_guard):
    assert _wait_for_samx(connection)["data"]["total"] == 1, "samx never appeared"
    app = build_app(Settings(), connection=connection)

    with stdout_guard():
        for tool, arguments in (
            ("bec_devices", {"device_type": "positioner"}),
            ("bec_device_info", {"device": "samx"}),
            ("bec_device_read", {"device": "samx"}),
        ):
            result = call_tool(app.server, tool, arguments)
            assert not result.is_error, result.content[0].text
            assert json.loads(result.content[0].text)["ok"] is True, tool

    signals = json.loads(
        call_tool(app.server, "bec_device_info", {"device": "samx"}).content[0].text
    )
    kinds = {row["name"]: row["kind"] for row in signals["data"]["signals"]}
    assert kinds["readback"] == "hinted" and kinds["velocity"] == "config"
    app.close()
