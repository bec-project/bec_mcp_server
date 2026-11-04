"""bec_queue and bec_beamline_states against bec_lib's own storage classes over fake Redis.

The client here is a namespace holding the real ScanManager, BeamlineStateManager and
BuiltinActorHli a BECClient builds, sharing one RedisConnector on fakeredis. Tests write the same
messages the scan server, scan bundler and state manager publish, so the digests are checked
against BEC 4 payloads rather than hand-made fakes.
"""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import anyio
import fakeredis
import pytest
from bec_lib import messages
from bec_lib.bl_state_manager import BeamlineStateManager
from bec_lib.builtin_actor_hli import BuiltinActorHli
from bec_lib.endpoints import MessageEndpoints
from bec_lib.redis_connector import RedisConnector
from bec_lib.scan_manager import ScanManager
from mcp.client import Client

from bec_mcp_server.app import build_app
from bec_mcp_server.bec import compat
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import queue_states
from bec_mcp_server.tools.queue_states import beamline_states_digest, queue_digest


def _fake_redis(host, port, **kwargs):
    return fakeredis.FakeRedis()


@pytest.fixture
def connector():
    connector = RedisConnector("localhost:1", redis_cls=_fake_redis)
    connector._managed_connection.flushall()  # pylint: disable=protected-access
    try:
        yield connector
    finally:
        connector.shutdown()


def _set_interlock(connector, enabled=False, trigger="pause_scan", watched=None):
    connector.xadd(
        MessageEndpoints.scan_interlock_enabled(),
        {"config": messages.BoolConfigDefaultFalse(value=enabled)},
        max_size=1,
    )
    connector.xadd(
        MessageEndpoints.scan_interlock_trigger_setting(),
        {"config": messages.ScanInterlockTriggerSettingMessage(value=trigger)},
        max_size=1,
    )
    if watched is not None:
        connector.set(
            MessageEndpoints.scan_interlock_states(),
            messages.ScanInterlockStateTableContent(states_watched=watched),
        )


def _publish_states(connector, *states):
    """states: (name, status, label) tuples, published as the state manager and actors do."""
    configs = [
        messages.BeamlineStateConfig(
            name=name,
            state_type="DeviceWithinLimitsState",
            parameters={
                "name": name,
                "device": "samx",
                "signal": None,
                "low_limit": -5.0,
                "high_limit": 5.0,
                "tolerance": 0.1,
            },
        )
        for name, _, _ in states
    ]
    connector.xadd(
        MessageEndpoints.available_beamline_states(),
        {"data": messages.AvailableBeamlineStatesMessage(states=configs)},
        max_size=1,
    )
    for name, status, label in states:
        set_state(connector, name, status, label)


def set_state(connector, name, status, label, timestamp=None):
    msg = messages.BeamlineStateMessage(name=name, status=status, label=label)
    if timestamp is not None:
        msg.timestamp = timestamp
    connector.xadd(MessageEndpoints.beamline_state(name), {"data": msg}, max_size=1)


@pytest.fixture
def make_client(connector):
    """Build the client after the test has written the initial Redis state, as a real start does."""
    built = []

    def _make(interlock=True):
        client = SimpleNamespace(connector=connector, queue=None, beamline_states=None)
        client.queue = ScanManager(connector)
        client.beamline_states = BeamlineStateManager(client)
        client.builtin_actors = BuiltinActorHli(client) if interlock else None
        built.append(client)
        return client

    yield _make
    for client in built:
        if client.builtin_actors is not None:
            client.builtin_actors.shutdown()
        client.queue.shutdown()


def _request_block(scan_type="line_scan", scan_number=12, scan_id="scan-12", **kwargs):
    return messages.RequestBlock(
        msg=messages.ScanQueueMessage(
            scan_type=scan_type,
            parameter={
                "args": {"samx": [-5, 5]},
                "kwargs": {"steps": 50, "exp_time": 0.1, "relative": True, "system_config": {}},
            },
        ),
        RID=f"rid-{scan_number}",
        readout_priority={"monitored": ["samx"]},
        is_scan=scan_type != "mv",
        scan_number=scan_number,
        scan_id=scan_id,
        **kwargs,
    )


def _queue_entry(queue_id="q-12", status="RUNNING", active=True, reason=None, **block_kwargs):
    block = _request_block(**block_kwargs)
    return messages.QueueInfoEntry(
        queue_id=queue_id,
        scan_id=[block.scan_id],
        is_scan=[block.is_scan],
        request_blocks=[block],
        scan_number=[block.scan_number],
        status=status,
        active_request_block=block if active else None,
        reason=reason,
    )


