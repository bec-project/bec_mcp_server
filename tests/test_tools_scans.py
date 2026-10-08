import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from bec_lib.device import DeviceBase, Positioner
from bec_lib.endpoints import MessageEndpoints
from bec_lib.messages import AvailableResourceMessage, ScanHistoryMessage
from bec_lib.scan_input_validator import ScanInputValidator
from mcp.client import Client

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecUnavailableError
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import scans

#: available_scans as published by a BEC 4.0 scan server running the demo config, plus two
#: plugin scans (timepix_scan, xas_simple_scan) from a superxas_bec stack
BEC4_SCANS = json.loads((Path(__file__).parent / "data" / "bec4_available_scans.json").read_text())
USER_SCANS = sorted(name for name, info in BEC4_SCANS.items() if not info.get("is_internal"))


class FakeConnector:
    """The two connector reads the scan tools make, with bec_lib's return conventions."""

    def __init__(self, available=None, history=()):
        self.available = available
        self.history = list(history)  # oldest first, like the Redis stream
        self.requested_counts = []

    def get(self, endpoint):
        assert endpoint.endpoint == MessageEndpoints.available_scans().endpoint
        if self.available is None:
            return None
        return AvailableResourceMessage(resource=self.available)

    def get_last(self, endpoint, count=1):
        assert endpoint.endpoint == MessageEndpoints.scan_history().endpoint
        self.requested_counts.append(count)
        entries = [{"data": msg} for msg in self.history[-count:]]
        if not entries:
            return None
        return entries if count > 1 else entries[0]


def make_client(available=BEC4_SCANS, history=(), motors=("samx", "samy")):
    devices = [Positioner(name=name) for name in motors] + [DeviceBase(name="bpm4i")]
    return SimpleNamespace(
        connector=FakeConnector(available, history),
        device_manager=SimpleNamespace(devices=SimpleNamespace(enabled_devices=devices)),
    )


def history_msg(number, status="closed", reason=None, start=1_791_400_000.0, duration=5.0):
    return ScanHistoryMessage(
        scan_id=f"id-{number}",
        scan_number=number,
        dataset_number=number,
        file_path=f"/data/S{number:05d}/S{number:05d}_master.h5",
        exit_status=status,
        reason=reason,
        start_time=start + number * 10,
        end_time=start + number * 10 + duration,
        scan_name="line_scan",
        num_points=20,
    )


def test_list_scans_matches_the_published_scans_without_internal_ones():
    result = scans.scan_list_digest(make_client())

    assert result["ok"] is True
    names = [row["name"] for row in result["data"]["scans"]]
    assert names == USER_SCANS
    assert "device_rpc" not in names
    assert result["summary"].startswith(f"{len(USER_SCANS)} scans available")


def test_list_scans_reports_moves_and_descriptions():
    rows = {row["name"]: row for row in scans.scan_list_digest(make_client())["data"]["scans"]}

    assert rows["line_scan"]["description"] == "A line scan for one or more motors."
    assert rows["line_scan"]["is_scan"] is True
    assert rows["mv"]["is_scan"] is False
    assert rows["umv"]["is_scan"] is False


def test_list_scans_filters_by_name():
    result = scans.scan_list_digest(make_client(), name_contains="LINE")

    assert [row["name"] for row in result["data"]["scans"]] == [
        "cont_line_scan",
        "line_scan",
        "line_sweep_scan",
        "multi_region_line_scan",
    ]


def test_list_scans_says_when_the_scan_server_published_nothing():
    result = scans.scan_list_digest(make_client(available=None))

    assert result["ok"] is False
    assert "bec_status" in result["summary"]
    assert result["data"]["scans"] == []
    assert result["findings"] == [
        "Error: The scan server has not published any scans; it may not be running. "
        "Next: bec_status()."
    ]


def test_scan_help_line_scan_builds_a_pasteable_example():
    result = scans.scan_help_digest(make_client(), "line_scan")

    assert result["ok"] is True
    data = result["data"]
    assert data["example"] == "scans.line_scan(dev.samx, -1.0, 1.0, steps=10, relative=True)"
    assert data["description"] == "A line scan for one or more motors."
    assert data["repeat"] == "pass (device, start, stop) as a group, at least 1 time"
    assert result["findings"] == []
    assert data["example"] in result["summary"]


