"""bec_logs against fake log streams built from real bec_lib message types."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import anyio
import pytest
import redis
from bec_lib import messages
from bec_lib.alarm_handler import AlarmBase, Alarms
from bec_lib.endpoints import MessageEndpoints
from bec_lib.redis_connector import RedisConnector
from mcp.client import Client

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecConnection, BecConnectionConfig, BecUnavailableError
from bec_mcp_server.tools.logs import (
    LOG_STREAM_CAPACITY,
    MAX_LIMIT,
    OUTPUT_CHAR_BUDGET,
    logs,
    logs_digest,
    message_shape,
    parse_alarm_line,
    read_log_stream,
)

NOW = 1_800_000_000.0
LEVEL_NO = {
    "DEBUG": 10,
    "INFO": 20,
    "CONSOLE_LOG": 21,
    "CONSOLE_LOG_ERROR": 22,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def log(
    message: str,
    *,
    level: str = "INFO",
    service: str = "ScanServer",
    t: float = NOW - 1,
    name: str = "bec_server.scan_server.scan_worker",
    function: str = "run",
) -> messages.LogMessage:
    """A LogMessage shaped like the one bec_lib's logger publishes (loguru serialize=True)."""
    record = {
        "message": message,
        "level": {"name": level, "no": LEVEL_NO[level], "icon": ""},
        "time": {"repr": "", "timestamp": t},
        "name": name,
        "function": function,
        "module": name.rsplit(".", 1)[-1],
        "line": 1,
        "exception": None,
        "extra": {},
    }
    return messages.LogMessage(
        log_type=level.lower(),
        log_msg={"text": f"{service} | {message}", "record": record, "service_name": service},
    )


def alarm(
    message: str,
    *,
    severity: Alarms = Alarms.MAJOR,
    compact: str | None = None,
    device: str | None = None,
    exception_type: str = "ValueError",
) -> AlarmBase:
    info = messages.ErrorInfo(
        error_message=message,
        compact_error_message=compact,
        exception_type=exception_type,
        device=device,
    )
    return AlarmBase(messages.AlarmMessage(severity=severity, info=info), severity=severity)


def raised_line(severity: Alarms, message: str, device: str | None = None) -> str:
    """The line RedisConnector.raise_alarm logs, captured from the installed bec_lib."""
    fake = SimpleNamespace(_managed_connection=mock.MagicMock(), notify=mock.MagicMock())
    info = messages.ErrorInfo(
        error_message=message, compact_error_message=None, exception_type="X", device=device
    )
    with mock.patch("bec_lib.redis_connector.hli.logger") as logger:
        RedisConnector.raise_alarm(fake, severity, info)
    call = (logger.error.call_args or logger.warning.call_args).args[0]
    return call


class FakeConnector:
    def __init__(self, stream: list[messages.LogMessage]):
        self.stream = stream
        self.calls: list[tuple[str, int]] = []
        self.up = True

    def ping(self):
        if not self.up:
            raise redis.exceptions.ConnectionError("Redis went away")
        return True

    def set_retry_enabled(self, enabled):
        pass

    def get_last(self, topic, key=None, count=1):
        self.calls.append((getattr(topic, "endpoint", topic), count))
        if not self.up:
            raise redis.exceptions.ConnectionError("Redis went away")
        newest = self.stream[-count:]
        if not newest:
            return None
        entries = [{"data": msg} for msg in newest]
        return entries if count > 1 else entries[0]


def fake_client(stream=(), alarms=()):
    handler = SimpleNamespace(get_unhandled_alarms=lambda severity=None: list(alarms))
    return SimpleNamespace(
        connector=FakeConnector(list(stream)),
        alarm_handler=handler,
        device_manager=object(),
        start=lambda: None,
        shutdown=lambda: None,
    )


def connection(client) -> BecConnection:
    """A real BecConnection whose client is the fake: lazy start, stdout guard, error mapping."""
    return BecConnection(BecConnectionConfig(), client_factory=lambda config, cls: client)


