"""Every diagnose rule against fake state: each finding code has a scenario that triggers it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bec_mcp_server.tools import Finding
from bec_mcp_server.tools import diagnose_rules as diag
from bec_mcp_server.tools import tool_result
from bec_mcp_server.tools.diagnose_state import (
    AlarmRecord,
    ConfigReset,
    DeviceState,
    InitProgress,
    LogRecord,
    QueueState,
    ServiceState,
    Snapshot,
)

NOW = 1_800_000_000.0
CORE = ("DeviceServer", "ScanServer", "ScanBundler", "FileWriterManager")


def healthy_services(**overrides: str | None) -> list[ServiceState]:
    statuses = {name: "RUNNING" for name in CORE}
    statuses.update(overrides)
    return [ServiceState(name, status) for name, status in statuses.items()]


def motor(name: str = "samx", **overrides) -> DeviceState:
    fields = {
        "enabled": True,
        "is_positioner": True,
        "readback": 1.0,
        "readback_time": NOW - 5,
        "limits": (-10.0, 10.0),
        "moving": False,
    }
    fields.update(overrides)
    return DeviceState(name, **fields)


def snapshot(*devices: DeviceState, **overrides) -> Snapshot:
    """A healthy beamline: core services up, devices loaded, idle queue, quiet logs."""
    devices = devices or (motor("samx"), motor("samy"), DeviceState("bpm4i", enabled=True))
    fields = {
        "now": NOW,
        "services": healthy_services(),
        "device_names": [device.name for device in devices],
        "devices": {device.name: device for device in devices},
        "queues": [QueueState("primary", "RUNNING")],
        "logs": [],
        "alarms": [],
    }
    fields.update(overrides)
    return Snapshot(**fields)


def init_log(name: str, *, level: str = "WARNING", age: float = 60) -> LogRecord:
    return LogRecord(
        level,
        30 if level == "WARNING" else 40,
        f"Failed to initialize device {name}: Traceback (most recent call last):\n"
        '  File "ophyd_devices/sim.py", line 12, in __init__\n'
        "ConnectionError: IOC X10SA-ES1 did not answer",
        service="DeviceServer",
        timestamp=NOW - age,
    )


def codes(findings) -> list[str]:
    return [finding.code for finding in findings]


def device_codes(snap: Snapshot, name: str = "samx") -> list[str]:
    return codes(diag.device_findings(snap, name))


def beamline_findings(snap: Snapshot) -> list:
    """bec_diagnose's own findings: the devices overview, log errors and unreadable sources."""
    return (
        diag.device_overview_findings(snap)
        + diag.log_findings(snap)
        + diag.unavailable_findings(snap)
    )


def beamline_codes(snap: Snapshot) -> list[str]:
    return codes(beamline_findings(snap))


def beamline_report(snap: Snapshot, *extra, max_findings: int = 20) -> dict:
    entries = diag.finding_entries(beamline_findings(snap)) + list(extra)
    return diag.report(
        entries, subject="the beamline", data=diag.overview(snap), max_findings=max_findings
    )


# ---------------------------------------------------------------------------
# one triggering scenario per finding code
# ---------------------------------------------------------------------------