def _publish_queue(connector, entries=(), status="RUNNING", locks=()):
    msg = messages.ScanQueueStatusMessage(
        queue={
            "primary": messages.ScanQueueStatus(
                info=list(entries), status=status, locks=list(locks)
            )
        }
    )
    connector.set_and_publish(MessageEndpoints.scan_queue_status(), msg)


def _push_history(connector, entry, status="COMPLETED"):
    connector.lpush(
        MessageEndpoints.scan_queue_history(),
        messages.ScanQueueHistoryMessage(status=status, queue_id=entry.queue_id, info=entry),
        max_size=100,
    )


def _publish_scan_status(connector, scan_id="scan-12", status="open", started=None, num_points=50):
    msg = messages.ScanStatusMessage(
        scan_id=scan_id, status=status, scan_number=12, num_points=num_points, info={}
    )
    if started is not None:
        msg.timestamp = started
    connector.set(MessageEndpoints.public_scan_info(scan_id), msg)


def _publish_progress(connector, value, max_value=50, scan_id="scan-12", done=False):
    connector.set_and_publish(
        MessageEndpoints.scan_progress(),
        messages.ProgressMessage(
            value=value, max_value=max_value, done=done, metadata={"scan_id": scan_id}
        ),
    )


def _messages_text(result):
    return " ".join(result["findings"])


def _structured(result):
    return result["data"].get("findings", [])


# --- bec_queue ------------------------------------------------------------------------------------


def test_queue_reports_running_scan_with_progress(connector, make_client):
    _publish_queue(connector, [_queue_entry(), _queue_entry(queue_id="q-13", status="PENDING")])
    _publish_scan_status(connector, started=time.time() - 20)
    _publish_progress(connector, value=10)
    client = make_client()

    result = queue_digest(client)

    running = result["data"]["running"]
    assert running["request"] == "line_scan"
    assert running["scan_number"] == 12
    assert running["scan_status"] == "open"
    assert running["num_points"] == 50
    assert running["progress"] == {"value": 10, "max_value": 50, "percent": 20.0, "done": False}
    assert running["args"] == "samx [-5, 5], steps=50, exp_time=0.1, relative=True"
    assert 19 <= running["elapsed_s"] <= 25
    assert 70 <= running["remaining_s_estimate"] <= 100
    assert result["summary"].startswith("Primary queue RUNNING, running line_scan #12")
    assert "1 more queued" in result["summary"]
    assert result["data"]["queues"]["primary"]["size"] == 2
    assert result["ok"] is True


@pytest.mark.filterwarnings("ignore:RedisConnector methods with a string topic")
def test_queue_reads_cached_queue_without_queue_status_callback(connector, make_client):
    """A queue update the client missed is still reported (refresh via compat)."""
    _publish_queue(connector, [])
    client = make_client()
    msg = messages.ScanQueueStatusMessage(
        queue={"primary": messages.ScanQueueStatus(info=[_queue_entry()], status="RUNNING")}
    )
    # a raw key write: the queue changed without a publish reaching this client
    connector.set(MessageEndpoints.scan_queue_status().endpoint, msg)
    assert client.queue.queue_storage.current_scan_queue["primary"].info == []

    result = queue_digest(client)

    assert result["data"]["running"]["scan_number"] == 12


def test_queue_ignores_progress_of_a_previous_scan(connector, make_client):
    _publish_queue(connector, [_queue_entry()])
    _publish_scan_status(connector)
    _publish_progress(connector, value=50, scan_id="scan-11", done=True)
    client = make_client()

    running = queue_digest(client)["data"]["running"]

    assert running["progress"] is None
    assert "remaining_s_estimate" not in running


def test_queue_prefers_the_clients_scan_storage_for_status(connector, make_client):
    _publish_queue(connector, [_queue_entry()])
    client = make_client()
    client.queue.scan_storage.add_scan_item(
        queue_id="q-12", scan_number=12, scan_id="scan-12", status="open"
    )
    client.queue.scan_storage.update_with_scan_status(
        messages.ScanStatusMessage(scan_id="scan-12", status="paused", scan_number=12, info={})
    )

    result = queue_digest(client)

    assert result["data"]["running"]["scan_status"] == "paused"
    assert "Scan line_scan #12 is paused." in _messages_text(result)
    assert result["ok"] is False


