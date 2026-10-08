"""The answer shape and the finding convention shared by every tool."""

from __future__ import annotations

from bec_mcp_server.tools import Finding, tool_result


def test_finding_reads_as_one_sentence_with_the_next_call():
    finding = Finding(
        "error",
        "service_error",
        "DeviceServer reports ERROR",
        subject="DeviceServer",
        next_tool="bec_logs",
        next_args={"service": "DeviceServer", "level": "error"},
    )

    assert finding.text() == (
        'Error: DeviceServer reports ERROR. Next: bec_logs(service="DeviceServer", level="error").'
    )
    assert finding.as_dict() == {
        "severity": "error",
        "code": "service_error",
        "subject": "DeviceServer",
        "next": {"tool": "bec_logs", "args": {"service": "DeviceServer", "level": "error"}},
    }


def test_finding_without_next_call_keeps_its_own_punctuation():
    assert Finding("info", "idle", "The queue is idle.").text() == "Info: The queue is idle."
    assert Finding("warning", "x", "Is it stale?").as_dict()["next"] is None


def test_tool_result_always_has_the_same_keys():
    assert tool_result(True, "All good.") == {
        "ok": True,
        "summary": "All good.",
        "findings": [],
        "data": {},
        "truncated": False,
    }


def test_structured_findings_get_a_twin_in_data():
    result = tool_result(
        False,
        "One problem.",
        findings=[Finding("warning", "stale", "samx readback is 2 h old"), "Plain note."],
        data={"count": 1},
    )

    assert result["findings"] == ["Warning: samx readback is 2 h old.", "Plain note."]
    assert result["data"]["count"] == 1
    assert result["data"]["findings"] == [
        {"severity": "warning", "code": "stale", "subject": None, "next": None}
    ]


def test_plain_findings_add_no_twin():
    assert tool_result(True, "x", findings=["Plain."])["data"] == {}