RULE_CASES = {
    # one device
    "device.not_found": lambda: device_codes(snapshot(), "samz"),
    "device.config_reset": lambda: device_codes(
        snapshot(
            device_names=[], devices={}, config_resets=[ConfigReset(NOW - 120, ("samx", "samy"))]
        )
    ),
    "device.init_failed": lambda: device_codes(
        snapshot(motor(enabled=False), logs=[init_log("samx")])
    ),
    "device.disabled": lambda: device_codes(snapshot(motor(enabled=False))),
    "device.initializing": lambda: device_codes(
        snapshot(init_progress=InitProgress("samx", False, True, 4, 10))
    ),
    "device.data_unverified": lambda: device_codes(
        snapshot(services=[ServiceState("ScanServer", "RUNNING")])
    ),
    "device.no_readback": lambda: device_codes(snapshot(motor(readback=None, readback_time=None))),
    "device.readback_old": lambda: device_codes(snapshot(motor(readback_time=NOW - 7200))),
    "device.outside_limits": lambda: device_codes(snapshot(motor(readback=12.5))),
    "device.moving_without_scan": lambda: device_codes(snapshot(motor(moving=True))),
    "device.moving": lambda: device_codes(
        snapshot(motor(moving=True), queues=[QueueState("primary", "RUNNING", running=True)])
    ),
    "device.alarms": lambda: device_codes(
        snapshot(alarms=[AlarmRecord("MAJOR", "limit hit", "LimitError", ("samx",), NOW - 10)])
    ),
    "device.log_errors": lambda: device_codes(
        snapshot(logs=[LogRecord("ERROR", 40, "samx did not reach target", "ScanServer", NOW)])
    ),
    # devices across the beamline
    "devices.none_loaded": lambda: beamline_codes(snapshot(device_names=[], devices={})),
    "devices.config_reset": lambda: beamline_codes(
        snapshot(config_resets=[ConfigReset(NOW - 300, ("samx",))])
    ),
    "devices.loading": lambda: beamline_codes(
        snapshot(init_progress=InitProgress("samy", False, True, 120, 215))
    ),
    "devices.init_failed": lambda: beamline_codes(
        snapshot(motor(enabled=False), logs=[init_log("samx")])
    ),
    "devices.disabled": lambda: beamline_codes(snapshot(motor(enabled=False))),
    "devices.outside_limits": lambda: beamline_codes(snapshot(motor(readback=-11.0))),
    "devices.moving_without_scan": lambda: beamline_codes(snapshot(motor(moving=True))),
    # logs and unreadable sources
    "logs.errors": lambda: beamline_codes(
        snapshot(logs=[LogRecord("ERROR", 40, "file write failed", "FileWriter", NOW - 5)])
    ),
    "diagnose.incomplete": lambda: beamline_codes(
        snapshot(unavailable={"the queue": "TimeoutError"})
    ),
}


def _codes_in_module() -> set[str]:
    source = Path(diag.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'"((?:devices?|logs|diagnose)\.[a-z_]+)"', source))


def test_every_rule_in_the_module_has_a_triggering_case():
    assert _codes_in_module() == set(RULE_CASES)


@pytest.mark.parametrize("code", sorted(RULE_CASES))
def test_rule_triggers_on_fake_state(code):
    assert code in RULE_CASES[code]()


def test_healthy_beamline_has_no_findings():
    result = beamline_report(snapshot())

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["truncated"] is False
    assert result["summary"] == "No problems found for the beamline."
    assert set(result) == {"ok", "summary", "findings", "data", "truncated"}


def test_healthy_device_has_no_findings():
    result = diag.diagnose_device(snapshot(), "samx")

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["data"]["enabled"] is True
    assert result["data"]["readback_age_s"] == 5.0
    assert result["data"]["limits"] == [-10.0, 10.0]


# ---------------------------------------------------------------------------
# one device
# ---------------------------------------------------------------------------


def test_not_found_suggests_similar_names():
    (finding,) = diag.device_findings(snapshot(), "samz")

    assert finding.severity == "error"
    assert "Similar names: samy, samx." in finding.message
    assert finding.next_tool == diag.TOOL_DEVICES


def test_not_found_without_device_list_still_answers():
    (finding,) = diag.device_findings(snapshot(device_names=None, devices={}), "samx")

    assert finding.message == "samx is not a configured device."


def test_device_dropped_by_a_config_reset_is_an_error_not_a_miss():
    snap = snapshot(device_names=[], devices={}, config_resets=[ConfigReset(NOW - 120, ("samx",))])

    (finding,) = diag.device_findings(snap, "samx")

    assert finding.severity == "error"
    assert finding.message.startswith("samx is not loaded. The device config was reset 2 min ago")


def test_old_config_reset_is_outside_the_window():
    snap = snapshot(device_names=[], devices={}, config_resets=[ConfigReset(NOW - 7200, ("samx",))])

    assert device_codes(snap) == ["device.not_found"]


def test_device_loaded_again_after_reset_gets_a_note():
    snap = snapshot(config_resets=[ConfigReset(NOW - 300, ("samx",))])

    (finding,) = diag.device_findings(snap, "samx")

    assert finding.code == "device.config_reset"
    assert finding.severity == "info"
    assert "has been loaded again since" in finding.message


