from bec_mcp_server.client import (
    BecConnection,
    _first_line,
    _scan_details,
    _summarize_device,
    _summarize_scan,
)


def test_first_line_returns_empty_string_for_missing_text():
    assert _first_line(None) == ""


def test_first_line_returns_first_nonempty_line():
    assert _first_line("hello\nworld") == "hello"


def test_summarize_device_extracts_common_fields():
    summary = _summarize_device(
        "samx",
        {
            "deviceClass": "Positioner",
            "enabled": True,
            "readoutPriority": "monitored",
            "deviceTags": ["motor"],
        },
    )

    assert summary == {
        "name": "samx",
        "device_class": "Positioner",
        "enabled": True,
        "readout_priority": "monitored",
        "device_tags": ["motor"],
    }


def test_summarize_scan_extracts_description_and_shape():
    summary = _summarize_scan(
        "line_scan",
        {
            "doc": "Line scan\nDetailed docs",
            "base_class": "ScanBaseV4",
            "required_kwargs": ["exp_time"],
            "arg_input": {"motor": "DeviceBase"},
            "signature": [{"name": "motor"}],
        },
    )

    assert summary == {"name": "line_scan", "description": "Line scan", "base_class": "ScanBaseV4"}


def test_scan_details_extracts_full_scan_metadata():
    summary = _scan_details(
        "line_scan",
        {
            "doc": "Line scan\nDetailed docs",
            "base_class": "ScanBaseV4",
            "required_kwargs": ["exp_time"],
            "arg_input": {"motor": "DeviceBase"},
            "signature": [{"name": "motor"}],
        },
    )

    assert summary == {
        "name": "line_scan",
        "description": "Line scan",
        "base_class": "ScanBaseV4",
        "required_kwargs": ["exp_time"],
        "arg_input": {"motor": "DeviceBase"},
        "signature": [{"name": "motor"}],
    }


class _FakeScan:
    def __init__(self, scan_info):
        self.scan_info = scan_info


class _FakeScansNamespace:
    def __init__(self, scans):
        self._available_scans = scans


class _FakeClient:
    def __init__(self, scans):
        self.scans = _FakeScansNamespace(scans)


def _make_connection(scans):
    connection = BecConnection.__new__(BecConnection)
    connection._client = _FakeClient(scans)
    return connection


def test_scans_excludes_internal_entries_by_default():
    connection = _make_connection(
        {
            "_internal_scan": _FakeScan({"doc": "Internal", "base_class": "ScanComponent"}),
            "line_scan": _FakeScan({"doc": "Line scan", "base_class": "ScanBaseV4"}),
        }
    )

    result = connection.scans()

    assert result == {
        "count": 1,
        "scans": [{"name": "line_scan", "description": "Line scan", "base_class": "ScanBaseV4"}],
    }


def test_scans_support_prefix_and_base_class_filters():
    connection = _make_connection(
        {
            "line_scan": _FakeScan({"doc": "Line", "base_class": "ScanBaseV4"}),
            "list_scan": _FakeScan({"doc": "List", "base_class": "ScanBase"}),
            "log_scan": _FakeScan({"doc": "Log", "base_class": "ScanBaseV4"}),
        }
    )

    result = connection.scans(prefix="li", base_class="ScanBase")

    assert result == {
        "count": 1,
        "scans": [{"name": "list_scan", "description": "List", "base_class": "ScanBase"}],
    }


def test_scan_details_returns_full_payload_for_single_scan():
    connection = _make_connection(
        {
            "line_scan": _FakeScan(
                {
                    "doc": "Line scan\nDetailed docs",
                    "base_class": "ScanBaseV4",
                    "required_kwargs": ["exp_time"],
                    "arg_input": {"motor": "DeviceBase"},
                    "signature": [{"name": "motor"}],
                }
            )
        }
    )

    result = connection.scan_details("line_scan")

    assert result == {
        "found": True,
        "scan_name": "line_scan",
        "scan": {
            "name": "line_scan",
            "description": "Line scan",
            "base_class": "ScanBaseV4",
            "required_kwargs": ["exp_time"],
            "arg_input": {"motor": "DeviceBase"},
            "signature": [{"name": "motor"}],
        },
    }


def test_scan_details_reports_missing_scan():
    connection = _make_connection(
        {"line_scan": _FakeScan({"doc": "Line", "base_class": "ScanBaseV4"})}
    )

    result = connection.scan_details("grid_scan")

    assert result == {"found": False, "scan_name": "grid_scan", "available_scans": ["line_scan"]}
