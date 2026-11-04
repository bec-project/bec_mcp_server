"""Device tools against real bec_lib device proxies backed by an in-memory Redis stand-in."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from bec_lib import messages
from bec_lib.device import ComputedSignal, Device, DeviceBase, Positioner, Signal
from bec_lib.devicemanager import DeviceContainer, DeviceManagerBase
from bec_lib.endpoints import MessageEndpoints

from bec_mcp_server.app import build_app
from bec_mcp_server.bec import compat
from bec_mcp_server.tools import devices as tools

NOW = 1_800_000_000.0


class FakeConnector:
    """The only connector call cached reads make is get(endpoint)."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.fail: Exception | None = None

    def get(self, endpoint: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        return self.store.get(getattr(endpoint, "endpoint", endpoint))

    def put(self, endpoint: Any, signals: dict[str, Any]) -> None:
        self.store[endpoint.endpoint] = messages.DeviceMessage(signals=signals, metadata={})

    def shutdown(self) -> None:
        pass


class FakeDeviceManager(DeviceManagerBase):
    """A client device manager holding proxies, without a BEC service behind it."""

    # pylint: disable=super-init-not-called
    def __init__(self) -> None:
        self.connector = FakeConnector()
        self.devices = DeviceContainer()
        self.parent = None
        self._allow_override = True

    def add(self, cls: type, name: str, info: dict | None = None, **config: Any) -> DeviceBase:
        full_config = {
            "name": name,
            "deviceClass": config.pop("deviceClass", f"ophyd_devices.Sim{cls.__name__}"),
            "enabled": config.pop("enabled", True),
            "readoutPriority": config.pop("readoutPriority", "baseline"),
            **config,
        }
        device = cls(name=name, info=info or {}, config=full_config, parent=self)
        self.devices[name] = device
        return device


def _signal(obj_name: str, kind: str, *, write: bool = True, **describe: Any) -> dict:
    return {
        "component_name": obj_name,
        "signal_class": "SetableSignal" if write else "ReadOnlySignal",
        "obj_name": obj_name,
        "kind_str": kind,
        "doc": "",
        "describe": {"source": f"SIM:{obj_name}", "dtype": "number", "shape": [], **describe},
        "metadata": {"connected": True, "read_access": True, "write_access": write},
    }


def _positioner_info(name: str) -> dict:
    signals = {
        "readback": _signal(name, "hinted", write=False, units="mm"),
        "setpoint": _signal(f"{name}_setpoint", "normal"),
        "motor_is_moving": _signal(f"{name}_motor_is_moving", "normal"),
        "velocity": _signal(f"{name}_velocity", "config"),
        "high_limit_travel": _signal(f"{name}_high_limit_travel", "omitted"),
    }
    signals["velocity"]["doc"] = "Velocity of the motor in mm/s\nmore detail"
    return {
        "device_base_class": "positioner",
        "device_class": "SimPositioner",
        "signals": signals,
        "hints": {"fields": [name]},
        "describe": {name: {"source": f"SIM:{name}", "dtype": "number", "shape": []}},
        "describe_configuration": {f"{name}_velocity": {"dtype": "number"}, "egu": "mm"},
        "sub_devices": [],
        "custom_user_access": {
            "registered_proxies": {"type": "dict"},
            "sim": {
                "device_class": "SimulatedDataMonitor",
                "info": {
                    "select_model": {
                        "type": "func",
                        "doc": "\nSelect the active simulation model.\n",
                        "signature": [
                            {
                                "name": "model",
                                "kind": "POSITIONAL_OR_KEYWORD",
                                "default": "_empty",
                                "annotation": "str",
                            }
                        ],
                    },
                    "params": {"type": "dict"},
                },
            },
        },
    }


def _monitor_info(name: str) -> dict:
    # a root signal has no signal tree; describe() lists it, and the device carries access
    return {
        "device_base_class": "signal",
        "device_class": "SimMonitor",
        "read_access": True,
        "write_access": False,
        "signals": {},
        "hints": {"fields": [name]},
        "describe": {name: {"source": f"SIM:{name}", "dtype": "integer", "shape": []}},
        "describe_configuration": {"egu": None},
        "custom_user_access": {"registered_proxies": {"type": "dict"}},
    }


def _camera_info(name: str) -> dict:
    return {
        "device_base_class": "device",
        "device_class": "SimCamera",
        "signals": {
            "image": _signal(f"{name}_image", "omitted", write=False, shape=[100, 100]),
            "exp_time": _signal(f"{name}_exp_time", "config"),
            "frames": _signal(f"{name}_frames", "config"),
        },
        "hints": {"fields": []},
        "describe": {},
        "describe_configuration": {"egu": None},
        "sub_devices": [],
        "custom_user_access": {},
    }


@pytest.fixture
def beamline(monkeypatch):
    """samx/samy/samz motors, a disabled motor, a monitor, a computed signal and a camera."""
    monkeypatch.setattr(tools.time, "time", lambda: NOW)
    dm = FakeDeviceManager()
    for name in ("samx", "samy", "samz"):
        dm.add(
            Positioner,
            name,
            _positioner_info(name),
            deviceTags={"user motors"},
            deviceConfig={"limits": [-50, 50], "tolerance": 0.01},
            description=f"sample stage {name[-1]}",
        )
    dm.add(Positioner, "mono_theta", _positioner_info("mono_theta"), enabled=False)
    dm.add(Signal, "bpm4i", _monitor_info("bpm4i"), readoutPriority="monitored")
    dm.add(ComputedSignal, "pseudo", _monitor_info("pseudo"), readoutPriority="monitored")
    dm.add(Device, "eiger", _camera_info("eiger"), readoutPriority="async")

    connector = dm.connector
    connector.put(
        MessageEndpoints.device_readback("samx"),
        {
            "samx": {"value": 4.9974, "timestamp": NOW - 125.0},
            "samx_setpoint": {"value": 5.0, "timestamp": NOW - 126.0},
            "samx_motor_is_moving": {"value": 0, "timestamp": NOW - 126.0},
        },
    )
    connector.put(
        MessageEndpoints.device_read_configuration("samx"),
        {"samx_velocity": {"value": 100, "timestamp": NOW - 7200.0}},
    )
    connector.put(
        MessageEndpoints.device_limits("samx"),
        {"low": {"value": -50, "timestamp": NOW}, "high": {"value": 50, "timestamp": NOW}},
    )
    connector.put(
        MessageEndpoints.device_readback("bpm4i"), {"bpm4i": {"value": 97, "timestamp": NOW}}
    )
    connector.put(
        MessageEndpoints.device_read_configuration("eiger"),
        {
            "eiger_exp_time": {"value": 0.1, "timestamp": NOW - 30},
            "eiger_frames": {"value": 1, "timestamp": NOW - 30},
        },
    )
    return SimpleNamespace(device_manager=dm, connector=connector)


def _all_tool_calls(client: Any) -> list[dict]:
    return [
        tools.list_devices(client),
        tools.list_devices(client, device_type="positioner", enabled_only=True, limit=1),
        tools.list_devices(client, tag="USER MOTORS", prefix="sam", offset=1),
        *(tools.device_info(client, name) for name in client.device_manager.devices),
        *(tools.device_read(client, name) for name in client.device_manager.devices),
        tools.device_read(client, "samx", signals=["velocity", "setpoint", "nope"]),
        tools.device_read(client, "samx.velocity", include_config=False),
        tools.device_info(client),
        tools.device_read(client),
    ]


def test_list_devices_filters_by_type_tag_priority_and_enabled(beamline):
    motors = tools.list_devices(beamline, device_type="positioner")
    assert motors["ok"] is True
    assert [row["name"] for row in motors["data"]["devices"]] == [
        "mono_theta",
        "samx",
        "samy",
        "samz",
    ]
    assert "4 of 7 devices match device_type=positioner" in motors["summary"]

    enabled = tools.list_devices(beamline, device_type="positioner", enabled_only=True)
    assert "mono_theta" not in [row["name"] for row in enabled["data"]["devices"]]

    tagged = tools.list_devices(beamline, tag="User Motors")
    assert tagged["data"]["total"] == 3
    samx = tagged["data"]["devices"][0]
    assert samx == {
        "name": "samx",
        "type": "positioner",
        "device_class": "ophyd_devices.SimPositioner",
        "enabled": True,
        "readout_priority": "baseline",
        "tags": ["user motors"],
        "description": "sample stage x",
    }

    monitored = tools.list_devices(beamline, readout_priority="monitored")
    assert {row["name"]: row["type"] for row in monitored["data"]["devices"]} == {
        "bpm4i": "signal",
        "pseudo": "computed_signal",
    }
    assert tools.list_devices(beamline, prefix="SAM")["data"]["total"] == 3

    nothing = tools.list_devices(beamline, prefix="zzz")
    assert nothing["ok"] is True
    assert nothing["data"]["devices"] == []
    assert nothing["summary"] == "No devices match prefix=zzz; 7 devices in total."


def test_list_devices_pages_with_offset_and_caps_the_limit(monkeypatch):
    dm = FakeDeviceManager()
    for index in range(1234):
        dm.add(Positioner, f"m{index:04d}", _positioner_info(f"m{index:04d}"))
    client = SimpleNamespace(device_manager=dm)

    first = tools.list_devices(client)
    assert first["truncated"] is True
    assert first["data"]["returned"] == tools.DEFAULT_DEVICE_LIMIT == 100
    assert first["data"]["next_offset"] == 100
    assert "Call again with offset=100" in first["summary"]

    capped = tools.list_devices(client, limit=10_000)
    assert capped["data"]["returned"] == tools.MAX_DEVICE_LIMIT == 500
    assert capped["data"]["devices"][-1]["name"] == "m0499"

    names = []
    offset = 0
    while offset is not None:
        page = tools.list_devices(client, limit=500, offset=offset)
        names.extend(row["name"] for row in page["data"]["devices"])
        offset = page["data"]["next_offset"]
    assert names == sorted(dm.devices)
    assert page["truncated"] is False

    past = tools.list_devices(client, offset=5000)
    assert past["data"]["devices"] == [] and past["data"]["next_offset"] is None
    assert "past the last match" in past["summary"]
    assert len(json.dumps(first)) < 25_000


def test_device_info_lists_signals_with_kind_units_access_and_user_methods(beamline):
    result = tools.device_info(beamline, "samx")

    assert result["ok"] is True
    data = result["data"]
    assert data["type"] == "positioner"
    assert data["egu"] == "mm"
    assert data["on_failure"] == "retry"
    assert data["hinted_signals"] == ["samx"]
    assert data["signal_kinds"] == {"hinted": 1, "normal": 2, "config": 1, "omitted": 1}
    assert [row["kind"] for row in data["signals"]] == [
        "hinted",
        "normal",
        "normal",
        "config",
        "omitted",
    ]
    readback = data["signals"][0]
    assert readback["name"] == "readback"
    assert readback["key"] == "samx"
    assert readback["units"] == "mm"
    assert readback["access"] == "read-only"
    velocity = next(row for row in data["signals"] if row["name"] == "velocity")
    assert velocity["access"] == "read-write"
    assert velocity["doc"] == "Velocity of the motor in mm/s"
    assert data["user_methods"] == [
        {
            "name": "sim.select_model",
            "signature": "(model: str)",
            "doc": "Select the active simulation model.",
        }
    ]
    assert data["user_attributes"] == ["registered_proxies", "sim.params"]
    assert data["device_config"] == {"limits": [-50, 50], "tolerance": 0.01}
    assert result["summary"].startswith(
        "samx is a positioner (ophyd_devices.SimPositioner), enabled, readout priority baseline"
    )


def test_device_info_for_a_plain_signal_falls_back_to_describe(beamline):
    data = tools.device_info(beamline, "bpm4i")["data"]

    assert data["type"] == "signal"
    assert data["access"] == "read-only"
    assert data["signals"] == [
        {
            "name": "bpm4i",
            "key": "bpm4i",
            "kind": "hinted",
            "dtype": "integer",
            "access": "read-only",
            "source": "SIM:bpm4i",
        }
    ]


def test_device_info_filters_by_kind_and_truncates_long_signal_lists(beamline):
    only_config = tools.device_info(beamline, "samx", kind="config")
    assert [row["name"] for row in only_config["data"]["signals"]] == ["velocity"]

    limited = tools.device_info(beamline, "samx", limit=2)
    assert limited["truncated"] is True
    assert len(limited["data"]["signals"]) == 2
    assert limited["data"]["signal_count"] == 5
    assert "Showing the first 2 signals" in limited["summary"]


def test_device_read_returns_values_with_age_limits_and_units(beamline):
    result = tools.device_read(beamline, "samx")

    assert result["ok"] is True
    data = result["data"]
    assert data["limits"] == {"low": -50, "high": 50}
    assert data["egu"] == "mm"
    assert data["readback"]["samx"] == {
        "value": 4.9974,
        "timestamp": "2027-01-15T07:57:55+00:00",
        "age_s": 125.0,
    }
    assert data["configuration"]["samx_velocity"]["age_s"] == 7200.0
    # every reading carries its age
    for section in ("readback", "configuration"):
        assert all(reading["age_s"] is not None for reading in data[section].values())
    assert result["summary"] == (
        "samx = 4.9974 mm (2.1 min old); limits -50 to 50 mm. "
        "Ages count from the last published update."
    )


def test_device_read_without_config_and_without_limits(beamline):
    result = tools.device_read(beamline, "samy", include_config=False)
    assert "configuration" not in result["data"]
    # no limits message published: bec_lib reports [0, 0], which ophyd treats as "no limits"
    assert result["data"]["limits"] is None
    assert result["summary"] == "samy has no cached values."

    camera = tools.device_read(beamline, "eiger")
    assert camera["data"]["readback"] == {}
    assert set(camera["data"]["configuration"]) == {"eiger_exp_time", "eiger_frames"}
    assert camera["summary"].startswith("eiger has no cached readback; 2 configuration values")


def test_device_read_flags_disabled_devices(beamline):
    beamline.connector.put(
        MessageEndpoints.device_readback("mono_theta"),
        {"mono_theta": {"value": 12.5, "timestamp": NOW - 90_000}},
    )
    result = tools.device_read(beamline, "mono_theta")
    assert result["data"]["enabled"] is False
    assert "device is disabled, so values may be stale" in result["summary"]
    assert "(1.0 d old)" in result["summary"]


def test_device_read_digests_large_arrays_long_lists_and_long_strings(beamline):
    image = np.arange(10_000, dtype=float).reshape(100, 100)
    image[0, 0] = np.nan
    beamline.connector.put(
        MessageEndpoints.device_readback("eiger"),
        {
            "eiger_image": {"value": image, "timestamp": NOW},
            "eiger_trace": {"value": [float(i) for i in range(17)], "timestamp": NOW},
            "eiger_roi": {"value": np.arange(16), "timestamp": NOW},
            "eiger_note": {"value": "x" * 5000, "timestamp": NOW},
        },
    )
    readback = tools.device_read(beamline, "eiger")["data"]["readback"]

    assert readback["eiger_image"]["value"] == {
        "shape": [100, 100],
        "dtype": "float64",
        "min": 1.0,
        "max": 9999.0,
        "mean": 5000.0,
        "non_finite_count": 1,
    }
    assert readback["eiger_trace"]["value"]["shape"] == [17]
    assert readback["eiger_roi"]["value"] == list(range(16))
    assert readback["eiger_note"]["value"].endswith("… (5000 chars)")
    assert len(readback["eiger_note"]["value"]) < 600


def test_device_read_selects_signals_by_component_reading_or_dotted_name(beamline):
    dotted = tools.device_read(beamline, "samx.velocity")
    assert dotted["data"]["readback"] == {}
    assert list(dotted["data"]["configuration"]) == ["samx_velocity"]
    assert dotted["summary"].startswith("samx_velocity = 100 (2.0 h old)")

    picked = tools.device_read(beamline, "samx", signals=["readback", "samx_setpoint", "nope"])
    assert set(picked["data"]["readback"]) == {"samx", "samx_setpoint"}
    assert picked["data"]["unknown_signals"] == ["nope"]
    assert "samx_velocity" in picked["data"]["available_signals"]
    assert "Unknown signals: nope" in picked["summary"]


def test_unknown_device_names_raise_with_close_matches(beamline):
    for tool in (tools.device_read, tools.device_info):
        with pytest.raises(LookupError) as error:
            tool(beamline, "samq")
        message = str(error.value)
        assert message.startswith("Device 'samq' not found; similar: sam")
        assert {"samx", "samy", "samz"} <= set(message.replace(",", " ").replace(".", " ").split())
        assert message.endswith("Call bec_devices to list all 7 devices.")

    assert tools.device_info(beamline, "SAMX")["data"]["name"] == "samx"


def test_a_bare_call_names_the_missing_device_argument(beamline):
    for tool in (tools.device_read, tools.device_info):
        answer = tool(beamline)
        assert answer["ok"] is False
        assert answer["summary"] == (
            "Name a device, e.g. device='bpm4i'; bec_devices lists all 7 devices."
        )


def test_device_read_surfaces_redis_errors_instead_of_reporting_no_values(beamline):
    beamline.connector.fail = ConnectionError("Redis went away")
    with pytest.raises(ConnectionError):
        tools.device_read(beamline, "samx")


def test_forbid_device_rpc_blocks_non_cached_reads_and_rpc_attributes(beamline, monkeypatch):
    sent = []

    def record_rpc(*args, **kwargs):
        sent.append(args)
        return {}

    monkeypatch.setattr(DeviceBase, "_run_rpc_call", record_rpc)
    samx = beamline.device_manager.devices["samx"]
    bpm4i = beamline.device_manager.devices["bpm4i"]

    with compat.forbid_device_rpc():
        with pytest.raises(compat.DeviceRPCRefused):
            samx.read(cached=False)
        with pytest.raises(compat.DeviceRPCRefused):
            samx.velocity.get()
        with pytest.raises(compat.DeviceRPCRefused):
            # a non-callable user-access attribute is served over RPC on attribute access
            bpm4i.registered_proxies  # pylint: disable=pointless-statement

    assert not sent
    # outside the guard bec_lib would really send the RPC: the guard is what stops it
    samx.read(cached=False)
    assert len(sent) == 1


def test_device_tools_never_attempt_a_device_rpc(beamline, monkeypatch):
    attempted = []
    sent = []

    def record_refusal(device, func_call, *args, **kwargs):
        attempted.append(f"{device}.{func_call}")
        raise compat.DeviceRPCRefused(func_call)

    monkeypatch.setattr(compat, "_refuse_rpc", record_refusal)
    monkeypatch.setattr(DeviceBase, "_run_rpc_call", lambda *args, **kwargs: sent.append(args))

    results = _all_tool_calls(beamline)

    assert not attempted
    assert not sent
    assert all(
        set(result) == {"ok", "summary", "findings", "data", "truncated"} for result in results
    )
    json.dumps(results)


class InlineConnection:
    """Stands in for BecConnection: lends the fake client to each call."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def run(self, func, /, *args, **kwargs):
        return func(self.client, *args, **kwargs)

    def connect(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def state(self) -> dict:
        return {"state": "connected"}


def _payload(answer):
    assert not answer.is_error, answer.content[0].text
    return json.loads(answer.content[0].text)


def test_device_tools_through_the_app_are_read_only_and_answer_bare_calls(
    beamline, call_tool, stdout_guard
):
    app = build_app(connection=InlineConnection(beamline))
    listed = {tool.name: tool for tool in asyncio.run(app.server.list_tools())}

    assert {"bec_devices", "bec_device_info", "bec_device_read"} <= set(listed)
    for name in ("bec_devices", "bec_device_info", "bec_device_read"):
        tool = listed[name]
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description.count(". ") == 0
        assert not tool.input_schema.get("required"), name
        with stdout_guard():
            assert set(_payload(call_tool(app.server, name))) == {
                "ok",
                "summary",
                "findings",
                "data",
                "truncated",
            }
    assert "positioner" in json.dumps(listed["bec_devices"].input_schema["properties"])


def test_device_tools_answer_through_an_mcp_session(beamline, call_tool, stdout_guard):
    server = build_app(connection=InlineConnection(beamline)).server

    with stdout_guard():
        motors = _payload(call_tool(server, "bec_devices", {"device_type": "positioner"}))
        samx = _payload(call_tool(server, "bec_device_read", {"device": "samx"}))
        info = _payload(call_tool(server, "bec_device_info", {"device": "samx", "kind": "config"}))
        missing = call_tool(server, "bec_device_read", {"device": "samq"})

    assert motors["data"]["total"] == 4
    assert samx["data"]["limits"] == {"low": -50, "high": 50}
    assert [row["name"] for row in info["data"]["signals"]] == ["velocity"]
    # an unknown name is an expected failure: the agent gets the message, not a crash
    assert missing.is_error is True
    assert "Device 'samq' not found; similar: sam" in missing.content[0].text
