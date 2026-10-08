"""Command-line and environment parsing, and the shutdown path of main()."""

from __future__ import annotations

import signal
import threading

import pytest

from bec_mcp_server import cli
from bec_mcp_server.settings import DEFAULT_MAX_BEC_CALLS, DEFAULT_TOOL_TIMEOUT_S

_ENV = (
    "BEC_MCP_TRANSPORT",
    "BEC_MCP_HOST",
    "BEC_MCP_PORT",
    "BEC_MCP_PATH",
    "BEC_MCP_MODE",
    "BEC_MCP_TOKEN",
    "BEC_MCP_TOKEN_FILE",
    "BEC_MCP_ALLOW_UNAUTHENTICATED",
    "BEC_MCP_ALLOWED_HOSTS",
    "BEC_MCP_ALLOWED_ORIGINS",
    "BEC_MCP_TOOL_TIMEOUT",
    "BEC_MCP_MAX_BEC_CALLS",
    "BEC_MCP_AUDIT_LOG",
    "BEC_MCP_LOG_LEVEL",
    "XDG_STATE_HOME",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_serve_stdio_in_diagnose_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = cli.parse_args([])

    # stdio is what command-based registration (claude mcp add) expects
    assert settings.transport == "stdio"
    assert settings.mode == "diagnose"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.path == "/mcp"
    assert settings.token is None
    assert settings.tool_timeout == DEFAULT_TOOL_TIMEOUT_S
    assert settings.max_bec_calls == DEFAULT_MAX_BEC_CALLS
    assert settings.audit_log == tmp_path / ".local" / "state" / "bec-mcp-server" / "audit.jsonl"


def test_every_flag_has_an_environment_variable(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("abc\n", encoding="utf-8")
    env = {
        "BEC_MCP_TRANSPORT": "streamable-http",
        "BEC_MCP_HOST": "0.0.0.0",
        "BEC_MCP_PORT": "9000",
        "BEC_MCP_PATH": "/bec",
        "BEC_MCP_MODE": "diagnose",
        "BEC_MCP_TOKEN_FILE": str(token_file),
        "BEC_MCP_ALLOW_UNAUTHENTICATED": "yes",
        "BEC_MCP_ALLOWED_HOSTS": "host.containers.internal:*",
        "BEC_MCP_ALLOWED_ORIGINS": "https://console.psi.ch",
        "BEC_MCP_TOOL_TIMEOUT": "5",
        "BEC_MCP_MAX_BEC_CALLS": "2",
        "BEC_MCP_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "BEC_MCP_LOG_LEVEL": "debug",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    settings = cli.parse_args([])

    assert settings.transport == "streamable-http"
    assert (settings.host, settings.port, settings.path) == ("0.0.0.0", 9000, "/bec")
    assert settings.token == "abc"
    assert settings.allow_unauthenticated is True
    assert settings.allowed_hosts == ("host.containers.internal:*",)
    assert settings.allowed_origins == ("https://console.psi.ch",)
    assert (settings.tool_timeout, settings.max_bec_calls) == (5.0, 2)
    assert settings.audit_log == tmp_path / "audit.jsonl"
    assert settings.log_level == "DEBUG"


def test_flags_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("BEC_MCP_PORT", "9000")
    assert cli.parse_args(["--port", "9100"]).port == 9100


@pytest.mark.parametrize("value", ["off", "OFF", "none", "0", "false"])
def test_audit_log_can_be_switched_off(value):
    assert cli.parse_args(["--audit-log", value]).audit_log is None


def test_token_is_only_read_for_http(monkeypatch, tmp_path):
    # a stdio server must not fail on a token file it never uses
    monkeypatch.setenv("BEC_MCP_TOKEN_FILE", str(tmp_path / "missing"))
    assert cli.parse_args([]).token is None
    with pytest.raises(SystemExit, match="could not read token file"):
        cli.parse_args(["--transport", "streamable-http"])


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("BEC_MCP_TRANSPORT", "http", "BEC_MCP_TRANSPORT"),
        ("BEC_MCP_MODE", "diagnos", "BEC_MCP_MODE"),
        ("BEC_MCP_PORT", "8000x", "BEC_MCP_PORT"),
        ("BEC_MCP_TOOL_TIMEOUT", "soon", "BEC_MCP_TOOL_TIMEOUT"),
        ("BEC_MCP_TOOL_TIMEOUT", "0", "BEC_MCP_TOOL_TIMEOUT"),
        ("BEC_MCP_MAX_BEC_CALLS", "0", "BEC_MCP_MAX_BEC_CALLS"),
        ("BEC_MCP_LOG_LEVEL", "chatty", "log level"),
        ("BEC_MCP_ALLOWED_HOSTS", "http://host", "--allowed-hosts"),
    ],
)
def test_deployment_typos_exit_with_a_readable_message(monkeypatch, variable, value, message):
    # a typo in a systemd unit must name the variable, not print a traceback
    monkeypatch.setenv(variable, value)
    with pytest.raises(SystemExit, match=message):
        cli.parse_args([])


@pytest.fixture(autouse=True)
def _restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_main_closes_the_app_when_the_transport_ends(monkeypatch):
    events = []

    class _App:
        context = None

        def close(self):
            events.append("close")

    monkeypatch.setattr(cli, "build_app", lambda settings: _App())
    monkeypatch.setattr(cli, "run", lambda app: events.append("run"))
    monkeypatch.setattr(cli, "_exit_if_stuck", lambda: events.append("exit-check"))

    cli.main(["--audit-log", "off"])

    assert events == ["run", "close", "exit-check"]


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_main_closes_the_app_when_the_transport_fails(monkeypatch, error):
    closed = []

    class _App:
        def close(self):
            closed.append(True)

    def failing_run(app):
        raise error("stop")

    monkeypatch.setattr(cli, "build_app", lambda settings: _App())
    monkeypatch.setattr(cli, "run", failing_run)
    monkeypatch.setattr(cli, "_exit_if_stuck", lambda: None)

    if error is KeyboardInterrupt:
        cli.main(["--audit-log", "off"])
    else:
        with pytest.raises(RuntimeError):
            cli.main(["--audit-log", "off"])
    assert closed == [True]


def test_exit_check_waits_for_finishing_threads(monkeypatch):
    exits = []
    monkeypatch.setattr(cli.os, "_exit", exits.append)
    worker = threading.Thread(target=threading.Event().wait, args=(0.1,))
    worker.start()

    cli._exit_if_stuck(grace=2)

    assert not worker.is_alive()
    assert exits == []


def test_exit_check_exits_when_a_thread_stays_blocked(monkeypatch):
    exits = []
    monkeypatch.setattr(cli.os, "_exit", exits.append)
    monkeypatch.setattr(cli.logging, "shutdown", lambda: None)
    release = threading.Event()
    worker = threading.Thread(target=release.wait, args=(5,), name="stuck-bec-call")
    worker.start()
    try:
        cli._exit_if_stuck(grace=0.1)
    finally:
        release.set()
        worker.join()

    assert exits == [0]


def test_sigterm_takes_the_ctrl_c_path(monkeypatch):
    closed = []

    class _App:
        def close(self):
            closed.append(True)

    def run_until_terminated(app):
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(cli, "build_app", lambda settings: _App())
    monkeypatch.setattr(cli, "run", run_until_terminated)
    monkeypatch.setattr(cli, "_exit_if_stuck", lambda: None)

    cli.main(["--audit-log", "off"])

    assert closed == [True]