def test_scan_help_lists_arguments_with_units_bounds_and_defaults():
    arguments = {
        arg["name"]: arg
        for arg in scans.scan_help_digest(make_client(), "line_scan")["data"]["arguments"]
    }

    assert list(arguments)[:5] == ["device", "start", "stop", "steps", "relative"]
    assert arguments["device"] == {
        "name": "device",
        "type": "device",
        "kind": "repeated",
        "required": True,
    }
    assert arguments["steps"]["required"] is True
    assert arguments["steps"]["constraint"] == "> 0"
    assert arguments["exp_time"] == {
        "name": "exp_time",
        "type": "float",
        "kind": "keyword",
        "required": False,
        "default": 0,
        "units": "s",
        "constraint": ">= 0",
        "description": "Exposure time in seconds",
    }
    assert "args" not in arguments and "kwargs" not in arguments


def test_scan_help_uses_two_distinct_motors_for_grid_scans():
    result = scans.scan_help_digest(make_client(), "grid_scan")

    assert result["data"]["example"] == (
        "scans.grid_scan(dev.samx, -1.0, 1.0, 10, dev.samy, -1.0, 1.0, 10, relative=True)"
    )
    assert result["data"]["repeat"] == (
        "pass (device, start, stop, steps) as a group, at least 2 times"
    )


def test_scan_help_keeps_bounded_ranges_inside_their_limits():
    # xas_simple_scan takes energies in eV with ge=4500
    result = scans.scan_help_digest(make_client(), "xas_simple_scan")

    assert result["data"]["example"] == "scans.xas_simple_scan(4500.0, 4950.0, 0.1, 0.1)"


@pytest.mark.parametrize("scan_name", USER_SCANS)
def test_every_bec4_example_passes_bec_lib_input_validation(scan_name):
    """The example must survive the same validation the BEC client runs before sending."""
    example = scans.scan_help_digest(make_client(), scan_name)["data"]["example"]
    devices = {name: Positioner(name=name) for name in ("samx", "samy")}
    recorded = {}

    def record(*args, **kwargs):
        recorded.update(args=args, kwargs=kwargs)

    namespace = {"scans": SimpleNamespace(**{scan_name: record}), "dev": SimpleNamespace(**devices)}
    eval(example, namespace)  # pylint: disable=eval-used

    validator = ScanInputValidator(device_manager=SimpleNamespace(devices=devices))
    validator.validate(scan_name, BEC4_SCANS[scan_name], recorded["args"], recorded["kwargs"])


@pytest.mark.parametrize("spelling", ["scans.line_scan", "line_scan()", "  line_scan "])
def test_scan_help_accepts_call_like_spellings(spelling):
    assert scans.scan_help_digest(make_client(), spelling)["data"]["name"] == "line_scan"


def test_scan_help_unknown_scan_suggests_close_names():
    with pytest.raises(LookupError, match=r"'linescan' not found\. Similar: line_scan.*bec_scans"):
        scans.scan_help_digest(make_client(), "linescan")


def test_scan_help_refuses_internal_scans():
    with pytest.raises(LookupError, match="'device_rpc' not found"):
        scans.scan_help_digest(make_client(), "device_rpc")


def test_scan_help_without_motors_marks_placeholders():
    result = scans.scan_help_digest(make_client(motors=()), "line_scan")

    assert (
        result["data"]["example"] == "scans.line_scan(dev.samx, -1.0, 1.0, steps=10, relative=True)"
    )
    assert result["findings"] == [
        "Info: This session has 0 enabled motors, so the example uses placeholders (dev.samx); "
        'replace them with real motors. Next: bec_devices(device_type="positioner", '
        "enabled_only=true)."
    ]
    assert result["data"]["findings"] == [
        {
            "severity": "info",
            "code": "example_placeholder_motors",
            "subject": "line_scan",
            "next": {
                "tool": "bec_devices",
                "args": {"device_type": "positioner", "enabled_only": True},
            },
        }
    ]


