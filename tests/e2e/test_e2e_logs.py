"""bec_logs against real BEC 4 services.

With --start-servers a separate process logs an error and raises a MAJOR alarm, as a failing
scan or device would, and bec_logs has to find both (a separate process because BECClient is a
per-process singleton). Against services that are already running (BEC_MCP_E2E=1) nothing is
provoked: the tests only read the stream.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
import uuid

import pytest

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import logs as log_tools

#: the provoking client has to start, log, raise and shut down
PROVOKE_WAIT_S = 120

#: log records and the alarm reach the stream and the server's alarm handler within seconds
SEEN_WAIT_S = 30

_PROVOKE = textwrap.dedent("""
    import sys
    import time

    from bec_lib import messages
    from bec_lib.alarm_handler import Alarms
    from bec_lib.client import BECClient
    from bec_lib.logger import bec_logger
    from bec_lib.redis_connector import RedisConnector
    from bec_lib.service_config import ServiceConfig

    client = BECClient(ServiceConfig(sys.argv[1]), RedisConnector, forced=True, wait_for_server=True)
    client.start()
    try:
        marker = sys.argv[2]
        bec_logger.logger.error(f"{marker}: scan request rejected, unknown argument 'stepz'")
        client.connector.raise_alarm(
            Alarms.MAJOR,
            messages.ErrorInfo(
                error_message=f"{marker}: samx hit its low limit",
                compact_error_message=None,
                exception_type="LimitError",
                device="samx",
            ),
        )
        time.sleep(1)
    finally:
        client.shutdown()
    """)


@pytest.fixture
def connection(bec_servers, bec_services_config_file_path, tmp_path):
    connection = BecConnection(
        BecConnectionConfig(
            service_config_path=str(bec_services_config_file_path), log_dir=str(tmp_path / "logs")
        )
    )
    yield connection
    connection.shutdown()


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _wait_for_marker(connection, marker):
    """Poll until the error, the alarm and the server's own copy of the alarm are all in."""
    deadline = time.monotonic() + SEEN_WAIT_S
    while True:
        result = log_tools.logs(connection, level="error", minutes=10, text=marker)
        alarms = result["data"]["alarms"]
        if (result["data"]["entries"] and alarms and alarms[0].get("unhandled")) or (
            time.monotonic() > deadline
        ):
            return result
        time.sleep(0.5)


def test_bec_logs_finds_a_provoked_error_and_alarm(
    request, connection, bec_services_config_file_path, call_tool, stdout_guard
):
    if not request.config.getoption("--start-servers", default=False):
        pytest.skip("provoking an error writes to BEC; only on services started for this test")
    # start the server's client first, so its alarm handler receives the alarm
    connection.connect()
    marker = f"mcp-e2e-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [sys.executable, "-c", _PROVOKE, str(bec_services_config_file_path), marker],
        check=True,
        timeout=PROVOKE_WAIT_S,
    )

    result = _wait_for_marker(connection, marker)

    assert result["ok"] is True, result
    (entry,) = result["data"]["entries"]
    assert entry["level"] == "ERROR" and "unknown argument 'stepz'" in entry["message"]
    (alarm,) = result["data"]["alarms"]
    assert alarm["severity"] == "MAJOR" and alarm["device"] == "samx"
    assert alarm["count"] == 1
    assert alarm.get("unhandled") is True, "the server's client never received the alarm"
    assert result["findings"][0].startswith("Error: MAJOR alarm on samx, raised once")
    json.dumps(result, allow_nan=False)

    app = build_app(Settings(), connection=connection)
    with stdout_guard():
        payload = _payload(
            call_tool(app.server, "bec_logs", {"level": "error", "minutes": 10, "text": marker})
        )
    assert payload["data"]["alarms"][0]["device"] == "samx"
    app.close()


def test_bec_logs_reads_the_whole_stream_within_bounds(connection, call_tool, stdout_guard):
    app = build_app(Settings(), connection=connection)
    with stdout_guard():
        bare = call_tool(app.server, "bec_logs")
        full = call_tool(app.server, "bec_logs", {"limit": 200, "detail": "full"})

    for result in (bare, full):
        payload = _payload(result)
        assert payload["ok"] is True
        # every service logs while it starts, and so does the server's own client
        assert payload["data"]["window"]["scanned"] > 0
        assert len(result.content[0].text) < 100_000
    app.close()
