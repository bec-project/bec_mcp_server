"""The server as an MCP client sees it: tools, answers, errors, deadlines and the primer."""

from __future__ import annotations

import json
import threading
import time

import anyio
import pytest
from mcp.client import Client

from bec_mcp_server import app as app_module
from bec_mcp_server.app import BEC_PRIMER, build_app
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import READ_ONLY_TOOL

ANSWER_KEYS = {"ok", "summary", "findings", "data", "truncated"}


def _list_tools(server):
    async def run():
        async with Client(server, mode="legacy") as client:
            return (await client.list_tools()).tools

    return anyio.run(run)


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_diagnose_mode_serves_mcp_status():
    tools = _list_tools(build_app().server)

    assert [tool.name for tool in tools] == ["mcp_status", "bec_docs_search", "bec_docs_page"]


def test_every_tool_is_annotated_read_only():
    tools = _list_tools(build_app().server)

    assert tools
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


def test_every_tool_answers_a_call_without_arguments(call_tool, stdout_guard):
    # small models call tools bare; every tool must work that way and print nothing
    app = build_app()
    for tool in _list_tools(app.server):
        with stdout_guard():
            payload = _payload(call_tool(app.server, tool.name))
        assert set(payload) == ANSWER_KEYS, tool.name


def test_tool_descriptions_are_one_sentence():
    for tool in _list_tools(build_app().server):
        assert tool.description.count(". ") == 0, tool.name
        assert "Safe to run" not in tool.description, tool.name


def test_mcp_status_reports_the_server_without_bec(call_tool):
    settings = Settings(tool_timeout=12, max_bec_calls=3)
    payload = _payload(call_tool(build_app(settings).server, "mcp_status"))

    assert payload["ok"] is True
    assert payload["summary"].startswith("MCP server ")
    data = payload["data"]
    assert data["transport"] == {"kind": "stdio"}
    assert data["mode"] == "diagnose"
    assert data["bec"] == {"state": "not_configured"}
    assert data["tool_timeout_s"] == 12
    assert data["pools"] == {
        "bec": {"capacity": 3, "in_flight": 0},
        "docs": {"capacity": 4, "in_flight": 0},
    }
    assert data["versions"]["mcp_sdk"].startswith("2.")
    assert data["audit_log"] == "off"


def test_mcp_status_reports_the_http_endpoint_without_the_token(call_tool):
    settings = Settings(
        transport="streamable-http", token="s3cret", allowed_hosts=("host.containers.internal",)
    )
    result = call_tool(build_app(settings).server, "mcp_status")
    payload = _payload(result)

    assert payload["data"]["transport"] == {
        "kind": "streamable-http",
        "url": "http://127.0.0.1:8000/mcp",
        "auth": "bearer token",
        "extra_allowed_hosts": ["host.containers.internal"],
    }
    assert "s3cret" not in result.content[0].text


def test_mcp_status_reads_the_connection_state_without_connecting(call_tool):
    class _Connection:
        def connect(self):
            raise AssertionError("mcp_status must not connect")

        def shutdown(self):
            pass

        def state(self):
            return {"state": "connected", "redis": "localhost:6379"}

    payload = _payload(call_tool(build_app(connection=_Connection()).server, "mcp_status"))

    assert payload["data"]["bec"] == {"state": "connected", "redis": "localhost:6379"}


def test_mcp_status_survives_a_broken_connection_state(call_tool):
    class _Connection:
        def connect(self):
            pass

        def shutdown(self):
            pass

        def state(self):
            raise RuntimeError("boom")

    payload = _payload(call_tool(build_app(connection=_Connection()).server, "mcp_status"))

    assert payload["data"]["bec"] == {"state": "unknown", "error": "RuntimeError: boom"}


def _with_probe(app, func, name="bec_probe"):
    """Register a test tool that runs ``func`` on the BEC pool, like the BEC tools will."""

    @app.server.tool(name=name, description="Test probe.", annotations=READ_ONLY_TOOL)
    async def probe() -> dict:
        return await app.context.bec_call(func)

    return app


def test_slow_bec_call_fails_within_the_deadline_with_a_clear_error(call_tool):
    release = threading.Event()

    def wedged():
        release.wait(5)

    app = _with_probe(build_app(Settings(tool_timeout=0.2)), wedged)
    started = time.monotonic()
    try:
        result = call_tool(app.server, "bec_probe")
    finally:
        release.set()

    assert time.monotonic() - started < 3
    assert result.is_error is True
    assert "did not finish within 0.2s" in result.content[0].text