def test_scan_help_flags_the_missing_second_motor():
    # a laser-delay session with a single positioner, seen on a live superxas stack
    result = scans.scan_help_digest(make_client(motors=("laser_delay",)), "grid_scan")

    assert result["data"]["example"] == (
        "scans.grid_scan(dev.laser_delay, -1.0, 1.0, 10, dev.motor2, -1.0, 1.0, 10, relative=True)"
    )
    assert "dev.motor2" in result["findings"][0]
    assert result["findings"][0].startswith("Info: This session has 1 enabled motor,")


def test_scan_help_with_enough_motors_has_no_findings():
    assert scans.scan_help_digest(make_client(), "grid_scan")["findings"] == []


def test_scan_help_prefers_the_scan_authors_example_and_hides_hidden_options():
    payload = {
        "custom_scan": {
            "doc": "A custom scan.\n\nArgs:\n    gain (int): Detector gain.",
            "is_scan": True,
            "arg_input": {},
            "arg_bundle_size": {"bundle": 0, "min": None, "max": None},
            "signature": [
                {
                    "name": "gain",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": "_empty",
                    "annotation": {
                        "Annotated": {
                            "type": "int",
                            "metadata": {"ScanArgument": {"example": 7, "expert": True}},
                        }
                    },
                },
                {
                    "name": "mode",
                    "kind": "KEYWORD_ONLY",
                    "default": "_empty",
                    "annotation": {"Literal": ["fast", "slow"]},
                },
                {
                    "name": "debug",
                    "kind": "KEYWORD_ONLY",
                    "default": False,
                    "annotation": {
                        "Annotated": {
                            "type": "bool",
                            "metadata": {"ScanArgument": {"hidden": True}},
                        }
                    },
                },
            ],
        }
    }
    data = scans.scan_help_digest(make_client(available=payload), "custom_scan")["data"]

    assert data["example"] == "scans.custom_scan(7, mode='fast')"
    assert [arg["name"] for arg in data["arguments"]] == ["gain", "mode"]
    assert data["arguments"][0]["expert"] is True
    assert data["arguments"][0]["description"] == "Detector gain."
    assert data["arguments"][1]["type"] == "Literal['fast', 'slow']"


@pytest.mark.parametrize(
    "annotation, expected",
    [
        ("DeviceBase", "device"),
        (["float", "NoneType"], "float | None"),
        ({"Generic": {"origin": "list", "args": ["float"]}}, "list[float]"),
        ({"Literal": ["corridor", None]}, "Literal['corridor', None]"),
        ("_empty", "any"),
    ],
)
def test_render_type(annotation, expected):
    assert scans.render_type(annotation) == expected


def test_scan_history_is_newest_first_with_status_and_duration():
    history = [history_msg(n) for n in range(1, 6)]
    result = scans.scan_history_digest(make_client(history=history), limit=3)

    rows = result["data"]["scans"]
    assert [row["scan_number"] for row in rows] == [5, 4, 3]
    assert rows[0] == {
        "scan_number": 5,
        "scan_name": "line_scan",
        "status": "closed",
        "start_time": "2026-10-07T19:07:30+00:00",
        "duration_s": 5.0,
        "num_points": 20,
        "file_path": "/data/S00005/S00005_master.h5",
    }
    assert result["truncated"] is True
    assert result["summary"] == "Last 3 scans (numbers 3 to 5): 3 closed."


def test_scan_history_reports_scans_that_did_not_close_cleanly():
    history = [
        history_msg(1),
        history_msg(2, status="aborted", reason="alarm"),
        history_msg(3, status="user_completed"),
        history_msg(4, status="halted", reason="user"),
        history_msg(5, status="halted"),
    ]
    # scan 2 started ten minutes before "now", scan 5 nine and a half; both windows round up
    result = scans.scan_history_digest(make_client(history=history), now=1_791_400_620.0)

    assert result["findings"] == [
        "Warning: Scan 5 (line_scan) ended halted. "
        'Next: bec_logs(min_level="warning", minutes=11).',
        "Info: Scan 4 (line_scan) was halted by a user.",
        "Warning: Scan 2 (line_scan) ended aborted after an alarm. "
        'Next: bec_logs(min_level="warning", minutes=11).',
    ]
    assert [finding["code"] for finding in result["data"]["findings"]] == [
        "scan_not_closed",
        "scan_stopped_by_user",
        "scan_not_closed",
    ]
    assert result["truncated"] is False


