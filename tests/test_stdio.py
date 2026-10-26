"""A real stdio session against the installed entry point, as Claude Code runs it."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters


def _server_env(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BEC_MCP_", "BEC_REDIS_")) and key != "BEC_SERVICE_CONFIG"
    }
    env["BEC_MCP_AUDIT_LOG"] = str(tmp_path / "audit.jsonl")
    env["BEC_MCP_LOG_DIR"] = str(tmp_path / "logs")
    return env


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_stdio_session_lists_tools_and_answers(tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bec_mcp_server"], env=_server_env(tmp_path)
    )

    async def run():
        with anyio.fail_after(60):
            async with Client(params, mode="legacy") as client:
                tools = (await client.list_tools()).tools
                status = await client.call_tool("mcp_status", {})
                return [tool.name for tool in tools], status

    names, status = anyio.run(run)

    assert names[:3] == ["mcp_status", "bec_ping", "bec_status"]
    assert {"bec_devices", "bec_device_info", "bec_device_read"} <= set(names)
    assert {"bec_queue", "bec_beamline_states"} <= set(names)
    assert {"bec_scans", "bec_scan_help", "bec_scan_history"} <= set(names)
    assert "bec_logs" in names
    assert {"bec_diagnose", "bec_device_diagnose"} <= set(names)
    assert not status.is_error
    assert json.loads(status.content[0].text)["data"]["transport"] == {"kind": "stdio"}
    # the session ended cleanly, so the audit writer flushed its line on shutdown
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["tool"] for line in lines] == ["mcp_status"]


def test_bec_down_is_answered_within_the_deadline_and_the_process_exits(tmp_path):
    # a real BECClient against a port nobody listens on: the answer must arrive well inside the
    # deadline, nothing may reach stdout, and no client thread may keep the process alive
    env = _server_env(tmp_path)
    env["BEC_MCP_TOOL_TIMEOUT"] = "20"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bec_mcp_server", "--redis", f"127.0.0.1:{_closed_port()}"],
        env=env,
    )

    async def run():
        with anyio.fail_after(60):
            async with Client(params, mode="legacy") as client:
                started = time.monotonic()
                answer = await client.call_tool("bec_status", {})
                elapsed = time.monotonic() - started
                status = await client.call_tool("mcp_status", {})
                return answer, elapsed, status

    started = time.monotonic()
    answer, elapsed, status = anyio.run(run)
    total = time.monotonic() - started

    payload = json.loads(answer.content[0].text)
    assert payload["ok"] is False
    assert payload["summary"].startswith("BEC is unreachable: Could not connect to BEC's Redis")
    assert elapsed < 20
    assert json.loads(status.content[0].text)["data"]["bec"]["state"] == "failed"
    # the session closed and the server process ended without the exit-grace fallback
    assert total < 60


def test_stdio_server_exits_when_its_input_closes(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "-m", "bec_mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_server_env(tmp_path),
    )
    out, err = process.communicate(input=b"", timeout=60)

    assert process.returncode == 0, err.decode()
    # nothing but protocol messages may reach stdout, and there were none to send
    assert out == b""


def test_http_server_shuts_down_cleanly_on_sigterm(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("t0ken\n", encoding="utf-8")
    env = _server_env(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bec_mcp_server",
            "--transport",
            "streamable-http",
            "--port",
            "0",
            "--token-file",
            str(token_file),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        for line in process.stderr:
            if b"Uvicorn running" in line:
                break
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()

    # 0, not -15: SIGTERM ran the normal shutdown path instead of killing the process
    assert process.returncode == 0
