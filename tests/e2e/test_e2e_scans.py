"""bec_scans, bec_scan_help and bec_scan_history against real BEC 4 services.

The scan list must be exactly what the BEC client offers, and every example call must pass the
client's own request builder with the session's devices. Nothing is submitted: the request is
built and validated, never sent. With --start-servers a separate process loads bec_lib's test
device config first, so the examples have real motors to use.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import scans as scan_tools

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


def _wait_for_two_motors(connection):
    deadline = time.monotonic() + DEVICES_WAIT_S
    while True:
        motors = connection.run(scan_tools.example_motors)
        if len(motors) >= 2 or time.monotonic() > deadline:
            return motors
        time.sleep(1)


def test_scan_list_is_what_the_bec_client_offers(connection):
    listing = connection.run(scan_tools.scan_list_digest)
    with connection.use() as client:
        # pylint: disable=protected-access
        offered = sorted(client.scans._available_scans)  # the names `scans.<TAB>` completes

    assert listing["ok"] is True
    assert [row["name"] for row in listing["data"]["scans"]] == offered
    assert {"line_scan", "grid_scan", "mv"} <= set(offered)


def test_every_example_passes_the_bec_clients_request_builder(connection):
    assert len(_wait_for_two_motors(connection)) >= 2, "the session never had two motors"

    with connection.use() as client:
        # pylint: disable=protected-access
        published = {name: scan.scan_info for name, scan in client.scans._available_scans.items()}
        for name, info in published.items():
            answer = scan_tools.scan_help_digest(client, name)
            assert answer["findings"] == [], (name, answer["findings"])
            recorded = {}

            def record(*args, recorded=recorded, **kwargs):
                recorded.update(args=args, kwargs=kwargs)

            namespace = {
                "scans": SimpleNamespace(**{name: record}),
                "dev": client.device_manager.devices,
            }
            eval(answer["data"]["example"], namespace)  # pylint: disable=eval-used
            kwargs = dict(
                recorded["kwargs"],
                system_config={"file_suffix": None, "file_directory": None},
                user_metadata={},
            )
            # builds and validates the ScanQueueMessage the client would send; sends nothing
            client.scans.prepare_scan_request(name, info, *recorded["args"], **kwargs)


def test_scan_tools_answer_through_mcp(connection, call_tool, stdout_guard):
    app = build_app(Settings(), connection=connection)

    with stdout_guard():
        for tool, arguments in (
            ("bec_scans", {}),
            ("bec_scan_help", {}),
            ("bec_scan_help", {"scan_name": "grid_scan"}),
            ("bec_scan_history", {"limit": 5}),
        ):
            result = call_tool(app.server, tool, arguments)
            assert not result.is_error, result.content[0].text
            payload = json.loads(result.content[0].text)
            assert payload["ok"] is True, tool
            json.dumps(payload, allow_nan=False)

    unknown = call_tool(app.server, "bec_scan_help", {"scan_name": "linescan"})
    assert unknown.is_error and "Similar: line_scan" in unknown.content[0].text
    app.close()