def test_init_failure_from_log_quotes_the_exception_and_replaces_disabled():
    snap = snapshot(motor(enabled=False), logs=[init_log("samx")])

    findings = diag.device_findings(snap, "samx")

    assert codes(findings) == ["device.init_failed"]
    assert findings[0].severity == "error"
    assert '"ConnectionError: IOC X10SA-ES1 did not answer"' in findings[0].message
    assert findings[0].next_args == {"text": "samx", "level": "warning"}


def test_init_failure_from_progress_message():
    snap = snapshot(motor(enabled=False), init_progress=InitProgress("samx", True, False, 10, 10))

    findings = diag.device_findings(snap, "samx")

    assert codes(findings) == ["device.init_failed"]
    assert "Reason" not in findings[0].message


def test_init_failure_logged_as_error_from_a_config_add():
    record = LogRecord("ERROR", 40, "Failed to initialize device samx: boom", "DeviceServer", NOW)
    snap = snapshot(motor(enabled=False), logs=[record])

    findings = diag.device_findings(snap, "samx")

    # the init-failure record is reported once, not again as a log error
    assert codes(findings) == ["device.init_failed"]
    assert '"boom"' in findings[0].message


def test_init_failure_of_a_device_enabled_since_does_not_count():
    snap = snapshot(motor(enabled=True), logs=[init_log("samx")])

    assert "device.init_failed" not in device_codes(snap)


def test_init_failure_outside_the_window_does_not_count():
    snap = snapshot(motor(enabled=False), logs=[init_log("samx", age=7200)])

    assert device_codes(snap) == ["device.disabled"]


def test_disabled_device_skips_readback_rules():
    snap = snapshot(motor(enabled=False, readback=50.0, readback_time=None, moving=True))

    assert device_codes(snap) == ["device.disabled"]


def test_initializing_counts_from_one():
    snap = snapshot(init_progress=InitProgress("samx", False, True, 4, 10))

    (finding,) = diag.device_findings(snap, "samx")

    assert "(device 5 of 10)" in finding.message


@pytest.mark.parametrize(
    ("services", "text"),
    [
        ([ServiceState("ScanServer", "RUNNING")], "The device server is not running"),
        (healthy_services(DeviceServer="ERROR"), "The device server reports ERROR"),
    ],
)
def test_cached_values_are_unverified_without_a_live_device_server(services, text):
    (finding,) = diag.device_findings(snapshot(services=services), "samx")

    assert finding.code == "device.data_unverified"
    assert finding.message.startswith(text)


def test_unread_services_do_not_mark_data_unverified():
    assert device_codes(snapshot(services=None)) == []


@pytest.mark.parametrize(("positioner", "severity"), [(True, "warning"), (False, "info")])
def test_missing_readback_is_a_warning_only_for_positioners(positioner, severity):
    device = motor(is_positioner=positioner, readback=None, readback_time=None, limits=None)

    (finding,) = diag.device_findings(snapshot(device), "samx")

    assert finding.code == "device.no_readback"
    assert finding.severity == severity


def test_readback_without_timestamp_is_neither_missing_nor_old():
    assert device_codes(snapshot(motor(readback=1.0, readback_time=None))) == []


def test_old_readback_is_a_note_with_its_age_and_value():
    (finding,) = diag.device_findings(snapshot(motor(readback_time=NOW - 3 * 3600)), "samx")

    assert finding.severity == "info"
    assert finding.message.startswith("samx's readback last changed 3 h ago (value 1)")


def test_stale_threshold_is_configurable():
    snap = snapshot(motor(readback_time=NOW - 120), stale_after_s=60)

    assert device_codes(snap) == ["device.readback_old"]


@pytest.mark.parametrize("readback", [10.5, -10.000001])
def test_outside_limits_names_value_and_limits(readback):
    (finding,) = diag.device_findings(snapshot(motor(readback=readback)), "samx")

    assert finding.code == "device.outside_limits"
    assert f"reads {readback:.6g}, outside its soft limits [-10, 10]" in finding.message


@pytest.mark.parametrize(
    "device",
    [
        motor(readback=10.0),  # on the limit is inside
        motor(readback=5.0, limits=(0.0, 0.0)),  # BEC's [0, 0] means no limits
        motor(readback=5.0, limits=(3.0, 3.0)),
        motor(readback=50.0, limits=None),
        motor(readback="moving", limits=(-1.0, 1.0)),
        motor(readback=float("nan"), limits=(-1.0, 1.0)),
        motor(readback=True, limits=(-1.0, 1.0)),
    ],
)
def test_no_limit_finding_when_limits_do_not_apply(device):
    assert "device.outside_limits" not in device_codes(snapshot(device))


