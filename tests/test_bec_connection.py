import threading
import time

import pytest
import redis.exceptions

from bec_mcp_server.bec import connection as connection_module
from bec_mcp_server.bec.connection import (
    BecConnection,
    BecConnectionConfig,
    BecUnavailableError,
    parse_redis_address,
    read_acl_env_file,
)

# ---------------------------------------------------------------------------- lifecycle


def test_client_starts_lazily_once_and_is_reused(make_connection):
    connection = make_connection()
    assert connection.factory.clients == []
    assert connection.state()["state"] == "not_connected"

    with connection.use() as first:
        pass
    with connection.use() as second:
        pass

    assert first is second
    assert len(connection.factory.clients) == 1
    assert connection.state()["state"] == "connected"
    assert connection.state()["client_starts"] == 1


def test_redis_retries_are_disabled_after_start(make_connection):
    connection = make_connection()
    with connection.use() as client:
        assert client.connector.retry_enabled == [False]


def test_client_gets_a_connector_with_socket_timeouts(make_connection):
    connection = make_connection(BecConnectionConfig(host="localhost", redis_timeout_s=2.5))
    connection.connect()
    connector_cls = connection.factory.connector_classes[0]

    captured = {}

    class RecordingRedis:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def pubsub(self):
            return type("PubSub", (), {"ignore_subscribe_messages": False})()

    connector_cls("localhost:1", redis_cls=RecordingRedis, name="test")
    assert captured["socket_connect_timeout"] == 2.5
    assert captured["socket_timeout"] == 2.5


def test_swallowed_start_failure_raises_a_clear_error_and_leaves_no_client(make_connection):
    # BECClient.start() catches redis ConnectionError itself and returns (BMS-003)
    connection = make_connection(
        BecConnectionConfig(host="bec-host", port=6391), swallow_failure=True
    )

    with pytest.raises(
        BecUnavailableError, match=r"Could not connect to BEC's Redis at bec-host:6391"
    ):
        connection.connect()

    client = connection.factory.clients[0]
    assert client.shutdown_calls == [BecConnection.SHUTDOWN_THREAD_TIMEOUT_S]
    state = connection.state()
    assert state["state"] == "failed"
    assert "bec-host:6391" in state["last_error"]

    # the next call tries again with a fresh client
    with pytest.raises(BecUnavailableError):
        connection.connect()
    assert len(connection.factory.clients) == 2


def test_start_raising_a_redis_timeout_is_reported_with_the_deadline(make_connection):
    connection = make_connection(start_error=redis.exceptions.TimeoutError("Timeout reading"))

    with pytest.raises(BecUnavailableError, match="did not answer within 5 s"):
        connection.connect()
    assert connection.factory.clients[0].shutdown_calls


def test_refused_login_points_at_the_acl_file(make_connection):
    class BECAuthenticationError(Exception):
        pass

    connection = make_connection(start_error=BECAuthenticationError("Could not connect to Redis."))
    with pytest.raises(BecUnavailableError, match="--acl-file"):
        connection.connect()


def test_fallback_to_another_account_is_refused(make_connection):
    connection = make_connection(
        BecConnectionConfig(host="localhost", username="mcp_ro", password="pw"), username="default"
    )

    with pytest.raises(BecUnavailableError, match="did not accept the account 'mcp_ro'"):
        connection.connect()
    assert connection.factory.clients[0].shutdown_calls


def test_configured_account_is_accepted(make_connection):
    connection = make_connection(
        BecConnectionConfig(host="localhost", username="mcp_ro", password="pw"), username="mcp_ro"
    )
    connection.connect()
    assert connection.state()["account"] == "mcp_ro"


# ---------------------------------------------------------------------------- failures in use


def test_redis_failure_during_a_call_replaces_the_client(make_connection):
    connection = make_connection()

    with pytest.raises(BecUnavailableError, match="Lost the connection"):
        with connection.use():
            raise redis.exceptions.ConnectionError("Connection refused")

    assert connection.state()["state"] == "restart_pending"
    with connection.use() as client:
        pass

    first, second = connection.factory.clients
    assert client is second
    assert first.shutdown_calls
    assert "Redis failure" in connection.state()["last_restart_reason"]