def test_queue_idle_with_history(connector, make_client):
    _publish_queue(connector, [])
    for number in range(1, 9):
        entry = _queue_entry(
            queue_id=f"q-{number}", status="COMPLETED", scan_number=number, scan_id=f"s-{number}"
        )
        _push_history(connector, entry)
    client = make_client()

    result = queue_digest(client, history=3)

    assert result["data"]["running"] is None
    assert [entry["scan_numbers"] for entry in result["data"]["history"]] == [[8], [7], [6]]
    assert result["summary"] == (
        "Primary queue RUNNING, nothing queued; last finished: line_scan #8 COMPLETED."
    )
    assert result["findings"] == []
    assert len(queue_digest(client, history=50)["data"]["history"]) == queue_states.MAX_HISTORY
    assert queue_digest(client, history=0)["data"]["history"] == []


def test_queue_flags_aborted_last_request(connector, make_client):
    _publish_queue(connector, [])
    _push_history(connector, _queue_entry(status="STOPPED", reason="alarm"), status="STOPPED")
    client = make_client()

    result = queue_digest(client)

    assert result["findings"] == [
        "Warning: The last finished request (line_scan, #12) ended STOPPED (reason: alarm). "
        "Next: bec_logs()."
    ]
    assert _structured(result) == [
        {
            "severity": "warning",
            "code": "last_request_not_completed",
            "subject": "q-12",
            "next": {"tool": "bec_logs", "args": {}},
        }
    ]


def test_queue_locked_by_interlock(connector, make_client):
    lock = messages.ScanQueueLock(
        identifier=queue_states.INTERLOCK_LOCK_ID,
        reason="Interlock for beamline states: ['shutter']",
    )
    _publish_queue(connector, [_queue_entry(status="PENDING")], status="LOCKED", locks=[lock])
    client = make_client()

    result = queue_digest(client)

    assert result["data"]["running"] is None
    assert result["summary"] == (
        "Primary queue LOCKED, 1 request(s) waiting, locked by ScanInterlockActor."
    )
    assert _structured(result)[0]["severity"] == "error"
    assert _structured(result)[0]["code"] == "queue_locked_by_interlock"
    assert result["findings"][0].endswith("Next: bec_beamline_states().")
    assert result["ok"] is False


def test_queue_paused_and_waiting_for_device_locks(connector, make_client):
    entry = _queue_entry(status="PAUSED", pending_device_locks=["samx"])
    _publish_queue(connector, [entry], status="PAUSED")
    _publish_scan_status(connector)
    client = make_client()

    result = queue_digest(client)

    text = _messages_text(result)
    assert "The primary queue is paused with 1 request(s) in it" in text
    assert "line_scan #12 waits for device locks on samx." in text
    assert result["data"]["queues"]["primary"]["items"][0]["waiting_for_device_locks"] == ["samx"]


def test_queue_truncates_long_queues(connector, make_client):
    entries = [_queue_entry(queue_id=f"q-{n}", status="PENDING") for n in range(15)]
    _publish_queue(connector, entries)
    client = make_client()

    result = queue_digest(client)

    assert len(result["data"]["queues"]["primary"]["items"]) == queue_states.MAX_QUEUE_ITEMS
    assert result["data"]["queues"]["primary"]["size"] == 15
    assert result["truncated"] is True


def test_queue_non_scan_request(connector, make_client):
    _publish_queue(connector, [_queue_entry(scan_type="mv", scan_number=None, scan_id=None)])
    client = make_client()

    running = queue_digest(client)["data"]["running"]

    assert running["request"] == "mv"
    assert running["is_scan"] is False
    assert "scan_status" not in running


def test_queue_without_published_queue(connector, make_client):
    client = make_client()

    result = queue_digest(client)

    assert result["summary"] == "The scan server has not published a queue yet."
    assert result["data"] == {"queues": {}, "running": None, "history": []}


def test_queue_before_client_start():
    result = queue_digest(SimpleNamespace(queue=None))

    assert result["summary"] == "The BEC client has no scan queue yet."
    assert set(result) == {"ok", "summary", "findings", "data", "truncated"}


# --- bec_beamline_states --------------------------------------------------------------------------


