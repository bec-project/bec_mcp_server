"""bec_log_range and bec_log_analysis on the log cache, against fake streams of real bec_lib types."""

from __future__ import annotations

import json

import anyio
import pytest
from log_fakes import NOW, SCAN_ABORTION_TRACEBACK, connection_for, log_message, stream_client
from mcp.client import Client

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.log_stream import parse_log_message
from bec_mcp_server.tools import log_analysis as la
from bec_mcp_server.tools.log_rules import RULES, classify
from bec_mcp_server.tools.logs import LogSource

#: what a DeviceInstructionError leaves in the log on a BEC 4 stack, in stream order
LIMIT_FAILURE = [
    ("INFO", "ScanServer", "New scan queue:"),
    ("ERROR", "ScanServer", SCAN_ABORTION_TRACEBACK.split("\n\nThe above exception")[0]),
    (
        "ERROR",
        "ScanServer",
        "Alarm (MAJOR) raised for device 'samx': LimitError: position=50 not within limits "
        "(-5, 5)\nThe status DeviceStatus from device samx failed",
    ),
    ("ERROR", "ScanServer", SCAN_ABORTION_TRACEBACK),
    (
        "INFO",
        "ScanServer",
        "New scan status: None / aborted / ScanInfo({'scan_name': 'umv', 'scan_number': 7})",
    ),
]


def records_from(rows, start=NOW - 60.0, step=0.01):
    """LogRecords with cache ids from (level, service, message) rows."""
    out = []
    for index, (level, service, message) in enumerate(rows):
        record = parse_log_message(
            log_message(message, level=level, service=service, t=start + index * step)
        )
        out.append(record.__class__(**{**record.__dict__, "id": index + 1}))
    return out


def cached(records, capacity=3000) -> LogSource:
    return LogSource(records, capacity, cached=True)


def analyze(records, now=NOW, **kwargs):
    request = la.analysis_request(now=now, **kwargs)
    return la.log_analysis(records, request, cached(records), now=now)


def fill(n, start=NOW - 1000.0):
    return records_from(
        [("INFO", "ScanServer", f"line {i}") for i in range(n)], start=start, step=1
    )


# ---------------------------------------------------------------------------- bec_log_range


def test_range_around_a_record_shows_both_sides():
    records = fill(50)
    out = la.log_range(records, la.range_request(around=20, before=2, after=3), cached(records))

    assert [r["id"] for r in out["data"]["records"]] == [18, 19, 20, 21, 22, 23]
    selection = out["data"]["selection"]
    assert (selection["mode"], selection["more_before"], selection["more_after"]) == (
        "around",
        17,
        27,
    )
    assert "ids 18 to 23" in out["summary"]


def test_range_between_ids_reads_forward_from_start():
    records = fill(100)
    out = la.log_range(records, la.range_request(start=10, end=90, limit=5), cached(records))

    assert [r["id"] for r in out["data"]["records"]] == [10, 11, 12, 13, 14]
    assert out["data"]["selection"]["more_after"] == 76 + 10


def test_range_up_to_an_end_id_shows_the_records_before_it():
    records = fill(100)
    out = la.log_range(records, la.range_request(end=50, limit=3), cached(records))
    assert [r["id"] for r in out["data"]["records"]] == [48, 49, 50]


def test_range_by_time_accepts_iso_times_in_utc():
    records = fill(100)
    since = la._iso_ms(records[9].timestamp)  # pylint: disable=protected-access
    until = la._iso_ms(records[12].timestamp).replace(
        "+00:00", "Z"
    )  # pylint: disable=protected-access
    out = la.log_range(records, la.range_request(since=since, until=until), cached(records))
    assert [r["id"] for r in out["data"]["records"]] == [10, 11, 12, 13]


def test_range_without_arguments_shows_the_newest_records():
    records = fill(100)
    out = la.log_range(records, la.range_request(), cached(records))
    ids = [r["id"] for r in out["data"]["records"]]
    assert ids == list(range(61, 101))


def test_range_filters_apply_inside_the_range_but_keep_the_anchor():
    records = records_from(
        [
            ("INFO", "DeviceServer", "a"),
            ("INFO", "ScanServer", "b"),
            ("INFO", "ScanServer", "c"),
            ("ERROR", "DeviceServer", "d"),
        ]
    )
    out = la.log_range(
        records,
        la.range_request(around=2, before=1, after=2, service="DeviceServer"),
        cached(records),
    )
    assert [r["message"] for r in out["data"]["records"]] == ["a", "b", "d"]