def test_moving_with_unread_queue_is_only_a_note():
    snap = snapshot(motor(moving=True), queues=None)

    assert device_codes(snap) == ["device.moving"]


def test_moving_while_scans_are_pending_is_only_a_note():
    snap = snapshot(motor(moving=True), queues=[QueueState("primary", "RUNNING", pending=2)])

    assert device_codes(snap) == ["device.moving"]


def test_alarm_naming_the_device_in_its_text():
    alarm = AlarmRecord("MINOR", "samx_setpoint out of range", None, (), NOW - 10)

    (finding,) = diag.device_findings(snapshot(alarms=[alarm]), "samx")

    assert finding.code == "device.alarms"
    assert finding.severity == "warning"
    assert "1 alarm about samx in the last 60 min" in finding.message


def test_major_alarm_makes_the_device_finding_an_error():
    alarms = [
        AlarmRecord("MINOR", "first", None, ("samx",), NOW - 20),
        AlarmRecord("MAJOR", "second", "LimitError", ("samx", "samy"), NOW - 10),
    ]

    (finding,) = diag.device_findings(snapshot(alarms=alarms), "samx")

    assert finding.severity == "error"
    assert 'latest: MAJOR LimitError "second"' in finding.message


@pytest.mark.parametrize(
    "alarm",
    [
        AlarmRecord("MAJOR", "samx broke", None, (), NOW - 7200),  # too old
        AlarmRecord("MAJOR", "samx2 broke", None, ("samx2",), NOW),  # another device
    ],
)
def test_alarms_outside_window_or_about_other_devices_are_ignored(alarm):
    assert device_codes(snapshot(alarms=[alarm])) == []


def test_log_errors_count_and_quote_the_latest():
    logs = [
        LogRecord("ERROR", 40, "samx: first", "ScanServer", NOW - 300),
        LogRecord("INFO", 20, "samx moved", "ScanServer", NOW - 200),
        LogRecord("CRITICAL", 50, "\x1b[31msamx: second\x1b[0m\nmore", "DeviceServer", NOW - 60),
        LogRecord("ERROR", 40, "samy: unrelated", "ScanServer", NOW - 30),
    ]

    (finding,) = diag.device_findings(snapshot(logs=logs), "samx")

    assert finding.message == (
        "2 error log records mention samx in the last 60 min; latest from DeviceServer 60 s ago: "
        '"samx: second".'
    )


def test_long_untrusted_text_is_shortened():
    logs = [LogRecord("ERROR", 40, "samx " + "x" * 500, "ScanServer", NOW)]

    (finding,) = diag.device_findings(snapshot(logs=logs), "samx")

    assert len(finding.message) < 300
    assert "…" in finding.message


def test_records_without_time_count_as_recent():
    logs = [LogRecord("ERROR", 40, "samx failed")]

    assert device_codes(snapshot(logs=logs)) == ["device.log_errors"]


# ---------------------------------------------------------------------------
# devices across the beamline
# ---------------------------------------------------------------------------


def test_no_devices_loaded_mentions_the_reset():
    snap = snapshot(device_names=[], devices={}, config_resets=[ConfigReset(NOW - 60, ("samx",))])

    findings = [f for f in beamline_findings(snap) if f.code.startswith("devices.")]

    assert codes(findings) == ["devices.none_loaded"]
    assert "The device config was reset 60 s ago" in findings[0].message


def test_finished_loading_is_not_reported():
    snap = snapshot(init_progress=InitProgress("samy", True, True, 215, 215))

    assert beamline_codes(snap) == []


def test_init_failed_devices_are_listed_once_and_not_as_disabled():
    devices = (motor("samx", enabled=False), motor("samy", enabled=False), motor("samz"))
    snap = snapshot(*devices, logs=[init_log("samx")])

    findings = beamline_findings(snap)

    assert codes(findings) == ["devices.init_failed", "devices.disabled"]
    assert findings[0].message == "1 device failed to initialize and was disabled: samx."
    assert findings[1].message == "1 device is disabled: samy."
    assert findings[1].next_args == {"device": "samy"}


