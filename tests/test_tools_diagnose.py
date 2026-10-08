"""bec_diagnose and bec_device_diagnose: the collector against real bec_lib device proxies and an
in-memory stand-in for the client's connector, the merge with the other tools' findings, and
both tools through an MCP session.

bec_status, bec_scan_history and the alarms of bec_logs run for real here; the queue and
beamline-state digests are stubbed, since they read client objects these fakes do not model and
their own tests cover them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import redis.exceptions
from bec_lib import messages
from bec_lib.device import Positioner, Signal
from bec_lib.devicemanager import DeviceContainer, DeviceManagerBase
from bec_lib.endpoints import MessageEndpoints
from bec_lib.messages import BECStatus
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from bec_mcp_server.app import build_app
from bec_mcp_server.bec.connection import BecUnavailableError
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import Finding
from bec_mcp_server.tools import diagnose as tools
from bec_mcp_server.tools import tool_result

NOW = 1_800_000_000.0
CORE = ("DeviceServer", "ScanServer", "ScanBundler", "FileWriterManager")


class FakeConnector:
    """Keys, lists and streams by endpoint; ``fail`` makes reads of one endpoint raise."""

    username = "mcp_ro"

    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.lists: dict[str, list[Any]] = {}
        self.streams: dict[str, list[Any]] = {}
        self.fail: dict[str, BaseException] = {}

    def _key(self, topic: Any) -> str:
        key = getattr(topic, "endpoint", topic)
        if key in self.fail:
            raise self.fail[key]
        return key

    def ping(self) -> bool:
        return True

    def get(self, topic: Any, pipe: Any = None) -> Any:
        return self.keys.get(self._key(topic))

    def lrange(self, topic: Any, start: int, end: int, pipe: Any = None) -> list[Any]:
        items = self.lists.get(self._key(topic), [])
        return items[start : None if end == -1 else end + 1]

    def get_last(self, topic: Any, key: Any = None, count: int = 1) -> Any:
        entries = [{"data": message} for message in self.streams.get(self._key(topic), [])]
        entries = entries[-count:]
        if not entries:
            return None
        return entries if count > 1 else entries[0]

    def shutdown(self) -> None:
        pass


class FakeDeviceManager(DeviceManagerBase):
    """A client device manager holding real proxies, without a BEC service behind it."""

    # pylint: disable=super-init-not-called
    def __init__(self, connector: FakeConnector) -> None:
        self.connector = connector
        self.devices = DeviceContainer()
        self.parent = None
        self._allow_override = True

    def add(self, cls: type, name: str, *, enabled: bool = True) -> Any:
        config = {
            "name": name,
            "deviceClass": f"ophyd_devices.Sim{cls.__name__}",
            "enabled": enabled,
            "readoutPriority": "baseline",
        }
        device = cls(name=name, info={}, config=config, parent=self)
        self.devices[name] = device
        return device


class FakeBEC:
    """The parts of BECClient the tools read, preset to a healthy idle beamline."""

    def __init__(self) -> None:
        self.connector = FakeConnector()
        self.device_manager = FakeDeviceManager(self.connector)
        self.service_status = {
            name: messages.StatusMessage(name=name, status=BECStatus.RUNNING, info={})
            for name in CORE
        }
        self.alarm_handler = None
        self.queue_entries: list[Any] = []
        self.queue = SimpleNamespace(
            queue_storage=SimpleNamespace(current_scan_queue={}, _update_queue=self._update_queue)
        )
        for name in ("samx", "samy"):
            self.motor(name, 1.0)
        self.device_manager.add(Signal, "bpm4i")

    def _update_queue(self) -> None:
        self.queue.queue_storage.current_scan_queue = {
            "primary": SimpleNamespace(info=self.queue_entries, status="RUNNING", locks=[])
        }

    # -- state builders -----------------------------------------------------

    def motor(self, name: str, value: float, *, limits=(-10.0, 10.0), enabled=True, age=5.0):
        self.device_manager.add(Positioner, name, enabled=enabled)
        self.readback(name, value, age=age)
        low, high = limits
        self.connector.keys[MessageEndpoints.device_limits(name).endpoint] = messages.DeviceMessage(
            signals={"low": {"value": low}, "high": {"value": high}}
        )
        self.moving(name, False)

    def readback(self, name: str, value: Any, *, age: float = 5.0) -> None:
        self.connector.keys[MessageEndpoints.device_readback(name).endpoint] = (
            messages.DeviceMessage(
                signals={
                    name: {"value": value, "timestamp": NOW - age},
                    f"{name}_setpoint": {"value": value, "timestamp": NOW - age},
                }
            )
        )

    def moving(self, name: str, moving: bool) -> None:
        self.connector.keys[MessageEndpoints.device_status(name).endpoint] = (
            messages.DeviceStatusMessage(device=name, status=int(moving))
        )

    def log(self, text: str, *, level: str = "ERROR", service: str = "DeviceServer", age=30.0):
        level_no = {"WARNING": 30, "ERROR": 40, "INFO": 20}[level]
        record = {
            "message": text,
            "level": {"name": level, "no": level_no},
            "time": {"timestamp": NOW - age},
        }
        self.connector.streams.setdefault(MessageEndpoints.log().endpoint, []).append(
            messages.LogMessage(
                log_type=level.lower(), log_msg={"record": record, "service_name": service}
            )
        )

    def history(self, *exit_statuses: tuple[str, str | None]) -> None:
        self.connector.streams[MessageEndpoints.scan_history().endpoint] = [
            messages.ScanHistoryMessage(
                scan_id=f"id{number}",
                scan_number=number,
                dataset_number=number,
                file_path=f"/data/S{number:05d}.h5",
                exit_status=status,
                reason=reason,
                start_time=NOW - 600,
                end_time=NOW - 500,
                scan_name="line_scan",
            )
            for number, (status, reason) in enumerate(exit_statuses, start=1)
        ]


class StubConnection:
    """What the tool functions need from BecConnection: ``use()`` yielding the client."""

    redis_target = "localhost:6379"

    def __init__(self, client: Any) -> None:
        self.client = client

    @contextlib.contextmanager
    def use(self):
        if isinstance(self.client, BaseException):
            raise self.client
        yield self.client


class Peers:
    """Stand-ins for the queue and beamline-state digests, recording how they were called."""

    def __init__(self) -> None:
        self.queue = tool_result(True, "The queue is idle.")
        self.states = tool_result(True, "No beamline states are configured.")
        self.calls: list[tuple[str, tuple]] = []

    def queue_digest(self, client: Any, history: int = 5) -> dict:
        self.calls.append(("queue", (client, history)))
        if isinstance(self.queue, BaseException):
            raise self.queue
        return self.queue

    def beamline_states_digest(self, client: Any) -> dict:
        self.calls.append(("states", (client,)))
        return self.states


@pytest.fixture
def peers(monkeypatch) -> Peers:
    stand_ins = Peers()
    monkeypatch.setattr(tools.queue_states, "queue_digest", stand_ins.queue_digest)
    monkeypatch.setattr(
        tools.queue_states, "beamline_states_digest", stand_ins.beamline_states_digest
    )
    return stand_ins


@pytest.fixture
def bec(monkeypatch, peers) -> FakeBEC:
    monkeypatch.setattr(tools.time, "time", lambda: NOW)
    return FakeBEC()


def diagnose(bec: FakeBEC) -> dict:
    return tools.diagnose(StubConnection(bec))


def device_diagnose(bec: FakeBEC, name: str = "samx") -> dict:
    return tools.device_diagnose(StubConnection(bec), name)


def codes(result: dict) -> list[str]:
    return [finding["code"] for finding in result["data"].get("findings", [])]


# ---------------------------------------------------------------------------
# healthy state
# ---------------------------------------------------------------------------


def test_healthy_beamline(bec, peers):
    result = diagnose(bec)

    assert result["ok"] is True
    assert result["summary"] == "No problems found for the beamline."
    assert result["findings"] == []
    assert result["data"] == {
        "window_s": 3600.0,
        "services": {name: "RUNNING" for name in sorted(CORE)},
        "devices": {"configured": 3, "disabled": 0},
        "queues": {"primary": {"status": "RUNNING", "pending": 0}},
    }
    assert peers.calls == [("queue", (bec, 1)), ("states", (bec,))]


def test_healthy_device_reports_its_cached_facts(bec):
    result = device_diagnose(bec, "SAMX")

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["data"] == {
        "device": "samx",
        "configured": True,
        "enabled": True,
        "readback": 1.0,
        "readback_age_s": 5.0,
        "limits": [-10.0, 10.0],
        "moving": False,
        "window_s": 3600.0,
    }


def test_device_diagnose_calls_no_other_tool(bec, peers):
    bec.connector.fail[MessageEndpoints.scan_history().endpoint] = RuntimeError("boom")

    assert device_diagnose(bec)["ok"] is True
    assert peers.calls == []


# ---------------------------------------------------------------------------
# Jan's test: disable a device, narrow a motor's limits
# ---------------------------------------------------------------------------


def test_disabled_device_and_narrowed_limits(bec):
    bec.device_manager.devices["samy"]._config["enabled"] = False
    bec.connector.keys[MessageEndpoints.device_limits("samx").endpoint] = messages.DeviceMessage(
        signals={"low": {"value": -0.5}, "high": {"value": 0.5}}
    )

    beamline = diagnose(bec)
    samx = device_diagnose(bec, "samx")
    samy = device_diagnose(bec, "samy")

    assert codes(beamline) == ["devices.outside_limits", "devices.disabled"]
    assert beamline["findings"][0] == (
        "Warning: 1 device read outside its soft limits: samx (1 not in [-0.5, 0.5]). "
        'Next: bec_device_diagnose(device="samx").'
    )
    assert codes(samx) == ["device.outside_limits"]
    assert codes(samy) == ["device.disabled"]
    assert samy["summary"] == (
        "1 warning for samy. Most important: samy is disabled, so BEC does not use it until it "
        "is enabled again."
    )


def test_unknown_device_suggests_names(bec):
    result = device_diagnose(bec, "samz")

    assert codes(result) == ["device.not_found"]
    assert "Similar names: samy, samx." in result["findings"][0]
    assert result["data"]["configured"] is False


# ---------------------------------------------------------------------------
# each source the collector reads
# ---------------------------------------------------------------------------


def test_failed_initialization_from_progress_and_log(bec):
    bec.device_manager.devices["samy"]._config["enabled"] = False
    bec.connector.keys[MessageEndpoints.device_initialization_progress().endpoint] = (
        messages.DeviceInitializationProgressMessage(
            device="samy", finished=True, success=False, index=3, total=3
        )
    )
    bec.log(
        "Failed to initialize device samy: Traceback (most recent call last):\n"
        "  ...\nTimeoutError: IOC did not answer",
        level="WARNING",
    )

    result = device_diagnose(bec, "samy")

    assert codes(result) == ["device.init_failed"]
    assert '"TimeoutError: IOC did not answer"' in result["findings"][0]
    assert codes(diagnose(bec)) == ["devices.init_failed"]


def test_loading_config_is_a_note(bec):
    bec.connector.keys[MessageEndpoints.device_initialization_progress().endpoint] = (
        messages.DeviceInitializationProgressMessage(
            device="samy", finished=False, success=True, index=1, total=3
        )
    )

    assert codes(diagnose(bec)) == ["devices.loading"]
    assert codes(device_diagnose(bec, "samy")) == ["device.initializing"]


def test_moving_motor_while_the_queue_is_idle(bec):
    bec.moving("samx", True)

    assert codes(diagnose(bec)) == ["devices.moving_without_scan"]
    assert codes(device_diagnose(bec)) == ["device.moving_without_scan"]


def test_moving_motor_during_a_scan(bec):
    bec.moving("samx", True)
    bec.queue_entries = [SimpleNamespace(status="RUNNING")]

    assert codes(diagnose(bec)) == []
    assert codes(device_diagnose(bec)) == ["device.moving"]


def test_stale_and_missing_readbacks(bec):
    bec.readback("samx", 1.0, age=3 * 3600)
    del bec.connector.keys[MessageEndpoints.device_readback("samy").endpoint]

    assert codes(device_diagnose(bec, "samx")) == ["device.readback_old"]
    assert codes(device_diagnose(bec, "samy")) == ["device.no_readback"]
    # neither is a beamline-wide finding
    assert codes(diagnose(bec)) == []


def test_config_reset_from_history(bec):
    bec.connector.lists[MessageEndpoints.device_config_history().endpoint] = [
        messages.AvailableResourceMessage(
            resource=[{"name": "samx"}, {"name": "gone"}], metadata={"removed_at": NOW - 120}
        ),
        messages.AvailableResourceMessage(
            resource=[{"name": "samx"}], metadata={"removed_at": NOW - 9000}
        ),
    ]

    assert codes(diagnose(bec)) == ["devices.config_reset"]
    gone = device_diagnose(bec, "gone")
    assert codes(gone) == ["device.config_reset"]
    assert "reset 2 min ago" in gone["findings"][0]


def test_log_errors_are_counted_per_service_and_device(bec):
    bec.log("samx did not reach its target", service="ScanServer")
    bec.log("samx moved", level="INFO", service="ScanServer")
    bec.log("old failure about samx", service="ScanServer", age=7200)

    beamline = diagnose(bec)
    samx = device_diagnose(bec)

    assert codes(beamline) == ["logs.errors"]
    assert "1 error log record in the last 60 min from ScanServer" in beamline["findings"][0]
    assert codes(samx) == ["device.log_errors"]


def test_window_minutes_widens_the_log_window(bec):
    bec.log("old failure about samx", service="ScanServer", age=7200)

    assert codes(tools.device_diagnose(StubConnection(bec), "samx", 180)) == ["device.log_errors"]


def test_alarms_name_their_device_and_are_not_log_errors(bec):
    bec.log("Alarm (MAJOR) raised for device 'samx': Traceback\nLimitError: samx at limit")
    bec.log(
        "An alarm has occurred. Severity: MAJOR.\nLimitError.\n\t samx at limit",
        service="BECIPythonClient",
    )
    bec.log("Alarm (MINOR) raised for device '['samy', 'samz']': readback slow", age=10)

    samx = device_diagnose(bec, "samx")
    samy = device_diagnose(bec, "samy")

    assert codes(samx) == ["device.alarms"]
    assert samx["data"]["findings"][0]["severity"] == "error"
    assert codes(samy) == ["device.alarms"]
    # on the beamline they come from bec_logs, once each, and not as log errors
    beamline = codes(diagnose(bec))
    assert "logs.errors" not in beamline
    assert sorted(beamline) == ["alarm.major", "alarm.minor"]


def test_core_service_down(bec):
    del bec.service_status["DeviceServer"]

    beamline = diagnose(bec)
    samx = device_diagnose(bec)

    # bec_status owns the service rule; the device view says what it means for samx
    assert codes(beamline) == ["core_service_missing"]
    assert beamline["data"]["findings"][0]["severity"] == "error"
    assert codes(samx) == ["device.data_unverified"]


def test_failed_scans_come_from_scan_history(bec):
    bec.history(("closed", None), ("aborted", "alarm"), ("user_completed", "user"))

    result = diagnose(bec)

    assert codes(result) == ["scan_not_closed"]
    assert "Scan 2 (line_scan) ended aborted after an alarm." in result["findings"][0]


# ---------------------------------------------------------------------------
# merging with the other tools
# ---------------------------------------------------------------------------


def test_other_tools_findings_are_merged_by_severity_then_area(bec, peers):
    peers.queue = tool_result(
        False,
        "The queue is paused.",
        findings=[
            Finding(
                "warning", "queue_paused", "The primary queue is paused", "primary", "bec_queue"
            )
        ],
    )
    peers.states = tool_result(
        False,
        "1 beamline state: 1 invalid.",
        findings=[
            Finding(
                "error", "state_invalid", "Beamline state shutter is invalid: closed", "shutter"
            )
        ],
    )
    bec.device_manager.devices["samy"]._config["enabled"] = False
    bec.log("write failed", service="FileWriterManager")

    result = diagnose(bec)

    assert codes(result) == ["state_invalid", "queue_paused", "logs.errors", "devices.disabled"]
    assert result["summary"] == (
        "1 error, 2 warnings for the beamline. Most important: Beamline state shutter is "
        "invalid: closed."
    )
    assert result["findings"][1] == ("Warning: The primary queue is paused. Next: bec_queue().")


def test_a_failing_tool_makes_the_diagnosis_incomplete_not_failed(bec, peers):
    peers.queue = LookupError("queue storage not loaded")
    bec.device_manager.devices["samy"]._config["enabled"] = False

    result = diagnose(bec)

    assert codes(result) == ["diagnose.incomplete", "devices.disabled"]
    assert result["findings"][0] == (
        "Warning: Could not check the queue: LookupError: queue storage not loaded. "
        "Next: mcp_status()."
    )
    assert ("states", (bec,)) in peers.calls


def test_one_unreadable_source_does_not_hide_the_rest(bec):
    bec.connector.fail[MessageEndpoints.device_config_history().endpoint] = ValueError("bad")
    bec.device_manager.devices["samy"]._config["enabled"] = False

    result = diagnose(bec)

    assert codes(result) == ["diagnose.incomplete", "devices.disabled"]
    assert result["findings"][0] == (
        'Warning: Could not check the device config history: "ValueError: bad". '
        "Next: bec_status()."
    )


def test_unreadable_log_stream_is_reported_once(bec, monkeypatch):
    bec.connector.fail[MessageEndpoints.log().endpoint] = ValueError("bad payload")
    monkeypatch.setattr(
        tools.logs, "logs_digest", lambda *args, **kwargs: pytest.fail("bec_logs must not run")
    )

    result = diagnose(bec)

    assert codes(result) == ["diagnose.incomplete"]
    assert "Could not check the log stream" in result["findings"][0]


def test_redis_failure_inside_a_read_ends_the_call(bec):
    bec.connector.fail[MessageEndpoints.log().endpoint] = redis.exceptions.ConnectionError("gone")

    with pytest.raises(redis.exceptions.ConnectionError):
        diagnose(bec)


@pytest.mark.parametrize("func", [tools.diagnose, tools.device_diagnose])
def test_unreachable_bec_is_the_answer(func):
    args = ("samx",) if func is tools.device_diagnose else ()

    result = func(StubConnection(BecUnavailableError("Redis at localhost:6379 refused")), *args)

    assert result["ok"] is False
    assert result["findings"] == [
        "Error: BEC is unreachable: Redis at localhost:6379 refused. Next: mcp_status()."
    ]


# ---------------------------------------------------------------------------
# bare calls and MCP
# ---------------------------------------------------------------------------


def test_bare_device_call_checks_all_devices(bec):
    bec.device_manager.devices["samy"]._config["enabled"] = False
    bec.motor("samz", 20.0)

    result = tools.device_diagnose(StubConnection(bec))

    assert codes(result) == ["devices.outside_limits", "devices.disabled"]
    assert result["data"]["devices"] == {"configured": 4, "disabled": 1}


def test_bare_device_call_on_healthy_devices_asks_for_a_name(bec):
    result = tools.device_diagnose(StubConnection(bec))

    assert result["summary"] == (
        "No problems found for the devices. Name a device to check its readback, limits, errors "
        "and alarms."
    )


def _server(connection: Any) -> MCPServer:
    server = MCPServer("bec-test")

    async def call(func, *args):
        return await anyio.to_thread.run_sync(lambda: func(*args))

    tools.register(server, connection, call)
    return server


async def _session(server: MCPServer, calls: list[tuple[str, dict]]) -> tuple[Any, list[Any]]:
    async with Client(server) as client:
        listed = await client.list_tools()
        results = [await client.call_tool(name, arguments) for name, arguments in calls]
    return listed, results


def test_tools_are_read_only_and_answer_through_mcp(bec):
    bec.device_manager.devices["samy"]._config["enabled"] = False
    listed, (beamline, samy) = asyncio.run(
        _session(
            _server(StubConnection(bec)),
            [("bec_diagnose", {}), ("bec_device_diagnose", {"device": "samy"})],
        )
    )

    assert {tool.name for tool in listed.tools} == {"bec_diagnose", "bec_device_diagnose"}
    assert all(tool.annotations.read_only_hint for tool in listed.tools)
    assert all(". " not in tool.description for tool in listed.tools)
    assert beamline.is_error is False
    assert beamline.structured_content["findings"] == [
        'Info: 1 device is disabled: samy. Next: bec_device_diagnose(device="samy").'
    ]
    assert samy.structured_content["data"]["findings"][0]["code"] == "device.disabled"


def test_window_minutes_is_bounded(bec):
    _, (result,) = asyncio.run(
        _session(_server(StubConnection(bec)), [("bec_diagnose", {"window_minutes": 0})])
    )

    assert result.is_error is True


def test_registered_in_the_app_and_answer_bare_calls(make_connection, call_tool, stdout_guard):
    # the conftest client has no device manager or queue: sources fail, the tools still answer
    app = build_app(Settings(), connection=make_connection())

    for name in ("bec_diagnose", "bec_device_diagnose"):
        with stdout_guard():
            result = call_tool(app.server, name)
        payload = json.loads(result.content[0].text)
        assert not result.is_error, payload
        assert set(payload) == {"ok", "summary", "findings", "data", "truncated"}
        assert payload["ok"] is False
        assert "diagnose.incomplete" in [f["code"] for f in payload["data"]["findings"]]


def test_through_bec_connection_with_no_stdout(make_connection, capsys, monkeypatch, peers):
    monkeypatch.setattr(tools.time, "time", lambda: NOW)
    connection = make_connection()
    connection.connect()
    fake = FakeBEC()
    client = connection.factory.clients[0]
    noisy_logs = fake.connector.get_last

    def get_last(*args, **kwargs):
        print("bec_lib helper printing")
        return noisy_logs(*args, **kwargs)

    fake.connector.get_last = get_last
    for attribute in ("connector", "device_manager", "queue", "alarm_handler"):
        setattr(client, attribute, getattr(fake, attribute))
    client._service_status = fake.service_status

    _, (result,) = asyncio.run(_session(_server(connection), [("bec_diagnose", {})]))

    assert result.structured_content["ok"] is True, result.structured_content
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bec_lib helper printing" in captured.err