def test_other_errors_propagate_and_keep_the_client(make_connection):
    connection = make_connection()

    with pytest.raises(KeyError):
        with connection.use():
            raise KeyError("samx")

    with connection.use():
        pass
    assert len(connection.factory.clients) == 1
    assert connection.state()["calls_in_flight"] == 0


def test_run_passes_the_client(make_connection):
    connection = make_connection()
    assert connection.run(lambda client, value: (client.started, value), 3) == (True, 3)


# ---------------------------------------------------------------------------- lifetime


def test_client_restarts_after_its_max_age_when_idle(make_connection, fake_clock):
    clock = fake_clock
    connection = make_connection(
        BecConnectionConfig(host="localhost", client_max_age_s=60), clock=clock
    )
    connection.connect()
    clock.now += 30
    assert connection.state()["client_age_s"] == 30.0

    clock.now += 31
    with connection.use() as client:
        pass

    first, second = connection.factory.clients
    assert client is second
    assert first.shutdown_calls
    assert "maximum age" in connection.state()["last_restart_reason"]
    assert connection.state()["client_age_s"] == 0.0


def test_busy_client_is_not_restarted_until_idle(make_connection, fake_clock):
    clock = fake_clock
    connection = make_connection(
        BecConnectionConfig(host="localhost", client_max_age_s=60), clock=clock
    )
    connection.connect()

    with connection.use() as outer:
        clock.now += 120
        # a long call still holds the aged client, so a concurrent call shares it
        with connection.use() as inner:
            assert inner is outer
        assert not outer.shutdown_calls

    with connection.use() as fresh:
        assert fresh is not outer
    assert outer.shutdown_calls


def test_zero_max_age_never_restarts(make_connection, fake_clock):
    clock = fake_clock
    connection = make_connection(
        BecConnectionConfig(host="localhost", client_max_age_s=0), clock=clock
    )
    connection.connect()
    clock.now += 10**7
    connection.connect()
    assert len(connection.factory.clients) == 1
    assert connection.state()["client_max_age_s"] is None


# ---------------------------------------------------------------------------- shutdown


def test_shutdown_stops_the_client_once_and_refuses_later_calls(make_connection):
    connection = make_connection()
    connection.connect()
    client = connection.factory.clients[0]

    connection.shutdown()
    connection.shutdown()

    assert client.shutdown_calls == [BecConnection.SHUTDOWN_THREAD_TIMEOUT_S]
    assert connection.state()["state"] == "closed"
    with pytest.raises(BecUnavailableError, match="shut down"):
        connection.connect()


def test_shutdown_without_a_client_is_a_no_op(make_connection):
    connection = make_connection()
    connection.shutdown()
    assert connection.factory.clients == []


def test_shutdown_during_a_slow_start_leaves_no_client_behind(make_connection, monkeypatch):
    monkeypatch.setattr(BecConnection, "START_LOCK_TIMEOUT_S", 0.05)
    gate = threading.Event()
    connection = make_connection(start_gate=gate)
    errors = []

    def call():
        try:
            connection.connect()
        except BecUnavailableError as exc:
            errors.append(exc)

    starter = threading.Thread(target=call)
    starter.start()
    while not connection.factory.clients:
        time.sleep(0.01)

    # shutdown gives up on the held start lock instead of hanging the exit path
    started = time.monotonic()
    connection.shutdown()
    assert time.monotonic() - started < 1

    gate.set()
    starter.join(5)
    assert errors and "shut down" in str(errors[0])
    # the starter shut its own client down instead of keeping a zombie
    assert connection.factory.clients[0].shutdown_calls


def test_waiting_for_another_start_is_bounded(make_connection, monkeypatch):
    monkeypatch.setattr(BecConnection, "START_LOCK_TIMEOUT_S", 0.05)
    gate = threading.Event()
    connection = make_connection(start_gate=gate)
    starter = threading.Thread(target=connection.connect)
    starter.start()
    while not connection.factory.clients:
        time.sleep(0.01)
    try:
        with pytest.raises(BecUnavailableError, match="still connecting"):
            connection.connect()
    finally:
        gate.set()
        starter.join(5)