def digest(stream, alarms=(), **kwargs):
    kwargs.setdefault("now", NOW)
    return logs_digest(stream, alarms, **kwargs)


# --------------------------------------------------------------------------------------------
# reading the stream
# --------------------------------------------------------------------------------------------


def test_read_log_stream_reads_the_log_endpoint_up_to_capacity():
    client = fake_client([log("a"), log("b")])
    assert [m.log_msg["record"]["message"] for m in read_log_stream(client)] == ["a", "b"]
    assert client.connector.calls == [(MessageEndpoints.log().endpoint, LOG_STREAM_CAPACITY)]


def test_read_log_stream_handles_empty_stream_and_single_entry():
    assert read_log_stream(fake_client([])) == []
    assert len(read_log_stream(fake_client([log("only")]), count=1)) == 1


def test_bec_logs_on_empty_stream():
    out = logs(connection(fake_client()))
    assert out["ok"] is True
    assert out["summary"] == "The BEC log stream is empty."
    assert set(out) == {"ok", "summary", "findings", "data", "truncated"}


def test_bec_logs_writes_nothing_to_stdout(stdout_guard):
    stream = [log(f"m{i}", level="ERROR") for i in range(50)]
    with stdout_guard():
        logs(connection(fake_client(stream, [alarm("boom")])), level="error", minutes=5)


def test_unreachable_bec_raises_a_connection_error_for_the_agent():
    client = fake_client([log("a")])
    conn = connection(client)
    logs(conn)
    client.connector.up = False
    with pytest.raises(BecUnavailableError):
        logs(conn)


def test_bad_arguments_are_refused_before_bec_is_touched():
    client = fake_client([log("a")])
    with pytest.raises(ValueError, match="Unknown level 'loud'"):
        logs(connection(client), level="loud")
    assert client.connector.calls == []


# --------------------------------------------------------------------------------------------
# filters ported from bec_service_logs
# --------------------------------------------------------------------------------------------


def test_service_filter_is_a_case_insensitive_substring():
    stream = [log("a", service="ScanServer"), log("b", service="DeviceServer")]
    out = digest(stream, service="scanserv")
    assert [e["message"] for e in out["data"]["entries"]] == ["a"]


def test_service_filter_ignores_a_status_instance_suffix():
    stream = [log("a", service="BECIPythonClient"), log("b", service="DeviceServer")]
    out = digest(stream, service="BECIPythonClient/4b9d1af8")
    assert [e["message"] for e in out["data"]["entries"]] == ["a"]


def test_unknown_service_lists_the_services_present():
    out = digest([log("a", service="ScanServer"), log("b", service="DeviceServer")], service="nope")
    assert out["ok"] is True
    assert out["data"]["entries"] == []
    assert "DeviceServer, ScanServer" in out["summary"]


def test_level_keeps_that_level_and_above():
    stream = [log("i"), log("w", level="WARNING"), log("e", level="ERROR")]
    out = digest(stream, level="warning")
    assert [e["message"] for e in out["data"]["entries"]] == ["w", "e"]
    assert out["data"]["levels"] == {"ERROR": 1, "WARNING": 1}


def test_console_log_error_counts_as_error():
    stream = [log("console tb", level="CONSOLE_LOG_ERROR", service="BECIPythonClient_CONSOLE")]
    out = digest(stream, level="error")
    assert out["data"]["entries"][0]["level"] == "CONSOLE_LOG_ERROR"


def test_unknown_level_is_a_clear_error():
    with pytest.raises(ValueError) as error:
        digest([log("a")], level="loud")
    assert "warning" in str(error.value) and "console" not in str(error.value)


def test_text_filter_matches_message_and_logger_name():
    stream = [
        log("motor samx stalled"),
        log("frame dropped", name="superxas_bec.devices.timepix.client"),
        log("unrelated"),
    ]
    assert len(digest(stream, text="SAMX")["data"]["entries"]) == 1
    assert digest(stream, text="timepix")["data"]["entries"][0]["message"] == "frame dropped"


