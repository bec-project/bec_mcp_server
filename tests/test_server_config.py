import asyncio
import json

import pytest

from bec_mcp_server.docs.build import build_docs_index
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
    "bec_docs_search",
    "bec_docs_page",
}


def test_parse_args_defaults_to_streamable_http():
    args = parse_args([])

    assert args.transport == "streamable-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.path == "/mcp"
    assert args.lazy_connect is False
    assert args.docs_index is None


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


def _call(server, name, arguments):
    content = asyncio.run(server.call_tool(name, arguments))
    blocks = content[0] if isinstance(content, tuple) else content
    return json.loads(blocks[0].text)


@pytest.fixture
def docs_index(tmp_path):
    """A docs index built from a two-page bec_docs-like repository."""
    repo = tmp_path / "bec_docs"
    (repo / "docs" / "how-to").mkdir(parents=True)
    (repo / "zensical.toml").write_text(
        '[project]\nsite_url = "https://bec.example.org/docs/"\ndocs_dir = "docs"\n'
    )
    (repo / "docs" / "index.md").write_text("# Welcome to BEC\n\nBEC runs scans.\n")
    (repo / "docs" / "how-to" / "line-scan.md").write_text(
        "# Run a line scan\n\n## Start it\n\nCall scans.line_scan with a motor.\n"
    )
    index = tmp_path / "index.json.gz"
    build_docs_index(repo, index)
    return str(index)


def test_docs_tools_search_and_read_the_configured_index(docs_index):
    server = build_server(connect_on_startup=False, docs_index=docs_index)

    found = _call(server, "bec_docs_search", {"query": "run a line scan"})
    first = found["results"][0]
    page = _call(server, "bec_docs_page", {"path": first["path"]})

    assert first["path"] == "how-to/line-scan.md"
    assert found["docs_index"]["loaded_from"] == docs_index
    assert page["found"] is True and page["title"] == "Run a line scan"


def test_docs_search_without_query_lists_the_pages(docs_index):
    server = build_server(connect_on_startup=False, docs_index=docs_index)

    contents = _call(server, "bec_docs_search", {})

    assert [page["path"] for page in contents["pages"]] == ["how-to/line-scan.md", "index.md"]


def test_unknown_docs_page_names_similar_pages(docs_index):
    server = build_server(connect_on_startup=False, docs_index=docs_index)

    page = _call(server, "bec_docs_page", {"path": "how-to/line-scans.md"})

    assert page["found"] is False
    assert "Similar pages" in page["message"]


def test_docs_tools_say_when_the_index_cannot_be_loaded(tmp_path):
    server = build_server(connect_on_startup=False, docs_index=str(tmp_path / "missing.json"))

    found = _call(server, "bec_docs_search", {"query": "line scan"})
    page = _call(server, "bec_docs_page", {"path": "index.md"})

    assert found["available"] is False and "missing.json" in found["message"]
    assert page["found"] is False and "missing.json" in page["message"]


def test_docs_index_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BEC_MCP_DOCS_INDEX", str(tmp_path / "index.json.gz"))

    assert parse_args([]).docs_index == str(tmp_path / "index.json.gz")