def test_a_record_with_a_traceback_comes_parsed():
    records = records_from(LIMIT_FAILURE)
    out = la.log_range(records, la.range_request(around=4, before=0, after=0), cached(records))

    (record,) = out["data"]["records"]
    assert record["message"] == "ScanAbortion"
    assert record["traceback"]["root_cause"].startswith("DeviceInstructionError: An error")
    assert record["traceback"]["origin"]["path"] == "bec_server/scan_server/scans/scan_status.py"
    assert record["source"] == "bec_server.scan_server.scan_worker:run:1"
    assert "1 carry a traceback: ScanAbortion (id 4)" in out["summary"]


def test_logger_exception_records_carry_type_and_value():
    message = log_message(
        "probe failed",
        level="ERROR",
        exception={"type": "ZeroDivisionError", "value": "division by zero", "traceback": True},
    )
    record = la.record_digest(parse_log_message(message))
    assert record["exception"] == "ZeroDivisionError: division by zero"


@pytest.mark.parametrize(
    "kwargs, error, text",
    [
        ({"around": 3}, LookupError, "has left the log cache"),
        ({"around": 500}, LookupError, "does not exist; the newest record is 150"),
        ({"start": 10, "end": 5}, ValueError, "before start"),
        ({"since": "yesterday"}, ValueError, "ISO 8601"),
        ({"level": "loud"}, ValueError, "Unknown level"),
    ],
)
def test_range_errors_say_what_to_do(kwargs, error, text):
    records = fill(150)[100:]
    with pytest.raises(error, match=text):
        la.log_range(records, la.range_request(**kwargs), cached(records))


def test_ids_need_the_cache():
    records = [r.__class__(**{**r.__dict__, "id": None}) for r in fill(5)]
    source = LogSource(records, 10_000, cached=False)
    with pytest.raises(ValueError, match="need the server's log cache"):
        la.log_range(records, la.range_request(around=2), source)
    assert len(la.log_range(records, la.range_request(), source)["data"]["records"]) == 5


def test_range_output_stays_within_its_budget():
    records = records_from([("ERROR", "ScanServer", SCAN_ABORTION_TRACEBACK)] * 200)
    out = la.log_range(records, la.range_request(limit=200), cached(records))
    assert len(json.dumps(out)) < la.RANGE_CHAR_BUDGET * 1.3
    assert out["truncated"] is True
    assert out["data"]["records"][-1]["id"] == 200


def test_range_around_drops_the_farthest_records_first_when_over_budget():
    records = records_from([("ERROR", "ScanServer", SCAN_ABORTION_TRACEBACK)] * 200)
    out = la.log_range(
        records, la.range_request(around=100, before=100, after=100), cached(records)
    )
    ids = [r["id"] for r in out["data"]["records"]]
    assert 100 in ids
    assert abs((100 - ids[0]) - (ids[-1] - 100)) <= 1


# ---------------------------------------------------------------------------- bec_log_analysis


def test_a_device_error_and_the_abort_it_causes_are_one_incident_with_the_alarm_beside_it():
    out = analyze(records_from(LIMIT_FAILURE))
    incidents = {i["title"]: i for i in out["data"]["incidents"]}

    failure = incidents["DeviceInstructionError in ScanServer"]
    assert (failure["count"], failure["ids"]) == (2, [2, 4])
    assert failure["category"] == "limits"
    assert failure["devices"] == ["samx"]
    assert failure["scan"] == {"name": "umv", "number": 7}
    assert failure["traceback"]["origin"]["function"] == "_raise_if_failed"
    assert "logged as ScanAbortion" in failure["message"]
    assert failure["suggestion"].endswith("(dev.samx.limits = [low, high])")

    alarm = incidents["MAJOR alarm on samx"]
    assert alarm["alarm"] == {"severity": "MAJOR", "device": "samx"}
    assert alarm["related"] == [failure["ref"]]
    assert "started together" in out["summary"]
    assert f"the first, {failure['ref']} DeviceInstructionError" in out["summary"]


def test_findings_point_at_the_device_and_carry_cause_and_suggestion():
    out = analyze(records_from(LIMIT_FAILURE))
    finding = out["findings"][0]
    assert finding.startswith("Error: I")
    assert "Likely cause: a move was requested to a position outside" in finding
    assert 'Next: bec_device_diagnose(device="samx").' in finding
    assert out["data"]["findings"][0]["next"] == {
        "tool": "bec_device_diagnose",
        "args": {"device": "samx"},
    }


def test_context_shows_the_records_before_the_first_occurrence():
    rows = [("INFO", "ScanServer", "Scan 7 started"), ("INFO", "DeviceServer", "samx moving")]
    out = analyze(records_from(rows + LIMIT_FAILURE))
    failure = next(i for i in out["data"]["incidents"] if i["kind"] == "traceback")
    assert [c["message"] for c in failure["before"]] == [
        "Scan 7 started",
        "samx moving",
        "New scan queue:",
    ]
    assert failure["after"][0]["message"].startswith("Alarm (MAJOR) raised")