def test_ansi_codes_are_stripped():
    out = digest([log("\x1b[1mprimary queue\x1b[0m")])
    assert out["data"]["entries"][0]["message"] == "primary queue"


def test_string_log_msg_is_tolerated():
    msg = messages.LogMessage(log_type="error", log_msg="bare text")
    out = digest([msg], level="error")
    assert out["data"]["entries"][0] == {
        "service": None,
        "level": "ERROR",
        "count": 1,
        "last": None,
        "message": "bare text",
    }


# --------------------------------------------------------------------------------------------
# time window
# --------------------------------------------------------------------------------------------


def test_minutes_keeps_only_the_window():
    stream = [log("old", t=NOW - 3600), log("new", t=NOW - 60)]
    out = digest(stream, minutes=10)
    assert [e["message"] for e in out["data"]["entries"]] == ["new"]
    assert out["data"]["window"]["complete"] is True
    assert "last 10 min" in out["summary"]


def test_window_reports_when_a_full_stream_does_not_reach_back():
    stream = [log(f"spam {i}", t=NOW - 30 + i * 0.001) for i in range(100)]
    out = digest(stream, minutes=10, stream_capacity=100)
    assert out["data"]["window"]["complete"] is False
    assert "only reaches back 30 s" in out["summary"]


def test_invalid_minutes_is_a_clear_error():
    with pytest.raises(ValueError, match="minutes must be a positive number"):
        digest([log("a")], minutes=0)


# --------------------------------------------------------------------------------------------
# collapsing repeats
# --------------------------------------------------------------------------------------------


def test_identical_repeats_collapse_into_one_count():
    stream = [log("redis down", level="ERROR", t=NOW - 100 + i) for i in range(5)]
    (entry,) = digest(stream)["data"]["entries"]
    assert entry["count"] == 5
    assert entry["first"] < entry["last"]
    assert "variants" not in entry


def test_repeats_differing_in_numbers_collapse_with_variants():
    stream = [log(f"Number of pixels {n} does not match expected 5000.") for n in (10, 20, 10)]
    (entry,) = digest(stream)["data"]["entries"]
    assert (entry["count"], entry["variants"]) == (3, 2)
    assert entry["message"].startswith("Number of pixels 10 ")


def test_names_with_digits_stay_apart():
    out = digest([log("samx1 stalled"), log("samx2 stalled")])
    assert len(out["data"]["entries"]) == 2


def test_same_text_from_different_services_or_levels_stays_apart():
    stream = [log("x", service="A"), log("x", service="B"), log("x", service="A", level="ERROR")]
    assert len(digest(stream)["data"]["entries"]) == 3


def test_tracebacks_show_first_and_last_line_in_short_detail():
    tb = "Connection timed out: Traceback (most recent call last):\n  File a\n  File b\nOSError: refused"
    short = digest([log(tb, level="ERROR")])["data"]["entries"][0]
    full = digest([log(tb, level="ERROR")], detail="full")["data"]["entries"][0]
    assert short["message"] == (
        "Connection timed out: Traceback (most recent call last): … [2 more lines] … "
        "OSError: refused"
    )
    assert full["message"] == tb
    assert full["source"] == "bec_server.scan_server.scan_worker:run"


def test_message_shape_masks_ids_and_numbers_only():
    shape = message_shape("scan 12 id 3fa85f64-5717-4562-b3fc-2c963f66afa6 at 0xdeadbeef on roi2")
    assert shape == "scan # id <id> at 0x# on roi2"


def test_entries_are_the_newest_groups_oldest_first():
    stream = [log(f"msg-{chr(97 + i)}") for i in range(5)]
    out = digest(stream, limit=2)
    assert [e["message"] for e in out["data"]["entries"]] == ["msg-d", "msg-e"]
    assert out["truncated"] is True
    assert "raise limit" in out["summary"]