# ---------------------------------------------------------------------------- stdout guard


def test_prints_inside_use_go_to_stderr(make_connection, capsys):
    connection = make_connection(on_start=lambda client: print("client banner"))

    with connection.use():
        print("rich table")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "client banner" in captured.err and "rich table" in captured.err


def test_prints_outside_use_and_on_other_threads_are_untouched(make_connection, capsys):
    connection = make_connection()
    with connection.use():
        worker = threading.Thread(target=print, args=("from another thread",))
        worker.start()
        worker.join()
    print("after")

    out = capsys.readouterr().out
    assert "from another thread" in out and "after" in out


# ---------------------------------------------------------------------------- service config


def test_service_config_leaves_redis_and_acl_to_bec_lib_unless_given(make_connection, tmp_path):
    connection = BecConnection(BecConnectionConfig())
    overrides = connection._service_config_overrides()
    # a site or deployment config must not be clobbered by a hardcoded localhost target
    assert "redis" not in overrides and "acl" not in overrides
    assert overrides["log_writer"]["base_path"] == str(tmp_path / "logs")
    assert (tmp_path / "logs").is_dir()

    connection = BecConnection(BecConnectionConfig(host="bec-host"))
    assert connection._service_config_overrides()["redis"] == {"host": "bec-host", "port": 6379}


def test_credentials_skip_the_deployment_acl_env_file():
    connection = BecConnection(BecConnectionConfig(username="mcp_ro", password="pw"))
    assert connection._service_config_overrides()["acl"] == {
        "env_file": "",
        "user": "mcp_ro",
        "password": "pw",
    }


def test_site_service_config_keeps_its_log_writer(monkeypatch):
    connection = BecConnection(BecConnectionConfig(service_config_path="/tmp/site.yaml"))
    assert "log_writer" not in connection._service_config_overrides()

    monkeypatch.setenv("BEC_SERVICE_CONFIG", '{"redis": {"host": "x", "port": 1}}')
    connection = BecConnection(BecConnectionConfig())
    assert "log_writer" not in connection._service_config_overrides()


def test_real_service_config_is_built_with_the_overrides(make_connection, tmp_path):
    connection = make_connection(
        BecConnectionConfig(host="bec-host", port=6391, log_dir=str(tmp_path / "mcp-logs"))
    )
    connection.connect()
    service_config = connection.factory.service_configs[0]
    assert service_config.redis == "bec-host:6391"
    assert service_config.config["log_writer"]["base_path"] == str(tmp_path / "mcp-logs")
    assert connection.redis_target == "bec-host:6391"


def test_missing_service_config_file_is_a_clear_error(make_connection, tmp_path):
    connection = make_connection(BecConnectionConfig(service_config_path=str(tmp_path / "no.yaml")))
    with pytest.raises(BecUnavailableError, match="service config not found"):
        connection.connect()


# ---------------------------------------------------------------------------- configuration


def test_config_from_env_leaves_unset_fields_none():
    config = BecConnectionConfig.from_env({})
    assert (config.host, config.port, config.username, config.password) == (None,) * 4
    assert config.client_max_age_s == connection_module.DEFAULT_CLIENT_MAX_AGE_S


def test_config_from_env_reads_all_variables(tmp_path):
    config = BecConnectionConfig.from_env(
        {
            "BEC_REDIS_HOST": "bec-host",
            "BEC_REDIS_PORT": "6391",
            "BEC_REDIS_USERNAME": "mcp_ro",
            "BEC_REDIS_PASSWORD": "pw",
            "BEC_MCP_SERVICE_CONFIG": "/etc/bec/client.yaml",
            "BEC_MCP_LOG_DIR": "/var/log/bec-mcp",
            "BEC_MCP_CLIENT_MAX_AGE": "3600",
            "BEC_MCP_REDIS_TIMEOUT": "2",
        }
    )
    assert config == BecConnectionConfig(
        host="bec-host",
        port=6391,
        username="mcp_ro",
        password="pw",
        service_config_path="/etc/bec/client.yaml",
        log_dir="/var/log/bec-mcp",
        client_max_age_s=3600.0,
        redis_timeout_s=2.0,
    )