def test_anticipated_failures_reach_the_agent_with_their_message(call_tool):
    def refused():
        raise ConnectionError("Could not connect to BEC Redis at localhost:1")

    result = call_tool(_with_probe(build_app(), refused).server, "bec_probe")

    assert result.is_error is True
    assert "Could not connect to BEC Redis at localhost:1" in result.content[0].text


def test_crash_text_stays_on_the_server(call_tool):
    def crash():
        raise AttributeError("'NoneType' object has no attribute 'secret_internal'")

    app = _with_probe(build_app(), crash)
    result = call_tool(app.server, "bec_probe")
    status = _payload(call_tool(app.server, "mcp_status"))

    assert result.is_error is True
    assert "secret_internal" not in result.content[0].text
    failure = status["data"]["recent_failures"][0]
    assert failure["tool"] == "bec_probe"
    assert "AttributeError" in failure["error"]
    assert "secret_internal" not in failure["error"]
    assert status["findings"] and status["findings"][0].startswith("Warning: bec_probe failed")
    assert status["data"]["findings"][0]["code"] == "recent_tool_failure"


def test_mcp_status_flags_a_saturated_pool(call_tool):
    app = build_app(Settings(max_bec_calls=1, tool_timeout=5))
    entered = threading.Event()
    release = threading.Event()

    def blocked():
        entered.set()
        release.wait(5)

    _with_probe(app, blocked)

    async def run():
        async with Client(app.server, mode="legacy") as client:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(client.call_tool, "bec_probe", {})
                with anyio.fail_after(3):
                    while not entered.is_set():
                        await anyio.sleep(0.01)
                status = await client.call_tool("mcp_status", {})
                release.set()
            return status

    try:
        payload = _payload(anyio.run(run))
    finally:
        release.set()

    assert payload["ok"] is False
    assert payload["data"]["pools"]["bec"] == {"capacity": 1, "in_flight": 1}
    assert payload["findings"][0].startswith("Error: All 1 bec call slots are busy")
    assert payload["data"]["findings"][0]["severity"] == "error"


def test_primer_resource_is_the_server_instructions():
    app = build_app()

    async def run():
        async with Client(app.server, mode="legacy") as client:
            return (await client.read_resource("bec://primer")).contents[0].text

    assert anyio.run(run) == app.server.instructions
    assert "READ-ONLY" in app.server.instructions


def test_primer_stays_small_and_never_sends_the_agent_to_files():
    # every session loads the primer; keep it under ~300 tokens (about 4 characters each)
    assert len(BEC_PRIMER) < 1200
    lowered = BEC_PRIMER.lower()
    for phrase in ("hdf5", "open the file", "read the file", "file path"):
        assert phrase not in lowered


def test_unknown_mode_is_refused():
    with pytest.raises(SystemExit, match="invalid mode"):
        Settings(mode="dev")


def test_stdout_guard_catches_a_printing_tool(call_tool, stdout_guard):
    app = _with_probe(build_app(), lambda: print("rich table"), name="noisy")

    with pytest.raises(AssertionError, match="corrupts the stdio transport"):
        with stdout_guard():
            call_tool(app.server, "noisy")


def test_close_shuts_down_the_connection_and_the_audit_log(tmp_path):
    calls = []

    class _Connection:
        def connect(self):
            pass

        def shutdown(self):
            calls.append("shutdown")

        def state(self):
            return {}

    app = build_app(Settings(audit_log=tmp_path / "audit.jsonl"), connection=_Connection())
    app.close()

    assert calls == ["shutdown"]
    assert not app.audit_log._thread.is_alive()


def test_bec_tool_modules_get_the_connection_and_the_deadline_bound_call(call_tool, monkeypatch):
    class _Connection:
        def connect(self):
            pass

        def shutdown(self):
            pass

        def state(self):
            return {"state": "connected"}

        def run(self, func, *args):
            return func("client", *args)

    def read_devices(client, prefix):
        return {"client": client, "devices": [f"{prefix}x"], "main": _on_main_thread()}

    class _DevicesModule:
        @staticmethod
        def register(server, connection, call):
            @server.tool(description="Device probe.", annotations=READ_ONLY_TOOL)
            async def device_probe() -> dict:
                return await call(connection.run, read_devices, "sam")

    monkeypatch.setitem(app_module.TIER_MODULES, "diagnose", (_DevicesModule,))
    with_bec = build_app(connection=_Connection())
    without_bec = build_app()

    payload = _payload(call_tool(with_bec.server, "device_probe"))

    # the read ran on a worker thread with the connection's client
    assert payload == {"client": "client", "devices": ["samx"], "main": False}
    # without a BEC connection no BEC tool module is registered
    assert "device_probe" not in [tool.name for tool in _list_tools(without_bec.server)]


def _on_main_thread():
    return threading.current_thread() is threading.main_thread()
