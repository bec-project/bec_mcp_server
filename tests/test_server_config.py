import asyncio

from bec_mcp_server.server import build_server, parse_args

EXPECTED_TOOLS = {
    "bec_ping",
    "bec_summary",
    "bec_services",
    "bec_devices",
    "bec_device_config",
    "bec_scans",
    "bec_scan_details",
    "bec_scan_interlock",
    "bec_queue",
}


def test_parse_args_defaults_to_streamable_http():
    args = parse_args([])

    assert args.transport == "streamable-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.path == "/mcp"
    assert args.lazy_connect is False


def test_parse_args_accepts_custom_http_values():
    args = parse_args(["--host", "0.0.0.0", "--port", "9000", "--path", "/bec"])

    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.path == "/bec"


def test_parse_args_accepts_lazy_connect_flag():
    args = parse_args(["--lazy-connect"])

    assert args.lazy_connect is True


def test_build_server_registers_only_read_only_tools_without_connecting():
    server = build_server(connect_on_startup=False)

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert all(tool.annotations.readOnlyHint for tool in tools)