# --------------------------------------------------------------------------------------------
# bounded output on a full stream
# --------------------------------------------------------------------------------------------


def full_stream(make):
    return [make(i) for i in range(LOG_STREAM_CAPACITY)]


@pytest.mark.parametrize("detail", ["short", "full"])
def test_output_is_bounded_on_a_full_stream_of_distinct_tracebacks(detail):
    tb = "\n".join(f"  File frame{j}.py" for j in range(60))
    stream = full_stream(
        lambda i: log(f"failure kind {chr(65 + i % 26)}{i}\n{tb}\nE{i}Error: x", level="ERROR")
    )
    out = digest(stream, limit=MAX_LIMIT, detail=detail)
    assert out["data"]["window"]["scanned"] == LOG_STREAM_CAPACITY
    assert out["data"]["distinct_messages"] == LOG_STREAM_CAPACITY
    assert out["truncated"] is True
    assert len(out["data"]["entries"]) <= MAX_LIMIT
    assert sum(len(e["message"]) for e in out["data"]["entries"]) <= OUTPUT_CHAR_BUDGET
    assert len(json.dumps(out)) < OUTPUT_CHAR_BUDGET + 40_000


def test_a_full_stream_of_one_repeated_error_collapses_to_one_entry():
    stream = full_stream(
        lambda i: log(
            "RedisConnector failed to connect to redis (atlas:6380). Is the server running?",
            level="ERROR",
            service="SciHub",
            t=NOW - 37 + i * 0.0037,
        )
    )
    out = digest(stream, level="error", minutes=10)
    assert [e["count"] for e in out["data"]["entries"]] == [LOG_STREAM_CAPACITY]
    assert out["data"]["window"]["complete"] is False
    assert len(json.dumps(out)) < 2_000


def test_default_call_on_a_full_mixed_stream_stays_small():
    stream = full_stream(
        lambda i: log(f"point {i} of scan {i // 100} done", t=NOW - 600 + i * 0.05)
    )
    out = logs(connection(fake_client(stream)))
    assert out["data"]["distinct_messages"] == 1
    assert len(json.dumps(out)) < 2_000


# --------------------------------------------------------------------------------------------
# alarms
# --------------------------------------------------------------------------------------------


def test_alarm_line_formats_match_the_installed_bec_lib():
    raised = parse_alarm_line(raised_line(Alarms.MAJOR, "Motor not connected", device="samx"))
    assert (raised.severity, raised.device, raised.message, raised.echo) == (
        "MAJOR",
        "samx",
        "Motor not connected",
        False,
    )
    warning = parse_alarm_line(raised_line(Alarms.WARNING, "almost full"))
    assert (warning.severity, warning.device) == ("WARNING", None)
    echo = parse_alarm_line(str(alarm("long text", compact="short", severity=Alarms.MINOR)))
    assert (echo.severity, echo.exception_type, echo.message, echo.echo) == (
        "MINOR",
        "ValueError",
        "short",
        True,
    )


def test_raised_alarm_moves_from_entries_to_alarms_and_findings():
    line = raised_line(Alarms.MAJOR, "Motor not connected", device="samx")
    stream = [log("ok"), log(line, level="ERROR", service="DeviceServer")]
    out = digest(stream)
    assert [e["message"] for e in out["data"]["entries"]] == ["ok"]
    (entry,) = out["data"]["alarms"]
    assert entry["severity"] == "MAJOR" and entry["device"] == "samx"
    assert entry["services"] == ["DeviceServer"]
    assert out["findings"] == [
        "Error: MAJOR alarm on samx, raised once, last 1 s ago: Motor not connected. "
        'Next: bec_device_diagnose(device="samx").'
    ]
    assert out["data"]["findings"] == [
        {
            "severity": "error",
            "code": "alarm.major",
            "subject": "samx",
            "next": {"tool": "bec_device_diagnose", "args": {"device": "samx"}},
        }
    ]
    assert "1 alarm(s), 1 MAJOR" in out["summary"]