@pytest.mark.parametrize(
    "env, message",
    [
        ({"BEC_REDIS_PORT": "63x79"}, "BEC_REDIS_PORT"),
        ({"BEC_MCP_CLIENT_MAX_AGE": "-1"}, "BEC_MCP_CLIENT_MAX_AGE"),
        ({"BEC_MCP_REDIS_TIMEOUT": "0"}, "BEC_MCP_REDIS_TIMEOUT"),
    ],
)
def test_config_typos_exit_with_a_readable_message(env, message):
    with pytest.raises(SystemExit, match=message):
        BecConnectionConfig.from_env(env)


def test_acl_file_from_env_and_explicit_credentials_win(tmp_path):
    acl = tmp_path / "mcp_ro.env"
    acl.write_text("REDIS_USER=mcp_ro\nREDIS_PASSWORD=s3cret\n", encoding="utf-8")

    config = BecConnectionConfig.from_env({"BEC_MCP_ACL_FILE": str(acl)})
    assert (config.username, config.password) == ("mcp_ro", "s3cret")

    config = BecConnectionConfig.from_env(
        {"BEC_MCP_ACL_FILE": str(acl), "BEC_REDIS_USERNAME": "override", "BEC_REDIS_PASSWORD": "pw"}
    )
    assert (config.username, config.password) == ("override", "pw")

    with pytest.raises(SystemExit, match="must be set together"):
        BecConnectionConfig.from_env({"BEC_MCP_ACL_FILE": str(acl), "BEC_REDIS_USERNAME": "x"})


def test_cli_overrides(tmp_path):
    acl = tmp_path / "acl.env"
    acl.write_text("REDIS_USER=mcp_ro\nREDIS_PASSWORD=pw\n", encoding="utf-8")
    config = BecConnectionConfig().with_overrides(
        redis="bec-host:6391",
        service_config_path="/etc/bec/client.yaml",
        acl_file=str(acl),
        client_max_age_s=0,
    )
    assert (config.host, config.port) == ("bec-host", 6391)
    assert (config.username, config.password) == ("mcp_ro", "pw")
    assert config.service_config_path == "/etc/bec/client.yaml"
    assert config.client_max_age_s == 0

    explicit = BecConnectionConfig(username="u", password="p").with_overrides(acl_file=str(acl))
    assert (explicit.username, explicit.password) == ("u", "p")
    assert BecConnectionConfig().with_overrides() == BecConnectionConfig()


@pytest.mark.parametrize(
    "value, expected",
    [
        ("bec-host:6391", ("bec-host", 6391)),
        ("bec-host", ("bec-host", None)),
        ("[::1]:6380", ("::1", 6380)),
        ("[::1]", ("::1", None)),
        (":6391", (None, 6391)),
    ],
)
def test_parse_redis_address(value, expected):
    assert parse_redis_address(value) == expected


def test_parse_redis_address_rejects_a_bad_port():
    with pytest.raises(SystemExit, match="port must be an integer"):
        parse_redis_address("bec-host:abc")


def test_read_acl_env_file_parses_and_validates(tmp_path):
    acl = tmp_path / "mcp_ro.env"
    acl.write_text(
        "# service account\nREDIS_USER = mcp_ro\nREDIS_PASSWORD='s3cret'\nIGNORED\n",
        encoding="utf-8",
    )
    assert read_acl_env_file(str(acl)) == ("mcp_ro", "s3cret")

    # dotenv style: "export " prefixes, and only matched quote pairs are stripped
    acl.write_text("export REDIS_USER=mcp_ro\nexport REDIS_PASSWORD=secret'\n", encoding="utf-8")
    assert read_acl_env_file(str(acl)) == ("mcp_ro", "secret'")

    acl.write_text("REDIS_USER=mcp_ro\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="must define REDIS_USER and REDIS_PASSWORD"):
        read_acl_env_file(str(acl))

    with pytest.raises(SystemExit, match="could not read ACL env file"):
        read_acl_env_file(str(tmp_path / "missing.env"))