def test_states_report_status_label_and_age(connector, make_client):
    _publish_states(connector, ("samx_in_range", "valid", "samx within limits"))
    set_state(connector, "samx_in_range", "valid", "samx within limits", time.time() - 30)
    _set_interlock(connector)
    client = make_client()

    result = beamline_states_digest(client)

    state = result["data"]["states"][0]
    assert state["name"] == "samx_in_range"
    assert state["type"] == "DeviceWithinLimitsState"
    assert state["status"] == "valid"
    assert state["label"] == "samx within limits"
    assert 29 <= state["age_s"] <= 35
    assert state["parameters"] == "device=samx, low_limit=-5.0, high_limit=5.0, tolerance=0.1"
    assert result["summary"] == "1 beamline state(s): 1 valid. Scan interlock disabled."
    assert result["ok"] is True


def test_states_invalid_state_trips_enabled_interlock(connector, make_client):
    _publish_states(
        connector,
        ("samx_in_range", "valid", "samx within limits"),
        ("shutter_open", "invalid", "Shutter is closed"),
    )
    _set_interlock(
        connector,
        enabled=True,
        watched={"shutter_open": ["valid", "warning"], "samx_in_range": ["valid"]},
    )
    lock = messages.ScanQueueLock(identifier=queue_states.INTERLOCK_LOCK_ID, reason="interlock")
    _publish_queue(connector, [], status="LOCKED", locks=[lock])
    client = make_client()

    result = beamline_states_digest(client)

    interlock = result["data"]["interlock"]
    assert interlock == {
        "enabled": True,
        "trigger": "pause_scan",
        "watched": {"shutter_open": ["valid", "warning"], "samx_in_range": ["valid"]},
        "tripped_by": ["shutter_open"],
        "primary_queue_locked": True,
    }
    assert result["summary"] == (
        "2 beamline state(s): 1 invalid, 1 valid. Scan interlock enabled (pause_scan), watching "
        "2 state(s), tripped by shutter_open; primary queue locked."
    )
    assert [finding["code"] for finding in _structured(result)] == [
        "state_invalid",
        "interlock_tripped",
    ]
    assert result["findings"][0] == (
        "Error: Beamline state shutter_open is invalid: Shutter is closed. "
        'Next: bec_device_read(device="samx").'
    )
    assert _structured(result)[1]["next"] == {"tool": "bec_queue", "args": {}}
    assert result["ok"] is False


def test_states_follow_a_state_driven_to_invalid(connector, make_client):
    _publish_states(connector, ("shutter_open", "valid", "Shutter is open"))
    _set_interlock(connector, enabled=True, watched={"shutter_open": ["valid", "warning"]})
    client = make_client()
    assert beamline_states_digest(client)["ok"] is True

    set_state(connector, "shutter_open", "invalid", "Shutter is closed")

    result = beamline_states_digest(client)
    assert result["data"]["states"][0]["status"] == "invalid"
    assert result["data"]["interlock"]["tripped_by"] == ["shutter_open"]


def test_states_interlock_watches_unconfigured_state(connector, make_client):
    _publish_states(connector, ("shutter_open", "valid", "Shutter is open"))
    _set_interlock(connector, enabled=True, watched={"ghost": ["valid"]})
    client = make_client()

    result = beamline_states_digest(client)

    text = _messages_text(result)
    assert "watches ghost, which is not a configured beamline state" in text
    assert result["data"]["interlock"]["tripped_by"] == ["ghost"]


def test_states_stale_interlock_lock(connector, make_client):
    _publish_states(connector, ("shutter_open", "valid", "Shutter is open"))
    _set_interlock(connector, enabled=True, watched={"shutter_open": ["valid"]})
    lock = messages.ScanQueueLock(identifier=queue_states.INTERLOCK_LOCK_ID, reason="interlock")
    _publish_queue(connector, [], status="LOCKED", locks=[lock])
    client = make_client()

    result = beamline_states_digest(client)

    assert _structured(result)[0]["code"] == "interlock_lock_without_trip"
    assert "still carries the scan interlock lock" in result["findings"][0]


def test_states_none_configured(connector, make_client):
    client = make_client(interlock=False)

    result = beamline_states_digest(client)

    assert result["summary"] == "No beamline states are configured. Scan interlock not available."
    assert result["data"] == {"states": [], "interlock": None}
    assert result["ok"] is True