def test_alarm_findings_map_severities_and_skip_next_without_a_device():
    stream = [
        log(raised_line(Alarms.WARNING, "disk 91% full"), level="WARNING", t=NOW - 120),
        log(raised_line(Alarms.MINOR, "eiger slow", device="eiger"), level="ERROR", t=NOW - 60),
    ]
    out = digest(stream)
    assert out["findings"] == [
        "Warning: MINOR alarm on eiger, raised once, last 60 s ago: eiger slow. "
        'Next: bec_device_diagnose(device="eiger").',
        "Info: WARNING alarm, raised once, last 2 min ago: disk 91% full.",
    ]
    assert out["data"]["findings"][1] == {
        "severity": "info",
        "code": "alarm.warning",
        "subject": "alarms",
        "next": None,
    }


def test_client_echoes_merge_into_the_raised_alarm_without_inflating_the_count():
    raised = raised_line(Alarms.MINOR, "Detector temperature 41 C above limit")
    echo = str(alarm("Detector temperature 41 C above limit", severity=Alarms.MINOR))
    stream = [
        log(echo, level="WARNING", service="BECIPythonClient", t=NOW - 9.9),
        log(raised, level="ERROR", service="DeviceServer", t=NOW - 10),
        log(echo, level="WARNING", service="BECWidgets", t=NOW - 9.8),
    ]
    (entry,) = digest(stream)["data"]["alarms"]
    assert entry["count"] == 1
    assert entry["services"] == ["DeviceServer"]
    assert entry["exception_type"] == "ValueError"


def test_echo_with_compact_text_merges_by_time():
    raised = raised_line(Alarms.MINOR, "Traceback ...\nRuntimeError: stage failed for eiger")
    echo = str(alarm("unused", compact="eiger stage failed", severity=Alarms.MINOR))
    stream = [
        log(raised, level="ERROR", service="DeviceServer", t=NOW - 10),
        log(echo, level="WARNING", service="BECWidgets", t=NOW - 9),
    ]
    assert len(digest(stream)["data"]["alarms"]) == 1


def test_echo_only_alarms_count_raises_not_clients():
    first = str(alarm("disk almost full", severity=Alarms.WARNING))
    stream = [
        log(first, level="WARNING", service="BECIPythonClient", t=NOW - 100),
        log(first, level="WARNING", service="BECWidgets", t=NOW - 100),
        log(first, level="WARNING", service="BECIPythonClient", t=NOW - 50),
        log(first, level="WARNING", service="BECWidgets", t=NOW - 50),
    ]
    (entry,) = digest(stream)["data"]["alarms"]
    assert entry["count"] == 2
    assert entry["services"] == ["BECIPythonClient", "BECWidgets"]


def test_alarms_respect_level_and_window_filters():
    warning = raised_line(Alarms.WARNING, "almost full")
    major = raised_line(Alarms.MAJOR, "limit switch hit", device="samy")
    stream = [log(warning, level="WARNING", t=NOW - 30), log(major, level="ERROR", t=NOW - 3000)]
    assert [a["severity"] for a in digest(stream, level="error")["data"]["alarms"]] == ["MAJOR"]
    assert [a["severity"] for a in digest(stream, minutes=10)["data"]["alarms"]] == ["WARNING"]


def test_client_alarm_marks_the_matching_log_alarm_unhandled():
    line = raised_line(Alarms.MAJOR, "limit switch hit", device="samy")
    out = digest(
        [log(line, level="ERROR", service="DeviceServer")],
        [alarm("limit switch hit", device="samy", exception_type="LimitError")],
    )
    (entry,) = out["data"]["alarms"]
    assert entry["unhandled"] is True
    assert entry["exception_type"] == "LimitError"
    assert entry["count"] == 1