def test_scan_history_reads_the_stream_not_the_client_cache():
    client = make_client(history=[history_msg(1)])
    scans.scan_history_digest(client, limit=500)

    # bounded, and one more than shown so truncation can be reported
    assert client.connector.requested_counts == [scans.MAX_HISTORY + 1]


def test_read_scan_history_handles_a_single_entry():
    assert [
        msg.scan_number for msg in scans.read_scan_history(make_client(history=[history_msg(7)]), 1)
    ] == [7]


def test_scan_history_empty():
    result = scans.scan_history_digest(make_client(history=()))

    assert result["ok"] is True
    assert result["data"] == {"scans": []}


class FakeConnection:
    """What build_app and the scan tools use of BecConnection."""

    def __init__(self, client=None, error=None):
        self.client = client
        self.error = error

    def run(self, func, *args):
        if self.error is not None:
            raise self.error
        return func(self.client, *args)

    def connect(self):
        pass

    def shutdown(self):
        pass

    def state(self):
        return {"state": "connected"}


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _app(client=None, error=None):
    return build_app(Settings(), connection=FakeConnection(client, error))


def test_tools_are_registered_read_only_in_diagnose_mode():
    async def list_tools():
        async with Client(_app(make_client()).server, mode="legacy") as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(list_tools)}
    assert {"bec_scans", "bec_scan_help", "bec_scan_history"} <= set(tools)
    for name in ("bec_scans", "bec_scan_help", "bec_scan_history"):
        assert tools[name].annotations.read_only_hint is True


@pytest.mark.parametrize("tool", ["bec_scans", "bec_scan_help", "bec_scan_history"])
def test_tools_answer_bare_calls_through_mcp_without_stdout(call_tool, stdout_guard, tool):
    app = _app(make_client(history=[history_msg(1), history_msg(2, status="aborted")]))
    with stdout_guard():
        payload = _payload(call_tool(app.server, tool))

    assert set(payload) == {"ok", "summary", "findings", "data", "truncated"}
    assert payload["ok"] is True


def test_bec_scan_help_through_mcp(call_tool):
    payload = _payload(
        call_tool(_app(make_client()).server, "bec_scan_help", {"scan_name": "grid_scan"})
    )

    assert payload["data"]["example"] == (
        "scans.grid_scan(dev.samx, -1.0, 1.0, 10, dev.samy, -1.0, 1.0, 10, relative=True)"
    )


def test_unknown_scan_reaches_the_agent_as_an_error_with_suggestions(call_tool):
    result = call_tool(_app(make_client()).server, "bec_scan_help", {"scan_name": "linescan"})

    assert result.is_error
    assert "Similar: line_scan" in result.content[0].text


def test_unreachable_bec_reaches_the_agent_with_its_reason(call_tool):
    error = BecUnavailableError("Could not connect to BEC Redis at localhost:6379")
    result = call_tool(_app(error=error).server, "bec_scans")

    assert result.is_error
    assert "Could not connect to BEC Redis at localhost:6379" in result.content[0].text


def test_tools_run_on_a_real_bec_connection(make_connection, call_tool, stdout_guard):
    fake = make_client(history=[history_msg(1)])

    def wire(client):
        # the conftest client has no scan endpoints; give it these, keeping its health checks
        client.connector.get = fake.connector.get
        client.connector.get_last = fake.connector.get_last

    app = build_app(Settings(), connection=make_connection(on_start=wire))
    with stdout_guard():
        payload = _payload(call_tool(app.server, "bec_scan_help"))

    # the conftest client has no device manager, so the example falls back to placeholders
    assert (
        payload["data"]["example"]
        == "scans.line_scan(dev.samx, -1.0, 1.0, steps=10, relative=True)"
    )
    assert payload["data"]["findings"][0]["code"] == "example_placeholder_motors"