def test_many_disabled_devices_are_capped():
    devices = [motor(f"m{index:02d}", enabled=False) for index in range(13)]

    (finding,) = diag.device_overview_findings(snapshot(*devices))

    assert finding.message.startswith("13 devices are disabled: m00, m01")
    assert finding.message.endswith("m09 and 3 more.")


def test_disabled_device_outside_limits_is_not_reported_as_outside():
    snap = snapshot(motor(enabled=False, readback=50.0))

    assert beamline_codes(snap) == ["devices.disabled"]


def test_outside_limits_lists_each_device():
    snap = snapshot(motor("samx", readback=12.0), motor("samy", readback=-12.0))

    (finding,) = diag.device_overview_findings(snap)

    assert finding.message == (
        "2 devices read outside their soft limits: samx (12 not in [-10, 10]), "
        "samy (-12 not in [-10, 10])."
    )


def test_moving_during_a_scan_is_not_a_beamline_finding():
    snap = snapshot(motor(moving=True), queues=[QueueState("primary", "RUNNING", running=True)])

    assert beamline_codes(snap) == []


# ---------------------------------------------------------------------------
# log errors
# ---------------------------------------------------------------------------


def test_log_errors_from_one_service_point_at_that_service():
    snap = snapshot(logs=[LogRecord("ERROR", 40, "atlas unreachable", "SciHub", NOW - 5)] * 3)

    (finding,) = diag.log_findings(snap)

    assert finding.next_args == {"service": "SciHub", "level": "error"}


def test_beamline_data_lists_services_without_clients():
    services = healthy_services() + [
        ServiceState("DAPServer/LmfitService1D", "RUNNING"),
        ServiceState("BECIPythonClient/4b9d1af8-44ed-4f3a", "BUSY"),
        ServiceState("BECWidgets/5b85fc82-f17b-42b8", "RUNNING"),
        ServiceState("pylsp-bec/6708a676-a5cb-48db", "RUNNING"),
        ServiceState("BuiltinActors/c1719c09-14b4-4c86", "RUNNING"),
    ]

    data = diag.overview(snapshot(services=services))

    # instance suffixes are cut to 8 characters, as bec_status shows them
    assert list(data["services"]) == [
        "BuiltinActors/c1719c09",
        "DAPServer/LmfitSer",
        "DeviceServer",
        "FileWriterManager",
        "ScanBundler",
        "ScanServer",
    ]


def test_log_errors_name_their_services_and_skip_reported_init_failures():
    snap = snapshot(
        motor(enabled=False),
        logs=[
            init_log("samx", level="ERROR"),
            LogRecord("ERROR", 40, "write failed", "FileWriter", NOW - 20),
            LogRecord("WARNING", 30, "slow", "ScanServer", NOW - 10),
            LogRecord("ERROR", 40, "no beam", "ScanServer", NOW - 5),
        ],
    )

    (finding,) = [f for f in beamline_findings(snap) if f.code == "logs.errors"]

    assert finding.message == (
        "2 error log records in the last 60 min from FileWriter, ScanServer; "
        'latest from ScanServer 5 s ago: "no beam".'
    )


def test_old_logs_are_outside_the_window():
    snap = snapshot(logs=[LogRecord("ERROR", 40, "old", "ScanServer", NOW - 7200)])

    assert beamline_codes(snap) == []


# ---------------------------------------------------------------------------
# merging with other tools' findings, ordering and report shape
# ---------------------------------------------------------------------------


def tool_result_of(*findings: Finding) -> dict:
    return tool_result(not findings, "from another tool", findings=findings)


def test_other_tools_findings_keep_their_text_and_twin():
    queue = Finding(
        "warning", "queue_paused", "The primary queue is paused", "primary", "bec_queue"
    )

    (entry,) = diag.tool_entries("queue", tool_result_of(queue))

    assert entry.area == "queue"
    assert entry.severity == "warning"
    assert entry.text == "Warning: The primary queue is paused. Next: bec_queue()."
    assert entry.message == "The primary queue is paused."
    assert entry.twin == queue.as_dict()


def test_plain_text_findings_get_their_severity_from_the_prefix():
    result = {"findings": ["Error: scan 4 failed.", "something odd"], "data": {}}

    entries = diag.tool_entries("scans", result)

    assert [(entry.severity, entry.twin) for entry in entries] == [
        ("error", None),
        ("warning", None),
    ]