def test_states_state_without_status_message(connector, make_client):
    configs = [
        messages.BeamlineStateConfig(
            name="new_state",
            state_type="DeviceWithinLimitsState",
            parameters={"name": "new_state", "device": "samy", "low_limit": 0, "high_limit": 1},
        )
    ]
    connector.xadd(
        MessageEndpoints.available_beamline_states(),
        {"data": messages.AvailableBeamlineStatesMessage(states=configs)},
    )
    client = make_client(interlock=False)

    result = beamline_states_digest(client)

    state = result["data"]["states"][0]
    assert state["status"] == "unknown"
    assert state["age_s"] is None
    assert _structured(result)[0]["severity"] == "warning"
    assert _structured(result)[0]["next"] == {"tool": "bec_device_read", "args": {"device": "samy"}}


# --- through the MCP server ------------------------------------------------------------------


class _LendingConnection:
    """Lends the bec_lib-backed test client the way BecConnection.use() lends the real one."""

    def __init__(self, client):
        self.client = client

    @contextmanager
    def use(self):
        yield self.client

    def connect(self):
        pass

    def shutdown(self):
        pass

    def state(self):
        return {"state": "connected"}


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _busy_beamline(connector):
    _publish_states(connector, ("shutter_open", "invalid", "Shutter is closed"))
    _set_interlock(connector, enabled=True, watched={"shutter_open": ["valid"]})
    _publish_queue(connector, [_queue_entry()])
    _publish_scan_status(connector)
    _publish_progress(connector, value=5)
    _push_history(connector, _queue_entry(status="STOPPED", reason="user"), status="STOPPED")


def test_tools_are_registered_read_only(connector, make_client):
    app = build_app(Settings(), connection=_LendingConnection(make_client()))

    async def list_tools():
        async with Client(app.server, mode="legacy") as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(list_tools)}
    assert {"bec_queue", "bec_beamline_states"} <= set(tools)
    for name in ("bec_queue", "bec_beamline_states"):
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].description.count(". ") == 0


@pytest.mark.parametrize(
    "tool, arguments",
    [("bec_queue", {}), ("bec_queue", {"history": 1}), ("bec_beamline_states", {})],
)
def test_tools_answer_through_mcp_without_stdout(
    connector, make_client, call_tool, stdout_guard, monkeypatch, tool, arguments
):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("printing helper called")

    _busy_beamline(connector)
    client = make_client()
    # patched after the client is built: louie's saferef calls str(ScanManager) on register,
    # which renders the queue table into a capture buffer, not stdout
    monkeypatch.setattr(BeamlineStateManager, "show_all", _forbidden)
    monkeypatch.setattr("bec_lib.queue_items.QueueStorage.describe_queue", _forbidden)
    app = build_app(Settings(), connection=_LendingConnection(client))

    with stdout_guard():
        payload = _payload(call_tool(app.server, tool, arguments))

    assert payload["ok"] is False
    # bec_diagnose aggregates data["findings"]; each sentence has exactly one structured twin
    assert payload["findings"]
    assert len(payload["findings"]) == len(payload["data"]["findings"])
    for text, twin in zip(payload["findings"], payload["data"]["findings"]):
        assert text.startswith(f"{twin['severity'].capitalize()}: ")
    if tool == "bec_queue":
        assert payload["data"]["running"]["progress"]["value"] == 5
        assert len(payload["data"]["history"]) == arguments.get("history", 1)
    else:
        assert payload["data"]["interlock"]["tripped_by"] == ["shutter_open"]


@pytest.mark.parametrize("tool", ["bec_queue", "bec_beamline_states"])
def test_bec_down_answers_with_the_reason(make_connection, call_tool, tool):
    app = build_app(Settings(tool_timeout=5), connection=make_connection(swallow_failure=True))

    payload = _payload(call_tool(app.server, tool))

    assert payload["ok"] is False
    assert payload["findings"][0].startswith("Error: Could not connect")
    assert payload["data"]["findings"][0]["next"] == {"tool": "mcp_status", "args": {}}


# --- compat -----------------------------------------------------------------------------------


def test_compat_refresh_queue_uses_queue_storage(connector, make_client):
    _publish_queue(connector, [], status="PAUSED")
    _push_history(connector, _queue_entry(status="COMPLETED"))
    client = make_client()
    storage = client.queue.queue_storage
    storage.current_scan_queue = storage.queue_history = None

    compat.refresh_queue(storage)

    assert storage.current_scan_queue["primary"].status == "PAUSED"
    assert storage.queue_history[0].queue_id == "q-12"