def test_repeated_errors_collapse_and_are_called_a_loop():
    rows = [
        ("ERROR", "SciHub", f"failed to connect to bec-atlas-qa (attempt {i})") for i in range(40)
    ]
    out = analyze(records_from(rows, step=0.5))
    (incident,) = out["data"]["incidents"]
    assert incident["count"] == 40
    assert incident["category"] == "unreachable"
    assert "it repeats (40 records" in incident["likely_cause"]
    assert out["data"]["findings"][0]["next"] == {"tool": "bec_status", "args": {}}


def test_unclassified_errors_point_at_their_records():
    out = analyze(records_from([("ERROR", "FileWriter", "something odd happened")]))
    (incident,) = out["data"]["incidents"]
    assert incident["category"] == "unclassified"
    assert out["data"]["findings"][0]["next"] == {"tool": "bec_log_range", "args": {"around": 1}}


def test_warnings_count_only_when_asked_unless_they_carry_a_traceback():
    rows = [
        ("WARNING", "ScanServer", "slow readback"),
        ("WARNING", "DeviceServer", f"retrying\n{SCAN_ABORTION_TRACEBACK}"),
    ]
    default = analyze(records_from(rows))
    assert [i["services"] for i in default["data"]["incidents"]] == [["DeviceServer"]]
    assert default["data"]["incidents"][0]["severity"] == "warning"

    warnings = analyze(records_from(rows), level="warning")
    assert len(warnings["data"]["incidents"]) == 2


def test_an_info_record_that_only_mentions_a_file_is_not_an_incident():
    out = analyze(records_from([("INFO", "ScanServer", 'Loaded File "x.py", line 3, in config')]))
    assert out["data"]["incidents"] == []


def test_client_alarm_echoes_are_not_separate_incidents():
    echo = "An alarm has occurred. Severity: MINOR.\nValueError.\n\t boom"
    rows = [("ERROR", "ScanServer", "Alarm (MINOR) raised for device 'samy': ValueError: boom")]
    out = analyze(records_from(rows + [("WARNING", "BECIPythonClient", echo)]), level="warning")
    assert [i["title"] for i in out["data"]["incidents"]] == ["MINOR alarm on samy"]


def test_window_and_service_filters():
    rows = [("ERROR", "ScanServer", "old"), ("ERROR", "DeviceServer", "new")]
    records = records_from(rows, start=NOW - 600, step=590)
    assert [i["message"] for i in analyze(records, minutes=1)["data"]["incidents"]] == ["new"]
    assert [i["message"] for i in analyze(records, service="scan")["data"]["incidents"]] == ["old"]


def test_most_severe_and_most_recent_come_first_and_limit_cuts():
    rows = [
        ("ERROR", "A", "first error"),
        ("WARNING", "B", f"tb\n{SCAN_ABORTION_TRACEBACK}"),
        ("ERROR", "C", "second error"),
    ]
    out = analyze(records_from(rows, step=60), limit=2)
    assert [i["message"] for i in out["data"]["incidents"]] == ["second error", "first error"]
    assert out["truncated"] is True
    assert out["data"]["incidents_found"] == 3


def test_a_quiet_log_says_so():
    out = analyze(fill(10))
    assert out["summary"] == "No errors, tracebacks or alarms in the 10 cached records."
    assert out["findings"] == []


@pytest.mark.parametrize(
    "types, text, category",
    [
        ("", "LimitError: position=50 not within limits (-5, 5)", "limits"),
        ("bec_server.scan_server.errors.ScanAbortion", "", "scan_aborted"),
        ("redis.exceptions.ConnectionError", "Error 61 connecting to localhost:6379", "redis"),
        ("redis.exceptions.TimeoutError", "Timeout reading from socket", "redis"),
        ("", "samx: PV X10DA-ES1:MOTOR.RBV not connected", "epics"),
        ("TimeoutError", "samx did not reach its target in time", "timeout"),
        ("", "failed to connect to bec-atlas-qa.psi.ch", "unreachable"),
        ("ModuleNotFoundError", "No module named 'debye_bec'", "missing_module"),
        ("redis.exceptions.NoPermissionError", "NOPERM user mcp_ro", "permission"),
        ("pydantic_core.ValidationError", "1 validation error for ScanQueueMessage", "validation"),
        ("", "Device samz not found", "unknown_device"),
        ("", "Device samx is disabled", "device_disabled"),
        ("OSError", "No space left on device", "file_io"),
        ("", "Failed to initialize device eiger: boom", "device_init"),
        ("AttributeError", "'NoneType' object has no attribute 'get'", "code_error"),
        ("", "something odd", "unclassified"),
    ],
)
def test_rules_name_the_usual_suspect(types, text, category):
    assert classify(types, text).category == category