def test_client_alarm_without_a_log_line_shows_only_without_a_window():
    stack = [alarm("trimmed long ago", device="samz")]
    shown = digest([log("ok")], stack)
    (entry,) = shown["data"]["alarms"]
    assert (entry["count"], entry["last"], entry["unhandled"]) == (1, None, True)
    windowed = digest([log("ok")], stack, minutes=10)
    assert windowed["data"]["alarms"] == []
    assert "1 unhandled MAJOR alarm(s)" in windowed["summary"]


def test_client_alarms_follow_text_and_service_filters():
    stack = [alarm("limit switch hit", device="samy")]
    assert len(digest([log("ok")], stack, text="samy")["data"]["alarms"]) == 1
    assert digest([log("ok")], stack, text="eiger")["data"]["alarms"] == []
    assert digest([log("ok")], stack, service="DeviceServer")["data"]["alarms"] == []


def test_bec_logs_reads_the_client_alarm_stack():
    client = fake_client([log("ok")], [alarm("held", device="samx")])
    assert logs(connection(client))["data"]["alarms"][0]["device"] == "samx"


# --------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------


def _list_tools(server):
    async def run():
        async with Client(server, mode="legacy") as client:
            return (await client.list_tools()).tools

    return anyio.run(run)


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_the_app_serves_bec_logs_with_a_flat_read_only_schema():
    tools = {
        tool.name: tool
        for tool in _list_tools(build_app(connection=connection(fake_client())).server)
    }
    tool = tools["bec_logs"]
    props = tool.input_schema["properties"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert set(props) == {"service", "level", "text", "minutes", "limit", "detail"}
    assert "error" in json.dumps(props["level"])
    assert not tool.input_schema.get("required")
    assert tool.description.count(". ") == 0


def test_bec_logs_through_the_app(call_tool, stdout_guard):
    line = raised_line(Alarms.MAJOR, "limit switch hit", device="samy")
    stream = [log("boom", level="ERROR"), log(line, level="ERROR", service="DeviceServer")]
    app = build_app(connection=connection(fake_client(stream)))
    with stdout_guard():
        bare = _payload(call_tool(app.server, "bec_logs"))
        errors = _payload(call_tool(app.server, "bec_logs", {"level": "error", "minutes": 5}))
    assert bare["ok"] is True and bare["data"]["entries"][0]["message"] == "boom"
    assert errors["findings"][0].startswith("Error: MAJOR alarm on samy, raised once")


def test_bec_logs_through_the_app_when_bec_is_down(call_tool):
    client = fake_client([log("a")])
    client.connector.up = False
    result = call_tool(build_app(connection=connection(client)).server, "bec_logs")
    assert result.is_error
    assert "Redis" in result.content[0].text


def test_bec_logs_through_the_app_ignores_letter_case(call_tool):
    # small models send ERROR, Full or SCANSERVER; the schema still lists lowercase values
    stream = [
        log("info line"),
        log("boom\nTraceback\nValueError: x", level="ERROR", service="ScanServer"),
        log("other", level="ERROR", service="DeviceServer"),
    ]
    app = build_app(connection=connection(fake_client(stream)))
    payload = _payload(
        call_tool(
            app.server, "bec_logs", {"level": "ERROR", "detail": "Full", "service": "SCANSERVER"}
        )
    )
    (entry,) = payload["data"]["entries"]
    assert entry["service"] == "ScanServer"
    assert entry["message"] == "boom\nTraceback\nValueError: x"


def test_level_and_detail_ignore_letter_case_for_direct_callers():
    stream = [log("i"), log("w\nlast", level="WARNING")]
    out = digest(stream, level=" Warning ", detail="FULL")
    assert [e["message"] for e in out["data"]["entries"]] == ["w\nlast"]


def test_bec_logs_through_the_app_refuses_an_unknown_level(call_tool):
    client = fake_client([log("a")])
    result = call_tool(build_app(connection=connection(client)).server, "bec_logs", {"level": "x"})
    assert result.is_error
    assert client.connector.calls == []