def test_merged_findings_are_ordered_by_severity_then_area():
    services = tool_result_of(Finding("error", "core_service_error", "DeviceServer reports ERROR"))
    states = tool_result_of(Finding("error", "state_invalid", "Beamline state shutter is invalid"))
    queue = tool_result_of(Finding("warning", "queue_paused", "The primary queue is paused"))
    snap = snapshot(motor(enabled=False), logs=[LogRecord("ERROR", 40, "boom", "ScanServer", NOW)])

    result = beamline_report(
        snap,
        *diag.tool_entries("states", states),
        *diag.tool_entries("queue", queue),
        *diag.tool_entries("services", services),
    )

    assert [finding["code"] for finding in result["data"]["findings"]] == [
        "core_service_error",
        "state_invalid",
        "queue_paused",
        "logs.errors",
        "devices.disabled",
    ]
    assert result["ok"] is False
    assert result["summary"] == (
        "2 errors, 2 warnings for the beamline. Most important: DeviceServer reports ERROR."
    )


def test_every_own_finding_names_a_next_tool():
    snap = snapshot(
        motor(enabled=False),
        motor("samy", readback=50.0, moving=True),
        init_progress=InitProgress("samy", False, True, 1, 2),
        config_resets=[ConfigReset(NOW - 60, ("samx",))],
        logs=[LogRecord("ERROR", 40, "y", "ScanServer", NOW)],
        unavailable={"the scan queue": "boom"},
    )

    result = beamline_report(snap, max_findings=50)

    assert len(result["findings"]) == len(result["data"]["findings"]) >= 5
    assert all(" Next: bec_" in line for line in result["findings"])
    assert all(finding["next"]["tool"].startswith("bec_") for finding in result["data"]["findings"])


def test_notes_alone_keep_the_report_ok():
    result = beamline_report(snapshot(motor(enabled=False)))

    assert result["ok"] is True
    assert result["summary"] == (
        "No problems found for the beamline; 1 note. First: 1 device is disabled: samx."
    )
    assert result["findings"] == [
        'Info: 1 device is disabled: samx. Next: bec_device_diagnose(device="samx").'
    ]


def test_report_is_truncated_to_max_findings():
    states = tool_result_of(
        Finding("error", "state_invalid", "a is invalid"),
        Finding("error", "state_invalid", "b is invalid"),
    )
    snap = snapshot(motor(readback=50.0), logs=[LogRecord("ERROR", 40, "x", "ScanServer", NOW)])

    result = beamline_report(snap, *diag.tool_entries("states", states), max_findings=2)

    assert len(result["findings"]) == len(result["data"]["findings"]) == 2
    assert result["truncated"] is True
    assert result["summary"].startswith("2 errors, 2 warnings")


def test_beamline_data_is_a_compact_overview():
    result = beamline_report(snapshot(motor(enabled=False), motor("samy")))

    assert result["data"] == {
        "window_s": 3600.0,
        "services": {name: "RUNNING" for name in sorted(CORE)},
        "devices": {"configured": 2, "disabled": 1},
        "queues": {"primary": {"status": "RUNNING", "pending": 0}},
        "findings": [
            {
                "severity": "info",
                "code": "devices.disabled",
                "subject": "devices",
                "next": {"tool": "bec_device_diagnose", "args": {"device": "samx"}},
            }
        ],
    }


def test_unread_sources_are_left_out_of_the_data():
    assert beamline_report(Snapshot(now=NOW)) == {
        "ok": True,
        "summary": "No problems found for the beamline.",
        "findings": [],
        "data": {"window_s": 3600.0},
        "truncated": False,
    }


def test_device_report_for_unknown_device():
    result = diag.diagnose_device(snapshot(), "samz")

    assert result["ok"] is False
    assert result["data"]["device"] == "samz"
    assert result["data"]["configured"] is False
    assert result["summary"].startswith("1 error for samz. Most important: samz is not")


def test_device_report_marks_an_incomplete_diagnosis():
    snap = snapshot(unavailable={"the log stream": "ConnectionError: Redis went away"})

    result = diag.diagnose_device(snap, "samx")

    assert [finding["code"] for finding in result["data"]["findings"]] == ["diagnose.incomplete"]
    assert result["findings"] == [
        'Warning: Could not check the log stream: "ConnectionError: Redis went away". '
        "Next: bec_status()."
    ]
