from bec_mcp_server.server import parse_args


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
