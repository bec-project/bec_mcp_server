"""Command-line and environment parsing, and the shutdown path of main()."""

from __future__ import annotations

import signal
import threading

import pytest

from bec_mcp_server import cli
from bec_mcp_server.bec.connection import DEFAULT_CLIENT_MAX_AGE_S, BecUnavailableError
from bec_mcp_server.bec.log_cache import DEFAULT_LOG_CACHE_SIZE
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
    "BEC_MCP_LOG_CACHE_SIZE",
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

    monkeypatch.setattr(cli, "build_app", lambda settings, **kwargs: _App())
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

    monkeypatch.setattr(cli, "build_app", lambda settings, **kwargs: _App())
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

    monkeypatch.setattr(cli, "build_app", lambda settings, **kwargs: _App())
    monkeypatch.setattr(cli, "run", run_until_terminated)
    monkeypatch.setattr(cli, "_exit_if_stuck", lambda: None)

    cli.main(["--audit-log", "off"])

    assert closed == [True]


# ---------------------------------------------------------------------------- BEC connection


def _connection(argv):
    args = cli.build_parser().parse_args(argv)
    return cli.connection_from_args(args, cli.settings_from_args(args))


def test_connection_defaults_leave_redis_to_bec_lib():
    config = _connection([]).config
    assert (config.host, config.port, config.username) == (None, None, None)
    assert config.client_max_age_s == DEFAULT_CLIENT_MAX_AGE_S


def test_connection_flags(tmp_path):
    acl = tmp_path / "mcp_ro.env"
    acl.write_text("REDIS_USER=mcp_ro\nREDIS_PASSWORD=pw\n", encoding="utf-8")
    config = _connection(
        [
            "--redis",
            "bec-host:6391",
            "--config",
            "/etc/bec/client.yaml",
            "--acl-file",
            str(acl),
            "--client-max-age",
            "0",
        ]
    ).config
    assert (config.host, config.port) == ("bec-host", 6391)
    assert config.service_config_path == "/etc/bec/client.yaml"
    assert (config.username, config.password) == ("mcp_ro", "pw")
    assert config.client_max_age_s == 0


def test_connection_environment_and_flag_precedence(monkeypatch, tmp_path):
    env_acl = tmp_path / "env.env"
    env_acl.write_text("REDIS_USER=from_env\nREDIS_PASSWORD=a\n", encoding="utf-8")
    flag_acl = tmp_path / "flag.env"
    flag_acl.write_text("REDIS_USER=from_flag\nREDIS_PASSWORD=b\n", encoding="utf-8")
    monkeypatch.setenv("BEC_REDIS_HOST", "env-host")
    monkeypatch.setenv("BEC_REDIS_PORT", "6400")
    monkeypatch.setenv("BEC_MCP_ACL_FILE", str(env_acl))
    monkeypatch.setenv("BEC_MCP_CLIENT_MAX_AGE", "60")

    config = _connection([]).config
    assert (config.host, config.port, config.username, config.client_max_age_s) == (
        "env-host",
        6400,
        "from_env",
        60.0,
    )

    config = _connection(["--redis", "flag-host:6401", "--acl-file", str(flag_acl)]).config
    assert (config.host, config.port, config.username) == ("flag-host", 6401, "from_flag")


def test_main_hands_the_connection_to_the_app(monkeypatch):
    received = {}

    class _App:
        def close(self):
            pass

    def fake_build_app(settings, *, connection=None):
        received["connection"] = connection
        return _App()

    monkeypatch.setattr(cli, "build_app", fake_build_app)
    monkeypatch.setattr(cli, "run", lambda app: None)
    monkeypatch.setattr(cli, "_exit_if_stuck", lambda: None)

    cli.main(["--audit-log", "off", "--redis", "bec-host:6391"])

    assert received["connection"].redis_target == "bec-host:6391"
    assert received["connection"].state()["state"] == "not_connected"


def test_connect_on_startup_failure_exits_with_the_reason_and_closes(monkeypatch):
    events = []

    class _App:
        def close(self):
            events.append("close")

    def refuse(self):
        raise BecUnavailableError("Could not connect to BEC's Redis at bec-host:6391.")

    monkeypatch.setattr(cli.BecConnection, "connect", refuse)
    monkeypatch.setattr(cli, "build_app", lambda settings, **kwargs: _App())
    monkeypatch.setattr(cli, "run", lambda app: events.append("run"))

    with pytest.raises(SystemExit, match="cannot connect to BEC at startup: Could not connect"):
        cli.main(["--audit-log", "off", "--connect-on-startup"])
    assert events == ["close"]


# ---------------------------------------------------------------------------- the log cache


def test_log_cache_size_from_flag_and_environment(monkeypatch):
    assert _connection([]).config.log_cache_size == DEFAULT_LOG_CACHE_SIZE
    assert _connection(["--log-cache-size", "0"]).config.log_cache_size == 0
    assert _connection(["--log-cache-size", "0"]).log_cache is None
    monkeypatch.setenv("BEC_MCP_LOG_CACHE_SIZE", "500")
    assert _connection([]).config.log_cache_size == 500
    assert _connection(["--log-cache-size", "100"]).log_cache.capacity == 100


class _StartRecorder:
    """Stands in for threading.Thread and records what would have started."""

    started: list[str] = []

    def __init__(self, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon

    def start(self):
        _StartRecorder.started.append(self.name)
        assert self.daemon
        self.target()


class _Connection:
    def __init__(self, cache=True, state="not_connected", error=None):
        self.log_cache = object() if cache else None
        self._state = state
        self.error = error
        self.connects = 0

    def state(self):
        return {"state": self._state}

    def connect(self):
        self.connects += 1
        if self.error is not None:
            raise self.error


def test_serving_starts_the_log_cache_in_the_background(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_start_log_cache", started.append)
    monkeypatch.setattr(cli, "run_stdio", lambda server: None)
    app = type("App", (), {})()
    app.server = object()
    app.context = type("Context", (), {"settings": cli.Settings(), "connection": "conn"})()

    cli.run(app)

    assert started == ["conn"]


def test_the_background_start_connects_once_and_only_when_needed(monkeypatch):
    monkeypatch.setattr(cli.threading, "Thread", _StartRecorder)
    _StartRecorder.started = []
    waiting, connected, no_cache = _Connection(), _Connection(state="connected"), _Connection(False)

    for connection in (waiting, connected, no_cache, None):
        cli._start_log_cache(connection)  # pylint: disable=protected-access

    assert _StartRecorder.started == ["bec-mcp-log-cache-start"]
    assert (waiting.connects, connected.connects, no_cache.connects) == (1, 0, 0)


def test_a_failed_background_start_only_logs(monkeypatch, caplog):
    monkeypatch.setattr(cli.threading, "Thread", _StartRecorder)
    down = _Connection(error=BecUnavailableError("Redis at localhost:6379 is down"))
    broken = _Connection(error=RuntimeError("bug"))

    with caplog.at_level("INFO", logger=cli.__name__):
        cli._start_log_cache(down)  # pylint: disable=protected-access
        cli._start_log_cache(broken)  # pylint: disable=protected-access

    assert "starting on the first call" in caplog.text
    assert "background connect failed" in caplog.text