def test_every_rule_has_a_cause_and_a_suggestion():
    for rule in RULES:
        assert rule.cause and rule.suggestion and not rule.cause.endswith(".")


# ---------------------------------------------------------------------------- through the server


def _list_tools(server):
    async def run():
        async with Client(server, mode="legacy") as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}

    return anyio.run(run)


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _stream(rows, start=NOW - 60.0):
    return [
        log_message(message, level=level, service=service, t=start + i * 0.01)
        for i, (level, service, message) in enumerate(rows)
    ]


def test_the_app_serves_both_tools_read_only_with_flat_optional_arguments():
    tools = _list_tools(build_app(connection=connection_for(stream_client())).server)
    for name in ("bec_log_range", "bec_log_analysis"):
        tool = tools[name]
        assert tool.annotations.read_only_hint is True
        assert not tool.input_schema.get("required")
        assert all("properties" not in prop for prop in tool.input_schema["properties"].values())
    assert set(tools["bec_log_range"].input_schema["properties"]) == {
        "around",
        "before",
        "after",
        "start",
        "end",
        "since",
        "until",
        "service",
        "level",
        "limit",
    }
    assert set(tools["bec_log_analysis"].input_schema["properties"]) == {
        "minutes",
        "service",
        "level",
        "limit",
    }


def test_analysis_and_range_through_the_app_on_the_cache(call_tool, stdout_guard):
    client = stream_client(_stream(LIMIT_FAILURE))
    connection = connection_for(client)
    app = build_app(connection=connection)
    with stdout_guard():
        analysis = _payload(call_tool(app.server, "bec_log_analysis", {"level": "ERROR"}))
        first_id = analysis["data"]["incidents"][0]["first_id"]
        client.connector.publish(log_message("after the analysis", t=NOW))
        around = _payload(call_tool(app.server, "bec_log_range", {"around": first_id}))
    assert analysis["data"]["cache"]["source"] == "cache"
    assert around["data"]["records"][-1]["message"] == "after the analysis"
    assert around["data"]["records"][-1]["id"] == len(LIMIT_FAILURE) + 1
    # the cache seeded once; the second call read no stream
    assert client.connector.get_last_calls == [10_000]
    connection.shutdown()


def test_without_the_cache_the_tools_read_the_stream(call_tool):
    client = stream_client(_stream(LIMIT_FAILURE))
    app = build_app(connection=connection_for(client, log_cache_size=0))
    analysis = _payload(call_tool(app.server, "bec_log_analysis"))
    assert analysis["data"]["cache"]["source"] == "stream"
    assert analysis["data"]["incidents"][0]["first_id"] is None
    assert analysis["data"]["findings"][0]["next"]["tool"] == "bec_device_diagnose"
    refused = call_tool(app.server, "bec_log_range", {"around": 2})
    assert refused.is_error and "log cache" in refused.content[0].text


def test_when_bec_goes_away_the_cached_records_are_still_served(call_tool):
    client = stream_client(_stream(LIMIT_FAILURE))
    connection = connection_for(client)
    app = build_app(connection=connection)
    _payload(call_tool(app.server, "bec_log_analysis"))

    client.connector.up = False
    connection._mark_broken(
        connection._generation, "Redis went away"
    )  # pylint: disable=protected-access
    out = _payload(call_tool(app.server, "bec_log_analysis"))

    assert out["summary"].startswith("BEC is unreachable")
    assert out["data"]["incidents_found"] == 2
    connection.shutdown()


def test_bec_logs_entries_and_alarms_carry_cache_ids(call_tool):
    rows = [
        ("ERROR", "ScanServer", "boom"),
        ("ERROR", "ScanServer", "Alarm (MAJOR) raised: no beam"),
    ]
    app = build_app(connection=connection_for(stream_client(_stream(rows))))
    out = _payload(call_tool(app.server, "bec_logs"))
    assert out["data"]["entries"][0]["last_id"] == 1
    assert out["data"]["alarms"][0]["last_id"] == 2
    assert out["findings"][0].endswith("Next: bec_log_range(around=2).")


def test_mcp_status_shows_the_cache(call_tool):
    connection = connection_for(stream_client(_stream(LIMIT_FAILURE)))
    app = build_app(connection=connection)
    _payload(call_tool(app.server, "bec_log_analysis"))
    status = _payload(call_tool(app.server, "mcp_status"))
    cache = status["data"]["bec"]["log_cache"]
    assert (cache["state"], cache["records"], cache["last_id"]) == ("following", 5, 5)
    connection.shutdown()
